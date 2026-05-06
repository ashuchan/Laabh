"""Side-effect gateway — routes Telegram and GitHub calls through a single seam.

Live mode delegates to the real services; replay mode captures every call into
a buffer (NoOpGateway) without making any real network requests.

Usage (live path — default, no change required):
    from src.services.side_effect_gateway import get_gateway
    await get_gateway().send_telegram("hello")

Usage (replay path — set once at replay start):
    from src.services.side_effect_gateway import NoOpGateway, set_gateway
    gw = NoOpGateway()
    with set_gateway(gw):
        ...  # all sends captured, not dispatched
    captured = gw.record_capture()
"""
from __future__ import annotations

import contextlib
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class SideEffectGateway(Protocol):
    """Single seam through which all external side-effects flow."""

    async def send_telegram(self, msg: str, *, parse_mode: str = "Markdown") -> str:
        """Send a Telegram message. Returns a synthetic or real message ID.

        ``parse_mode`` defaults to legacy ``Markdown``. Callers that emit
        MarkdownV2-escaped text (FNO formatters) must pass ``"MarkdownV2"``;
        HTML-formatted text must pass ``"HTML"``.
        """
        ...

    async def file_github_issue(
        self,
        title: str,
        body: str,
        labels: list[str] | None = None,
    ) -> str:
        """File a GitHub issue. Returns the issue URL."""
        ...

    def record_capture(self) -> list[dict]:
        """Return all captured actions (NoOpGateway only; live returns [])."""
        ...


# ---------------------------------------------------------------------------
# Live gateway — delegates to existing services
# ---------------------------------------------------------------------------

class LiveGateway:
    """Delegates side-effects to the real Telegram/GitHub services."""

    def __init__(self) -> None:
        self._captures: list[dict] = []  # always empty for live

    async def send_telegram(self, msg: str, *, parse_mode: str = "Markdown") -> str:
        from src.services.notification_service import NotificationService
        svc = NotificationService()
        await svc.send_text(msg, parse_mode=parse_mode)
        return "live"

    async def file_github_issue(
        self,
        title: str,
        body: str,
        labels: list[str] | None = None,
    ) -> str:
        from src.fno.issue_filer import _create_issue  # type: ignore[attr-defined]
        import httpx
        async with httpx.AsyncClient(timeout=30) as client:
            url = await _create_issue(client, title, body)
        return url or ""

    def record_capture(self) -> list[dict]:
        return []


# ---------------------------------------------------------------------------
# No-op gateway — captures calls without dispatching
# ---------------------------------------------------------------------------

class NoOpGateway:
    """Captures all side-effect calls without any real I/O."""

    def __init__(self) -> None:
        self._log: list[dict] = []

    async def send_telegram(self, msg: str, *, parse_mode: str = "Markdown") -> str:
        entry = {"type": "telegram", "msg": msg, "parse_mode": parse_mode}
        self._log.append(entry)
        return f"noop-{len(self._log)}"

    async def file_github_issue(
        self,
        title: str,
        body: str,
        labels: list[str] | None = None,
    ) -> str:
        entry = {"type": "github_issue", "title": title, "body": body, "labels": labels or []}
        self._log.append(entry)
        return f"https://github.com/noop/issues/{len(self._log)}"

    def record_capture(self) -> list[dict]:
        return list(self._log)


# ---------------------------------------------------------------------------
# Context-var accessor
# ---------------------------------------------------------------------------

_GATEWAY_VAR: ContextVar[SideEffectGateway] = ContextVar(
    "side_effect_gateway",
    default=LiveGateway(),  # type: ignore[arg-type]
)


def get_gateway() -> SideEffectGateway:
    """Return the current context's gateway (default: LiveGateway)."""
    return _GATEWAY_VAR.get()


@contextlib.contextmanager
def set_gateway(gw: SideEffectGateway):
    """Context manager: override the gateway for the current async context."""
    token = _GATEWAY_VAR.set(gw)
    try:
        yield gw
    finally:
        _GATEWAY_VAR.reset(token)
