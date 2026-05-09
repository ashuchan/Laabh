"""Load RBI policy repo rate history into ``rbi_repo_history``.

The RBI publishes the policy repo rate via its monetary-policy press
releases. The published format on rbi.org.in is HTML and unstable across
years, so we don't scrape — instead the operator provides a small CSV
maintained by hand or imported from a third-party data source.

Expected CSV format (header optional, header rows auto-detected):

    date,repo_rate_pct
    2020-03-27,4.40
    2020-05-22,4.00
    2022-05-04,4.40
    ...

Decision Note (manual ingest):
  * RBI typically has < 50 row total over 20 years. The data is small and
    high-impact (drives the risk-free rate in BS pricing); a hand-curated
    CSV that ships with the repo or sits in a known location avoids
    silent regressions when the RBI website's HTML changes.
  * The loader is idempotent via ``ON CONFLICT (date) DO UPDATE`` so the
    operator can safely re-run after correcting a row.
"""
from __future__ import annotations

import csv
import uuid
from datetime import date as date_type
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from loguru import logger
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.db import session_scope
from src.models.rbi_repo_history import RBIRepoHistory


def _parse_csv(text: str) -> list[tuple[date_type, Decimal]]:
    """Parse a (date, repo_rate_pct) CSV string. Skips header rows.

    Accepts ISO-format dates (YYYY-MM-DD). Drops rows where either column
    fails to parse.
    """
    out: list[tuple[date_type, Decimal]] = []
    reader = csv.reader(text.splitlines())
    for row in reader:
        if not row or len(row) < 2:
            continue
        d_raw = row[0].strip()
        r_raw = row[1].strip()
        if not d_raw or not r_raw:
            continue
        # Skip header
        if d_raw.lower() in {"date", "effective_date"}:
            continue
        try:
            d = datetime.strptime(d_raw, "%Y-%m-%d").date()
        except ValueError:
            continue
        try:
            r = Decimal(r_raw)
        except InvalidOperation:
            continue
        out.append((d, r))
    return out


async def load_from_csv(
    csv_path: str | Path,
    *,
    source: str = "manual",
    as_of: datetime | None = None,
    dryrun_run_id: uuid.UUID | None = None,
) -> dict[str, int]:
    """Upsert rows from a (date, repo_rate_pct) CSV into ``rbi_repo_history``.

    Args:
        csv_path: Path to the CSV file.
        source: Source-tag stored on each row (default "manual").
        as_of, dryrun_run_id: CLAUDE.md convention parameters. The
            ``rbi_repo_history`` table has no ``dryrun_run_id`` column
            (RBI rates are policy-published; the dry-run notion doesn't
            apply to them in the same way as live ticks). Accepted with
            default ``None`` so downstream pipelines don't break, but not
            persisted.

    Returns:
        ``{"parsed": N, "upserted": M}``. Both counts are equal in practice
        because every parsed row is upserted — the field is kept separate
        so callers can spot parse-time drops.
    """
    p = Path(csv_path)
    if not p.exists():
        raise FileNotFoundError(f"RBI repo CSV not found: {p}")
    text = p.read_text(encoding="utf-8")
    rows = _parse_csv(text)
    if not rows:
        logger.warning(f"rbi_repo_history: no parseable rows in {p}")
        return {"parsed": 0, "upserted": 0}

    upserted = 0
    async with session_scope() as session:
        for d, r in rows:
            stmt = (
                pg_insert(RBIRepoHistory)
                .values(date=d, repo_rate_pct=r, source=source)
                .on_conflict_do_update(
                    index_elements=["date"],
                    set_={"repo_rate_pct": r, "source": source},
                )
            )
            await session.execute(stmt)
            upserted += 1

    logger.info(
        f"rbi_repo_history: upserted {upserted} rows from {p} (source={source})"
    )
    return {"parsed": len(rows), "upserted": upserted}


async def get_repo_rate_for(d: date_type) -> Decimal | None:
    """Return the repo rate in effect on date ``d``.

    Looks up the most recent rate at-or-before ``d``. Returns None if no
    rows exist on or before that date.
    """
    from sqlalchemy import select

    async with session_scope() as session:
        q = (
            select(RBIRepoHistory.repo_rate_pct)
            .where(RBIRepoHistory.date <= d)
            .order_by(RBIRepoHistory.date.desc())
            .limit(1)
        )
        row = (await session.execute(q)).first()
        return row[0] if row else None
