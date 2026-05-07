"""News-related tool executors: search_raw_content, get_filings, search_transcript_chunks,
get_analyst_track_record.

These replace the stub executors in registry.py when TOOLS_BACKEND=sql.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import text

if TYPE_CHECKING:
    from src.agents.tools.registry import ToolContext


async def execute_search_raw_content(params: dict, ctx: "ToolContext") -> dict:
    """Search raw_content for one instrument over a time window."""
    instrument_id = params["instrument_id"]
    since = params["since"]
    until = params.get("until")
    limit = min(int(params.get("limit", 25)), 50)
    min_credibility = float(params.get("min_credibility", 0.0))
    include_types = params.get("include_types") or []

    type_filter = ""
    bind: dict = {
        "instrument_id": str(instrument_id),
        "since": since,
        "limit": limit,
        "min_cred": min_credibility,
    }

    if until:
        date_filter = "AND rc.published_at < :until"
        bind["until"] = until
    else:
        date_filter = ""

    if include_types:
        type_filter = "AND rc.media_type = ANY(:include_types)"
        bind["include_types"] = include_types

    credibility_join = (
        "LEFT JOIN data_sources ds ON ds.id = rc.source_id"
        if min_credibility > 0
        else "LEFT JOIN data_sources ds ON ds.id = rc.source_id"
    )
    credibility_where = (
        "AND COALESCE((ds.extraction_schema->>'credibility_weight')::numeric, 0.5) >= :min_cred"
        if min_credibility > 0
        else ""
    )

    try:
        async with ctx.db() as db:
            result = await db.execute(
                text(f"""
                    SELECT rc.id, rc.title, rc.content_text, rc.url, rc.author,
                           rc.published_at, rc.media_type, rc.language,
                           COALESCE((ds.extraction_schema->>'credibility_weight')::numeric, 0.5) AS credibility_weight,
                           ds.name AS source_name
                    FROM raw_content rc
                    {credibility_join}
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
        return {"result": [], "error": str(e)}


async def execute_get_filings(params: dict, ctx: "ToolContext") -> dict:
    """Retrieve SEBI/BSE/NSE regulatory filings for one instrument."""
    instrument_id = params["instrument_id"]
    since = params["since"]
    filing_types = params.get("filing_types") or []

    type_filter = ""
    bind: dict = {"instrument_id": str(instrument_id), "since": since, "limit": 20}

    if filing_types:
        type_filter = "AND rc.media_type = ANY(:filing_types)"
        bind["filing_types"] = filing_types

    try:
        async with ctx.db() as db:
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
        return {"result": [], "error": str(e)}


async def execute_search_transcript_chunks(params: dict, ctx: "ToolContext") -> dict:
    """Search analyst podcast/video transcript chunks by symbol."""
    symbol = params["symbol"]
    since = params["since"]
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
                {"symbol_pat": f"%{symbol}%", "since": since, "limit": limit},
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
        return {"result": [], "error": str(e)}


async def execute_get_analyst_track_record(params: dict, ctx: "ToolContext") -> dict:
    """Retrieve an analyst's historical credibility score and prediction accuracy."""
    analyst_id = params["analyst_id"]

    try:
        async with ctx.db() as db:
            result = await db.execute(
                text("""
                    SELECT name, organization, designation,
                           total_signals, signals_hit_target, signals_hit_sl,
                           hit_rate, avg_return_pct, avg_days_to_target,
                           best_sector, credibility_score
                    FROM analysts
                    WHERE id = :analyst_id
                """),
                {"analyst_id": str(analyst_id)},
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
        return {"result": None, "error": str(e)}
