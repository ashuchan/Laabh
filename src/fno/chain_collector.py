"""Options chain collector — fetches live chain data from Angel One SmartAPI."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Sequence

from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

from src.config import get_settings
from src.db import session_scope
from src.fno.calendar import next_weekly_expiry
from src.fno.chain_parser import ChainRow, ChainSnapshot, enrich_chain_row
from src.models.fno_chain import OptionsChain
from src.models.instrument import Instrument

_OPTION_TYPES = ("CE", "PE")
_EXPIRY_COUNT = 2  # Fetch nearest 2 expiries per instrument


async def _get_angel_session():
    """Return an authenticated Angel One SmartConnect instance."""
    import pyotp
    from smartapi import SmartConnect  # type: ignore[import]

    settings = get_settings()
    totp = pyotp.TOTP(settings.angel_one_totp_secret).now()
    obj = SmartConnect(api_key=settings.angel_one_api_key)
    data = obj.generateSession(
        settings.angel_one_client_id,
        settings.angel_one_password,
        totp,
    )
    if data.get("status") is False:
        raise RuntimeError(f"Angel One auth failed: {data.get('message')}")
    return obj


@retry(stop=stop_after_attempt(5), wait=wait_exponential(min=2, max=30))
async def _fetch_option_chain_from_api(symbol: str, expiry_str: str) -> dict:
    """Fetch raw option chain from Angel One REST API."""
    obj = await _get_angel_session()
    return obj.optionGreek(
        {
            "name": symbol,
            "expirydate": expiry_str,
        }
    )


def _row_from_api(
    instrument_id: object,
    snapshot_at: datetime,
    expiry_date: date,
    strike: float,
    opt_type: str,
    data: dict,
    underlying_ltp: Decimal | None,
) -> ChainRow:
    """Convert Angel One API response fields into a ChainRow."""

    def _d(v: object) -> Decimal | None:
        try:
            return Decimal(str(v)) if v is not None else None
        except Exception:
            return None

    return ChainRow(
        instrument_id=instrument_id,
        expiry_date=expiry_date,
        strike_price=Decimal(str(strike)),
        option_type=opt_type,
        ltp=_d(data.get("ltp")),
        bid_price=_d(data.get("bidprice")),
        ask_price=_d(data.get("askprice")),
        bid_qty=data.get("bidqty"),
        ask_qty=data.get("askqty"),
        volume=data.get("tradedVolume"),
        oi=data.get("openInterest"),
        iv=data.get("impliedVolatility"),
        delta=data.get("delta"),
        gamma=data.get("gamma"),
        theta=data.get("theta"),
        vega=data.get("vega"),
        underlying_ltp=underlying_ltp,
    )


async def collect(
    instrument: "Instrument",
    snapshot_rows: list[ChainRow] | None = None,
) -> ChainSnapshot | None:
    """Collect chain data for `instrument` and persist to `options_chain`.

    If `snapshot_rows` is provided (for tests), skip the live API call.
    Returns a ChainSnapshot, or None on failure.
    """
    now = datetime.now(tz=timezone.utc)

    if snapshot_rows is not None:
        # Test / mock path
        snapshot = ChainSnapshot(
            instrument_id=instrument.id,
            snapshot_at=now,
            rows=snapshot_rows,
        )
    else:
        symbol = instrument.symbol
        try:
            snapshot = await _collect_live(instrument, now)
        except Exception as exc:
            logger.error(f"chain_collector: failed for {symbol}: {exc}")
            return None

    if not snapshot or not snapshot.rows:
        return snapshot

    await _persist_snapshot(snapshot)
    return snapshot


async def _collect_live(instrument: "Instrument", now: datetime) -> ChainSnapshot:
    """Fetch live chain from Angel One for up to 2 nearest expiries."""
    symbol = instrument.symbol
    rows: list[ChainRow] = []
    underlying_ltp: Decimal | None = None

    # Determine the 2 nearest expiries
    ref = now.date()
    expiry1 = next_weekly_expiry(symbol, reference=ref)
    expiry2 = next_weekly_expiry(symbol, reference=expiry1)
    expiries = [expiry1, expiry2]

    for expiry in expiries:
        expiry_str = expiry.strftime("%d%b%Y").upper()  # e.g. "28APR2026"
        try:
            raw = await _fetch_option_chain_from_api(symbol, expiry_str)
        except Exception as exc:
            logger.warning(f"chain_collector: {symbol} expiry {expiry_str} failed: {exc}")
            continue

        if raw.get("status") is False:
            logger.warning(f"chain_collector: API error for {symbol}: {raw.get('message')}")
            continue

        chain_data = raw.get("data", {})
        if underlying_ltp is None and chain_data.get("underlyingValue"):
            underlying_ltp = Decimal(str(chain_data["underlyingValue"]))

        for entry in chain_data.get("optionChainData", []):
            strike = entry.get("strikePrice")
            if strike is None:
                continue
            for opt_type in _OPTION_TYPES:
                opt_data = entry.get(opt_type.lower(), {})
                if not opt_data:
                    continue
                row = _row_from_api(
                    instrument.id, now, expiry, strike, opt_type, opt_data, underlying_ltp
                )
                rows.append(row)

    return ChainSnapshot(
        instrument_id=instrument.id,
        snapshot_at=now,
        rows=rows,
        underlying_ltp=underlying_ltp,
    )


async def _persist_snapshot(snapshot: ChainSnapshot) -> None:
    """Write chain rows to the `options_chain` table, skipping duplicates."""
    async with session_scope() as session:
        for row in snapshot.rows:
            # enrich with IV/Greeks if missing
            if row.underlying_ltp and (row.iv is None or row.delta is None):
                from datetime import date as date_cls
                T = max(0.0, (row.expiry_date - snapshot.snapshot_at.date()).days / 365.0)
                row = enrich_chain_row(row, T)

            session.add(OptionsChain(
                instrument_id=row.instrument_id,
                snapshot_at=snapshot.snapshot_at,
                expiry_date=row.expiry_date,
                strike_price=row.strike_price,
                option_type=row.option_type,
                ltp=row.ltp,
                bid_price=row.bid_price,
                ask_price=row.ask_price,
                bid_qty=row.bid_qty,
                ask_qty=row.ask_qty,
                volume=row.volume,
                oi=row.oi,
                oi_change=None,  # computed in a separate pass
                iv=row.iv,
                delta=row.delta,
                gamma=row.gamma,
                theta=row.theta,
                vega=row.vega,
                underlying_ltp=row.underlying_ltp,
            ))


async def collect_all() -> None:
    """Collect chain data for all F&O-eligible instruments."""
    from sqlalchemy import select

    async with session_scope() as session:
        result = await session.execute(
            select(Instrument).where(
                Instrument.is_fno == True,  # noqa: E712
                Instrument.is_active == True,  # noqa: E712
            )
        )
        instruments = result.scalars().all()

    logger.info(f"chain_collector: collecting chains for {len(instruments)} instruments")
    for inst in instruments:
        await collect(inst)
        await asyncio.sleep(0.2)  # Rate-limit: 5 req/sec
