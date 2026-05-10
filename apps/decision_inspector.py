"""Decision Inspector — Streamlit page.

PR 3 of the inspector spec. Ships the *interaction shell*:

  * Sidebar selectors: portfolio → run → focus symbol.
  * Run summary strip (NAV / P&L / trade count).
  * Plotly scrubber: focus-symbol price + VIX + trade markers + per-tick
    activity bars; click a tick to set the focused virtual_time.
  * Placeholder regions for PR 4 (Tick Inspector waterfall),
    PR 5 (per-arm heatmap + diff strip).

Everything below the scrubber renders an explicit "_PR 4 will fill this in_"
notice so the interaction model can be validated end-to-end before adding
content.

Launch:
    streamlit run apps/decision_inspector.py

Architecture note — async/sync bridge:
    The inspector readers in ``src.quant.inspector`` are async
    (asyncpg-backed). Streamlit is sync and re-runs the script top-to-bottom
    on every interaction. We bridge with ``asyncio.run`` per reader call,
    *and dispose the engine afterwards* — asyncpg pool connections are
    bound to the event loop that created them, and Streamlit reruns spin
    up new loops. Without disposal the next rerun would hit
    ``Event loop is closed`` errors. ~50 ms reconnect overhead per cache
    miss is acceptable for PR 3; PR 6 may add a long-lived loop in a
    background thread if it bottlenecks.
"""
from __future__ import annotations

import asyncio
import sys
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

# ``streamlit run apps/decision_inspector.py`` only adds ``apps/`` to
# sys.path. ``src/*`` is pip-installed (per pyproject.toml) so imports
# from src work, but ``apps`` is not a package — to import the sibling
# renderers module we prepend the project root before any project imports.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

from apps._inspector_renderers import (
    HEATMAP_COLOR_OPTIONS,
    render_arm_heatmap,
    render_bandit_tournament,
    render_diff_strip,
    render_feature_bundle,
    render_outcome_banner,
    render_primitive_cards,
    render_sizer_cascade,
)
from src.quant.inspector import (
    ArmHistory,
    RunMetadata,
    SessionSkeleton,
    TickDiff,
    TickState,
    UnderlyingTimeline,
    list_runs,
    load_arm_matrix,
    load_session_skeleton,
    load_tick_diff,
    load_tick_state,
    load_underlying_timeline,
)


# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Decision Inspector",
    page_icon=None,
    layout="wide",
    initial_sidebar_state="expanded",
)


# ---------------------------------------------------------------------------
# Async/sync bridge — long-lived background event loop
# ---------------------------------------------------------------------------
#
# Earlier iterations used ``asyncio.run(coro)`` per call + ``dispose_engine``
# to flush the asyncpg pool between calls. That breaks on Windows under the
# Proactor event loop: asyncpg connections own a Proactor socket bound to
# the loop that created them, and calling ``dispose`` from a *new* loop
# leaks a "send on closed loop" error on the very next reader call.
#
# The robust pattern: run a single event loop in a background thread for
# the lifetime of the Streamlit process. Coroutines submitted from the
# main thread share that loop, so the engine + pool stay valid forever.
# Streamlit's ``@st.cache_data`` already prevents re-issuing identical
# queries, so the pool sees only real work.
# ---------------------------------------------------------------------------

_BG_LOOP: asyncio.AbstractEventLoop | None = None
_BG_THREAD: threading.Thread | None = None
_BG_LOCK = threading.Lock()


def _ensure_bg_loop() -> asyncio.AbstractEventLoop:
    """Return the long-lived background event loop, starting it on first use.

    On first creation we also wipe ``src.db``'s engine + session-factory
    references. Earlier attempts (or interpreter state from a previous
    Streamlit run within the same process) may have created an engine on
    a now-dead event loop; asyncpg connections in that pool would still
    be bound to the dead loop's protocol, and trying to use them on the
    bg loop produces the "Future attached to a different loop" error.
    Dropping the references lets ``get_engine()`` build a fresh engine on
    the bg loop the first time it's asked. The orphaned asyncpg sockets
    GC away (we can't ``await dispose()`` them — the loop they need is
    already gone).
    """
    global _BG_LOOP, _BG_THREAD
    with _BG_LOCK:
        if _BG_LOOP is not None and not _BG_LOOP.is_closed():
            return _BG_LOOP

        from src import db as _db
        _db._engine = None
        _db._session_factory = None

        loop = asyncio.new_event_loop()

        def _run_forever() -> None:
            asyncio.set_event_loop(loop)
            try:
                loop.run_forever()
            finally:
                loop.close()

        thread = threading.Thread(
            target=_run_forever, name="laabh-inspector-loop", daemon=True,
        )
        thread.start()
        _BG_LOOP = loop
        _BG_THREAD = thread
        return loop


def _run(coro):
    """Run an async coroutine on the background loop, block until done.

    Streamlit's main thread is sync; ``run_coroutine_threadsafe`` hands the
    coroutine off to the background loop and ``.result()`` blocks until
    completion. Errors propagate naturally.
    """
    loop = _ensure_bg_loop()
    fut = asyncio.run_coroutine_threadsafe(coro, loop)
    return fut.result(timeout=60)


# ---------------------------------------------------------------------------
# DB read helpers (cached). Each takes only str/uuid args so Streamlit's
# cache key is stable across reruns.
# ---------------------------------------------------------------------------

@st.cache_data(ttl=10, show_spinner=False)
def _list_portfolios_with_runs() -> list[dict]:
    """Distinct portfolio_ids that have any backtest_runs.

    Tiny query — used to populate the sidebar's portfolio dropdown. We hit
    the DB directly rather than going through the inspector reader because
    the reader API is keyed on a known portfolio_id; this helper bootstraps
    that.
    """
    import psycopg2
    import os

    dsn = {
        "host": os.environ.get("PGHOST", "localhost"),
        "database": os.environ.get("PGDATABASE", "laabh"),
        "user": os.environ.get("PGUSER", "postgres"),
        "password": os.environ.get("PGPASSWORD", "Ashu@007saxe"),
    }
    with psycopg2.connect(**dsn) as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT br.portfolio_id, p.name
            FROM backtest_runs br
            JOIN portfolios p ON p.id = br.portfolio_id
            ORDER BY p.name
        """)
        return [
            {"id": str(row[0]), "name": row[1]}
            for row in cur.fetchall()
        ]


@st.cache_data(ttl=15, show_spinner="Loading runs…")
def _cached_list_runs(portfolio_id: str, limit: int = 50) -> list[RunMetadata]:
    return _run(list_runs(uuid.UUID(portfolio_id), limit=limit))


@st.cache_data(ttl=60, show_spinner="Loading session…")
def _cached_skeleton(run_id: str) -> SessionSkeleton | None:
    return _run(load_session_skeleton(uuid.UUID(run_id)))


@st.cache_data(ttl=60, show_spinner="Loading underlying timeline…")
def _cached_timeline(run_id: str, symbol: str) -> UnderlyingTimeline | None:
    return _run(load_underlying_timeline(uuid.UUID(run_id), symbol))


@st.cache_data(ttl=30, show_spinner="Loading focused tick state…")
def _cached_focused_tick_state(
    run_id: str, virtual_time_iso: str, symbol: str
) -> TickState | None:
    """Symbol-filtered tick state — populates inputs panel + primitive cards.

    The signal-log query is restricted to the focus symbol so primitive
    cards only show *this* symbol's primitives. The FeatureBundle is
    recomputed for the focus underlying.
    """
    return _run(
        load_tick_state(
            uuid.UUID(run_id),
            datetime.fromisoformat(virtual_time_iso),
            symbol=symbol,
        )
    )


@st.cache_data(ttl=60, show_spinner="Loading arm matrix…")
def _cached_arm_matrix(run_id: str) -> list[ArmHistory]:
    """All signalling arms × all ticks for the heatmap."""
    return _run(load_arm_matrix(uuid.UUID(run_id)))


@st.cache_data(ttl=30, show_spinner="Loading diff…")
def _cached_tick_diff(
    run_id: str, t1_iso: str, t2_iso: str, symbol: str
) -> TickDiff | None:
    return _run(
        load_tick_diff(
            uuid.UUID(run_id),
            datetime.fromisoformat(t1_iso),
            datetime.fromisoformat(t2_iso),
            symbol,
        )
    )


@st.cache_data(ttl=30, show_spinner="Loading full tick state…")
def _cached_full_tick_state(
    run_id: str, virtual_time_iso: str
) -> TickState | None:
    """Cross-symbol tick state — populates bandit tournament + sizer cascade.

    The bandit picks ONE arm per tick across ALL symbols. A symbol-filtered
    tournament would hide the actual decision when the chosen arm came
    from a different symbol — so we load the full tick (symbol=None) for
    the tournament + sizer + outcome views.
    """
    return _run(
        load_tick_state(
            uuid.UUID(run_id),
            datetime.fromisoformat(virtual_time_iso),
            symbol=None,
        )
    )


# ---------------------------------------------------------------------------
# Session-state init — Streamlit reruns mean we must defensively populate
# selection state on first paint so downstream widgets find their keys.
# ---------------------------------------------------------------------------

def _init_state() -> None:
    """Defensive defaults so the first rerun doesn't read missing keys."""
    st.session_state.setdefault("selected_run_id", None)
    st.session_state.setdefault("selected_symbol", None)
    st.session_state.setdefault("selected_virtual_time", None)
    # PR 6: aggregation + heatmap-color preferences persist across reruns.
    st.session_state.setdefault("aggregation_minutes", 3)
    st.session_state.setdefault("heatmap_color_by", "posterior_mean")


# ---------------------------------------------------------------------------
# Sidebar — portfolio / run / symbol selectors
# ---------------------------------------------------------------------------

def _render_sidebar() -> tuple[str | None, RunMetadata | None, str | None]:
    """Render the sidebar selectors and return (portfolio_id, run, symbol)."""
    st.sidebar.title("Decision Inspector")
    st.sidebar.caption("Backtest replay — pick a run, focus a symbol, click a tick.")

    portfolios = _list_portfolios_with_runs()
    if not portfolios:
        st.sidebar.warning("No backtest runs found in DB.")
        return None, None, None

    pf_labels = [f"{p['name']} ({p['id'][:8]}…)" for p in portfolios]
    pf_idx = st.sidebar.selectbox(
        "Portfolio",
        options=range(len(portfolios)),
        format_func=lambda i: pf_labels[i],
        key="portfolio_idx",
    )
    portfolio_id = portfolios[pf_idx]["id"]

    runs = _cached_list_runs(portfolio_id)
    if not runs:
        st.sidebar.warning("No runs for this portfolio.")
        return portfolio_id, None, None

    run_labels = [
        f"{r.backtest_date}  ·  P&L {(r.pnl_pct or 0) * 100:+.2f}%  ·  {r.trade_count or 0} trades"
        for r in runs
    ]
    run_idx = st.sidebar.selectbox(
        "Run",
        options=range(len(runs)),
        format_func=lambda i: run_labels[i],
        key="run_idx",
    )
    run = runs[run_idx]

    # Symbol picker reads from the chosen run's universe. Old runs whose
    # ``backtest_runs.universe`` JSONB is empty are transparently
    # backfilled from the signal log by ``load_session_skeleton``; only
    # *truly* empty runs (no JSONB universe AND no signal log) hit the
    # placeholder branch below — the main page renders the stale-run
    # banner in that case.
    skel = _cached_skeleton(str(run.run_id))
    if skel is None:
        st.sidebar.error("Could not load this run.")
        return portfolio_id, None, None
    if not skel.universe:
        st.sidebar.warning(
            "This run has no signal data — see the banner on the main page."
        )
        # Return a non-None ``symbol`` so the page renders past its
        # "pick a symbol" guard and shows the stale banner. The symbol
        # value is harmless because the universe-empty path doesn't drive
        # any data fetches downstream.
        return portfolio_id, run, "—"

    sym_options = [u.symbol for u in skel.universe]
    sym_idx = st.sidebar.selectbox(
        "Focus symbol",
        options=range(len(sym_options)),
        format_func=lambda i: sym_options[i],
        key="symbol_idx",
    )
    symbol = sym_options[sym_idx]

    st.sidebar.divider()

    # PR 6 — display preferences. Stored in session_state so they survive
    # tick navigation reruns; the widget ``key`` does the binding for us.
    agg_keys = list(AGGREGATION_OPTIONS.keys())
    agg_idx = st.sidebar.selectbox(
        "Tick granularity",
        options=range(len(agg_keys)),
        format_func=lambda i: AGGREGATION_OPTIONS[agg_keys[i]],
        index=agg_keys.index(st.session_state.aggregation_minutes),
        key="aggregation_idx",
        help="Coarsens the scrubber's activity bars. Click resolution snaps "
             "to the first underlying 3-min tick in the bucket.",
    )
    st.session_state.aggregation_minutes = agg_keys[agg_idx]

    color_keys = list(HEATMAP_COLOR_OPTIONS.keys())
    color_idx = st.sidebar.selectbox(
        "Heatmap colour",
        options=range(len(color_keys)),
        format_func=lambda i: HEATMAP_COLOR_OPTIONS[color_keys[i]],
        index=color_keys.index(st.session_state.heatmap_color_by),
        key="heatmap_color_idx",
        help="Field that drives the per-arm heatmap colour. Bandit-only "
             "fields (sampled / signal_strength) fall back to primitive "
             "strength when the arm didn't reach the bandit.",
    )
    st.session_state.heatmap_color_by = color_keys[color_idx]

    st.sidebar.divider()
    if st.sidebar.button("Reset focus tick"):
        st.session_state.selected_virtual_time = None

    return portfolio_id, run, symbol


# ---------------------------------------------------------------------------
# Top summary strip
# ---------------------------------------------------------------------------

def _render_summary(skel: SessionSkeleton) -> None:
    md = skel.metadata
    cols = st.columns(5)
    cols[0].metric("Date", str(md.backtest_date))
    cols[1].metric("Starting NAV", f"₹{md.starting_nav:,.0f}")
    cols[2].metric(
        "Final NAV",
        f"₹{md.final_nav:,.0f}" if md.final_nav is not None else "—",
        delta=(
            f"{(md.pnl_pct or 0) * 100:+.2f}%"
            if md.pnl_pct is not None else None
        ),
    )
    # Trust the trades list over ``md.trade_count`` — older runs (pre-PR 6
    # recorder fix) have ``trade_count = 0`` even when trades exist; the
    # list is always derived from the live ``backtest_trades`` table.
    cols[3].metric("Trades", len(skel.trades))
    cols[4].metric("Universe", len(skel.universe))


# ---------------------------------------------------------------------------
# Scrubber — Plotly figure with click-to-focus
# ---------------------------------------------------------------------------

def _build_scrubber(
    *, skel: SessionSkeleton, timeline: UnderlyingTimeline, symbol: str
) -> go.Figure:
    """Two-row Plotly figure: price (with trade markers) + VIX.

    A third invisible scatter is the click target — its x-values are the
    orchestrator tick timestamps, so clicks snap to actual ticks rather
    than arbitrary points on the price line.
    """
    fig = make_subplots(
        rows=2, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.05,
        row_heights=[0.7, 0.3],
        subplot_titles=(f"{symbol} — price", "VIX"),
    )

    # --- Row 1: price line + per-tick activity bars + trade markers ---
    if timeline.bars:
        fig.add_trace(
            go.Scatter(
                x=[b.timestamp for b in timeline.bars],
                y=[b.close for b in timeline.bars],
                mode="lines",
                name=f"{symbol} close",
                line=dict(color="#1f77b4", width=1.5),
                hovertemplate="%{x|%H:%M}<br>₹%{y:,.2f}<extra></extra>",
            ),
            row=1, col=1,
        )

    # Trade entry / exit markers — one symbol of focus may appear in many
    # arms (different primitives), so we pick trades whose underlying_id
    # matches one of this symbol's universe entries.
    focus_underlying_ids = {u.instrument_id for u in skel.universe if u.symbol == symbol}
    sym_trades = [t for t in skel.trades if t.underlying_id in focus_underlying_ids]
    if sym_trades and timeline.bars:
        # Anchor markers to the price line by interpolating the close
        # closest to entry/exit times.
        bars_by_ts = {b.timestamp: b.close for b in timeline.bars}

        def _close_at(ts: datetime) -> float | None:
            # Snap to the nearest tick at-or-before ts
            candidates = [b for b in timeline.bars if b.timestamp <= ts]
            return candidates[-1].close if candidates else None

        entries_x = [t.entry_at for t in sym_trades]
        entries_y = [_close_at(t.entry_at) for t in sym_trades]
        fig.add_trace(
            go.Scatter(
                x=entries_x, y=entries_y, mode="markers",
                name="entry",
                marker=dict(symbol="triangle-up", size=12, color="#2ca02c"),
                hovertext=[f"entry · {t.arm_id} · {t.lots}lots" for t in sym_trades],
                hovertemplate="%{hovertext}<extra></extra>",
            ),
            row=1, col=1,
        )
        exits_with_time = [t for t in sym_trades if t.exit_at is not None]
        if exits_with_time:
            exit_x = [t.exit_at for t in exits_with_time]
            exit_y = [_close_at(t.exit_at) for t in exits_with_time]
            fig.add_trace(
                go.Scatter(
                    x=exit_x, y=exit_y, mode="markers",
                    name="exit",
                    marker=dict(symbol="triangle-down", size=12, color="#d62728"),
                    hovertext=[
                        f"exit · {t.arm_id} · P&L ₹{(t.realized_pnl or 0):+.0f}"
                        for t in exits_with_time
                    ],
                    hovertemplate="%{hovertext}<extra></extra>",
                ),
                row=1, col=1,
            )

    # --- Row 2: VIX line ---
    if timeline.vix:
        fig.add_trace(
            go.Scatter(
                x=[v.timestamp for v in timeline.vix],
                y=[v.value for v in timeline.vix],
                mode="lines+markers",
                name="VIX",
                line=dict(color="#9467bd", width=1.5),
                marker=dict(size=4),
                hovertemplate="%{x|%H:%M}<br>%{y:.2f}<extra></extra>",
            ),
            row=2, col=1,
        )

    # --- Click target: invisible bars at every tick, with hover info ---
    # The user clicks anywhere along the bar (full price-axis height) to
    # snap focus to that tick. ``customdata`` carries the ISO timestamp so
    # the click handler can read it back without parsing the x-axis.
    if skel.ticks and timeline.bars:
        # Bar height = max price observed today (covers the whole price axis)
        max_price = max(b.high for b in timeline.bars)
        min_price = min(b.low for b in timeline.bars)
        height = max_price - min_price if max_price > min_price else 1.0

        # PR 6: aggregate scrubber bars to the configured granularity.
        # Each bar's customdata still carries the ISO of an actual 3-min
        # tick (the bucket's first member) so click-to-focus works.
        agg_minutes = st.session_state.get("aggregation_minutes", 3)
        buckets, click_map = _aggregate_ticks(skel.ticks, minutes=agg_minutes)
        bar_width_ms = max(120_000, agg_minutes * 60 * 1000 - 30_000)

        fig.add_trace(
            go.Bar(
                x=[b.virtual_time for b in buckets],
                y=[height] * len(buckets),
                base=min_price,
                width=bar_width_ms,
                marker=dict(
                    color=[b.n_signals_strong for b in buckets],
                    colorscale="Blues",
                    opacity=0.18,
                    line=dict(width=0),
                ),
                customdata=[
                    [
                        click_map.get(b.virtual_time, b.virtual_time).isoformat(),
                        b.n_signals_strong,
                        b.n_opened,
                    ]
                    for b in buckets
                ],
                hovertemplate=(
                    "%{customdata[0]}<br>"
                    "strong signals: %{customdata[1]}<br>"
                    "opened: %{customdata[2]}<br>"
                    "<b>click to focus this tick</b>"
                    "<extra></extra>"
                ),
                name="tick activity",
                showlegend=False,
            ),
            row=1, col=1,
        )

    fig.update_layout(
        height=520,
        margin=dict(l=40, r=20, t=40, b=20),
        hovermode="closest",
        showlegend=True,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0.5, xanchor="center"),
        clickmode="event+select",
    )
    fig.update_xaxes(showspikes=True, spikemode="across", spikesnap="cursor")
    return fig


def _heatmap_click_to_tick(event: Any, all_ticks: list) -> datetime | None:
    """Resolve a heatmap cell click back to a known ``virtual_time``.

    The Heatmap trace's ``customdata`` carries ``{arm, time, reason, ...}``
    for each cell; the Scatter overlay (bandit-selected markers) carries
    the same ``time`` as ``x``. We look in both. Snaps to the closest
    skeleton tick within 90 s — same tolerance as the scrubber.
    """
    if not event:
        return None
    points = (event.get("selection") or {}).get("points") or []
    for p in points:
        clicked: datetime | None = None
        cd = p.get("customdata")
        if isinstance(cd, dict) and "time" in cd:
            try:
                clicked = datetime.fromisoformat(str(cd["time"]))
            except (ValueError, TypeError):
                clicked = None
        # Scatter overlay has ``x`` as a datetime/ISO string instead
        if clicked is None:
            x_val = p.get("x")
            if isinstance(x_val, str):
                try:
                    clicked = datetime.fromisoformat(x_val)
                except ValueError:
                    clicked = None
            elif isinstance(x_val, datetime):
                clicked = x_val
        if clicked is None:
            continue
        best = min(all_ticks, key=lambda t: abs(t.virtual_time - clicked))
        if abs(best.virtual_time - clicked) <= timedelta(seconds=90):
            return best.virtual_time
    return None


def _previous_tick(all_ticks: list, virtual_time: datetime) -> datetime | None:
    """Return the tick immediately before ``virtual_time`` in the skeleton.

    Used by the diff strip: T-1 vs T. Returns None when ``virtual_time``
    is the first tick of the session (no T-1 to diff against).
    """
    earlier = [t.virtual_time for t in all_ticks if t.virtual_time < virtual_time]
    return earlier[-1] if earlier else None


# ---------------------------------------------------------------------------
# Aggregation toggle (PR 6) — coarsens scrubber bars from 3-min to 6/15/60.
# ---------------------------------------------------------------------------

# Closed set the sidebar surfaces. Keys are minutes; values are display labels.
AGGREGATION_OPTIONS: dict[int, str] = {
    3:  "3 min (every tick)",
    6:  "6 min",
    15: "15 min",
    60: "60 min",
}


def _aggregate_ticks(
    ticks: list, *, minutes: int,
) -> tuple[list, dict[datetime, datetime]]:
    """Group ``TickSummary`` entries into ``minutes``-wide buckets.

    Returns ``(buckets, click_map)`` where:
      * ``buckets`` is a list of bucket-aggregate ``SimpleNamespace`` objects
        with the same field names as ``TickSummary`` plus ``virtual_time``
        set to the bucket start. Counts (``n_signals_total`` etc.) are
        summed across the constituent ticks.
      * ``click_map`` maps each bucket's ``virtual_time`` → the *first*
        underlying tick's ``virtual_time`` so click-to-focus snaps to a
        real tick (renderable by the inspector), not a synthetic bucket
        boundary.

    When ``minutes`` is 3 (no aggregation) returns the original ticks
    unchanged with an identity click_map.
    """
    if minutes <= 3 or not ticks:
        return ticks, {t.virtual_time: t.virtual_time for t in ticks}

    bucket_seconds = minutes * 60
    base = ticks[0].virtual_time

    def _bucket_start(ts: datetime) -> datetime:
        # Floor to a multiple of ``bucket_seconds`` from the session's first
        # tick, so 6-min buckets always pair adjacent 3-min ticks regardless
        # of whether the session opens at :15 or :00.
        offset = int((ts - base).total_seconds() // bucket_seconds) * bucket_seconds
        return base + timedelta(seconds=offset)

    grouped: dict[datetime, list] = {}
    for t in ticks:
        grouped.setdefault(_bucket_start(t.virtual_time), []).append(t)

    from types import SimpleNamespace
    buckets = []
    click_map: dict[datetime, datetime] = {}
    summable_fields = (
        "n_signals_total", "n_signals_strong", "n_opened",
        "n_lost_bandit", "n_sized_zero", "n_cooloff",
        "n_kill_switch", "n_capacity_full", "n_warmup",
    )
    for start in sorted(grouped):
        members = grouped[start]
        agg = SimpleNamespace(virtual_time=start)
        for f in summable_fields:
            setattr(agg, f, sum(getattr(m, f, 0) for m in members))
        buckets.append(agg)
        click_map[start] = members[0].virtual_time
    return buckets, click_map


def _selected_tick_from_event(event: Any, ticks: list) -> datetime | None:
    """Resolve the clicked Plotly point back to a ``virtual_time``.

    Streamlit ``on_select="rerun"`` returns a dict-shaped event whose
    ``selection.points`` is a list of clicked points. Each point includes
    a ``customdata`` array (when present on the trace) — the first element
    of the activity-bar trace's customdata is the ISO timestamp.

    Snaps to the nearest known ``virtual_time`` so floating-point
    discrepancies between Plotly's x-axis encoding and our datetime values
    don't lose the click.
    """
    if not event:
        return None
    points = (event.get("selection") or {}).get("points") or []
    for p in points:
        cd = p.get("customdata")
        if cd and isinstance(cd, (list, tuple)) and len(cd) >= 1:
            try:
                clicked = datetime.fromisoformat(str(cd[0]))
            except (ValueError, TypeError):
                continue
            # Snap to the closest known tick (Plotly may round microseconds)
            best = min(ticks, key=lambda t: abs(t.virtual_time - clicked))
            if abs(best.virtual_time - clicked) <= timedelta(seconds=90):
                return best.virtual_time
    return None


def _render_scrubber(
    *, skel: SessionSkeleton, timeline: UnderlyingTimeline, symbol: str
) -> None:
    fig = _build_scrubber(skel=skel, timeline=timeline, symbol=symbol)
    event = st.plotly_chart(
        fig,
        use_container_width=True,
        key="scrubber",
        on_select="rerun",
        selection_mode=("points", "box"),
    )
    new_focus = _selected_tick_from_event(event, skel.ticks)
    if new_focus is not None and new_focus != st.session_state.selected_virtual_time:
        st.session_state.selected_virtual_time = new_focus
        st.rerun()
    # Diagnostic: when the scrubber click *does* fire but our handler can't
    # parse it (Bar/Heatmap selection events are flaky in some Plotly
    # versions), the user can open this expander to see what came through.
    # The slider in _render_selection works regardless.
    if event:
        with st.expander("🔧 Click event debug (advanced)", expanded=False):
            st.write(event)


# ---------------------------------------------------------------------------
# Selected-tick read-out + placeholder regions
# ---------------------------------------------------------------------------

def _default_focused_tick(skel: SessionSkeleton) -> datetime | None:
    """Pick a sensible default focus tick — first tick with an opened trade,
    falling back to first tick with strong signals, falling back to first tick.

    Empty skeletons → None (inspector hidden).
    """
    if not skel.ticks:
        return None
    for t in skel.ticks:
        if t.n_opened > 0:
            return t.virtual_time
    for t in skel.ticks:
        if t.n_signals_strong > 0:
            return t.virtual_time
    return skel.ticks[0].virtual_time


def _render_selection(skel: SessionSkeleton) -> None:
    """Slider-based tick navigator + summary metrics.

    The slider is the *primary* tick selector. Clicks on the scrubber bars
    or heatmap cells also update ``selected_virtual_time``, but those rely
    on Plotly's selection-event wiring which can be flaky for Bar/Heatmap
    traces — the slider always works.
    """
    if not skel.ticks:
        return
    times = [t.virtual_time for t in skel.ticks]
    labels = [t.virtual_time.strftime("%H:%M:%S") for t in skel.ticks]

    # Default to first interesting tick on initial render so the inspector
    # has *something* to show before the user touches anything.
    if st.session_state.selected_virtual_time is None:
        st.session_state.selected_virtual_time = _default_focused_tick(skel)

    current = st.session_state.selected_virtual_time
    default_idx = times.index(current) if current in times else 0

    chosen_idx = st.select_slider(
        "Focused tick (or click a bar above / heatmap cell)",
        options=list(range(len(times))),
        format_func=lambda i: labels[i],
        value=default_idx,
        key=f"tick_slider_{id(skel)}",  # remount on run change
    )
    new_sel = times[chosen_idx]
    if new_sel != st.session_state.selected_virtual_time:
        st.session_state.selected_virtual_time = new_sel

    sel = st.session_state.selected_virtual_time
    summary = next((t for t in skel.ticks if t.virtual_time == sel), None)
    cols = st.columns(5)
    cols[0].metric("Focused tick", sel.strftime("%H:%M:%S"))
    if summary is not None:
        cols[1].metric("Signals (total)", summary.n_signals_total)
        cols[2].metric("Signals (strong)", summary.n_signals_strong)
        cols[3].metric("Opened", summary.n_opened)
        cols[4].metric("Lost bandit", summary.n_lost_bandit)


def _render_tick_inspector(
    *, run_id: str, symbol: str, virtual_time: datetime, skel: SessionSkeleton,
) -> None:
    """Tick Inspector waterfall — the depth view (PR 4 + heatmap/diff PR 5).

    Loads two tick states (focused + full-tick) plus the arm matrix and
    diff against the prior tick. Heatmap cell click re-focuses the
    selected ``virtual_time`` — alternative navigation to the scrubber.
    """
    iso = virtual_time.isoformat()
    focused = _cached_focused_tick_state(run_id, iso, symbol)
    full = _cached_full_tick_state(run_id, iso)
    if focused is None or full is None:
        st.error("Could not load this tick — run or virtual_time invalid.")
        return

    # Outcome banner reflects the *actual* decision (full tick), not the
    # symbol-filtered slice. If the focused symbol's primitives lost to
    # an arm on another symbol, the banner says so.
    render_outcome_banner(full)

    left, right = st.columns([2, 3])
    with left:
        st.subheader(f"Inputs — feature bundle for `{symbol}`")
        render_feature_bundle(focused.feature_bundle)
    with right:
        st.subheader("Per-arm heatmap")
        color_by = st.session_state.get("heatmap_color_by", "posterior_mean")
        st.caption(
            f"Cells coloured by {HEATMAP_COLOR_OPTIONS.get(color_by, color_by)}. "
            f"Click a cell to focus that tick."
        )
        matrix = _cached_arm_matrix(run_id)
        hm_event = render_arm_heatmap(matrix, skel.ticks, color_by=color_by)
        new_focus = _heatmap_click_to_tick(hm_event, skel.ticks)
        if new_focus is not None and new_focus != st.session_state.selected_virtual_time:
            st.session_state.selected_virtual_time = new_focus
            st.rerun()

    st.divider()
    st.subheader(f"Primitive signals on `{symbol}`")
    render_primitive_cards(focused.primitive_signals)

    st.divider()
    st.subheader("Bandit tournament — every arm that competed this tick")
    render_bandit_tournament(full.bandit_tournament)

    st.divider()
    st.subheader("Sizer cascade")
    if full.chosen_arm_id:
        st.caption(f"Cascade ran on `{full.chosen_arm_id}` (the bandit's pick).")
    render_sizer_cascade(full.sizer_outcome)

    st.divider()
    st.subheader(f"Diff strip — `{symbol}` features changed vs prior tick")
    prev = _previous_tick(skel.ticks, virtual_time)
    if prev is None:
        st.info("This is the first tick of the session — no prior tick to diff against.")
    else:
        st.caption(
            f"Comparing {prev.strftime('%H:%M:%S')} → {virtual_time.strftime('%H:%M:%S')}"
        )
        diff = _cached_tick_diff(run_id, prev.isoformat(), iso, symbol)
        render_diff_strip(diff)


def _render_placeholders() -> None:
    """Pre-focus state — shown until the user clicks a tick on the scrubber."""
    left, right = st.columns([2, 1])
    with left:
        st.subheader("Tick Inspector")
        st.caption("_Click a tick on the scrubber above to focus the inspector._")
    with right:
        st.subheader("Per-arm heatmap")
        st.caption("_PR 5 fills this in: row per arm × column per tick._")
    st.divider()
    st.subheader("Diff strip — what changed vs prior tick")
    st.caption("_PR 5 fills this in._")


# ---------------------------------------------------------------------------
# Main page
# ---------------------------------------------------------------------------

def main() -> None:
    _init_state()
    portfolio_id, run, symbol = _render_sidebar()
    if portfolio_id is None or run is None or symbol is None:
        st.title("Decision Inspector")
        st.write("Pick a portfolio + run + symbol from the sidebar to begin.")
        return

    # Reset per-run state when the user picks a different run
    if st.session_state.selected_run_id != str(run.run_id):
        st.session_state.selected_run_id = str(run.run_id)
        st.session_state.selected_virtual_time = None
    if st.session_state.selected_symbol != symbol:
        st.session_state.selected_symbol = symbol
        # Symbol switch keeps the focus tick — same time, different symbol
        # is a meaningful operation (compare two underlyings at one moment).

    skel = _cached_skeleton(str(run.run_id))
    timeline = _cached_timeline(str(run.run_id), symbol)
    if skel is None:
        st.error("Could not load this run.")
        return

    st.title("Decision Inspector")
    _render_summary(skel)

    # Edge case — runs created before PR 1's signal-log instrumentation
    # have no funnel data; the entire inspector is hollow without it.
    # Surface this prominently rather than letting the user click around
    # a series of "no data" placeholders.
    if not skel.ticks:
        st.warning(
            "**This run has no signal-log rows** — it predates the funnel-log "
            "instrumentation (PR 1) or never executed. The scrubber and "
            "inspector will be empty. Re-run the backtest to populate:\n\n"
            f"```\npython -m scripts.backtest_run "
            f"--start-date {run.backtest_date} --end-date {run.backtest_date} "
            f"--portfolio-id {run.portfolio_id}\n```"
        )

    st.divider()

    if timeline is None or not timeline.bars:
        st.warning(f"No intraday data for {symbol} on {run.backtest_date}.")
    else:
        _render_scrubber(skel=skel, timeline=timeline, symbol=symbol)
    st.divider()

    _render_selection(skel)
    st.divider()

    sel = st.session_state.selected_virtual_time
    # The slider in _render_selection populates a default focus on first
    # render. The only case sel stays None is "skel.ticks is empty" — for
    # those runs the stale-run banner above already explains.
    if sel is None or not skel.ticks:
        _render_placeholders()
    else:
        _render_tick_inspector(
            run_id=str(run.run_id), symbol=symbol, virtual_time=sel, skel=skel,
        )


# ``streamlit run`` executes the script with ``__name__ == "__main__"`` —
# so this is sufficient. Tests import the module to exercise helpers
# without triggering main() (which would need a live Streamlit runtime).
if __name__ == "__main__":
    main()

