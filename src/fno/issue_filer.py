"""Daily review-loop job — aggregates chain_collection_issues and files GitHub issues.

Runs at 18:30 IST daily.

For each unresolved (source, instrument, date) group:
  1. Compute idempotency key: chain-issue-{source}-{symbol}-{YYYYMMDD}
  2. Search existing open GitHub issues for that key in the body.
  3. If found and last comment < 6 hours old → skip; else add a comment.
  4. If not found → create a new issue with labels from config.
  5. Persist github_issue_url back to the originating rows.

Sends a Telegram summary at the end regardless of GitHub success.
If GITHUB_TOKEN is missing, logs an error, skips GitHub, still sends Telegram.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx
from loguru import logger
from sqlalchemy import select, update

from src.config import get_settings
from src.db import session_scope
from src.models.fno_chain_issue import ChainCollectionIssue
from src.models.instrument import Instrument
from src.services.side_effect_gateway import get_gateway

_settings = get_settings()

_GITHUB_API = "https://api.github.com"


# ---------------------------------------------------------------------------
# GitHub helpers
# ---------------------------------------------------------------------------

def _gh_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_settings.github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def _search_issues(client: httpx.AsyncClient, key: str) -> list[dict]:
    """Search open issues that contain the dedup key in their body."""
    repo = _settings.github_repo
    q = quote(f'"{key}" repo:{repo} is:issue is:open')
    try:
        resp = await client.get(
            f"{_GITHUB_API}/search/issues",
            headers=_gh_headers(),
            params={"q": q, "per_page": 5},
        )
        resp.raise_for_status()
        return resp.json().get("items", [])
    except Exception as exc:
        logger.error(f"issue_filer: GitHub search failed: {exc}")
        return []


async def _create_issue(
    client: httpx.AsyncClient,
    title: str,
    body: str,
) -> str | None:
    """Create a GitHub issue and return its HTML URL."""
    repo = _settings.github_repo
    labels = [lbl.strip() for lbl in _settings.github_issue_labels.split(",") if lbl.strip()]
    try:
        resp = await client.post(
            f"{_GITHUB_API}/repos/{repo}/issues",
            headers=_gh_headers(),
            json={"title": title, "body": body, "labels": labels},
        )
        resp.raise_for_status()
        return resp.json().get("html_url")
    except Exception as exc:
        logger.error(f"issue_filer: GitHub issue creation failed: {exc}")
        return None


async def _add_comment(
    client: httpx.AsyncClient,
    issue_number: int,
    body: str,
) -> None:
    repo = _settings.github_repo
    try:
        resp = await client.post(
            f"{_GITHUB_API}/repos/{repo}/issues/{issue_number}/comments",
            headers=_gh_headers(),
            json={"body": body},
        )
        resp.raise_for_status()
    except Exception as exc:
        logger.error(f"issue_filer: GitHub comment failed: {exc}")


async def _last_comment_age(client: httpx.AsyncClient, issue_number: int) -> float:
    """Return age of the latest comment in seconds (inf if no comments)."""
    repo = _settings.github_repo
    try:
        resp = await client.get(
            f"{_GITHUB_API}/repos/{repo}/issues/{issue_number}/comments",
            headers=_gh_headers(),
            params={"per_page": 1, "page": 1, "sort": "created", "direction": "desc"},
        )
        resp.raise_for_status()
        comments = resp.json()
        if not comments:
            return float("inf")
        last = comments[0].get("created_at", "")
        dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
        return (datetime.now(tz=timezone.utc) - dt).total_seconds()
    except Exception:
        return float("inf")


# ---------------------------------------------------------------------------
# Main run
# ---------------------------------------------------------------------------

async def run() -> None:
    """Aggregate today's unresolved chain issues and file/update GitHub issues."""
    now = datetime.now(tz=timezone.utc)
    cutoff = now - timedelta(hours=24)

    # Load unresolved issues from the last 24 hours
    async with session_scope() as session:
        result = await session.execute(
            select(ChainCollectionIssue, Instrument.symbol)
            .join(
                Instrument,
                ChainCollectionIssue.instrument_id == Instrument.id,
                isouter=True,
            )
            .where(
                ChainCollectionIssue.detected_at >= cutoff,
                ChainCollectionIssue.resolved_at.is_(None),
            )
            .order_by(ChainCollectionIssue.detected_at.desc())
        )
        raw_rows = result.all()

    if not raw_rows:
        logger.info("issue_filer: no unresolved chain issues in the last 24h")
        await get_gateway().send_telegram("✅ <b>Chain Collector Review</b>\nNo unresolved issues in the last 24h.")
        return

    # Group by (source, symbol, date)
    groups: dict[str, list[tuple[ChainCollectionIssue, str | None]]] = {}
    for issue, symbol in raw_rows:
        day = issue.detected_at.strftime("%Y%m%d") if issue.detected_at else "unknown"
        key = f"chain-issue-{issue.source}-{symbol or 'unknown'}-{day}"
        groups.setdefault(key, []).append((issue, symbol))

    new_issues = 0
    updated_issues = 0
    github_enabled = bool(_settings.github_token)

    async with httpx.AsyncClient(timeout=20.0) as gh_client:
        for dedup_key, group_rows in groups.items():
            sample_issue, symbol = group_rows[0]
            date_str = (
                sample_issue.detected_at.strftime("%Y-%m-%d")
                if sample_issue.detected_at
                else "unknown"
            )
            title = (
                f"[chain-collector] {sample_issue.source} "
                f"{sample_issue.issue_type} on {symbol} ({date_str})"
            )
            most_recent_error = group_rows[0][0].error_message
            most_recent_raw = group_rows[0][0].raw_response or ""

            body = (
                f"**Affected source:** {sample_issue.source}\n"
                f"**Underlying:** {symbol}\n"
                f"**Failure count:** {len(group_rows)}\n"
                f"**Most recent error:** {most_recent_error}\n\n"
                f"<details><summary>Raw response (truncated)</summary>\n\n"
                f"```\n{most_recent_raw[:4096]}\n```\n</details>\n\n"
                f"---\n<!-- dedup-key: {dedup_key} -->"
            )

            issue_url: str | None = None

            if github_enabled:
                existing = await _search_issues(gh_client, dedup_key)
                if existing:
                    issue_num = existing[0]["number"]
                    issue_url = existing[0]["html_url"]
                    age_sec = await _last_comment_age(gh_client, issue_num)
                    if age_sec < 6 * 3600:
                        logger.info(
                            f"issue_filer: recent comment on #{issue_num}, skipping"
                        )
                    else:
                        await _add_comment(
                            gh_client,
                            issue_num,
                            f"**Update:** {len(group_rows)} failures still unresolved.\n"
                            f"Most recent error: {most_recent_error}",
                        )
                        updated_issues += 1
                else:
                    issue_url = await _create_issue(gh_client, title, body)
                    if issue_url:
                        new_issues += 1
            else:
                logger.warning(
                    f"issue_filer: GITHUB_TOKEN missing — skipping issue for {dedup_key}"
                )

            # Persist github_issue_url back to DB rows
            if issue_url:
                issue_ids = [row[0].id for row in group_rows]
                async with session_scope() as session:
                    await session.execute(
                        update(ChainCollectionIssue)
                        .where(ChainCollectionIssue.id.in_(issue_ids))
                        .values(github_issue_url=issue_url)
                    )

    summary = (
        f"📋 <b>Chain Collector Review ({now.strftime('%Y-%m-%d')})</b>\n"
        f"Unresolved issue groups: {len(groups)}\n"
        f"GitHub issues created: {new_issues}\n"
        f"GitHub issues updated: {updated_issues}"
    )
    if not github_enabled:
        summary += "\n⚠️ GITHUB_TOKEN not set — no issues filed"

    await get_gateway().send_telegram(summary)
    logger.info(f"issue_filer: done — new={new_issues} updated={updated_issues}")
