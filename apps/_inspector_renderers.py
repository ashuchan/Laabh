"""Streamlit renderers for the Decision Inspector waterfall (PR 4).

Each ``render_*`` function takes a typed payload from
``src.quant.inspector`` and emits Streamlit widgets in-place. They never
fetch data — the page module owns I/O, renderers own presentation.

Pure formatting helpers (``_format_value``, ``_bucket_color``, etc.) are
unit-tested in ``tests/test_apps_inspector_renderers.py``; the renderers
themselves are smoke-tested under a Streamlit stub.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

import plotly.graph_objects as go
import streamlit as st

from src.quant.feature_store import FeatureBundle
from src.quant.inspector import (
    ArmHistory,
    BanditTournamentView,
    PrimitiveSignalView,
    SizerOutcomeView,
    TickDiff,
    TickState,
)


# ---------------------------------------------------------------------------
# Pure formatting helpers — unit-testable
# ---------------------------------------------------------------------------

# Visual taxonomy for the rejection-bucket chip. Colors map to Streamlit's
# built-in ``:<color>-background[...]`` markdown syntax, so no HTML.
_BUCKET_COLOR: dict[str, str] = {
    "opened":         "green",
    "weak_signal":    "orange",
    "warmup":         "gray",
    "kill_switch":    "red",
    "capacity_full":  "violet",
    "cooloff":        "gray",
    "lost_bandit":    "blue",
    "sized_zero":     "red",
}


def _bucket_color(reason: str) -> str:
    """Color name for a rejection_reason. Defaults to gray for unknown bucket."""
    return _BUCKET_COLOR.get(reason, "gray")


def _format_value(v: Any, *, places: int = 4) -> str:
    """Pretty-print a scalar for the inspector tables.

    Numbers ≥ 1000 get thousand separators. Floats use ``places`` decimals
    (defaults to 4 — the same precision the trace formulas display). Bool /
    None / strings pass through. Dict / list values are JSON-ish reprs.
    """
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int,)) and not isinstance(v, bool):
        return f"{v:,}" if abs(v) >= 1000 else str(v)
    if isinstance(v, (float, Decimal)):
        f = float(v)
        if abs(f) >= 1000:
            return f"{f:,.{places}f}"
        return f"{f:.{places}f}"
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_format_value(x, places=places) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ", ".join(f"{k}: {_format_value(v2, places=places)}" for k, v2 in v.items()) + "}"
    return str(v)


def _format_strength_bar(value: float, *, width: int = 20) -> str:
    """Render |value| ∈ [0,1] as an ASCII bar — independent of width.

    Returns a string like ``"████████░░░░░░░░░░░░ 0.4123"``. Negative
    values are shown as bullish/bearish via the caller's badge; the bar
    is always magnitude-based.
    """
    mag = max(0.0, min(1.0, abs(float(value))))
    filled = int(round(mag * width))
    bar = "█" * filled + "░" * (width - filled)
    return f"`{bar}` {value:+.4f}"


def _direction_badge(direction: str) -> str:
    """Compact colored badge for bullish/bearish/neutral."""
    if direction == "bullish":
        return ":green-background[**bullish**]"
    if direction == "bearish":
        return ":red-background[**bearish**]"
    return ":gray-background[**neutral**]"


def _bucket_chip(reason: str) -> str:
    """Colored markdown chip for the rejection bucket."""
    color = _bucket_color(reason)
    return f":{color}-background[**{reason}**]"


# ---------------------------------------------------------------------------
# 1. Feature-bundle inputs panel
# ---------------------------------------------------------------------------

# FeatureBundle fields grouped for human-friendly display. Order matters —
# the visual hierarchy follows what a quant researcher would scan first
# (price/volume → vol/IV → microstructure → context).
_FEATURE_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Price / volume", (
        "underlying_ltp", "underlying_volume_3min",
        "vwap_today",
        "session_start_ltp", "orb_high", "orb_low",
    )),
    ("Vol / IV", (
        "realized_vol_3min", "realized_vol_30min",
        "atm_iv", "atm_oi", "bb_width",
    )),
    ("Quotes / OFI", (
        "atm_bid", "atm_ask",
        "bid_volume_3min_change", "ask_volume_3min_change",
    )),
    ("Macro context", (
        "vix_value", "vix_regime",
        "constituent_basket_value",
    )),
)


def render_feature_bundle(bundle: FeatureBundle | None) -> None:
    """Render the FeatureBundle as grouped key-value pairs."""
    if bundle is None:
        st.info("No feature bundle available for this tick.")
        return
    for group_name, fields in _FEATURE_GROUPS:
        with st.expander(group_name, expanded=(group_name == "Price / volume")):
            for field in fields:
                value = getattr(bundle, field, None)
                cols = st.columns([2, 3])
                cols[0].markdown(f"`{field}`")
                cols[1].markdown(_format_value(value))


# ---------------------------------------------------------------------------
# 2. Per-primitive formula cards
# ---------------------------------------------------------------------------

def _render_trace_subtable(label: str, payload: dict | None) -> None:
    """One sub-section inside a primitive card (inputs OR intermediates)."""
    if not payload:
        return
    st.markdown(f"**{label}**")
    for key, value in payload.items():
        cols = st.columns([2, 3])
        cols[0].markdown(f"`{key}`")
        cols[1].markdown(_format_value(value))


def render_primitive_cards(signals: list[PrimitiveSignalView]) -> None:
    """Render one bordered card per primitive that emitted a signal."""
    if not signals:
        st.info(
            "No primitive emitted a signal on this symbol at this tick. "
            "Either warmup wasn't satisfied or no setup conditions were met."
        )
        return
    # Stable display order: alphabetical by primitive name so the layout
    # doesn't shuffle as you scrub between ticks.
    for sig in sorted(signals, key=lambda s: s.primitive_name):
        with st.container(border=True):
            header_cols = st.columns([3, 2, 2, 3])
            header_cols[0].markdown(f"### `{sig.primitive_name}`")
            header_cols[1].markdown(_direction_badge(sig.direction))
            header_cols[2].markdown(_bucket_chip(sig.rejection_reason))
            if sig.bandit_selected:
                header_cols[3].markdown(":green-background[**chosen by bandit**]")

            st.markdown(_format_strength_bar(sig.strength))
            if sig.posterior_mean is not None:
                st.caption(f"posterior_mean at decision: {sig.posterior_mean:+.6f}")

            trace = sig.primitive_trace or {}
            if trace:
                cols = st.columns(2)
                with cols[0]:
                    _render_trace_subtable("inputs", trace.get("inputs"))
                with cols[1]:
                    _render_trace_subtable("intermediates", trace.get("intermediates"))
                formula = trace.get("formula")
                if formula:
                    st.markdown("**formula**")
                    st.code(formula, language=None)


# ---------------------------------------------------------------------------
# 3. Bandit tournament
# ---------------------------------------------------------------------------

def render_bandit_tournament(tour: BanditTournamentView | None) -> None:
    """Render the bandit's per-arm tournament for this tick."""
    if tour is None:
        st.info(
            "No arms reached the bandit at this tick — gated upstream "
            "(weak signals, warmup, kill switch, capacity, or all in cooloff)."
        )
        return

    header_cols = st.columns([2, 3])
    header_cols[0].markdown(f"**Algorithm:** `{tour.algo}`")
    header_cols[1].markdown(f"**Competitors:** {tour.n_competitors}")

    if tour.context_vector and tour.context_dims:
        st.markdown("**Context vector**")
        ctx_cols = st.columns(len(tour.context_dims))
        for col, name, val in zip(ctx_cols, tour.context_dims, tour.context_vector):
            col.metric(label=name, value=f"{val:.3f}")

    if not tour.arms:
        st.warning("Tournament has no arm slices — bandit_trace shape may be off.")
        return

    # Sort arms by score descending so the row that "won" is at the top.
    rows = []
    for arm_id, payload in tour.arms.items():
        rows.append({
            "arm": arm_id,
            "selected": "✓" if arm_id == tour.selected_arm_id else "",
            "sampled_mean": float(payload.get("sampled_mean", 0.0)),
            "posterior_mean": float(payload.get("posterior_mean", 0.0)),
            "signal_strength": float(payload.get("signal_strength", 0.0)),
            "score": float(payload.get("score", 0.0)),
        })
    rows.sort(key=lambda r: r["score"], reverse=True)
    st.dataframe(
        rows,
        use_container_width=True,
        hide_index=True,
        column_config={
            "arm":             st.column_config.TextColumn("arm", width="medium"),
            "selected":        st.column_config.TextColumn("✓", width="small"),
            "sampled_mean":    st.column_config.NumberColumn(format="%+.4f"),
            "posterior_mean":  st.column_config.NumberColumn(format="%+.4f"),
            "signal_strength": st.column_config.NumberColumn(format="%.3f"),
            "score":           st.column_config.NumberColumn(format="%+.4f"),
        },
    )


# ---------------------------------------------------------------------------
# 4. Sizer cascade
# ---------------------------------------------------------------------------

def render_sizer_cascade(outcome: SizerOutcomeView | None) -> None:
    """Render the 9-step Kelly cascade for the chosen arm."""
    if outcome is None:
        st.info("No sizer ran — bandit didn't pick an arm at this tick.")
        return

    # Top banner: final lots vs blocking step.
    if outcome.blocking_step is None:
        st.success(f"**Sized to {outcome.final_lots} lots.** No step blocked entry.")
    else:
        st.error(
            f"**Sized to {outcome.final_lots} lots — blocked at "
            f"`{outcome.blocking_step}`.**"
        )

    cols = st.columns(2)
    with cols[0]:
        with st.expander("inputs", expanded=False):
            for k, v in outcome.inputs.items():
                row = st.columns([2, 3])
                row[0].markdown(f"`{k}`")
                row[1].markdown(_format_value(v))
    with cols[1]:
        with st.expander("constants", expanded=False):
            for k, v in outcome.constants.items():
                row = st.columns([2, 3])
                row[0].markdown(f"`{k}`")
                row[1].markdown(_format_value(v))

    st.markdown("**Cascade**")
    for i, step in enumerate(outcome.cascade, start=1):
        is_blocker = (
            outcome.blocking_step is not None
            and step.get("step") == outcome.blocking_step
        )
        with st.container(border=True):
            row = st.columns([1, 4, 6, 3])
            row[0].markdown(f"**{i}**")
            label = step.get("step", "?")
            if is_blocker:
                row[1].markdown(f":red-background[**`{label}`**]")
            else:
                row[1].markdown(f"`{label}`")
            row[2].code(step.get("formula", ""), language=None)
            row[3].markdown(f"**{_format_value(step.get('value'))}**")


# ---------------------------------------------------------------------------
# 5. Outcome banner
# ---------------------------------------------------------------------------

def render_outcome_banner(state: TickState) -> None:
    """One-line colored summary of the tick's net outcome on this symbol."""
    if not state.primitive_signals:
        st.info(
            f"At {state.virtual_time.strftime('%H:%M:%S')} — no primitive "
            f"emitted a signal on `{state.symbol}`."
        )
        return

    if state.chosen_arm_id and state.sizer_outcome:
        if state.sizer_outcome.final_lots > 0:
            st.success(
                f"**Opened** `{state.chosen_arm_id}` × {state.sizer_outcome.final_lots} lots."
            )
            return
        st.error(
            f"**Sized to zero** on `{state.chosen_arm_id}` "
            f"(blocked at `{state.sizer_outcome.blocking_step}`)."
        )
        return

    # No chosen arm — surface the dominant rejection bucket on this symbol.
    from collections import Counter
    counts = Counter(s.rejection_reason for s in state.primitive_signals)
    dominant, n = counts.most_common(1)[0]
    color_fn = {
        "opened": st.success,
        "weak_signal": st.warning,
        "lost_bandit": st.info,
        "sized_zero": st.error,
    }.get(dominant, st.info)
    color_fn(
        f"**No entry** on `{state.symbol}` at "
        f"{state.virtual_time.strftime('%H:%M:%S')} — dominant bucket: "
        f"`{dominant}` ({n}/{len(state.primitive_signals)} signals)."
    )


# ---------------------------------------------------------------------------
# 6. Per-arm heatmap (PR 5)
# ---------------------------------------------------------------------------

# Closed set of fields the heatmap can colour by. Each maps to an attribute
# on ``ArmTickState`` and a hover label (PR 6 color-by toggle).
HEATMAP_COLOR_OPTIONS: dict[str, str] = {
    "posterior_mean":   "posterior μ",
    "sampled_mean":     "sampled μ",
    "signal_strength":  "signal strength",
    "strength":         "primitive strength",
}


def _cell_value(atick: Any, color_by: str) -> float | None:
    """Read the configured field off an ``ArmTickState``, falling back to strength.

    The bandit-only fields (``sampled_mean``, ``signal_strength``) are only
    populated when the arm reached the bandit. For ticks where the arm
    fell out earlier (weak / cooloff / capacity / kill / warmup) those
    fields are None and we fall back to ``strength`` so the cell still
    has *some* value to colour.
    """
    primary = getattr(atick, color_by, None)
    if primary is not None:
        return float(primary)
    return float(atick.strength) if atick.strength is not None else None


def _build_heatmap_arrays(
    matrix: list[ArmHistory], all_ticks: list, *, color_by: str = "posterior_mean",
) -> tuple[list[str], list[Any], list[list[float | None]], list[list[dict]]]:
    """Project the arm matrix into Plotly Heatmap inputs.

    Returns (arm_labels, x_axis_ticks, z_values, customdata):
      * arm_labels — y-axis (one per arm, "SYMBOL_primitive")
      * x_axis_ticks — x-axis values (datetime, in skeleton-tick order)
      * z_values — 2D array; ``None`` for ticks where the arm didn't fire
      * customdata — same shape as z; carries (rejection_reason, virtual_time_iso)
        so the click handler can map a click back to a tick.

    Cells where the arm didn't fire are explicitly ``None`` so Plotly
    renders them as transparent (gaps in the trajectory). ``color_by``
    selects which ArmTickState field drives the heat — see
    ``HEATMAP_COLOR_OPTIONS``.
    """
    arm_labels = [m.arm_id for m in matrix]
    x_axis = [t.virtual_time for t in all_ticks]
    # Index ticks by virtual_time → column index for fast lookup
    col_by_time = {t.virtual_time: i for i, t in enumerate(all_ticks)}
    n_cols = len(x_axis)
    n_rows = len(arm_labels)
    z = [[None for _ in range(n_cols)] for _ in range(n_rows)]
    cd: list[list[dict]] = [[{} for _ in range(n_cols)] for _ in range(n_rows)]
    for r, arm in enumerate(matrix):
        for atick in arm.ticks:
            c = col_by_time.get(atick.virtual_time)
            if c is None:
                continue  # signal-log tick not in the skeleton's tick list — skip
            z[r][c] = _cell_value(atick, color_by)
            cd[r][c] = {
                "arm": arm.arm_id,
                "time": atick.virtual_time.isoformat(),
                "reason": atick.rejection_reason,
                "selected": atick.bandit_selected,
                "lots": atick.lots_sized,
            }
    return arm_labels, x_axis, z, cd


def _build_heatmap_figure(
    matrix: list[ArmHistory], all_ticks: list, *,
    focus_arm_id: str | None = None,
    color_by: str = "posterior_mean",
) -> go.Figure:
    """Construct the Plotly heatmap figure.

    A ``Scatter`` overlay marks ticks where ``bandit_selected`` is true so
    "this is where the arm got picked" is visible at a glance. ``color_by``
    drives the heat scale — see ``HEATMAP_COLOR_OPTIONS``.
    """
    arm_labels, x_axis, z, cd = _build_heatmap_arrays(
        matrix, all_ticks, color_by=color_by,
    )
    cb_title = HEATMAP_COLOR_OPTIONS.get(color_by, color_by)
    fig = go.Figure(
        data=go.Heatmap(
            z=z,
            x=x_axis,
            y=arm_labels,
            customdata=cd,
            colorscale="RdBu",
            zmid=0,
            hovertemplate=(
                "%{y}<br>"
                "%{x|%H:%M:%S}<br>"
                f"{cb_title}: %{{z:+.4f}}<br>"
                "reason: %{customdata.reason}<br>"
                "<b>click to focus this tick</b>"
                "<extra></extra>"
            ),
            colorbar=dict(title=cb_title, thickness=10),
        )
    )
    # Mark the cells where the bandit picked this arm — small black dot
    # overlay so the "winning" cells stand out.
    pick_x = []
    pick_y = []
    for r, arm in enumerate(matrix):
        for atick in arm.ticks:
            if atick.bandit_selected:
                pick_x.append(atick.virtual_time)
                pick_y.append(arm.arm_id)
    if pick_x:
        fig.add_trace(
            go.Scatter(
                x=pick_x, y=pick_y, mode="markers",
                marker=dict(symbol="circle", size=8, color="black",
                            line=dict(color="white", width=1)),
                name="bandit-selected",
                hovertemplate="%{y}<br>%{x|%H:%M:%S}<br>chosen by bandit<extra></extra>",
                showlegend=False,
            )
        )
    # Focus row highlight (when a row was clicked previously)
    if focus_arm_id is not None and focus_arm_id in arm_labels:
        idx = arm_labels.index(focus_arm_id)
        fig.add_shape(
            type="rect",
            xref="paper", yref="y",
            x0=0, x1=1, y0=idx - 0.5, y1=idx + 0.5,
            line=dict(color="orange", width=2),
            fillcolor="rgba(0,0,0,0)",
        )

    fig.update_layout(
        height=max(220, 24 * len(arm_labels) + 80),
        margin=dict(l=10, r=10, t=10, b=20),
        showlegend=False,
        clickmode="event+select",
    )
    fig.update_xaxes(showspikes=False)
    fig.update_yaxes(autorange="reversed")  # first arm at the top
    return fig


def render_arm_heatmap(
    matrix: list[ArmHistory], all_ticks: list, *,
    key: str = "arm_heatmap",
    focus_arm_id: str | None = None,
    color_by: str = "posterior_mean",
) -> dict | None:
    """Render the per-arm heatmap and return Streamlit's selection event.

    The page layer reads ``selection.points`` to map clicks back to ticks
    (see ``_heatmap_click_to_tick`` in the page module). ``color_by`` selects
    which ArmTickState field drives the heat (see ``HEATMAP_COLOR_OPTIONS``).
    """
    if not matrix:
        st.info(
            "No arm has signalled yet in this run — heatmap is empty. "
            "Re-run after the funnel-log instrumentation if this is a stale run."
        )
        return None
    fig = _build_heatmap_figure(
        matrix, all_ticks, focus_arm_id=focus_arm_id, color_by=color_by,
    )
    return st.plotly_chart(
        fig, use_container_width=True, key=key,
        on_select="rerun", selection_mode=("points",),
    )


# ---------------------------------------------------------------------------
# 7. Diff strip (PR 5)
# ---------------------------------------------------------------------------

# Pct-change magnitude above which the diff strip flags a feature as
# "interesting" (visually emphasised). 1% is a sane default for intraday
# 3-min deltas — anything bigger has likely shifted the regime.
_INTERESTING_PCT_THRESHOLD = 0.01


def _format_delta_value(name: str, v_t2: float | None) -> str:
    """Format the new value for display — preserves Decimal precision for ₹."""
    if v_t2 is None:
        return "—"
    if "ltp" in name or "vwap" in name or "bid" in name or "ask" in name:
        return f"{v_t2:,.2f}"
    if "vol" in name or "bb_width" in name or "iv" in name:
        return f"{v_t2:.4f}"
    return f"{v_t2:,.2f}"


def _format_delta_change(delta: float | None, pct: float | None) -> str | None:
    """Format the delta + pct annotation under each metric."""
    if delta is None:
        return None
    if pct is None:
        return f"{delta:+.4f}"
    return f"{delta:+,.4f} ({pct * 100:+.2f}%)"


def render_diff_strip(diff: TickDiff | None) -> None:
    """Render the per-feature delta strip.

    One ``st.metric`` per diffable field; features with ``|pct_change|``
    above the threshold get an emoji marker so they pop visually. The
    ``vix_regime`` change (categorical) gets its own dedicated banner.
    """
    if diff is None:
        st.info("Pick a tick that isn't the very first of the session to see deltas.")
        return

    if diff.regime_change:
        st.warning(
            f"VIX regime flipped: `{diff.regime_change[0]}` → `{diff.regime_change[1]}`"
        )

    cols_per_row = 4
    rows = [diff.deltas[i:i + cols_per_row] for i in range(0, len(diff.deltas), cols_per_row)]
    for row in rows:
        cols = st.columns(len(row))
        for col, d in zip(cols, row):
            label = d.name
            if d.pct_change is not None and abs(d.pct_change) >= _INTERESTING_PCT_THRESHOLD:
                # Subtle visual cue — leading bullet
                label = f"• {label}"
            col.metric(
                label=label,
                value=_format_delta_value(d.name, d.value_t2),
                delta=_format_delta_change(d.delta, d.pct_change),
            )
