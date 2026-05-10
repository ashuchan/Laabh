"""Tests for ``apps.decision_inspector`` — PR 3.

Streamlit pages are hard to unit-test exhaustively (the framework owns the
script lifecycle). We exercise the *pure* helpers that don't depend on
``st.*`` state: click-event resolution and figure construction.

The end-to-end "page actually loads" check is covered by launching
``streamlit run`` in CI / smoke (PR 3 self-review caught HTTP 200, no
errors in startup log).
"""
from __future__ import annotations

import importlib
import sys
import types
import uuid
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture(scope="module")
def page_module():
    """Import ``apps.decision_inspector`` with Streamlit stubbed.

    Streamlit's ``set_page_config`` raises if called outside a real Streamlit
    runtime. We install a no-op stub ``streamlit`` module before import so
    the page-module's top-level statements execute cleanly. The stub also
    provides ``cache_data`` (so cache-decorated functions don't blow up at
    import time).
    """
    class _CacheDecorator:
        def __call__(self, *args, **kwargs):
            # Support both @cache_data and @cache_data(ttl=...)
            if args and callable(args[0]):
                return args[0]
            return lambda f: f

    class _StreamlitStub(types.ModuleType):
        def __init__(self):
            super().__init__("streamlit")
            self.session_state = {}
            self.cache_data = _CacheDecorator()
            self.sidebar = types.SimpleNamespace()

        def __getattr__(self, name):
            # Any st.<x>(...) call becomes a no-op
            return lambda *a, **kw: None

    sys.modules["streamlit"] = _StreamlitStub()
    # Plotly is real — we want to test figure construction
    mod = importlib.import_module("apps.decision_inspector")
    yield mod
    sys.modules.pop("streamlit", None)


# ---------------------------------------------------------------------------
# Click-event resolution
# ---------------------------------------------------------------------------

def _make_tick(ts: datetime):
    """Lightweight stand-in for inspector.types.TickSummary.

    ``_selected_tick_from_event`` only reads ``.virtual_time``, so we can
    pass a SimpleNamespace and skip the full dataclass construction.
    """
    return types.SimpleNamespace(virtual_time=ts)


def test_selected_tick_from_event_returns_none_for_empty_event(page_module):
    assert page_module._selected_tick_from_event(None, []) is None
    assert page_module._selected_tick_from_event({}, []) is None
    assert page_module._selected_tick_from_event({"selection": {}}, []) is None


def test_selected_tick_from_event_resolves_customdata_to_tick_time(page_module):
    t1 = datetime(2026, 5, 8, 10, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 5, 8, 10, 3, tzinfo=timezone.utc)
    ticks = [_make_tick(t1), _make_tick(t2)]
    event = {
        "selection": {
            "points": [
                {"customdata": [t2.isoformat(), 5, 1]},
            ],
        },
    }
    result = page_module._selected_tick_from_event(event, ticks)
    assert result == t2


def test_selected_tick_from_event_snaps_to_nearest_known_tick(page_module):
    """Plotly may round microseconds — clicked timestamp must snap within tolerance."""
    t1 = datetime(2026, 5, 8, 10, 0, tzinfo=timezone.utc)
    ticks = [_make_tick(t1)]
    # Clicked time off by 30 seconds — within the 90-second snap window
    near_click = (t1 + timedelta(seconds=30)).isoformat()
    event = {"selection": {"points": [{"customdata": [near_click, 0, 0]}]}}
    assert page_module._selected_tick_from_event(event, ticks) == t1


def test_selected_tick_from_event_rejects_clicks_far_from_any_tick(page_module):
    """A click >90s from any known tick is not snapped — returns None.

    Prevents accidental focus shifts when the user clicks the price line
    *between* tick-bar regions of the chart.
    """
    t1 = datetime(2026, 5, 8, 10, 0, tzinfo=timezone.utc)
    far_click = (t1 + timedelta(minutes=10)).isoformat()
    event = {"selection": {"points": [{"customdata": [far_click, 0, 0]}]}}
    # Only t1 known; click is 10 minutes off — must reject
    assert page_module._selected_tick_from_event(event, [_make_tick(t1)]) is None


def test_selected_tick_from_event_handles_malformed_customdata(page_module):
    """Garbled customdata must not raise — return None."""
    t1 = datetime(2026, 5, 8, 10, 0, tzinfo=timezone.utc)
    bad_events = [
        {"selection": {"points": [{"customdata": "not-a-list"}]}},
        {"selection": {"points": [{"customdata": []}]}},
        {"selection": {"points": [{"customdata": ["not-a-timestamp"]}]}},
        {"selection": {"points": [{}]}},  # no customdata key
    ]
    for ev in bad_events:
        # Should never raise, always None
        assert page_module._selected_tick_from_event(ev, [_make_tick(t1)]) is None


# ---------------------------------------------------------------------------
# Heatmap click → tick (PR 5)
# ---------------------------------------------------------------------------

def test_heatmap_click_resolves_dict_customdata(page_module):
    """Heatmap cells carry ``customdata = {arm, time, reason, ...}``."""
    t1 = datetime(2026, 5, 8, 10, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 5, 8, 10, 3, tzinfo=timezone.utc)
    ticks = [_make_tick(t1), _make_tick(t2)]
    event = {
        "selection": {
            "points": [
                {"customdata": {"arm": "A", "time": t2.isoformat(), "reason": "opened"}},
            ],
        },
    }
    assert page_module._heatmap_click_to_tick(event, ticks) == t2


def test_heatmap_click_falls_back_to_x_value_for_scatter_overlay(page_module):
    """The bandit-selected Scatter overlay carries ``x`` instead of customdata."""
    t1 = datetime(2026, 5, 8, 10, 0, tzinfo=timezone.utc)
    ticks = [_make_tick(t1)]
    event = {"selection": {"points": [{"x": t1.isoformat()}]}}
    assert page_module._heatmap_click_to_tick(event, ticks) == t1


def test_heatmap_click_returns_none_for_empty_event(page_module):
    assert page_module._heatmap_click_to_tick(None, []) is None
    assert page_module._heatmap_click_to_tick({}, []) is None
    assert page_module._heatmap_click_to_tick({"selection": {}}, []) is None


def test_heatmap_click_rejects_clicks_far_from_any_tick(page_module):
    """Same 90s tolerance as the scrubber click handler."""
    t1 = datetime(2026, 5, 8, 10, 0, tzinfo=timezone.utc)
    far = (t1 + timedelta(minutes=10)).isoformat()
    event = {"selection": {"points": [{"customdata": {"time": far}}]}}
    assert page_module._heatmap_click_to_tick(event, [_make_tick(t1)]) is None


# ---------------------------------------------------------------------------
# _previous_tick (PR 5)
# ---------------------------------------------------------------------------

def test_previous_tick_finds_prior_in_skeleton(page_module):
    t1 = datetime(2026, 5, 8, 10, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 5, 8, 10, 3, tzinfo=timezone.utc)
    t3 = datetime(2026, 5, 8, 10, 6, tzinfo=timezone.utc)
    ticks = [_make_tick(t) for t in [t1, t2, t3]]
    assert page_module._previous_tick(ticks, t3) == t2
    assert page_module._previous_tick(ticks, t2) == t1


def test_previous_tick_returns_none_for_first_tick(page_module):
    t1 = datetime(2026, 5, 8, 10, 0, tzinfo=timezone.utc)
    assert page_module._previous_tick([_make_tick(t1)], t1) is None


def test_previous_tick_handles_unknown_time(page_module):
    """When ``virtual_time`` isn't in the skeleton, return the latest tick before it."""
    t1 = datetime(2026, 5, 8, 10, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 5, 8, 10, 3, tzinfo=timezone.utc)
    arbitrary = datetime(2026, 5, 8, 10, 5, tzinfo=timezone.utc)
    assert page_module._previous_tick([_make_tick(t1), _make_tick(t2)], arbitrary) == t2


# ---------------------------------------------------------------------------
# Aggregation (PR 6)
# ---------------------------------------------------------------------------

def _full_tick(vt: datetime, **counts):
    """TickSummary stand-in that supports the summable count fields."""
    base = dict(
        n_signals_total=0, n_signals_strong=0, n_opened=0,
        n_lost_bandit=0, n_sized_zero=0, n_cooloff=0,
        n_kill_switch=0, n_capacity_full=0, n_warmup=0,
    )
    base.update(counts)
    return types.SimpleNamespace(virtual_time=vt, **base)


def test_aggregate_ticks_no_op_when_minutes_eq_3(page_module):
    t1 = datetime(2026, 5, 8, 9, 15, tzinfo=timezone.utc)
    t2 = datetime(2026, 5, 8, 9, 18, tzinfo=timezone.utc)
    ticks = [_full_tick(t1, n_signals_total=5), _full_tick(t2, n_signals_total=3)]
    out, click_map = page_module._aggregate_ticks(ticks, minutes=3)
    assert out is ticks  # identity — no copy
    assert click_map == {t1: t1, t2: t2}


def test_aggregate_ticks_buckets_pairs_into_6min(page_module):
    """Two adjacent 3-min ticks fold into one 6-min bucket; counts sum."""
    t1 = datetime(2026, 5, 8, 9, 15, tzinfo=timezone.utc)
    t2 = datetime(2026, 5, 8, 9, 18, tzinfo=timezone.utc)
    t3 = datetime(2026, 5, 8, 9, 21, tzinfo=timezone.utc)
    t4 = datetime(2026, 5, 8, 9, 24, tzinfo=timezone.utc)
    ticks = [
        _full_tick(t1, n_signals_total=2, n_opened=1),
        _full_tick(t2, n_signals_total=3),
        _full_tick(t3, n_signals_total=4, n_opened=2),
        _full_tick(t4, n_signals_total=1),
    ]
    out, click_map = page_module._aggregate_ticks(ticks, minutes=6)
    assert len(out) == 2  # 2 buckets of 6 min
    assert out[0].virtual_time == t1
    assert out[0].n_signals_total == 5
    assert out[0].n_opened == 1
    assert out[1].virtual_time == t3
    assert out[1].n_signals_total == 5
    # Click on bucket-1 (starts at t3) snaps to first underlying tick (also t3)
    assert click_map[t3] == t3
    assert click_map[t1] == t1


def test_aggregate_ticks_handles_empty(page_module):
    out, click_map = page_module._aggregate_ticks([], minutes=15)
    assert out == []
    assert click_map == {}


def test_aggregate_ticks_15min_groups_correctly(page_module):
    """Five 3-min ticks fold into one 15-min bucket."""
    base = datetime(2026, 5, 8, 9, 15, tzinfo=timezone.utc)
    ticks = [_full_tick(base + timedelta(minutes=3 * i), n_signals_total=1) for i in range(5)]
    out, _ = page_module._aggregate_ticks(ticks, minutes=15)
    assert len(out) == 1
    assert out[0].n_signals_total == 5
