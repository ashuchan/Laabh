"""Shared helpers for the bootstrap-calibration backfill batch.

Plan reference: docs/llm_feature_generator/backfill_plan.md §3.2.

Every script in the backfill pipeline needs to agree on:
  * The deterministic UUID derived from a human-readable batch label,
    so multiple scripts (prereqs / LLM replay / calibration / dashboard)
    can scope their reads/writes to the same batch without copy-pasting
    a literal UUID.
  * The default holdout-tail window.

Hashing the label via ``uuid.uuid5(NAMESPACE_DNS, label)`` is stable
across machines, across Python versions, and across script invocations.
"""
from __future__ import annotations

import uuid

# Namespace under which we hash batch labels. Use NAMESPACE_DNS so the
# value is portable / well-defined; don't invent a custom namespace.
_NAMESPACE = uuid.NAMESPACE_DNS

# Plan default — 15 trading days reserved at the tail of the backfill
# window for true out-of-sample holdout scoring (§3.2).
DEFAULT_HOLDOUT_TAIL_DAYS = 15


def batch_label_to_uuid(label: str) -> uuid.UUID:
    """Deterministic mapping from a human-readable batch label to a UUID.

    Example: ``batch_label_to_uuid("MoneyRatnam_backfill_v1")`` always
    returns the same UUID, on any host, on any Python.
    """
    # Reject empty AND whitespace-only — otherwise both would map to
    # the same constant UUID (uuid5 of the empty string), silently
    # colliding distinct intent.
    stripped = (label or "").strip()
    if not stripped:
        raise ValueError("batch label must be a non-empty string")
    return uuid.uuid5(_NAMESPACE, stripped)
