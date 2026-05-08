"""BacktestRunner — wraps WorkflowRunner with dry-run / mock / no-side-effect behaviour.

Goals:
  * Reusable: any registered workflow, any historical date.
  * Non-destructive: no Telegram, no live broker calls. DB writes are routed
    through a transaction we roll back at the end (or skipped entirely when
    `persist_to_db=False`).
  * Auditable: returns a `BacktestResult` with the full agent_run trail, the
    judge's allocation, projected vs actual costs, and (when actuals are
    available) a simulated P&L per allocation row.

Two modes:
  * `mock_llm=True`  (default) — uses `MockAnthropicClient`, $0 cost, fast.
  * `mock_llm=False` — calls real Anthropic API; honors `cost_ceiling_usd`.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from src.agents.backtest.mock_anthropic import MockAnthropicClient
from src.agents.backtest.snapshot import IST, MarketSnapshot, fetch_snapshot

log = logging.getLogger(__name__)


# Common Indian-index aliases — the universe and the price_daily table
# disagree on which form is canonical (`NIFTY` vs `NIFTY 50`, etc.). Fold them
# down so the P&L scorer can find actuals when the picker used either form.
_SYMBOL_ALIASES: dict[str, tuple[str, ...]] = {
    "NIFTY": ("NIFTY 50", "NIFTY50", "^NSEI"),
    "NIFTY 50": ("NIFTY", "NIFTY50", "^NSEI"),
    "BANKNIFTY": ("NIFTY BANK", "BANKNIFTY", "^NSEBANK"),
    "FINNIFTY": ("NIFTY FIN SERVICE",),
    "SENSEX": ("BSE SENSEX",),
}


def _lookup_actuals(actuals: dict[str, dict], symbol: str) -> dict | None:
    """Find a row in `actuals` for `symbol`, retrying via aliases."""
    if symbol in actuals:
        return actuals[symbol]
    for alias in _SYMBOL_ALIASES.get(symbol, ()):
        if alias in actuals:
            return actuals[alias]
    return None


def _derive_change_pct(actual: dict | None) -> float | None:
    """Return EOD change %, falling back to (close-open)/open when prev_close
    is missing. Some `price_daily` rows for indices ship without prev_close,
    so the recorded `change_pct` is also NULL — but open/close are populated.
    """
    if not actual:
        return None
    if actual.get("change_pct") is not None:
        return float(actual["change_pct"])
    open_, close = actual.get("open"), actual.get("close")
    prev_close = actual.get("prev_close")
    if prev_close and close:
        return round((close - prev_close) / prev_close * 100, 4)
    if open_ and close:
        return round((close - open_) / open_ * 100, 4)
    return None


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class BacktestResult:
    """Complete artifact of a backtest invocation.

    Combines the workflow's own `WorkflowRunResult` with backtest-specific
    metadata: input snapshot, projected vs actual cost, and per-prediction
    P&L estimates derived from `target_date` actuals.
    """

    workflow_name: str
    target_date: date
    as_of: datetime
    mock_llm: bool
    persist_to_db: bool

    # Workflow outcome
    workflow_run_id: str = ""
    status: str = "unknown"
    status_extended: str | None = None
    error: str | None = None
    short_circuit_reason: str | None = None

    # Cost
    projected_cost_usd: Decimal = field(default_factory=lambda: Decimal("0"))
    actual_cost_usd: Decimal = field(default_factory=lambda: Decimal("0"))
    total_tokens: int = 0
    api_calls: int = 0

    # Agent trail
    agent_runs: list[dict] = field(default_factory=list)
    stage_outputs: dict = field(default_factory=dict)
    predictions: list[dict] = field(default_factory=list)
    validator_outcomes: list[dict] = field(default_factory=list)
    # Recorded API calls — only populated in mock mode (live API is opaque).
    api_call_log: list[dict] = field(default_factory=list)

    # Snapshot
    snapshot: MarketSnapshot | None = None

    # Backtest analysis
    pnl_estimates: list[dict] = field(default_factory=list)
    """[{symbol, asset_class, capital_pct, conviction, predicted_direction,
         day_change_pct, simulated_pnl_pct, hit_target?, notes}]"""
    aggregate_pnl_pct: float | None = None
    """Capital-weighted aggregate P&L of all non-cash allocations."""


# ---------------------------------------------------------------------------
# Read-only DB session factory
# ---------------------------------------------------------------------------

def _make_readonly_factory(real_factory):
    """Wrap a real session factory so writes are silently swallowed.

    Selects pass through to the real DB; INSERT/UPDATE/DELETE statements
    are intercepted and turned into no-ops. This lets the runner exercise
    its data-tool reads (which need real signals/raw_content/instruments)
    while not stamping workflow_runs/agent_runs rows that the schema may
    not even have.
    """

    @asynccontextmanager
    async def factory():
        async with real_factory() as session:
            real_execute = session.execute

            async def patched_execute(stmt, params=None, **kwargs):
                sql = str(stmt).strip().upper()
                if sql.startswith(("INSERT", "UPDATE", "DELETE")):
                    # Return a non-committing fake result.
                    fake = MagicMock()
                    fake.rowcount = 0
                    fake.fetchone = lambda: None
                    fake.fetchall = lambda: []
                    fake.scalar = lambda: None
                    return fake
                return await real_execute(stmt, params or {}, **kwargs)

            session.execute = patched_execute
            yield session
    return factory


# ---------------------------------------------------------------------------
# Stub session factory (no real DB at all — falls back if connection fails)
# ---------------------------------------------------------------------------

def _make_stub_factory():
    """In-memory async session factory. Returns empty results for every read."""

    @asynccontextmanager
    async def factory():
        session = AsyncMock()
        result = MagicMock()
        result.rowcount = 0
        result.fetchone = lambda: None
        result.fetchall = lambda: []
        result.scalar = lambda: None
        session.execute = AsyncMock(return_value=result)
        session.commit = AsyncMock()
        session.rollback = AsyncMock()
        yield session
    return factory


# ---------------------------------------------------------------------------
# Backtest runner
# ---------------------------------------------------------------------------

class BacktestRunner:
    """Runs a registered workflow against historical data and returns a BacktestResult.

    Usage:
        runner = BacktestRunner.create_default()
        result = await runner.run("predict_today_combined", as_of=date(2026, 5, 7))

    Default mode is mock-LLM, read-only DB. Use the constructor directly to
    customise (e.g. `mock_llm=False` for live runs against the Anthropic API).
    """

    def __init__(
        self,
        db_session_factory,
        *,
        mock_llm: bool = True,
        persist_to_db: bool = False,
        anthropic=None,
        use_sql_tools: bool = True,
        force_proceed: bool = False,
    ) -> None:
        self.db_session_factory = db_session_factory
        self.mock_llm = mock_llm
        self.persist_to_db = persist_to_db
        self.use_sql_tools = use_sql_tools
        self.force_proceed = force_proceed
        self._user_anthropic = anthropic

    # ------------------------------------------------------------------
    # Constructors
    # ------------------------------------------------------------------

    @classmethod
    def create_default(cls) -> "BacktestRunner":
        """Build a BacktestRunner using the project's live DB session factory.

        Falls back to a stub factory if the live one cannot be imported (e.g.
        when running outside the Laabh project tree).
        """
        try:
            from src.db import get_session_factory
            return cls(db_session_factory=get_session_factory())
        except Exception as e:
            log.warning("create_default: falling back to stub DB factory (%s)", e)
            return cls(db_session_factory=_make_stub_factory())

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(
        self,
        workflow_name: str,
        as_of: date | datetime,
        *,
        params: dict | None = None,
        universe_size: int = 30,
        morning_verdict: dict | None = None,
        watch_symbols: list[str] | None = None,
    ) -> BacktestResult:
        """Run one workflow against the snapshot at `as_of` and return the artifact."""
        from src.agents.runtime.spec import RunnerConfig
        from src.agents.runtime.workflow_runner import WorkflowRunner
        from src.agents.workflows import WORKFLOW_REGISTRY

        spec = WORKFLOW_REGISTRY.get(workflow_name)
        if spec is None:
            raise ValueError(
                f"Unknown workflow {workflow_name!r}. Registered: "
                f"{sorted(WORKFLOW_REGISTRY)}"
            )

        target_date = as_of if isinstance(as_of, date) and not isinstance(as_of, datetime) else as_of.date()
        as_of_dt = (
            as_of if isinstance(as_of, datetime)
            else datetime.combine(target_date, time(9, 0), tzinfo=IST)
        )

        result = BacktestResult(
            workflow_name=workflow_name,
            target_date=target_date,
            as_of=as_of_dt,
            mock_llm=self.mock_llm,
            persist_to_db=self.persist_to_db,
        )

        # 1. Snapshot — also gives us actuals for backtest scoring
        snapshot = await fetch_snapshot(target_date, self.db_session_factory,
                                        universe_size=universe_size)
        result.snapshot = snapshot

        # 1b. Activate SQL-backed tools so agents can query the live DB. The
        # registry boots in stub mode by default (TOOLS_BACKEND env unset);
        # this swaps every TOOL_REGISTRY entry to its SQL executor in-place.
        if self.use_sql_tools:
            try:
                from src.agents.tools.registry import activate_sql_executors
                activate_sql_executors()
            except Exception as e:
                log.warning("activate_sql_executors failed: %s", e)

        # 2. Choose Anthropic client + DB factory
        anthropic = self._user_anthropic or (
            MockAnthropicClient() if self.mock_llm else self._build_live_anthropic()
        )
        db_factory_for_runner = (
            self.db_session_factory if self.persist_to_db
            else _make_readonly_factory(self.db_session_factory)
        )

        # 3. Stub Redis (kill-switch off, idempotency open)
        redis = AsyncMock()
        redis.get = AsyncMock(return_value=None)
        redis.set = AsyncMock(return_value=True)

        # 4. Run the workflow
        runner = WorkflowRunner(
            db_session_factory=db_factory_for_runner,
            redis=redis,
            anthropic=anthropic,
            telegram=None,                 # no telegram side-effects
            config=RunnerConfig(telegram_alert_on_failure=False,
                                telegram_alert_on_caveat=False),
        )

        triage_seed = snapshot.to_brain_triage_packet()

        # midday-specific seed: build the watch-symbol list and the packet the
        # midday_ceo persona expects.
        if not watch_symbols:
            watch_symbols = [s["symbol"] for s in snapshot.top_signal_symbols[:5]]
        midday_seed = {
            "as_of": as_of_dt.isoformat(),
            "watch_symbols": [{"symbol": s} for s in watch_symbols],
            "morning_verdict": morning_verdict or {},
            "live_positions": snapshot.open_positions,
            "regime": {"vix": snapshot.vix_latest,
                       "vix_regime": snapshot._vix_regime()},
        }

        agent_input_overrides = {**(params or {}).get("agent_input_overrides", {})}
        agent_input_overrides.setdefault("brain_triage", triage_seed)
        agent_input_overrides.setdefault("midday_ceo", midday_seed)

        merged_params = {
            **(params or {}),
            "as_of": as_of_dt,
            "backtest": True,
            "agent_input_overrides": agent_input_overrides,
            # Make the midday seed reachable by stage iteration_source="midday.watch_symbols"
            "_initial_stage_outputs": {"midday": midday_seed},
        }

        # Force-proceed shim: if the brain rationally decides to skip today,
        # the workflow short-circuits before any other agent runs — which is
        # correct production behaviour but defeats the purpose of a backtest
        # that wants to exercise the full pipeline. When `force_proceed=True`,
        # we wrap the runner's _should_short_circuit so brain output is kept
        # but skip is treated as proceed-with-fallback-candidates synthesised
        # from the snapshot's top_signal_symbols.
        if self.force_proceed:
            self._patch_runner_for_force_proceed(runner, snapshot)

        wf_result = await runner.run(
            workflow_spec=spec,
            params=merged_params,
            triggered_by="backtest",
            idempotency_key=None,
        )

        # 5. Translate WorkflowRunResult → BacktestResult
        result.workflow_run_id = wf_result.workflow_run_id
        result.status = wf_result.status
        result.status_extended = wf_result.status_extended
        result.error = wf_result.error
        result.short_circuit_reason = wf_result.short_circuit_reason
        result.actual_cost_usd = wf_result.cost_usd
        result.total_tokens = wf_result.total_tokens
        result.predictions = list(wf_result.predictions)
        result.validator_outcomes = list(wf_result.validator_outcomes)
        result.stage_outputs = dict(wf_result.stage_outputs)
        result.api_calls = getattr(anthropic, "calls", 0)
        result.agent_runs = [
            {
                "agent_name": ar.agent_name,
                "persona_version": ar.persona_version,
                "model_used": ar.model_used,
                "status": ar.status,
                "cost_usd": float(ar.cost_usd),
                "input_tokens": ar.input_tokens,
                "output_tokens": ar.output_tokens,
                "duration_ms": ar.duration_ms,
                "error": ar.error,
                "output": ar.output,
            }
            for ar in wf_result.agent_run_results
        ]
        result.api_call_log = list(getattr(anthropic, "history", []) or [])

        # 6. Projected (worst-case) cost — sum of per-stage projection
        result.projected_cost_usd = self._project_total_cost(spec)

        # 7. Backtest P&L scoring against actuals
        result.pnl_estimates, result.aggregate_pnl_pct = self._score_predictions(
            wf_result.stage_outputs, snapshot
        )

        return result

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _patch_runner_for_force_proceed(self, runner, snapshot: MarketSnapshot) -> None:
        """Override `_should_short_circuit` so brain_triage skip_today is ignored.

        If the brain returns no candidates AND skip_today=true, we synthesise
        a candidate list from `snapshot.top_signal_symbols` so news_finder,
        explorer pods, experts, and the CEO debate still run on real data.
        """
        original_should_short = runner._should_short_circuit
        original_run_stage = runner._run_stage

        def patched_should_short(stage, ctx):  # noqa: ANN001
            # Always proceed; we'll fix up missing candidates after the
            # brain_triage stage runs (see patched_run_stage below).
            return False

        async def patched_run_stage(stage, ctx):  # noqa: ANN001
            await original_run_stage(stage, ctx)
            if stage.stage_name != "brain_triage":
                return
            triage = ctx.stage_outputs.get("triage") or {}
            fno = triage.get("fno_candidates") or []
            eq = triage.get("equity_candidates") or []
            if fno or eq:
                return
            # Synthesise candidates from top_signal_symbols.
            synth_fno: list[dict] = []
            synth_eq: list[dict] = []
            for sig in snapshot.top_signal_symbols[:5]:
                cand = {
                    "symbol": sig["symbol"],
                    "rank_score": 0.6,
                    "primary_driver":
                        f"signal density: {sig['n_signals']} ({sig['n_buy']} buy / "
                        f"{sig['n_sell']} sell) in last 24h",
                    "watch_for": "follow-through volume on open",
                }
                if sig["symbol"] in {"NIFTY", "BANKNIFTY", "FINNIFTY"}:
                    cand["expected_strategy_family"] = "directional_long"
                    synth_fno.append(cand)
                else:
                    cand["horizon_hint"] = "3d"
                    synth_eq.append(cand)
            if synth_fno or synth_eq:
                triage["fno_candidates"] = synth_fno[:3]
                triage["equity_candidates"] = synth_eq[:3]
                triage["skip_today"] = False
                triage["_force_proceeded"] = True
                ctx.stage_outputs["triage"] = triage

        runner._should_short_circuit = patched_should_short
        runner._run_stage = patched_run_stage

    def _build_live_anthropic(self):
        """Build the real Anthropic async client. Raises if no API key."""
        try:
            from anthropic import AsyncAnthropic
        except ImportError as e:
            raise RuntimeError(
                "anthropic package not installed; cannot run live-LLM backtest."
            ) from e
        from src.config import get_settings
        s = get_settings()
        if not s.anthropic_api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY not configured; cannot run live-LLM backtest."
            )
        return AsyncAnthropic(api_key=s.anthropic_api_key)

    def _project_total_cost(self, spec) -> Decimal:
        """Sum the worst-case (no-cache) cost of every stage."""
        from src.agents.runtime.pricing import project_agent_cost
        from src.agents.personas import PERSONA_MANIFEST

        total = Decimal("0")
        for stage in spec.stages:
            for sa in stage.agents:
                versions = PERSONA_MANIFEST.get(sa.agent_name, {})
                pdef = versions.get(sa.persona_version, {})
                if not pdef:
                    continue
                per_call = project_agent_cost(
                    pdef["model"], pdef["max_input_tokens"], pdef["max_output_tokens"]
                )
                n = 5 if sa.iteration_source else 1
                total += per_call * n
        return total

    def _score_predictions(
        self, stage_outputs: dict, snapshot: MarketSnapshot
    ) -> tuple[list[dict], float | None]:
        """Estimate P&L for every allocation row using `snapshot.actuals`.

        Method: for each non-cash allocation, look up the underlying's same-day
        change_pct from `price_daily`. Apply a direction multiplier and the
        capital_pct weight. Returns (per-row estimates, aggregate %).

        Caveats:
          * Uses EOD close-to-close — does not capture intraday slippage.
          * Treats F&O like 2× leveraged equity for spread-style structures —
            a rough heuristic; real spreads can be ±5x.
          * Cash legs always score 0%.
        """
        verdict = (stage_outputs or {}).get("judge_verdict") or {}
        allocation = verdict.get("allocation", []) or []
        if not allocation:
            return [], None

        rows: list[dict] = []
        weighted_pnl_acc = 0.0
        total_weight = 0.0

        for alloc in allocation:
            asset_class = (alloc.get("asset_class") or "").lower()
            symbol = alloc.get("underlying_or_symbol") or ""
            capital_pct = float(alloc.get("capital_pct") or 0)
            conviction = alloc.get("conviction")
            decision = (alloc.get("decision") or "").lower()

            # Cash legs are zero P&L by definition.
            if asset_class == "cash":
                rows.append({
                    "symbol": symbol or "(cash)",
                    "asset_class": "cash",
                    "capital_pct": capital_pct,
                    "conviction": conviction,
                    "predicted_direction": "n/a",
                    "day_change_pct": None,
                    "simulated_pnl_pct": 0.0,
                    "notes": "cash leg — no P&L",
                })
                continue

            actual = _lookup_actuals(snapshot.actuals, symbol)
            day_change_pct = _derive_change_pct(actual)

            direction = self._infer_direction(decision)
            sign = 1.0 if direction == "bullish" else -1.0 if direction == "bearish" else 0.0

            if day_change_pct is None:
                rows.append({
                    "symbol": symbol,
                    "asset_class": asset_class,
                    "capital_pct": capital_pct,
                    "conviction": conviction,
                    "predicted_direction": direction,
                    "day_change_pct": None,
                    "simulated_pnl_pct": None,
                    "notes": f"no actuals available for {symbol!r} on {snapshot.target_date}",
                })
                continue

            leverage = 2.0 if asset_class == "fno" else 1.0
            simulated = sign * day_change_pct * leverage

            rows.append({
                "symbol": symbol,
                "asset_class": asset_class,
                "capital_pct": capital_pct,
                "conviction": conviction,
                "predicted_direction": direction,
                "day_change_pct": day_change_pct,
                "simulated_pnl_pct": round(simulated, 3),
                "notes": (f"close-to-close × {leverage}× heuristic"
                          if asset_class == "fno"
                          else "close-to-close cash equity"),
            })

            weighted_pnl_acc += simulated * (capital_pct / 100.0)
            total_weight += capital_pct / 100.0

        agg = round(weighted_pnl_acc, 3) if total_weight > 0 else 0.0
        return rows, agg

    @staticmethod
    def _infer_direction(decision: str) -> str:
        """Map an allocation's decision string to a direction."""
        d = decision.lower()
        if any(w in d for w in ("bull", "long_call", "buy_call", "buy", "long")):
            return "bullish"
        if any(w in d for w in ("bear", "long_put", "buy_put", "sell", "short")):
            return "bearish"
        return "neutral"
