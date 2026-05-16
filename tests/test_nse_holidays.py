"""Tests for the NSE holidays loader used by the backfill scripts."""
from __future__ import annotations

import json
import os
from datetime import date

import pytest

from src.fno.nse_holidays import load_nse_holidays


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Each test starts with no env override pointing at the loader."""
    monkeypatch.delenv("LAABH_NSE_HOLIDAYS_FILE", raising=False)


def test_missing_file_returns_empty_set(tmp_path, monkeypatch) -> None:
    # Point the env var at a non-existent path so we don't accidentally
    # pick up the project's example file.
    monkeypatch.setenv("LAABH_NSE_HOLIDAYS_FILE", str(tmp_path / "does-not-exist.json"))
    assert load_nse_holidays() == frozenset()


def test_parses_valid_year_keys(tmp_path, monkeypatch) -> None:
    payload = {
        "2025": ["2025-02-26", "2025-08-15"],
        "2026": ["2026-01-26"],
    }
    p = tmp_path / "holidays.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("LAABH_NSE_HOLIDAYS_FILE", str(p))

    hs = load_nse_holidays()
    assert date(2025, 2, 26) in hs
    assert date(2025, 8, 15) in hs
    assert date(2026, 1, 26) in hs
    assert len(hs) == 3


def test_non_year_keys_are_silently_skipped(tmp_path, monkeypatch) -> None:
    # _README array + metadata key shouldn't produce warnings or polluted data.
    payload = {
        "_README": ["this is", "a comment"],
        "notes": "ignore me",
        "2026": ["2026-01-26"],
    }
    p = tmp_path / "holidays.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("LAABH_NSE_HOLIDAYS_FILE", str(p))

    hs = load_nse_holidays()
    assert hs == frozenset({date(2026, 1, 26)})


def test_invalid_date_strings_are_skipped(tmp_path, monkeypatch) -> None:
    payload = {"2025": ["2025-02-26", "not-a-date", "2025-13-99"]}
    p = tmp_path / "holidays.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("LAABH_NSE_HOLIDAYS_FILE", str(p))

    hs = load_nse_holidays()
    assert hs == frozenset({date(2025, 2, 26)})


def test_window_filtering(tmp_path, monkeypatch) -> None:
    payload = {
        "2025": ["2025-01-26", "2025-08-15", "2025-12-25"],
        "2026": ["2026-01-26", "2026-08-15"],
    }
    p = tmp_path / "holidays.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("LAABH_NSE_HOLIDAYS_FILE", str(p))

    inside = load_nse_holidays(date(2025, 12, 1), date(2026, 6, 30))
    assert inside == frozenset({date(2025, 12, 25), date(2026, 1, 26)})


def test_window_filtering_start_only(tmp_path, monkeypatch) -> None:
    # Bare ``load_nse_holidays(start, None)`` should include every date at-or-after start.
    payload = {"2025": ["2025-01-26", "2025-08-15"], "2026": ["2026-01-26"]}
    p = tmp_path / "holidays.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("LAABH_NSE_HOLIDAYS_FILE", str(p))

    inside = load_nse_holidays(date(2025, 6, 1), None)
    assert inside == frozenset({date(2025, 8, 15), date(2026, 1, 26)})


def test_window_filtering_end_only(tmp_path, monkeypatch) -> None:
    # Bare ``load_nse_holidays(None, end)`` should include every date at-or-before end.
    payload = {"2025": ["2025-01-26", "2025-08-15"], "2026": ["2026-01-26"]}
    p = tmp_path / "holidays.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("LAABH_NSE_HOLIDAYS_FILE", str(p))

    inside = load_nse_holidays(None, date(2025, 12, 31))
    assert inside == frozenset({date(2025, 1, 26), date(2025, 8, 15)})


def test_edits_picked_up_without_restart(tmp_path, monkeypatch) -> None:
    # Regression guard for the dropped lru_cache: re-reading after the
    # operator edits the file should see the new contents.
    p = tmp_path / "holidays.json"
    p.write_text(json.dumps({"2025": ["2025-01-26"]}), encoding="utf-8")
    monkeypatch.setenv("LAABH_NSE_HOLIDAYS_FILE", str(p))
    assert load_nse_holidays() == frozenset({date(2025, 1, 26)})

    p.write_text(
        json.dumps({"2025": ["2025-01-26", "2025-08-15"]}), encoding="utf-8"
    )
    assert load_nse_holidays() == frozenset(
        {date(2025, 1, 26), date(2025, 8, 15)}
    )


def test_malformed_json_returns_empty(tmp_path, monkeypatch) -> None:
    p = tmp_path / "holidays.json"
    p.write_text("not json {{{", encoding="utf-8")
    monkeypatch.setenv("LAABH_NSE_HOLIDAYS_FILE", str(p))

    assert load_nse_holidays() == frozenset()
