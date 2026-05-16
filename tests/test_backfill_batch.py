"""Tests for the backfill batch-label → UUID helper.

The contract is stability: the same label always hashes to the same UUID,
so the prereqs, LLM-replay, calibration, and dashboard scripts can scope
to the same batch without copy-pasting literal UUIDs.
"""
from __future__ import annotations

import uuid

import pytest

from src.fno.backfill_batch import batch_label_to_uuid


def test_same_label_produces_same_uuid() -> None:
    a = batch_label_to_uuid("MoneyRatnam_backfill_v1")
    b = batch_label_to_uuid("MoneyRatnam_backfill_v1")
    assert a == b


def test_label_yields_uuid_instance() -> None:
    u = batch_label_to_uuid("MoneyRatnam_backfill_v1")
    assert isinstance(u, uuid.UUID)
    assert u.version == 5   # NAMESPACE_DNS hashing


def test_different_labels_produce_different_uuids() -> None:
    a = batch_label_to_uuid("MoneyRatnam_backfill_v1")
    b = batch_label_to_uuid("MoneyRatnam_backfill_v2")
    assert a != b


def test_whitespace_does_not_change_label_identity() -> None:
    # Strip is documented in the helper — operators with stray whitespace
    # don't get a fresh UUID.
    a = batch_label_to_uuid("MoneyRatnam_backfill_v1")
    b = batch_label_to_uuid("  MoneyRatnam_backfill_v1  ")
    assert a == b


def test_empty_label_raises() -> None:
    with pytest.raises(ValueError):
        batch_label_to_uuid("")
    with pytest.raises(ValueError):
        batch_label_to_uuid("   ")


def test_known_uuid_for_default_label() -> None:
    # Regression guard: if someone changes the namespace or hashing
    # function, the same label would emit a different UUID and silently
    # split prod-data. Pinning the v1 mapping makes that surface here.
    expected = uuid.UUID("afebf518-d196-5eea-9fc1-9ad023975b7d")
    assert batch_label_to_uuid("MoneyRatnam_backfill_v1") == expected
