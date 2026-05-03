"""NSE F&O and cash-market bhavcopy fetcher with disk cache.

Downloads the NSE UDiFF bhavcopy ZIP for a given date, unzips it, parses the CSV,
and returns a normalised pandas DataFrame. Results are cached on disk so replays
of the same date don't re-download.

F&O URL pattern:
  https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{YYYYMMDD}_F_0000.csv.zip

Cash-market URL pattern:
  https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{YYYYMMDD}_F_0000.csv.zip
"""
from __future__ import annotations

import io
import zipfile
from datetime import date
from pathlib import Path

import httpx
import pandas as pd
from loguru import logger

from src.config import get_settings

_SETTINGS = get_settings()

_FO_URL_TEMPLATE = (
    "https://nsearchives.nseindia.com/content/fo/"
    "BhavCopy_NSE_FO_0_0_0_{yyyymmdd}_F_0000.csv.zip"
)
_CM_URL_TEMPLATE = (
    "https://nsearchives.nseindia.com/content/cm/"
    "BhavCopy_NSE_CM_0_0_0_{yyyymmdd}_F_0000.csv.zip"
)
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://www.nseindia.com/",
}

# Canonical column names after normalisation
_FO_COLUMNS = [
    "symbol", "instrument", "expiry_date", "strike_price", "option_type",
    "open", "high", "low", "close", "settle_price",
    "contracts", "value_in_lakh", "oi", "change_in_oi", "last_price",
]
_CM_COLUMNS = [
    "symbol", "series", "open", "high", "low", "close", "last_price",
    "prev_close", "tottrdqty", "tottrdval", "timestamp", "totaltrades", "isin",
]


class BhavcopyMissingError(Exception):
    """Raised when the NSE archive returns a 404 for the requested date."""


def _cache_path(segment: str, d: date) -> Path:
    cache_dir = Path(_SETTINGS.dryrun_bhavcopy_cache_dir).expanduser()
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / f"{segment}_{d.strftime('%Y%m%d')}.parquet"


async def _download_zip(url: str) -> bytes:
    """Download a ZIP from NSE archives. Raises BhavcopyMissingError on 404."""
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        # Prime the NSE cookie
        try:
            await client.get("https://www.nseindia.com", headers=_HEADERS)
        except Exception:
            pass  # best-effort cookie prime; proceed anyway
        resp = await client.get(url, headers=_HEADERS)
        if resp.status_code == 404:
            raise BhavcopyMissingError(f"NSE archive 404: {url}")
        resp.raise_for_status()
        return resp.content


def _extract_csv_from_zip(data: bytes) -> str:
    """Extract the first CSV from a ZIP bytes object."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        csv_names = [n for n in z.namelist() if n.lower().endswith(".csv")]
        if not csv_names:
            raise ValueError("No CSV found in bhavcopy ZIP")
        return z.read(csv_names[0]).decode("utf-8", errors="replace")


def _parse_fo_csv(csv_text: str) -> pd.DataFrame:
    """Parse F&O bhavcopy CSV into a normalised DataFrame."""
    df = pd.read_csv(io.StringIO(csv_text), dtype=str)
    # Normalise column names: strip whitespace, lowercase
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]

    # Map known NSE column aliases to canonical names.
    # Covers both legacy bhavcopy (finsymbol/expiry_dt/...) and current
    # UDiFF format (tckrsymb/xprydt/strkpric/optntp/...).
    rename_map = {
        # Legacy NSE bhavcopy
        "finsymbol": "symbol",
        "expiry_dt": "expiry_date",
        "strikeprice": "strike_price",
        "optiontype": "option_type",
        "openpric": "open",
        "highpric": "high",
        "lowpric": "low",
        "closingpric": "close",
        "setlpric": "settle_price",
        "no_of_contracts": "contracts",
        "val_inlakh": "value_in_lakh",
        "open_int": "oi",
        "chng_in_opn_int": "change_in_oi",
        "lastpric": "last_price",
        # UDiFF (current NSE bhavcopy format, post-2024)
        "tckrsymb": "symbol",
        "xprydt": "expiry_date",
        "strkpric": "strike_price",
        "optntp": "option_type",
        "fininstrmtp": "instrument",
        "opnpric": "open",
        "hghpric": "high",
        "lwpric": "low",
        "clspric": "close",
        "sttlmpric": "settle_price",
        "ttltradgvol": "contracts",
        "ttltrfval": "value_in_lakh",
        "opnintrst": "oi",
        "chnginopnintrst": "change_in_oi",
        "undrlygpric": "underlying_price",
    }
    df = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns})

    # Coerce numeric columns
    for col in ["strike_price", "open", "high", "low", "close", "settle_price",
                "contracts", "value_in_lakh", "oi", "change_in_oi", "last_price",
                "underlying_price"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # Normalise expiry_date to date objects
    if "expiry_date" in df.columns:
        df["expiry_date"] = pd.to_datetime(df["expiry_date"], errors="coerce").dt.date

    # Keep only options (skip futures). Legacy uses OPTSTK/OPTIDX; UDiFF uses STO/IDO.
    if "instrument" in df.columns:
        instr = df["instrument"].astype(str).str.upper()
        df = df[instr.str.startswith("OPT") | instr.isin(["STO", "IDO"])].copy()

    # Normalise option_type to CE/PE
    if "option_type" in df.columns:
        df["option_type"] = df["option_type"].astype(str).str.strip().str.upper()
        df = df[df["option_type"].isin(["CE", "PE"])].copy()

    return df.reset_index(drop=True)


def _parse_cm_csv(csv_text: str) -> pd.DataFrame:
    """Parse cash-market bhavcopy CSV into a normalised DataFrame."""
    df = pd.read_csv(io.StringIO(csv_text), dtype=str)
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]
    rename_map = {
        "symbol_x": "symbol",
        "tottrdqty": "tottrdqty",
    }
    df = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns})
    for col in ["open", "high", "low", "close", "last_price", "prev_close",
                "tottrdqty", "tottrdval", "totaltrades"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.reset_index(drop=True)


async def fetch_fo_bhavcopy(d: date) -> pd.DataFrame:
    """Return the F&O bhavcopy DataFrame for date d, using disk cache."""
    cache = _cache_path("fo", d)
    if cache.exists():
        logger.debug(f"bhavcopy: cache hit for F&O {d}")
        return pd.read_parquet(cache)

    yyyymmdd = d.strftime("%Y%m%d")
    url = _FO_URL_TEMPLATE.format(yyyymmdd=yyyymmdd)
    logger.info(f"bhavcopy: downloading F&O bhavcopy for {d}")
    raw = await _download_zip(url)
    csv_text = _extract_csv_from_zip(raw)
    df = _parse_fo_csv(csv_text)
    df.to_parquet(cache, index=False)
    logger.info(f"bhavcopy: F&O {d} — {len(df)} option rows cached to {cache}")
    return df


async def fetch_cm_bhavcopy(d: date) -> pd.DataFrame:
    """Return the cash-market bhavcopy DataFrame for date d, using disk cache."""
    cache = _cache_path("cm", d)
    if cache.exists():
        logger.debug(f"bhavcopy: cache hit for CM {d}")
        return pd.read_parquet(cache)

    yyyymmdd = d.strftime("%Y%m%d")
    url = _CM_URL_TEMPLATE.format(yyyymmdd=yyyymmdd)
    logger.info(f"bhavcopy: downloading CM bhavcopy for {d}")
    raw = await _download_zip(url)
    csv_text = _extract_csv_from_zip(raw)
    df = _parse_cm_csv(csv_text)
    df.to_parquet(cache, index=False)
    logger.info(f"bhavcopy: CM {d} — {len(df)} rows cached to {cache}")
    return df
