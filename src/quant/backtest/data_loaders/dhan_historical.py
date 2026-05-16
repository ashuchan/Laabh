"""Backfill ``price_intraday`` from Dhan v2 historical intraday API.

Iterates the F&O underlying universe, fetches per-day intraday bars from
Dhan, and inserts into ``price_intraday`` with idempotent ``ON CONFLICT DO
NOTHING`` on ``(instrument_id, timestamp)``.

Decision Notes (post-inspection of existing code):

  * **Auth**: reuses ``src.auth.dhan_token.get_dhan_headers`` so token refresh
    is handled in one place. On a 401 mid-call we force-refresh and retry once.
  * **Interval**: spec calls out "3-min granularity" but the Dhan v2 historical
    intraday endpoint only ships 1-minute bars (or wider). We pull 1-minute
    and store as-is — the BacktestFeatureStore (Task 8) aggregates to 3-min
    on read. The schema (price_intraday) is interval-agnostic.
  * **Resumable** without a new checkpoint table: ``MAX(timestamp)`` per
    instrument from ``price_intraday`` itself is the resume point. Adding a
    dedicated checkpoint table would be premature — it'd need its own
    migration and adds nothing the index already provides.
  * **Rate limit**: simple per-instance token-bucket sized from
    ``dhan_historical_rate_limit_per_min``. Sequential per-instrument
    iteration under the budget; the Dhan v2 docs suggest 30 req/min is safe.
  * **Idempotent**: composite PK on price_intraday + ON CONFLICT DO NOTHING.
"""
from __future__ import annotations

import asyncio
import time as _time
import uuid
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Iterable
from uuid import UUID

import httpx
import pytz
from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from src.auth.dhan_token import DhanAuthError, get_dhan_headers
from src.config import get_settings
from src.db import session_scope
from src.models.instrument import Instrument
from src.models.price_intraday import PriceIntraday
from src.quant.backtest.clock import trading_days_between


_IST = pytz.timezone("Asia/Kolkata")
_DHAN_INTRADAY_URL = "https://api.dhan.co/v2/charts/intraday"
# Per Dhan v2 docs: indices are IDX_I, F&O equities NSE_FNO, cash equities NSE_EQ.
# For intraday underlying OHLC we want NSE_EQ for stocks and IDX_I for indices.
_SEG_INDEX = "IDX_I"
_SEG_EQUITY = "NSE_EQ"
_INDEX_SYMBOLS = frozenset(
    {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50", "SENSEX"}
)
# 1-minute candles: most granular Dhan v2 intraday option.
_INTERVAL_MINUTES = "1"


class _DhanTransient(RuntimeError):
    """Transient Dhan failure (5xx, 429) — eligible for retry. Distinct
    type so ``tenacity.retry_if_exception_type`` only retries these and
    not the 4xx ``RuntimeError`` from ``_fetch_one_day``.
    """


# ---------------------------------------------------------------------------
# Rate limiter (simple token bucket sized per-minute)
# ---------------------------------------------------------------------------

class _RateLimiter:
    """Async token-bucket: ``max_per_min`` requests per rolling 60-second window.

    Used by the loader to stay under Dhan's published rate limit.
    Standalone class so tests can drive it directly.
    """

    def __init__(self, max_per_min: int):
        self._max = max(1, int(max_per_min))
        self._stamps: list[float] = []
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = _time.monotonic()
                # Drop stamps older than 60s
                self._stamps = [s for s in self._stamps if now - s < 60.0]
                if len(self._stamps) < self._max:
                    self._stamps.append(now)
                    return
                # Sleep until the oldest stamp ages out, then re-check.
                wait = 60.0 - (now - self._stamps[0]) + 0.01
                await asyncio.sleep(max(0.01, wait))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _segment_for(symbol: str) -> str:
    return _SEG_INDEX if symbol.upper() in _INDEX_SYMBOLS else _SEG_EQUITY


def _validate_ohlc(o: float, h: float, l: float, c: float, v: int) -> bool:
    """OHLC arithmetic invariants per spec acceptance criteria."""
    if v < 0:
        return False
    if not (l <= o <= h and l <= c <= h):
        return False
    return True


@retry(
    retry=retry_if_exception_type(
        (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError, _DhanTransient)
    ),
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    reraise=True,
)
async def _fetch_one_day(
    *,
    client: httpx.AsyncClient,
    security_id: str,
    symbol: str,
    target_date: date,
    timeout: float = 20.0,
) -> list[dict]:
    """Fetch one day's 1-min candles for ``security_id`` as a list of dicts.

    Each candle dict carries at minimum: ``open``, ``high``, ``low``, ``close``,
    ``volume``, ``timestamp`` (epoch seconds, IST-implied per Dhan v2).

    Returns an empty list if the day has no candles (holiday/weekend
    pre-filter or empty trading session).

    Retry policy (CLAUDE.md convention): exponential backoff on httpx
    transport errors and Dhan 5xx/429. 4xx other than 401 (which is
    handled inline by force-refreshing the token once) fail fast — they're
    almost always permanent client-side problems.
    """
    from_ist = _IST.localize(
        datetime(target_date.year, target_date.month, target_date.day, 9, 0, 0)
    )
    to_ist = _IST.localize(
        datetime(target_date.year, target_date.month, target_date.day, 15, 30, 0)
    )
    payload = {
        "securityId": security_id,
        "exchangeSegment": _segment_for(symbol),
        "instrument": "INDEX" if symbol.upper() in _INDEX_SYMBOLS else "EQUITY",
        "interval": _INTERVAL_MINUTES,
        "oi": False,
        "fromDate": from_ist.strftime("%Y-%m-%d %H:%M:%S"),
        "toDate": to_ist.strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        headers = await get_dhan_headers()
    except DhanAuthError as exc:
        raise RuntimeError(f"Dhan auth not configured: {exc}") from exc

    resp = await client.post(_DHAN_INTRADAY_URL, headers=headers, json=payload, timeout=timeout)
    if resp.status_code == 401:
        # Token may have been revoked server-side mid-life. Force-refresh and retry once.
        headers = await get_dhan_headers(force_refresh=True)
        resp = await client.post(_DHAN_INTRADAY_URL, headers=headers, json=payload, timeout=timeout)

    if resp.status_code == 429 or 500 <= resp.status_code < 600:
        # Transient — eligible for tenacity retry with exponential backoff.
        raise _DhanTransient(
            f"Dhan intraday {resp.status_code} (transient) for "
            f"{symbol}/{target_date}: {resp.text[:200]}"
        )
    if resp.status_code != 200:
        # Permanent 4xx (other than 401) — fail fast; tenacity does not retry.
        raise RuntimeError(
            f"Dhan intraday {resp.status_code} for {symbol}/{target_date}: "
            f"{resp.text[:200]}"
        )
    body = resp.json()
    # Dhan v2 returns candles either as parallel arrays or as a list. Handle both.
    if isinstance(body, dict) and "data" in body:
        body = body["data"]
    if isinstance(body, dict):
        # Parallel-array form: {"open": [...], "high": [...], ...}
        opens = body.get("open", [])
        highs = body.get("high", [])
        lows = body.get("low", [])
        closes = body.get("close", [])
        vols = body.get("volume", [])
        ts = body.get("timestamp") or body.get("start_Time") or body.get("startTime") or []
        return [
            {
                "open": opens[i],
                "high": highs[i],
                "low": lows[i],
                "close": closes[i],
                "volume": vols[i],
                "timestamp": ts[i] if i < len(ts) else None,
            }
            for i in range(min(len(opens), len(highs), len(lows), len(closes), len(vols)))
        ]
    if isinstance(body, list):
        return body
    return []


def _parse_dhan_timestamp(raw) -> datetime | None:
    """Coerce a Dhan candle timestamp into a UTC-aware datetime.

    Dhan v2 ships epoch seconds (IST-implied per docs) in most responses, but
    historical strings and parallel-array forms occasionally appear.
    """
    if raw is None:
        return None
    try:
        if isinstance(raw, (int, float)):
            # Epoch seconds (Dhan v2 docs). The epoch is unambiguous — we
            # convert to UTC directly. Some older Dhan responses ship the
            # epoch already aligned to UTC; others align to IST. Calling
            # ``.timestamp()`` from the IST-aware path produces a UTC epoch,
            # so reading it back via ``fromtimestamp(tz=UTC)`` round-trips.
            return datetime.fromtimestamp(float(raw), tz=timezone.utc)
        if isinstance(raw, str):
            # ISO 8601 parser — robust to "+05:30" and "Z" suffixes.
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(
                timezone.utc
            )
    except (ValueError, OSError, OverflowError):
        return None
    return None


async def _resume_point(session, instrument_id: UUID) -> date | None:
    """Return the most recent ``date`` with bars stored for this instrument."""
    q = select(func.max(PriceIntraday.timestamp)).where(
        PriceIntraday.instrument_id == instrument_id
    )
    row = (await session.execute(q)).first()
    if row is None or row[0] is None:
        return None
    last = row[0]
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return last.astimezone(_IST).date()


async def load_one_instrument_one_date(
    *,
    instrument_id: UUID,
    symbol: str,
    security_id: str,
    target_date: date,
    client: httpx.AsyncClient,
    as_of: datetime | None = None,
    dryrun_run_id: uuid.UUID | None = None,
) -> dict[str, int]:
    """Fetch and persist ``target_date``'s bars for one instrument.

    ``as_of`` / ``dryrun_run_id`` follow CLAUDE.md convention. The
    ``price_intraday`` table has no ``dryrun_run_id`` column (the table was
    introduced for the backtest harness, not for the live dryrun pipeline),
    so ``dryrun_run_id`` is accepted but not persisted.

    Returns ``{"fetched": N, "inserted": M, "invalid": K}``. Inserted may be
    less than fetched when bars already exist (idempotent) or fail OHLC checks.
    """
    candles = await _fetch_one_day(
        client=client,
        security_id=security_id,
        symbol=symbol,
        target_date=target_date,
    )
    if not candles:
        return {"fetched": 0, "inserted": 0, "invalid": 0}

    inserted = 0
    invalid = 0
    async with session_scope() as session:
        for candle in candles:
            ts = _parse_dhan_timestamp(candle.get("timestamp"))
            if ts is None:
                invalid += 1
                continue
            try:
                o = float(candle["open"])
                h = float(candle["high"])
                lo = float(candle["low"])
                c = float(candle["close"])
                v = int(candle.get("volume") or 0)
            except (KeyError, TypeError, ValueError):
                invalid += 1
                continue
            if not _validate_ohlc(o, h, lo, c, v):
                invalid += 1
                continue
            stmt = (
                pg_insert(PriceIntraday)
                .values(
                    instrument_id=instrument_id,
                    timestamp=ts,
                    open=Decimal(str(o)),
                    high=Decimal(str(h)),
                    low=Decimal(str(lo)),
                    close=Decimal(str(c)),
                    volume=v,
                )
                .on_conflict_do_nothing(index_elements=["instrument_id", "timestamp"])
            )
            await session.execute(stmt)
            inserted += 1

    return {"fetched": len(candles), "inserted": inserted, "invalid": invalid}


async def backfill(
    *,
    instruments: Iterable[tuple[UUID, str, str]],
    start_date: date,
    end_date: date,
    holidays: Iterable[date] = (),
    rate_limit_per_min: int | None = None,
    as_of: datetime | None = None,
    dryrun_run_id: uuid.UUID | None = None,
) -> dict[str, int]:
    """Backfill ``price_intraday`` over ``[start, end]`` for given instruments.

    Args:
        instruments: Iterable of ``(instrument_id, symbol, dhan_security_id)``.
            Caller is responsible for joining ``instruments`` to the Dhan
            instrument master to resolve security IDs.
        start_date: First date (inclusive).
        end_date: Last date (inclusive).
        holidays: NSE holiday set; skipped during enumeration.
        rate_limit_per_min: Override config value for tests.
        as_of, dryrun_run_id: CLAUDE.md convention parameters. Forwarded
            to ``load_one_instrument_one_date`` (which itself accepts but
            does not persist them — see that function's docstring).

    Returns aggregate counts plus ``failed_calls``.
    """
    settings = get_settings()
    rl_max = (
        rate_limit_per_min
        if rate_limit_per_min is not None
        else settings.laabh_quant_backtest_dhan_rate_limit_per_min
    )
    limiter = _RateLimiter(rl_max)
    days = trading_days_between(start_date, end_date, holidays=holidays)
    instr_list = list(instruments)
    logger.info(
        f"dhan_historical: backfilling {len(instr_list)} instruments × "
        f"{len(days)} days = {len(instr_list) * len(days)} calls budget"
    )
    t0 = _time.monotonic()

    totals = {
        "instruments": len(instr_list),
        "days": len(days),
        "calls": 0,
        "fetched": 0,
        "inserted": 0,
        "invalid": 0,
        "failed_calls": 0,
        "skipped_resume": 0,
    }

    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
        for instrument_id, symbol, security_id in instr_list:
            # Resume point: skip dates already loaded.
            async with session_scope() as session:
                resume = await _resume_point(session, instrument_id)
            for d in days:
                if resume is not None and d <= resume:
                    totals["skipped_resume"] += 1
                    continue
                await limiter.acquire()
                totals["calls"] += 1
                try:
                    res = await load_one_instrument_one_date(
                        instrument_id=instrument_id,
                        symbol=symbol,
                        security_id=security_id,
                        target_date=d,
                        client=client,
                        as_of=as_of,
                        dryrun_run_id=dryrun_run_id,
                    )
                    for k in ("fetched", "inserted", "invalid"):
                        totals[k] += res[k]
                except Exception as exc:
                    logger.warning(
                        f"dhan_historical: {symbol} {d} failed: {exc!r}"
                    )
                    totals["failed_calls"] += 1
    totals["duration_sec"] = round(_time.monotonic() - t0, 2)
    logger.info(f"dhan_historical: done. {totals}")
    return totals


async def load_universe_from_db(
    *,
    only_fno: bool = True,
    limit: int | None = None,
) -> list[tuple[UUID, str, str]]:
    """Helper for callers: pull ``(id, symbol, dhan_security_id)`` from DB.

    The Dhan security ID lives in ``Instrument.metadata_["dhan_security_id"]``
    when the bootstrap script populates it; otherwise the instrument is
    skipped and a warning is logged.
    """
    async with session_scope() as session:
        q = select(Instrument).where(Instrument.is_active.is_(True))
        if only_fno:
            q = q.where(Instrument.is_fno.is_(True))
        if limit:
            q = q.limit(limit)
        rows = list((await session.execute(q)).scalars())

    out: list[tuple[UUID, str, str]] = []
    skipped = 0
    for inst in rows:
        meta = inst.metadata_ or {}
        sec_id = meta.get("dhan_security_id")
        if not sec_id:
            skipped += 1
            continue
        out.append((inst.id, inst.symbol, str(sec_id)))
    if skipped:
        logger.warning(
            f"dhan_historical: {skipped} instruments missing dhan_security_id "
            "in metadata — skipped. Run scripts/bootstrap_fno_universe.py "
            "first."
        )
    return out


# ---------------------------------------------------------------------------
# CLI entry point — invoked as `python -m src.quant.backtest.data_loaders.dhan_historical`
# from the backfill plan (§5 Phase F) and the original quant backtest runbook.
# ---------------------------------------------------------------------------


def _parse_date_cli(s: str) -> date:
    from datetime import datetime as _dt
    return _dt.strptime(s, "%Y-%m-%d").date()


async def _run_cli(
    *,
    start_date: date,
    end_date: date,
    only_fno: bool,
    limit: int | None,
    rate_limit_per_min: int | None,
) -> int:
    instruments = await load_universe_from_db(only_fno=only_fno, limit=limit)
    if not instruments:
        logger.error(
            "dhan_historical CLI: no instruments resolved — "
            "ensure bootstrap_fno_universe.py has populated dhan_security_id."
        )
        return 1
    totals = await backfill(
        instruments=instruments,
        start_date=start_date,
        end_date=end_date,
        rate_limit_per_min=rate_limit_per_min,
    )
    logger.info(f"dhan_historical CLI: completed — {totals}")
    return 0 if totals.get("failed_calls", 0) == 0 else 1


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "Backfill price_intraday from Dhan v2 historical intraday API. "
            "Idempotent on (instrument_id, timestamp). Resume point is the "
            "max timestamp already stored per instrument."
        )
    )
    parser.add_argument("--start", type=_parse_date_cli, required=True,
                        help="Inclusive start date (YYYY-MM-DD).")
    parser.add_argument("--end", type=_parse_date_cli, required=True,
                        help="Inclusive end date (YYYY-MM-DD).")
    parser.add_argument("--only-fno", action="store_true", default=True,
                        help="Restrict to F&O underlyings (default true).")
    parser.add_argument("--all-instruments", dest="only_fno",
                        action="store_false",
                        help="Include every active instrument, not just F&O.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Cap on instrument count — useful for smoke runs.")
    parser.add_argument("--rate-limit-per-min", type=int, default=None,
                        help="Override the configured Dhan req/min ceiling.")
    args = parser.parse_args()

    raise SystemExit(asyncio.run(_run_cli(
        start_date=args.start,
        end_date=args.end,
        only_fno=args.only_fno,
        limit=args.limit,
        rate_limit_per_min=args.rate_limit_per_min,
    )))
