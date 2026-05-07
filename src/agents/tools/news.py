"""News-related tool executors: search_raw_content, get_filings, search_transcript_chunks,
get_analyst_track_record.

All executors:
  * accept `instrument_id` as UUID *or* symbol — `_helpers.resolve_instrument_id`
    normalises before binding, so the LLM can pass either form;
  * parse ISO datetime strings into datetime objects before binding (asyncpg
    refuses to coerce strings on its own);
  * never raise — return `{"result": [], "error": <msg>}` on failure so the
    LLM sees a structured error and can adapt.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import text

from src.agents.tools._helpers import (
    parse_dt,
    resolve_analyst_id,
    resolve_instrument_id,
)

if TYPE_CHECKING:
    from src.agents.tools.registry import ToolContext


async def execute_search_raw_content(params: dict, ctx: "ToolContext") -> dict:
    """Search raw_content for one instrument over a time window."""
    since_dt = parse_dt(params.get("since"))
    until_dt = parse_dt(params.get("until"))
    if since_dt is None:
        return {"result": [], "error": "since: invalid or missing datetime"}

    limit = min(int(params.get("limit", 25)), 50)
    min_credibility = float(params.get("min_credibility", 0.0))
    include_types = params.get("include_types") or []

    try:
        async with ctx.db() as db:
            iid = await resolve_instrument_id(db, params.get("instrument_id"))
            if iid is None:
                return {"result": [],
                        "error": f"instrument_id {params.get('instrument_id')!r} did not resolve to a known UUID or symbol"}

            bind: dict = {
                "instrument_id": iid,
                "since": since_dt,
                "limit": limit,
                "min_cred": min_credibility,
            }
            date_filter = ""
            if until_dt is not None:
                date_filter = "AND rc.published_at < :until"
                bind["until"] = until_dt

            type_filter = ""
            if include_types:
                type_filter = "AND rc.media_type = ANY(:include_types)"
                bind["include_types"] = include_types

            credibility_where = (
                "AND COALESCE((ds.extraction_schema->>'credibility_weight')::numeric, 0.5) >= :min_cred"
                if min_credibility > 0
                else ""
            )

            result = await db.execute(
                text(f"""
                    SELECT rc.id, rc.title, rc.content_text, rc.url, rc.author,
                           rc.published_at, rc.media_type, rc.language,
                           COALESCE((ds.extraction_schema->>'credibility_weight')::numeric, 0.5) AS credibility_weight,
                           ds.name AS source_name
                    FROM raw_content rc
                    LEFT JOIN data_sources ds ON ds.id = rc.source_id
                    WHERE rc.published_at >= :since
                      {date_filter}
                      {type_filter}
                      {credibility_where}
                      AND rc.id IN (
                          SELECT DISTINCT rc2.id FROM raw_content rc2
                          JOIN signals s ON s.content_id = rc2.id
                          WHERE s.instrument_id = :instrument_id
                          UNION
                          SELECT rc3.id FROM raw_content rc3
                          WHERE rc3.content_text ILIKE (
                              SELECT '%' || symbol || '%' FROM instruments WHERE id = :instrument_id LIMIT 1
                          )
                      )
                    ORDER BY rc.published_at DESC
                    LIMIT :limit
                """),
                bind,
            )
            rows = result.fetchall()
            items = [
                {
                    "id": str(r[0]),
                    "title": r[1],
                    "content_text": (r[2] or "")[:2000],
                    "url": r[3],
                    "author": r[4],
                    "published_at": str(r[5]),
                    "media_type": r[6],
                    "language": r[7],
                    "credibility_weight": float(r[8] or 0.5),
                    "source_name": r[9],
                }
                for r in rows
            ]
            return {"result": items, "count": len(items)}
    except Exception as e:
        return {"result": [], "error": f"{type(e).__name__}: {e}"}


async def execute_get_filings(params: dict, ctx: "ToolContext") -> dict:
    """Retrieve SEBI/BSE/NSE regulatory filings for one instrument."""
    since_dt = parse_dt(params.get("since"))
    if since_dt is None:
        return {"result": [], "error": "since: invalid or missing datetime"}

    filing_types = params.get("filing_types") or []

    try:
        async with ctx.db() as db:
            iid = await resolve_instrument_id(db, params.get("instrument_id"))
            if iid is None:
                return {"result": [],
                        "error": f"instrument_id {params.get('instrument_id')!r} did not resolve"}

            bind: dict = {"instrument_id": iid, "since": since_dt, "limit": 20}
            type_filter = ""
            if filing_types:
                type_filter = "AND rc.media_type = ANY(:filing_types)"
                bind["filing_types"] = filing_types

            result = await db.execute(
                text(f"""
                    SELECT rc.id, rc.title, rc.content_text, rc.url,
                           rc.published_at, rc.media_type, ds.name AS source_name
                    FROM raw_content rc
                    JOIN data_sources ds ON ds.id = rc.source_id
                    JOIN signals s ON s.content_id = rc.id
                    WHERE s.instrument_id = :instrument_id
                      AND rc.published_at >= :since
                      AND ds.type IN ('bse_filing', 'nse_announcement')
                      {type_filter}
                    ORDER BY rc.published_at DESC
                    LIMIT :limit
                """),
                bind,
            )
            rows = result.fetchall()
            return {
                "result": [
                    {
                        "id": str(r[0]),
                        "title": r[1],
                        "content_text": (r[2] or "")[:3000],
                        "url": r[3],
                        "published_at": str(r[4]),
                        "filing_type": r[5],
                        "source": r[6],
                    }
                    for r in rows
                ],
                "count": len(rows),
            }
    except Exception as e:
        return {"result": [], "error": f"{type(e).__name__}: {e}"}


async def execute_search_transcript_chunks(params: dict, ctx: "ToolContext") -> dict:
    """Search analyst podcast/video transcript chunks by symbol."""
    since_dt = parse_dt(params.get("since"))
    if since_dt is None:
        return {"result": [], "error": "since: invalid or missing datetime"}

    symbol = params.get("symbol")
    if not symbol:
        return {"result": [], "error": "symbol is required"}
    limit = min(int(params.get("limit", 10)), 25)

    try:
        async with ctx.db() as db:
            result = await db.execute(
                text("""
                    SELECT rc.id, rc.title, rc.content_text, rc.author,
                           rc.published_at, ds.name AS source_name
                    FROM raw_content rc
                    JOIN data_sources ds ON ds.id = rc.source_id
                    WHERE ds.type IN ('youtube_live', 'youtube_vod', 'podcast')
                      AND rc.published_at >= :since
                      AND rc.content_text ILIKE :symbol_pat
                    ORDER BY rc.published_at DESC
                    LIMIT :limit
                """),
                {"symbol_pat": f"%{symbol}%", "since": since_dt, "limit": limit},
            )
            rows = result.fetchall()
            return {
                "result": [
                    {
                        "id": str(r[0]),
                        "title": r[1],
                        "chunk": (r[2] or "")[:1500],
                        "author": r[3],
                        "published_at": str(r[4]),
                        "source": r[5],
                    }
                    for r in rows
                ],
                "count": len(rows),
            }
    except Exception as e:
        return {"result": [], "error": f"{type(e).__name__}: {e}"}


async def execute_get_analyst_track_record(params: dict, ctx: "ToolContext") -> dict:
    """Retrieve an analyst's historical credibility score and prediction accuracy."""
    try:
        async with ctx.db() as db:
            aid = await resolve_analyst_id(db, params.get("analyst_id"))
            if aid is None:
                return {"result": None, "error": "analyst not found"}

            result = await db.execute(
                text("""
                    SELECT name, organization, designation,
                           total_signals, signals_hit_target, signals_hit_sl,
                           hit_rate, avg_return_pct, avg_days_to_target,
                           best_sector, credibility_score
                    FROM analysts
                    WHERE id = :analyst_id
                """),
                {"analyst_id": aid},
            )
            row = result.fetchone()
            if not row:
                return {"result": None, "error": "analyst not found"}
            return {
                "result": {
                    "name": row[0],
                    "organization": row[1],
                    "designation": row[2],
                    "total_signals": row[3],
                    "signals_hit_target": row[4],
                    "signals_hit_sl": row[5],
                    "hit_rate": float(row[6] or 0),
                    "avg_return_pct": float(row[7] or 0),
                    "avg_days_to_target": float(row[8] or 0) if row[8] else None,
                    "best_sector": row[9],
                    "credibility_score": float(row[10] or 0.5),
                }
            }
    except Exception as e:
        return {"result": None, "error": f"{type(e).__name__}: {e}"}
