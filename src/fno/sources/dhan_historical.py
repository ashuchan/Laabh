"""Dhan historical option chain source — replays from Dhan v2 intraday API.

Uses Dhan's /v2/charts/intraday endpoint with oi:true to reconstruct a
chain snapshot for any historical timestamp. Filters contracts by liquidity
using the F&O bhavcopy for date D.

Disk cache: DRYRUN_DHAN_CACHE_DIR/{D}/{security_id}_{interval}.json
Each file stores the full candle list; the replay picks the candle at-or-before
the requested as_of timestamp.
"""
from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import ClassVar

import httpx
from loguru import logger

from src.config import get_settings
from src.fno.sources.base import BaseChainSource, ChainSnapshot, StrikeRow
from src.fno.sources.exceptions import AuthError, SourceUnavailableError

_SETTINGS = get_settings()

_DHAN_INTRADAY_URL = "https://api.dhan.co/v2/charts/intraday"
_DHAN_INSTRUMENT_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"

# Dhan segment codes
_SEG_INDEX = "IDX_I"
_SEG_EQUITY = "NSE_FNO"
_INDEX_SYMBOLS = frozenset({"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"})

_CANDLE_INTERVAL = "5"  # 5-minute candles
_CONCURRENCY = 10  # parallel Dhan API calls


class DhanHistoricalSource(BaseChainSource):
    """Reconstructs option chain snapshots from Dhan v2 historical intraday data."""

    name: ClassVar[str] = "dhan_historical"

    def __init__(self, replay_date: date) -> None:
        self._replay_date = replay_date
        self._bhavcopy_df = None  # loaded lazily
        self._instrument_master: dict | None = None  # loaded lazily
        self._cache_dir = (
            Path(_SETTINGS.dryrun_dhan_cache_dir).expanduser()
            / replay_date.strftime("%Y%m%d")
        )
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._semaphore = asyncio.Semaphore(_CONCURRENCY)
        self._client: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        token = _SETTINGS.dhan_access_token
        client_id = _SETTINGS.dhan_client_id
        if not token or not client_id:
            raise AuthError("DHAN_ACCESS_TOKEN or DHAN_CLIENT_ID not configured")
        return {
            "access-token": token,
            "client-id": client_id,
            "Content-Type": "application/json",
        }

    def _client_instance(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(20.0))
        return self._client

    @staticmethod
    def _segment_for(symbol: str) -> str:
        return _SEG_INDEX if symbol.upper() in _INDEX_SYMBOLS else _SEG_EQUITY

    async def _load_bhavcopy(self) -> None:
        """Load F&O bhavcopy for the replay date (filters by liquidity thresholds)."""
        if self._bhavcopy_df is not None:
            return
        from src.dryrun.bhavcopy import fetch_fo_bhavcopy
        df = await fetch_fo_bhavcopy(self._replay_date)
        min_oi = _SETTINGS.dryrun_min_contract_oi
        min_vol = _SETTINGS.dryrun_min_contract_volume
        # Apply liquidity filters
        mask = (
            (df.get("oi", 0) >= min_oi if "oi" in df.columns else True) &
            (df.get("contracts", 0) >= min_vol if "contracts" in df.columns else True)
        )
        self._bhavcopy_df = df[mask].copy() if not isinstance(mask, bool) else df
        logger.info(
            f"dhan_historical: loaded bhavcopy for {self._replay_date} "
            f"— {len(self._bhavcopy_df)} liquid contracts"
        )

    async def _load_instrument_master(self) -> None:
        """Download and parse the Dhan instrument master CSV."""
        if self._instrument_master is not None:
            return
        cache_path = self._cache_dir / "instrument_master.json"
        if cache_path.exists():
            with cache_path.open() as f:
                self._instrument_master = json.load(f)
            return

        logger.info("dhan_historical: downloading instrument master CSV")
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.get(_DHAN_INSTRUMENT_MASTER_URL)
            resp.raise_for_status()
        import io
        import pandas as pd
        df = pd.read_csv(io.StringIO(resp.text), dtype=str)
        df.columns = [c.strip().lower() for c in df.columns]
        # Build lookup: (symbol, expiry_yyyymmdd, strike, option_type) → security_id
        master: dict[str, str] = {}
        for _, row in df.iterrows():
            sym = str(row.get("sm_symbol_name", "") or "").strip().upper()
            expiry_raw = str(row.get("sm_expiry_date", "") or "").strip()
            strike_raw = str(row.get("sm_strike_price", "") or "").strip()
            opt_type = str(row.get("sm_option_type", "") or "").strip().upper()
            sec_id = str(row.get("sm_security_id", "") or "").strip()
            if sym and expiry_raw and strike_raw and opt_type in ("CE", "PE") and sec_id:
                try:
                    expiry_dt = datetime.strptime(expiry_raw[:10], "%Y-%m-%d").date()
                    expiry_key = expiry_dt.strftime("%Y%m%d")
                except ValueError:
                    continue
                key = f"{sym}|{expiry_key}|{strike_raw}|{opt_type}"
                master[key] = sec_id
        self._instrument_master = master
        with cache_path.open("w") as f:
            json.dump(master, f)
        logger.info(f"dhan_historical: instrument master loaded — {len(master)} entries")

    def _lookup_security_id(
        self, symbol: str, expiry_date: date, strike: float, option_type: str
    ) -> str | None:
        if self._instrument_master is None:
            return None
        key = f"{symbol.upper()}|{expiry_date.strftime('%Y%m%d')}|{strike:.2f}|{option_type.upper()}"
        result = self._instrument_master.get(key)
        if result is None:
            # Try without decimals (e.g. 18000.0 → 18000)
            key2 = f"{symbol.upper()}|{expiry_date.strftime('%Y%m%d')}|{int(strike)}|{option_type.upper()}"
            result = self._instrument_master.get(key2)
        return result

    def _lookup_underlying_security_id(self, symbol: str) -> str | None:
        """Find the security_id for the underlying index/equity."""
        if self._instrument_master is None:
            return None
        # Search for exact index/equity match (no expiry/strike in key)
        prefix = f"{symbol.upper()}|"
        for k, v in self._instrument_master.items():
            parts = k.split("|")
            if len(parts) == 4 and parts[0] == symbol.upper():
                # Found any option for this symbol — not what we want
                pass
        # For indices, look for a spot entry in the master
        # Fallback: return None and use bhavcopy close price
        return None

    async def _fetch_candles(self, security_id: str, as_of: datetime) -> list[dict]:
        """Fetch 5-min intraday candles with OI for a security_id, with disk cache."""
        cache_file = self._cache_dir / f"{security_id}_{_CANDLE_INTERVAL}.json"
        if cache_file.exists():
            with cache_file.open() as f:
                return json.load(f)

        # Dhan intraday endpoint requires date range in IST (API interprets strings as IST)
        import pytz as _pytz
        _ist_tz = _pytz.timezone("Asia/Kolkata")
        from_ist = _ist_tz.localize(datetime(
            self._replay_date.year, self._replay_date.month, self._replay_date.day,
            9, 0, 0
        ))
        to_ist = _ist_tz.localize(datetime(
            self._replay_date.year, self._replay_date.month, self._replay_date.day,
            15, 30, 0
        ))
        payload = {
            "securityId": security_id,
            "exchangeSegment": _SEG_EQUITY,
            "instrument": "OPTIDX",
            "interval": _CANDLE_INTERVAL,
            "oi": True,
            "fromDate": from_ist.strftime("%Y-%m-%d %H:%M:%S"),
            "toDate": to_ist.strftime("%Y-%m-%d %H:%M:%S"),
        }
        async with self._semaphore:
            async with self._client_instance() as client:
                resp = await client.post(
                    _DHAN_INTRADAY_URL,
                    headers=self._headers(),
                    json=payload,
                    timeout=20,
                )
        if resp.status_code in (401, 403):
            raise AuthError(f"Dhan auth failed: {resp.status_code}")
        if resp.status_code != 200:
            raise SourceUnavailableError(f"Dhan intraday {resp.status_code}: {resp.text[:200]}")

        data = resp.json()
        candles = data.get("data", data) if isinstance(data, dict) else data
        if not isinstance(candles, list):
            candles = []

        with cache_file.open("w") as f:
            json.dump(candles, f)
        return candles

    @staticmethod
    def _pick_candle(candles: list[dict], as_of: datetime) -> dict | None:
        """Return the last candle whose timestamp is at-or-before as_of."""
        best: dict | None = None
        as_of_ts = as_of.timestamp()
        for c in candles:
            ts_raw = c.get("timestamp") or c.get("time") or c.get("start_Time")
            if ts_raw is None:
                continue
            try:
                if isinstance(ts_raw, (int, float)):
                    c_ts = float(ts_raw)
                else:
                    c_ts = datetime.fromisoformat(str(ts_raw)).timestamp()
            except (ValueError, OSError):
                continue
            if c_ts <= as_of_ts:
                if best is None or c_ts > (best.get("_ts") or 0):
                    best = {**c, "_ts": c_ts}
        return best

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def fetch(
        self,
        symbol: str,
        expiry_date: date,
        *,
        as_of: datetime | None = None,
    ) -> ChainSnapshot:
        """Reconstruct a ChainSnapshot from Dhan historical data."""
        await self._load_bhavcopy()
        await self._load_instrument_master()

        if as_of is None:
            as_of = datetime.combine(self._replay_date, datetime.min.time()).replace(
                hour=15, minute=30, tzinfo=timezone.utc
            )

        df = self._bhavcopy_df
        if df is None or df.empty:
            return ChainSnapshot(
                symbol=symbol,
                expiry_date=expiry_date,
                underlying_ltp=None,
                snapshot_at=as_of,
                strikes=[],
            )

        # Filter bhavcopy to this symbol + expiry
        mask = df["symbol"].str.upper() == symbol.upper()
        if "expiry_date" in df.columns:
            mask &= df["expiry_date"] == expiry_date
        sub = df[mask]

        strikes: list[StrikeRow] = []
        tasks = []

        rows_to_process = []
        for _, bhav_row in sub.iterrows():
            opt_type = str(bhav_row.get("option_type", "")).strip().upper()
            if opt_type not in ("CE", "PE"):
                continue
            strike = float(bhav_row.get("strike_price", 0) or 0)
            if strike <= 0:
                continue
            sec_id = self._lookup_security_id(symbol, expiry_date, strike, opt_type)
            rows_to_process.append((bhav_row, opt_type, strike, sec_id))

        # Fetch all candles concurrently
        sec_ids_needed = [sec_id for _, _, _, sec_id in rows_to_process if sec_id]
        candles_by_sec: dict[str, list[dict]] = {}
        fetch_tasks = {
            sec_id: asyncio.create_task(self._fetch_candles(sec_id, as_of))
            for sec_id in set(sec_ids_needed)
        }
        for sec_id, task in fetch_tasks.items():
            try:
                candles_by_sec[sec_id] = await task
            except Exception as exc:
                logger.debug(f"dhan_historical: candle fetch failed for {sec_id}: {exc}")
                candles_by_sec[sec_id] = []

        # Build StrikeRows
        for bhav_row, opt_type, strike, sec_id in rows_to_process:
            candles = candles_by_sec.get(sec_id, []) if sec_id else []
            candle = self._pick_candle(candles, as_of)

            ltp: Decimal | None = None
            oi: int | None = None
            if candle:
                close_val = candle.get("close") or candle.get("c")
                oi_val = candle.get("oi") or candle.get("openInterest")
                if close_val is not None:
                    try:
                        ltp = Decimal(str(close_val))
                    except Exception:
                        pass
                if oi_val is not None:
                    try:
                        oi = int(oi_val)
                    except Exception:
                        pass
            # Fallback to bhavcopy close
            if ltp is None:
                bhav_close = bhav_row.get("close") or bhav_row.get("settle_price")
                if bhav_close is not None and not pd_is_na(bhav_close):
                    try:
                        ltp = Decimal(str(bhav_close))
                    except Exception:
                        pass
            if oi is None:
                bhav_oi = bhav_row.get("oi")
                if bhav_oi is not None and not pd_is_na(bhav_oi):
                    try:
                        oi = int(bhav_oi)
                    except Exception:
                        pass

            strikes.append(StrikeRow(
                strike=Decimal(str(strike)),
                option_type=opt_type,
                ltp=ltp,
                oi=oi,
            ))

        # Get underlying LTP from bhavcopy (CM) or return None
        underlying_ltp: Decimal | None = None
        try:
            from src.dryrun.bhavcopy import fetch_cm_bhavcopy
            cm_df = await fetch_cm_bhavcopy(self._replay_date)
            cm_row = cm_df[cm_df["symbol"].str.upper() == symbol.upper()]
            if not cm_row.empty:
                close_val = cm_row["close"].iloc[0]
                if not pd_is_na(close_val):
                    underlying_ltp = Decimal(str(float(close_val)))
        except Exception:
            pass

        return ChainSnapshot(
            symbol=symbol,
            expiry_date=expiry_date,
            underlying_ltp=underlying_ltp,
            snapshot_at=as_of,
            strikes=strikes,
        )

    async def health_check(self) -> bool:
        """Return True if Dhan credentials are present and the API is reachable."""
        if not _SETTINGS.dhan_access_token or not _SETTINGS.dhan_client_id:
            return False
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    "https://api.dhan.co/v2/marketstatus",
                    headers=self._headers(),
                )
                return resp.status_code < 500
        except Exception:
            return False


def pd_is_na(val) -> bool:
    """Return True if val is None or pandas NA."""
    try:
        import pandas as pd
        return pd.isna(val)
    except (TypeError, ValueError):
        return val is None
