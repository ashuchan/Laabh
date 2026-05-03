"""Thin context-manager wrappers for replay-mode side-effect isolation."""
from __future__ import annotations

import contextlib
import uuid
from contextvars import ContextVar

from src.services.side_effect_gateway import NoOpGateway, set_gateway

# Context variable for the current replay run_id; None in live mode
_DRYRUN_RUN_ID_VAR: ContextVar[uuid.UUID | None] = ContextVar(
    "dryrun_run_id", default=None
)


def get_dryrun_run_id() -> uuid.UUID | None:
    """Return the current replay run_id, or None when running live."""
    return _DRYRUN_RUN_ID_VAR.get()


@contextlib.contextmanager
def set_dryrun_run_id(run_id: uuid.UUID):
    """Context manager: stamp run_id on all DB writes in the current async context."""
    token = _DRYRUN_RUN_ID_VAR.set(run_id)
    try:
        yield run_id
    finally:
        _DRYRUN_RUN_ID_VAR.reset(token)
