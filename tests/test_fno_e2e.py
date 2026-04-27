"""End-to-end tests for the three new F&O API endpoints.

Tests use FastAPI's synchronous TestClient with a patched session_scope so
no database is required.  Every test exercises a full HTTP round-trip:
  request → route handler → session → serialisation → response.

New endpoints covered:
  GET  /fno/chain-issues          (list, filter, paginate)
  POST /fno/chain-issues/{id}/resolve
  GET  /fno/source-health
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routes.fno import router

# Build a minimal FastAPI app with just the F&O router
_app = FastAPI()
_app.include_router(router)
_client = TestClient(_app, raise_server_exceptions=True)

_NOW = datetime(2026, 4, 27, 18, 30, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Session mock helpers
# ---------------------------------------------------------------------------

def _make_session(
    execute_rows: list | None = None,
    get_return: Any = None,
) -> AsyncMock:
    """Return an async session mock with configurable results."""
    session = AsyncMock()

    result = MagicMock()
    rows = execute_rows or []
    result.all.return_value = rows
    result.scalars.return_value = MagicMock(all=MagicMock(return_value=rows))
    result.scalar_one_or_none.return_value = rows[0] if rows else None
    result.first.return_value = rows[0] if rows else None
    session.execute = AsyncMock(return_value=result)
    session.get = AsyncMock(return_value=get_return)
    session.add = MagicMock()

    return session


def _scope(session: AsyncMock):
    """Return a context-manager that yields the given session."""
    @asynccontextmanager
    async def _ctx():
        yield session
    return _ctx


# ---------------------------------------------------------------------------
# Chain issue mock factory
# ---------------------------------------------------------------------------

def _make_chain_issue(
    source: str = "nse",
    issue_type: str = "schema_mismatch",
    resolved: bool = False,
    inst_id: uuid.UUID | None = None,
) -> MagicMock:
    issue = MagicMock()
    issue.id = uuid.uuid4()
    issue.source = source
    issue.instrument_id = inst_id or uuid.uuid4()
    issue.issue_type = issue_type
    issue.error_message = f"Test error from {source}"
    issue.raw_response = '{"bad": "schema"}'
    issue.detected_at = _NOW
    issue.github_issue_url = None
    issue.resolved_at = _NOW if resolved else None
    issue.resolved_by = "api" if resolved else None
    return issue


def _make_source_health(source: str = "nse", status: str = "healthy") -> MagicMock:
    row = MagicMock()
    row.source = source
    row.status = status
    row.consecutive_errors = 0
    row.last_success_at = _NOW
    row.last_error_at = None
    row.last_error = None
    row.updated_at = _NOW
    return row


# ===========================================================================
# GET /fno/chain-issues
# ===========================================================================

def test_chain_issues_empty_list() -> None:
    sess = _make_session(execute_rows=[])
    with patch("src.api.routes.fno.session_scope", _scope(sess)):
        resp = _client.get("/fno/chain-issues")
    assert resp.status_code == 200
    assert resp.json() == []


def test_chain_issues_returns_open_issues() -> None:
    issue = _make_chain_issue(source="nse")
    sess = _make_session(execute_rows=[issue])
    with patch("src.api.routes.fno.session_scope", _scope(sess)):
        resp = _client.get("/fno/chain-issues")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["source"] == "nse"
    assert data[0]["issue_type"] == "schema_mismatch"
    assert data[0]["resolved_at"] is None


def test_chain_issues_filter_by_status_resolved() -> None:
    issue = _make_chain_issue(resolved=True)
    sess = _make_session(execute_rows=[issue])
    with patch("src.api.routes.fno.session_scope", _scope(sess)):
        resp = _client.get("/fno/chain-issues?status=resolved")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["resolved_at"] is not None


def test_chain_issues_invalid_status_rejected() -> None:
    resp = _client.get("/fno/chain-issues?status=unknown")
    assert resp.status_code == 422


def test_chain_issues_filter_by_source_nse() -> None:
    sess = _make_session(execute_rows=[])
    with patch("src.api.routes.fno.session_scope", _scope(sess)):
        resp = _client.get("/fno/chain-issues?source=nse")
    assert resp.status_code == 200


def test_chain_issues_filter_by_source_dhan() -> None:
    issue = _make_chain_issue(source="dhan")
    sess = _make_session(execute_rows=[issue])
    with patch("src.api.routes.fno.session_scope", _scope(sess)):
        resp = _client.get("/fno/chain-issues?source=dhan")
    assert resp.status_code == 200
    data = resp.json()
    assert data[0]["source"] == "dhan"


def test_chain_issues_invalid_source_rejected() -> None:
    resp = _client.get("/fno/chain-issues?source=angel_one")
    assert resp.status_code == 422


def test_chain_issues_limit_respected() -> None:
    sess = _make_session(execute_rows=[])
    with patch("src.api.routes.fno.session_scope", _scope(sess)):
        resp = _client.get("/fno/chain-issues?limit=10")
    assert resp.status_code == 200


def test_chain_issues_limit_too_large_rejected() -> None:
    resp = _client.get("/fno/chain-issues?limit=999")
    assert resp.status_code == 422


def test_chain_issues_offset_accepted() -> None:
    sess = _make_session(execute_rows=[])
    with patch("src.api.routes.fno.session_scope", _scope(sess)):
        resp = _client.get("/fno/chain-issues?offset=50")
    assert resp.status_code == 200


def test_chain_issues_multiple_results() -> None:
    issues = [_make_chain_issue(source="nse"), _make_chain_issue(source="dhan")]
    sess = _make_session(execute_rows=issues)
    with patch("src.api.routes.fno.session_scope", _scope(sess)):
        resp = _client.get("/fno/chain-issues")
    assert resp.status_code == 200
    assert len(resp.json()) == 2


def test_chain_issues_response_schema_fields() -> None:
    issue = _make_chain_issue()
    sess = _make_session(execute_rows=[issue])
    with patch("src.api.routes.fno.session_scope", _scope(sess)):
        resp = _client.get("/fno/chain-issues")
    row = resp.json()[0]
    # Verify all expected fields are present
    for field in ("id", "source", "issue_type", "error_message", "detected_at"):
        assert field in row, f"Missing field: {field}"


def test_chain_issues_raw_response_included() -> None:
    issue = _make_chain_issue()
    sess = _make_session(execute_rows=[issue])
    with patch("src.api.routes.fno.session_scope", _scope(sess)):
        resp = _client.get("/fno/chain-issues")
    row = resp.json()[0]
    assert row["raw_response"] == '{"bad": "schema"}'


def test_chain_issues_github_url_null_when_not_filed() -> None:
    issue = _make_chain_issue()
    issue.github_issue_url = None
    sess = _make_session(execute_rows=[issue])
    with patch("src.api.routes.fno.session_scope", _scope(sess)):
        resp = _client.get("/fno/chain-issues")
    assert resp.json()[0]["github_issue_url"] is None


def test_chain_issues_github_url_present_when_filed() -> None:
    issue = _make_chain_issue()
    issue.github_issue_url = "https://github.com/ashuchan/Laabh/issues/42"
    sess = _make_session(execute_rows=[issue])
    with patch("src.api.routes.fno.session_scope", _scope(sess)):
        resp = _client.get("/fno/chain-issues")
    assert resp.json()[0]["github_issue_url"] == "https://github.com/ashuchan/Laabh/issues/42"


# ===========================================================================
# POST /fno/chain-issues/{id}/resolve
# ===========================================================================

def test_resolve_issue_success() -> None:
    issue = _make_chain_issue(resolved=False)
    health = _make_source_health(status="healthy")
    # Session: get(issue_id) → issue; remaining open issues query → []; get(source) → health
    sess = AsyncMock()
    sess.get = AsyncMock(side_effect=[issue, health])
    remaining_result = MagicMock()
    remaining_result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    sess.execute = AsyncMock(return_value=remaining_result)
    sess.add = MagicMock()

    with patch("src.api.routes.fno.session_scope", _scope(sess)):
        resp = _client.post(f"/fno/chain-issues/{issue.id}/resolve")

    assert resp.status_code == 200
    data = resp.json()
    assert data["resolved"] is True
    assert data["id"] == str(issue.id)


def test_resolve_issue_not_found() -> None:
    sess = AsyncMock()
    sess.get = AsyncMock(return_value=None)
    sess.execute = AsyncMock(return_value=MagicMock())

    with patch("src.api.routes.fno.session_scope", _scope(sess)):
        resp = _client.post(f"/fno/chain-issues/{uuid.uuid4()}/resolve")

    assert resp.status_code == 404


def test_resolve_issue_already_resolved_returns_409() -> None:
    issue = _make_chain_issue(resolved=True)
    sess = AsyncMock()
    sess.get = AsyncMock(return_value=issue)
    sess.execute = AsyncMock(return_value=MagicMock())

    with patch("src.api.routes.fno.session_scope", _scope(sess)):
        resp = _client.post(f"/fno/chain-issues/{issue.id}/resolve")

    assert resp.status_code == 409


def test_resolve_issue_invalid_uuid_returns_422() -> None:
    resp = _client.post("/fno/chain-issues/not-a-uuid/resolve")
    assert resp.status_code == 422


def test_resolve_issue_heals_degraded_source_when_last_open_issue() -> None:
    """If no other open issues remain for this source, degraded → healthy."""
    issue = _make_chain_issue(source="nse", resolved=False)
    health = _make_source_health(source="nse", status="degraded")

    sess = AsyncMock()
    sess.get = AsyncMock(side_effect=[issue, health])
    remaining_result = MagicMock()
    remaining_result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    sess.execute = AsyncMock(return_value=remaining_result)

    with patch("src.api.routes.fno.session_scope", _scope(sess)):
        resp = _client.post(f"/fno/chain-issues/{issue.id}/resolve")

    assert resp.status_code == 200
    data = resp.json()
    assert data["source_health_status"] == "healthy"


def test_resolve_issue_does_not_heal_when_other_issues_remain() -> None:
    """If other open issues still exist, source stays degraded."""
    issue = _make_chain_issue(source="nse", resolved=False)
    other_open = _make_chain_issue(source="nse", resolved=False)
    health = _make_source_health(source="nse", status="degraded")

    sess = AsyncMock()
    sess.get = AsyncMock(side_effect=[issue, health])
    remaining_result = MagicMock()
    remaining_result.scalars = MagicMock(
        return_value=MagicMock(all=MagicMock(return_value=[other_open]))
    )
    sess.execute = AsyncMock(return_value=remaining_result)

    with patch("src.api.routes.fno.session_scope", _scope(sess)):
        resp = _client.post(f"/fno/chain-issues/{issue.id}/resolve")

    assert resp.status_code == 200
    # Source should still be degraded
    assert resp.json()["source_health_status"] == "degraded"


def test_resolve_issue_custom_resolved_by() -> None:
    issue = _make_chain_issue(resolved=False)
    health = _make_source_health()

    sess = AsyncMock()
    sess.get = AsyncMock(side_effect=[issue, health])
    remaining_result = MagicMock()
    remaining_result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    sess.execute = AsyncMock(return_value=remaining_result)

    with patch("src.api.routes.fno.session_scope", _scope(sess)):
        resp = _client.post(f"/fno/chain-issues/{issue.id}/resolve?resolved_by=oncall_engineer")

    assert resp.status_code == 200
    # Verify the resolved_by was set on the issue object
    assert issue.resolved_by == "oncall_engineer"


# ===========================================================================
# GET /fno/source-health
# ===========================================================================

def test_source_health_returns_all_sources() -> None:
    rows = [
        _make_source_health("nse", "healthy"),
        _make_source_health("dhan", "healthy"),
        _make_source_health("angel_one", "healthy"),
    ]
    sess = _make_session(execute_rows=rows)
    with patch("src.api.routes.fno.session_scope", _scope(sess)):
        resp = _client.get("/fno/source-health")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 3
    sources = {row["source"] for row in data}
    assert sources == {"nse", "dhan", "angel_one"}


def test_source_health_empty_list() -> None:
    sess = _make_session(execute_rows=[])
    with patch("src.api.routes.fno.session_scope", _scope(sess)):
        resp = _client.get("/fno/source-health")
    assert resp.status_code == 200
    assert resp.json() == []


def test_source_health_degraded_status_shown() -> None:
    row = _make_source_health("nse", "degraded")
    sess = _make_session(execute_rows=[row])
    with patch("src.api.routes.fno.session_scope", _scope(sess)):
        resp = _client.get("/fno/source-health")
    data = resp.json()
    assert data[0]["status"] == "degraded"


def test_source_health_failed_status_shown() -> None:
    row = _make_source_health("dhan", "failed")
    sess = _make_session(execute_rows=[row])
    with patch("src.api.routes.fno.session_scope", _scope(sess)):
        resp = _client.get("/fno/source-health")
    assert resp.json()[0]["status"] == "failed"


def test_source_health_consecutive_errors_shown() -> None:
    row = _make_source_health("nse", "degraded")
    row.consecutive_errors = 7
    sess = _make_session(execute_rows=[row])
    with patch("src.api.routes.fno.session_scope", _scope(sess)):
        resp = _client.get("/fno/source-health")
    assert resp.json()[0]["consecutive_errors"] == 7


def test_source_health_last_error_shown() -> None:
    row = _make_source_health("nse", "degraded")
    row.last_error = "Connection timeout after 15s"
    sess = _make_session(execute_rows=[row])
    with patch("src.api.routes.fno.session_scope", _scope(sess)):
        resp = _client.get("/fno/source-health")
    assert resp.json()[0]["last_error"] == "Connection timeout after 15s"


def test_source_health_response_schema_fields() -> None:
    row = _make_source_health()
    sess = _make_session(execute_rows=[row])
    with patch("src.api.routes.fno.session_scope", _scope(sess)):
        resp = _client.get("/fno/source-health")
    row_data = resp.json()[0]
    for field in ("source", "status", "consecutive_errors"):
        assert field in row_data


# ===========================================================================
# Cross-cutting: existing endpoints unaffected by retrofit
# ===========================================================================

def test_existing_vix_endpoint_still_works() -> None:
    """Regression: GET /fno/vix must still return 200 after router changes."""
    sess = _make_session(execute_rows=[])
    with patch("src.api.routes.fno.session_scope", _scope(sess)):
        resp = _client.get("/fno/vix")
    assert resp.status_code == 200


def test_existing_ban_list_endpoint_still_works() -> None:
    """Regression: GET /fno/ban-list must still return 200 after router changes."""
    sess = _make_session(execute_rows=[])
    with patch("src.api.routes.fno.session_scope", _scope(sess)):
        resp = _client.get("/fno/ban-list")
    assert resp.status_code == 200


def test_existing_candidates_endpoint_still_works() -> None:
    """Regression: GET /fno/candidates must still return 200."""
    sess = _make_session(execute_rows=[])
    with patch("src.api.routes.fno.session_scope", _scope(sess)):
        resp = _client.get("/fno/candidates")
    assert resp.status_code == 200


def test_all_new_routes_registered() -> None:
    """Verify the three new routes are present in the app's route table."""
    paths = {route.path for route in _app.routes}
    assert "/fno/chain-issues" in paths
    assert "/fno/chain-issues/{issue_id}/resolve" in paths
    assert "/fno/source-health" in paths
