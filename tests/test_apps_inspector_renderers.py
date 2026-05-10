"""Tests for ``apps._inspector_renderers`` — PR 4.

Two tiers:

  * **Pure formatters** — ``_format_value``, ``_bucket_color``,
    ``_format_strength_bar``, ``_direction_badge``, ``_bucket_chip``.
    Hit every shape (None, bool, int, float, Decimal, list, dict).

  * **Renderer smoke tests** — install a ``StreamlitRecorder`` stub that
    captures every ``st.*`` call. Asserts each renderer:
      - emits *something* when called with a populated payload,
      - emits the documented "_no data_" message when called with None,
      - never raises on edge inputs (empty list, missing trace fields).

We don't assert on the exact widgets emitted (Streamlit's API is too
broad — that's fragile). We assert on call count + on textual content
where it matters (e.g. blocking-step highlighted in red).
"""
from __future__ import annotations

import importlib
import sys
import types
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import pytest

from src.quant.feature_store import FeatureBundle


# ---------------------------------------------------------------------------
# Streamlit recorder stub
# ---------------------------------------------------------------------------

class _RecordingStreamlit(types.ModuleType):
    """Captures every st.<x>(...) call so tests can assert on it.

    Attribute access returns a callable that:
      * appends ``(name, args, kwargs)`` to ``self.calls``
      * for ``container``/``columns``/``expander``: returns a context
        manager whose ``__enter__`` returns either ``self`` (container/
        expander) or a list of ``self`` clones (columns).
    """

    def __init__(self):
        super().__init__("streamlit")
        self.calls: list[tuple[str, tuple, dict]] = []
        self.session_state: dict = {}

        class _CacheDecorator:
            def __call__(self, *a, **kw):
                if a and callable(a[0]):
                    return a[0]
                return lambda f: f

        self.cache_data = _CacheDecorator()

        # st.column_config namespace — attribute access returns no-op factories
        self.column_config = types.SimpleNamespace(
            TextColumn=lambda *a, **kw: None,
            NumberColumn=lambda *a, **kw: None,
        )

    def _record(self, name: str, *args, **kwargs):
        self.calls.append((name, args, kwargs))

    def __getattr__(self, name: str):
        def _captured(*args, **kwargs):
            self._record(name, *args, **kwargs)
            # container / expander act as context managers yielding self
            if name in {"container", "expander", "sidebar"}:
                return _CtxYielding(self)
            if name == "columns":
                spec = args[0] if args else 1
                n = spec if isinstance(spec, int) else len(spec)
                return [_CtxYielding(self) for _ in range(n)]
            return None
        return _captured

    def find(self, name: str) -> list[tuple[tuple, dict]]:
        """Return [(args, kwargs), ...] for every captured call to ``st.<name>``."""
        return [(a, kw) for n, a, kw in self.calls if n == name]

    def all_text(self) -> str:
        """Concatenate every positional string arg across all calls.

        Lets tests grep for substrings without coupling to a specific widget.
        """
        out = []
        for _name, args, _kwargs in self.calls:
            for a in args:
                if isinstance(a, str):
                    out.append(a)
        return "\n".join(out)


class _CtxYielding:
    """Context manager that yields the recorder for ``with col:`` blocks."""

    def __init__(self, recorder: _RecordingStreamlit):
        self._recorder = recorder
        # Per-column attribute access also routes through the recorder
        for n in ("markdown", "metric", "code", "caption"):
            setattr(self, n, getattr(recorder, n))
        self.columns = recorder.columns

    def __enter__(self):
        return self._recorder

    def __exit__(self, *exc):
        return False

    def __getattr__(self, name):
        return getattr(self._recorder, name)


@pytest.fixture
def recorder():
    """Install the recorder as ``streamlit`` and re-import the renderer module."""
    rec = _RecordingStreamlit()
    sys.modules["streamlit"] = rec
    # Re-import the renderers so they pick up the stubbed ``st``
    if "apps._inspector_renderers" in sys.modules:
        del sys.modules["apps._inspector_renderers"]
    mod = importlib.import_module("apps._inspector_renderers")
    yield rec, mod
    sys.modules.pop("streamlit", None)
    if "apps._inspector_renderers" in sys.modules:
        del sys.modules["apps._inspector_renderers"]


# ---------------------------------------------------------------------------
# Pure formatters
# ---------------------------------------------------------------------------

def test_format_value_handles_none(recorder):
    _, mod = recorder
    assert mod._format_value(None) == "—"


def test_format_value_handles_bool(recorder):
    _, mod = recorder
    assert mod._format_value(True) == "true"
    assert mod._format_value(False) == "false"


def test_format_value_thousand_separator_for_large_ints(recorder):
    _, mod = recorder
    assert mod._format_value(1234) == "1,234"
    assert mod._format_value(999) == "999"


def test_format_value_floats_rounded_to_4_places(recorder):
    _, mod = recorder
    assert mod._format_value(0.123456789) == "0.1235"
    assert mod._format_value(Decimal("3.14159")) == "3.1416"


def test_format_value_thousand_separator_for_large_floats(recorder):
    _, mod = recorder
    assert mod._format_value(123456.789) == "123,456.7890"


def test_format_value_recurses_into_lists_and_dicts(recorder):
    _, mod = recorder
    assert mod._format_value([1, 2.5, None]) == "[1, 2.5000, —]"
    assert mod._format_value({"a": 1, "b": 2.5}).startswith("{a: 1, b: 2.5000")


def test_bucket_color_known_buckets(recorder):
    _, mod = recorder
    assert mod._bucket_color("opened") == "green"
    assert mod._bucket_color("weak_signal") == "orange"
    assert mod._bucket_color("sized_zero") == "red"
    assert mod._bucket_color("lost_bandit") == "blue"


def test_bucket_color_unknown_bucket_defaults_gray(recorder):
    _, mod = recorder
    assert mod._bucket_color("totally_made_up") == "gray"


def test_strength_bar_magnitude_independent_of_sign(recorder):
    _, mod = recorder
    pos = mod._format_strength_bar(0.4)
    neg = mod._format_strength_bar(-0.4)
    # Bar fills are equal, signs differ in the trailing number
    assert pos.split("`")[1] == neg.split("`")[1]
    assert "+0.4000" in pos and "-0.4000" in neg


def test_strength_bar_clamps_to_unit_interval(recorder):
    _, mod = recorder
    out = mod._format_strength_bar(2.5)  # > 1
    # All width filled — the bar string has no remaining "░"
    bar = out.split("`")[1]
    assert "░" not in bar


def test_direction_badge_uses_correct_color(recorder):
    _, mod = recorder
    assert "green-background" in mod._direction_badge("bullish")
    assert "red-background" in mod._direction_badge("bearish")
    assert "gray-background" in mod._direction_badge("neutral")


# ---------------------------------------------------------------------------
# Renderer smoke tests
# ---------------------------------------------------------------------------

def _bundle() -> FeatureBundle:
    return FeatureBundle(
        underlying_id=uuid.uuid4(),
        underlying_symbol="X",
        captured_at=datetime(2026, 5, 8, 10, 0, tzinfo=timezone.utc),
        underlying_ltp=100.5,
        underlying_volume_3min=1234.0,
        vwap_today=99.8,
        realized_vol_3min=0.20,
        realized_vol_30min=0.18,
        atm_iv=0.22,
        atm_oi=12345.0,
        atm_bid=Decimal("50.0"),
        atm_ask=Decimal("50.5"),
        bid_volume_3min_change=100.0,
        ask_volume_3min_change=80.0,
        bb_width=0.012,
        vix_value=15.0,
        vix_regime="normal",
    )


def _signal(name: str = "momentum", reason: str = "lost_bandit", strength: float = 0.6,
            *, selected: bool = False, trace: dict | None = None):
    """Build a PrimitiveSignalView with sensible defaults.

    Imports lazily so the recorder fixture can install its stub before the
    renderer module loads — otherwise we'd capture the real Streamlit on
    the first import and then re-import would leave stale references.
    """
    from src.quant.inspector import PrimitiveSignalView
    return PrimitiveSignalView(
        arm_id=f"X_{name}", primitive_name=name, direction="bullish",
        strength=strength, rejection_reason=reason,
        posterior_mean=0.012, bandit_selected=selected, lots_sized=None,
        primitive_trace=trace,
    )


def test_render_feature_bundle_emits_groups(recorder):
    rec, mod = recorder
    mod.render_feature_bundle(_bundle())
    # Each of the 4 groups becomes an expander
    expanders = rec.find("expander")
    assert len(expanders) == 4
    text = rec.all_text()
    assert "Price / volume" in text
    assert "Vol / IV" in text


def test_render_feature_bundle_handles_none(recorder):
    rec, mod = recorder
    mod.render_feature_bundle(None)
    assert any(name == "info" for name, _, _ in rec.calls)


def test_render_primitive_cards_emits_one_container_per_signal(recorder):
    rec, mod = recorder
    sigs = [_signal("momentum"), _signal("orb", reason="weak_signal", strength=0.2)]
    mod.render_primitive_cards(sigs)
    # Each card is a bordered container
    containers = rec.find("container")
    assert len(containers) == 2
    assert all(kw.get("border") is True for _, kw in containers)


def test_render_primitive_cards_empty_list_shows_friendly_info(recorder):
    rec, mod = recorder
    mod.render_primitive_cards([])
    assert any(name == "info" for name, _, _ in rec.calls)


def test_render_primitive_cards_includes_trace_formula(recorder):
    rec, mod = recorder
    trace = {
        "name": "momentum",
        "inputs": {"rv": 0.18},
        "intermediates": {"weighted_mom": 0.0023},
        "formula": "tanh(2 × 0.0023 / 0.18) = 0.025",
    }
    mod.render_primitive_cards([_signal(trace=trace)])
    # The formula text gets rendered inside an st.code() call
    code_calls = rec.find("code")
    assert any("tanh" in (a[0] if a else "") for a, _ in code_calls)


def test_render_bandit_tournament_none_shows_info(recorder):
    rec, mod = recorder
    mod.render_bandit_tournament(None)
    assert any(name == "info" for name, _, _ in rec.calls)


def test_render_bandit_tournament_renders_dataframe_when_arms_present(recorder):
    rec, mod = recorder
    from src.quant.inspector import BanditTournamentView
    tour = BanditTournamentView(
        algo="lints",
        context_vector=[0.5, 0.3, 0.5, 0.5, 0.5],
        context_dims=["vix_norm", "tod_pct", "day_pnl_norm", "nifty_5d_norm", "rv30_pctile"],
        arms={
            "A": {"sampled_mean": 0.018, "posterior_mean": 0.01, "signal_strength": 0.6, "score": 0.0108},
            "B": {"sampled_mean": -0.004, "posterior_mean": 0.002, "signal_strength": 0.4, "score": -0.0016},
        },
        selected_arm_id="A",
        n_competitors=2,
    )
    mod.render_bandit_tournament(tour)
    df_calls = rec.find("dataframe")
    assert len(df_calls) == 1
    # First positional arg is the row list
    rows = df_calls[0][0][0]
    assert len(rows) == 2
    # Sorted by score descending — A first
    assert rows[0]["arm"] == "A"
    assert rows[0]["selected"] == "✓"


def test_render_sizer_cascade_none_shows_info(recorder):
    rec, mod = recorder
    mod.render_sizer_cascade(None)
    assert any(name == "info" for name, _, _ in rec.calls)


def test_render_sizer_cascade_success_uses_st_success(recorder):
    rec, mod = recorder
    from src.quant.inspector import SizerOutcomeView
    out = SizerOutcomeView(
        final_lots=3, blocking_step=None,
        inputs={"posterior_mean": 0.018},
        constants={"kelly_fraction": 0.5},
        cascade=[{"step": "p_sigmoid", "value": 0.55, "formula": "..."}],
    )
    mod.render_sizer_cascade(out)
    assert any(name == "success" for name, _, _ in rec.calls)
    assert not any(name == "error" for name, _, _ in rec.calls)


def test_render_sizer_cascade_blocked_uses_st_error(recorder):
    rec, mod = recorder
    from src.quant.inspector import SizerOutcomeView
    out = SizerOutcomeView(
        final_lots=0, blocking_step="cost_gate",
        inputs={}, constants={},
        cascade=[
            {"step": "p_sigmoid", "value": 0.55, "formula": "..."},
            {"step": "cost_gate", "value": "blocked", "formula": "..."},
        ],
    )
    mod.render_sizer_cascade(out)
    assert any(name == "error" for name, _, _ in rec.calls)


def test_render_outcome_banner_no_signals_shows_info(recorder):
    rec, mod = recorder
    from src.quant.inspector import TickState
    state = TickState(
        virtual_time=datetime(2026, 5, 8, 10, 0, tzinfo=timezone.utc),
        symbol="X", underlying_id=uuid.uuid4(),
        feature_bundle=None, primitive_signals=[],
        bandit_tournament=None, sizer_outcome=None, chosen_arm_id=None,
    )
    mod.render_outcome_banner(state)
    assert any(name == "info" for name, _, _ in rec.calls)


def test_render_outcome_banner_opened_uses_success(recorder):
    rec, mod = recorder
    from src.quant.inspector import TickState, SizerOutcomeView
    state = TickState(
        virtual_time=datetime(2026, 5, 8, 10, 0, tzinfo=timezone.utc),
        symbol="X", underlying_id=uuid.uuid4(),
        feature_bundle=None,
        primitive_signals=[_signal(selected=True)],
        bandit_tournament=None,
        sizer_outcome=SizerOutcomeView(
            final_lots=3, blocking_step=None, inputs={}, constants={}, cascade=[],
        ),
        chosen_arm_id="X_momentum",
    )
    mod.render_outcome_banner(state)
    assert any(name == "success" for name, _, _ in rec.calls)


def test_render_outcome_banner_sized_zero_uses_error(recorder):
    rec, mod = recorder
    from src.quant.inspector import TickState, SizerOutcomeView
    state = TickState(
        virtual_time=datetime(2026, 5, 8, 10, 0, tzinfo=timezone.utc),
        symbol="X", underlying_id=uuid.uuid4(),
        feature_bundle=None,
        primitive_signals=[_signal()],
        bandit_tournament=None,
        sizer_outcome=SizerOutcomeView(
            final_lots=0, blocking_step="cost_gate",
            inputs={}, constants={}, cascade=[],
        ),
        chosen_arm_id="X_momentum",
    )
    mod.render_outcome_banner(state)
    assert any(name == "error" for name, _, _ in rec.calls)


# ---------------------------------------------------------------------------
# Heatmap (PR 5)
# ---------------------------------------------------------------------------

def _arm_history(arm_id: str, symbol: str, primitive: str, ticks_data: list[tuple]):
    """Build an ArmHistory; ticks_data items are (virtual_time, post, sampled, selected)."""
    from src.quant.inspector import ArmHistory, ArmTickState
    ticks = [
        ArmTickState(
            virtual_time=vt,
            rejection_reason="opened" if selected else "lost_bandit",
            strength=0.6,
            posterior_mean=post,
            sampled_mean=sampled,
            signal_strength=0.6,
            score=sampled,
            bandit_selected=selected,
            lots_sized=2 if selected else None,
        )
        for vt, post, sampled, selected in ticks_data
    ]
    return ArmHistory(arm_id=arm_id, primitive_name=primitive, symbol=symbol, ticks=ticks)


def _tick_summary(vt: datetime):
    """Stand-in for inspector.types.TickSummary — only ``virtual_time`` is read."""
    return types.SimpleNamespace(virtual_time=vt)


def test_build_heatmap_arrays_aligns_to_skeleton_ticks(recorder):
    _, mod = recorder
    t1 = datetime(2026, 5, 8, 10, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 5, 8, 10, 3, tzinfo=timezone.utc)
    t3 = datetime(2026, 5, 8, 10, 6, tzinfo=timezone.utc)
    skel_ticks = [_tick_summary(t) for t in [t1, t2, t3]]
    matrix = [
        _arm_history("A", "X", "momentum", [(t1, 0.01, 0.02, False), (t3, 0.015, 0.025, True)]),
        _arm_history("B", "X", "orb", [(t2, -0.005, -0.01, False)]),
    ]
    arms, x_axis, z, cd = mod._build_heatmap_arrays(matrix, skel_ticks)
    assert arms == ["A", "B"]
    assert x_axis == [t1, t2, t3]
    # A has values at t1 and t3, None at t2; B has value only at t2
    assert z[0][0] == 0.01 and z[0][1] is None and z[0][2] == 0.015
    assert z[1][0] is None and z[1][1] == -0.005 and z[1][2] is None
    # customdata captures the rejection reason
    assert cd[0][0]["reason"] == "lost_bandit"
    assert cd[0][2]["reason"] == "opened"


def test_build_heatmap_arrays_skips_arm_ticks_outside_skeleton(recorder):
    """Defensive: a signal-log tick at an unknown time is silently skipped."""
    _, mod = recorder
    t1 = datetime(2026, 5, 8, 10, 0, tzinfo=timezone.utc)
    t_bogus = datetime(2026, 5, 8, 11, 0, tzinfo=timezone.utc)
    skel_ticks = [_tick_summary(t1)]
    matrix = [_arm_history("A", "X", "m", [(t1, 0.01, 0.02, False), (t_bogus, 0.5, 0.5, True)])]
    _, _, z, _ = mod._build_heatmap_arrays(matrix, skel_ticks)
    assert len(z[0]) == 1
    assert z[0][0] == 0.01  # the bogus-time tick was dropped


def test_render_arm_heatmap_empty_matrix_shows_info(recorder):
    rec, mod = recorder
    out = mod.render_arm_heatmap([], all_ticks=[])
    assert out is None
    assert any(name == "info" for name, _, _ in rec.calls)


def test_render_arm_heatmap_renders_plotly_chart(recorder):
    rec, mod = recorder
    t1 = datetime(2026, 5, 8, 10, 0, tzinfo=timezone.utc)
    matrix = [_arm_history("A", "X", "m", [(t1, 0.01, 0.02, True)])]
    skel_ticks = [_tick_summary(t1)]
    mod.render_arm_heatmap(matrix, skel_ticks)
    chart_calls = rec.find("plotly_chart")
    assert len(chart_calls) == 1
    # Renderer must request click events
    args, kwargs = chart_calls[0]
    assert kwargs.get("on_select") == "rerun"


def test_heatmap_color_by_posterior_mean_uses_post_field(recorder):
    """Default colour-by reads ``posterior_mean`` from each ArmTickState."""
    _, mod = recorder
    t1 = datetime(2026, 5, 8, 10, 0, tzinfo=timezone.utc)
    matrix = [_arm_history("A", "X", "m", [(t1, 0.01, 0.02, False)])]
    _, _, z, _ = mod._build_heatmap_arrays(matrix, [_tick_summary(t1)])
    assert z[0][0] == 0.01  # posterior_mean


def test_heatmap_color_by_sampled_mean_uses_sampled_field(recorder):
    _, mod = recorder
    t1 = datetime(2026, 5, 8, 10, 0, tzinfo=timezone.utc)
    matrix = [_arm_history("A", "X", "m", [(t1, 0.01, 0.025, True)])]
    _, _, z, _ = mod._build_heatmap_arrays(
        matrix, [_tick_summary(t1)], color_by="sampled_mean",
    )
    assert z[0][0] == 0.025


def test_heatmap_color_by_falls_back_to_strength_when_field_is_none(recorder):
    """When colour-by field is None on this tick (e.g. arm never reached the
    bandit), fall back to the primitive strength so the cell still has heat."""
    from src.quant.inspector import ArmHistory, ArmTickState
    _, mod = recorder
    t1 = datetime(2026, 5, 8, 10, 0, tzinfo=timezone.utc)
    # Sampled is None (didn't reach bandit), but strength is 0.7
    arm = ArmHistory(
        arm_id="A", primitive_name="m", symbol="X",
        ticks=[ArmTickState(
            virtual_time=t1, rejection_reason="weak_signal", strength=0.7,
            posterior_mean=None, sampled_mean=None,
            signal_strength=None, score=None,
            bandit_selected=False, lots_sized=None,
        )],
    )
    _, _, z, _ = mod._build_heatmap_arrays(
        [arm], [_tick_summary(t1)], color_by="sampled_mean",
    )
    assert z[0][0] == pytest.approx(0.7)


def test_heatmap_color_options_constant_includes_all_supported(recorder):
    """The constant exposed to the sidebar must list every supported field."""
    _, mod = recorder
    assert set(mod.HEATMAP_COLOR_OPTIONS.keys()) >= {
        "posterior_mean", "sampled_mean", "signal_strength", "strength",
    }


# ---------------------------------------------------------------------------
# Diff strip (PR 5)
# ---------------------------------------------------------------------------

def test_render_diff_strip_none_shows_info(recorder):
    rec, mod = recorder
    mod.render_diff_strip(None)
    assert any(name == "info" for name, _, _ in rec.calls)


def test_render_diff_strip_emits_one_metric_per_delta(recorder):
    rec, mod = recorder
    from src.quant.inspector import FeatureDelta, TickDiff
    deltas = [
        FeatureDelta(name="underlying_ltp", value_t1=100.0, value_t2=101.5, delta=1.5, pct_change=0.015),
        FeatureDelta(name="atm_iv", value_t1=0.20, value_t2=0.21, delta=0.01, pct_change=0.05),
    ]
    diff = TickDiff(
        symbol="X", t1=datetime(2026, 5, 8, 10, 0, tzinfo=timezone.utc),
        t2=datetime(2026, 5, 8, 10, 3, tzinfo=timezone.utc),
        deltas=deltas, regime_change=None,
    )
    mod.render_diff_strip(diff)
    metric_calls = rec.find("metric")
    assert len(metric_calls) == 2


def test_render_diff_strip_marks_interesting_pct_changes(recorder):
    """Features with |pct_change| >= 1% get a leading bullet in the label."""
    rec, mod = recorder
    from src.quant.inspector import FeatureDelta, TickDiff
    deltas = [
        FeatureDelta(name="big_mover", value_t1=100.0, value_t2=110.0, delta=10.0, pct_change=0.10),
        FeatureDelta(name="tiny_change", value_t1=100.0, value_t2=100.05, delta=0.05, pct_change=0.0005),
    ]
    diff = TickDiff(
        symbol="X", t1=datetime(2026, 5, 8, 10, 0, tzinfo=timezone.utc),
        t2=datetime(2026, 5, 8, 10, 3, tzinfo=timezone.utc),
        deltas=deltas, regime_change=None,
    )
    mod.render_diff_strip(diff)
    labels = []
    for _name, args, kwargs in rec.calls:
        if _name == "metric":
            labels.append(kwargs.get("label") or (args[0] if args else ""))
    assert any(lbl.startswith("• big_mover") for lbl in labels)
    assert any(lbl == "tiny_change" for lbl in labels)


def test_render_diff_strip_emits_warning_on_regime_change(recorder):
    rec, mod = recorder
    from src.quant.inspector import TickDiff
    diff = TickDiff(
        symbol="X", t1=datetime(2026, 5, 8, 10, 0, tzinfo=timezone.utc),
        t2=datetime(2026, 5, 8, 10, 3, tzinfo=timezone.utc),
        deltas=[], regime_change=("normal", "high"),
    )
    mod.render_diff_strip(diff)
    assert any(name == "warning" for name, _, _ in rec.calls)
