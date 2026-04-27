"""Tests for issue_filer.py — deduplication, GitHub issue creation, Telegram summary."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_NOW = datetime(2026, 4, 27, 18, 30, tzinfo=timezone.utc)


def _make_issue(
    source: str = "nse",
    symbol: str = "NIFTY",
    issue_type: str = "schema_mismatch",
    inst_id: uuid.UUID | None = None,
) -> tuple:
    """Return (ChainCollectionIssue mock, symbol str)."""
    issue = MagicMock()
    issue.id = uuid.uuid4()
    issue.source = source
    issue.instrument_id = inst_id or uuid.uuid4()
    issue.issue_type = issue_type
    issue.error_message = f"Test error from {source}"
    issue.raw_response = '{"bad": "schema"}'
    issue.detected_at = _NOW
    issue.resolved_at = None
    issue.github_issue_url = None
    return issue, symbol


# ---------------------------------------------------------------------------
# Deduplication — same (source, symbol, date) creates exactly one issue
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_same_group_creates_one_github_issue():
    """Two schema-mismatch rows for the same (source, underlying, date) → one issue."""
    issue1, sym = _make_issue()
    issue2, _ = _make_issue()
    issue2.detected_at = _NOW

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(
        return_value=MagicMock(
            all=MagicMock(return_value=[(issue1, sym), (issue2, sym)])
        )
    )
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_session)
    ctx.__aexit__ = AsyncMock(return_value=False)

    created_issues: list[dict] = []

    async def fake_create(client, title, body):
        created_issues.append({"title": title, "body": body})
        return "https://github.com/ashuchan/Laabh/issues/1"

    with (
        patch("src.fno.issue_filer.session_scope", return_value=ctx),
        patch("src.fno.issue_filer._search_issues", new_callable=AsyncMock, return_value=[]),
        patch("src.fno.issue_filer._create_issue", side_effect=fake_create),
        patch("src.fno.issue_filer._send_telegram", new_callable=AsyncMock),
        patch("src.fno.issue_filer._settings") as ms,
    ):
        ms.github_token = "tok"
        ms.github_repo = "ashuchan/Laabh"
        ms.github_issue_labels = "bug,chain-collector"
        ms.telegram_bot_token = ""
        ms.telegram_chat_id = ""

        from src.fno.issue_filer import run
        await run()

    assert len(created_issues) == 1


@pytest.mark.asyncio
async def test_rerun_with_same_data_does_not_create_second_issue():
    """If an existing open issue already has the dedup key, no new issue is created."""
    issue1, sym = _make_issue()

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(
        return_value=MagicMock(
            all=MagicMock(return_value=[(issue1, sym)])
        )
    )
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_session)
    ctx.__aexit__ = AsyncMock(return_value=False)

    existing_issue = {
        "number": 42,
        "html_url": "https://github.com/ashuchan/Laabh/issues/42",
    }

    created_issues: list = []

    with (
        patch("src.fno.issue_filer.session_scope", return_value=ctx),
        patch(
            "src.fno.issue_filer._search_issues",
            new_callable=AsyncMock,
            return_value=[existing_issue],
        ),
        patch(
            "src.fno.issue_filer._last_comment_age",
            new_callable=AsyncMock,
            return_value=1.0,  # < 6 hours → skip comment
        ),
        patch(
            "src.fno.issue_filer._create_issue",
            side_effect=lambda *a, **k: created_issues.append(1),
        ),
        patch("src.fno.issue_filer._send_telegram", new_callable=AsyncMock),
        patch("src.fno.issue_filer._settings") as ms,
    ):
        ms.github_token = "tok"
        ms.github_repo = "ashuchan/Laabh"
        ms.github_issue_labels = "bug"
        ms.telegram_bot_token = ""
        ms.telegram_chat_id = ""

        from src.fno.issue_filer import run
        await run()

    assert len(created_issues) == 0


@pytest.mark.asyncio
async def test_different_underlying_creates_second_issue():
    """A schema mismatch for a different underlying creates a separate issue."""
    issue1, sym1 = _make_issue(symbol="NIFTY")
    issue2, sym2 = _make_issue(symbol="RELIANCE")

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(
        return_value=MagicMock(
            all=MagicMock(return_value=[(issue1, sym1), (issue2, sym2)])
        )
    )
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_session)
    ctx.__aexit__ = AsyncMock(return_value=False)

    created_issues: list = []

    async def fake_create(client, title, body):
        created_issues.append(title)
        return "https://github.com/ashuchan/Laabh/issues/99"

    with (
        patch("src.fno.issue_filer.session_scope", return_value=ctx),
        patch("src.fno.issue_filer._search_issues", new_callable=AsyncMock, return_value=[]),
        patch("src.fno.issue_filer._create_issue", side_effect=fake_create),
        patch("src.fno.issue_filer._send_telegram", new_callable=AsyncMock),
        patch("src.fno.issue_filer._settings") as ms,
    ):
        ms.github_token = "tok"
        ms.github_repo = "ashuchan/Laabh"
        ms.github_issue_labels = "bug"
        ms.telegram_bot_token = ""
        ms.telegram_chat_id = ""

        from src.fno.issue_filer import run
        await run()

    assert len(created_issues) == 2


# ---------------------------------------------------------------------------
# Missing GITHUB_TOKEN → Telegram summary still fires
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_missing_github_token_still_sends_telegram():
    issue1, sym = _make_issue()

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(
        return_value=MagicMock(
            all=MagicMock(return_value=[(issue1, sym)])
        )
    )
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_session)
    ctx.__aexit__ = AsyncMock(return_value=False)

    telegram_calls: list[str] = []

    async def fake_telegram(msg: str) -> None:
        telegram_calls.append(msg)

    with (
        patch("src.fno.issue_filer.session_scope", return_value=ctx),
        patch("src.fno.issue_filer._send_telegram", side_effect=fake_telegram),
        patch("src.fno.issue_filer._settings") as ms,
    ):
        ms.github_token = ""  # missing token
        ms.github_repo = "ashuchan/Laabh"
        ms.github_issue_labels = "bug"
        ms.telegram_bot_token = "tgbot"
        ms.telegram_chat_id = "1234"

        from src.fno.issue_filer import run
        await run()

    # Telegram must still be called even with no GitHub token
    assert len(telegram_calls) >= 1
    assert any("GITHUB_TOKEN" in msg for msg in telegram_calls)


# ---------------------------------------------------------------------------
# No issues → clean Telegram message, no GitHub calls
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_issues_sends_clean_telegram():
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(
        return_value=MagicMock(all=MagicMock(return_value=[]))
    )
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_session)
    ctx.__aexit__ = AsyncMock(return_value=False)

    telegram_msgs: list[str] = []

    async def fake_telegram(msg: str) -> None:
        telegram_msgs.append(msg)

    with (
        patch("src.fno.issue_filer.session_scope", return_value=ctx),
        patch("src.fno.issue_filer._send_telegram", side_effect=fake_telegram),
        patch("src.fno.issue_filer._settings") as ms,
    ):
        ms.github_token = "tok"
        ms.github_repo = "ashuchan/Laabh"
        ms.github_issue_labels = "bug"
        ms.telegram_bot_token = "tgbot"
        ms.telegram_chat_id = "1234"

        from src.fno.issue_filer import run
        await run()

    assert len(telegram_msgs) == 1
    assert "No unresolved" in telegram_msgs[0]
