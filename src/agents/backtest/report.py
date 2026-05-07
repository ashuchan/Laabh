"""Backtest report renderer — markdown + console summary.

`render_backtest_report(result)` returns a multi-section markdown string. The
sections are stable across runs so two reports can be diffed.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any

from src.agents.backtest.runner import BacktestResult


def render_backtest_report(result: BacktestResult, *, full_prompts: bool = False) -> str:
    """Return a markdown report for a single BacktestResult.

    When `full_prompts=True` the per-agent detail section emits prompts and
    response payloads at full length — useful for prompt-engineering review
    but produces large files.
    """
    lines: list[str] = []

    lines += _header(result)
    lines += _summary(result)
    lines += _narrative(result)
    lines += _market_inputs(result)
    lines += _stage_traversal(result)
    lines += _agent_runs_table(result)
    lines += _decision_flow(result)
    lines += _per_agent_detail(result, full_prompts=full_prompts)
    lines += _judge_verdict(result)
    lines += _validators_section(result)
    lines += _backtest_pnl(result)
    lines += _cost_summary(result)
    lines += _footer(result)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------

def _header(r: BacktestResult) -> list[str]:
    return [
        f"# Agentic Workflow Backtest — {r.workflow_name}",
        "",
        f"**Target date:** {r.target_date.isoformat()}  ",
        f"**As-of timestamp:** {r.as_of.isoformat()}  ",
        f"**Mode:** {'mock-LLM' if r.mock_llm else 'live-LLM'} | "
        f"{'persist-to-DB' if r.persist_to_db else 'read-only DB'}  ",
        f"**Workflow run id:** `{r.workflow_run_id or '(not assigned)'}`  ",
        f"**Generated:** {datetime.utcnow().isoformat(timespec='seconds')}Z  ",
        "",
        "---",
        "",
    ]


def _summary(r: BacktestResult) -> list[str]:
    status_emoji = {
        "succeeded": "✅",
        "succeeded_with_caveats": "⚠️",
        "failed": "❌",
        "cancelled": "🛑",
        "unknown": "❔",
    }.get(r.status_extended or r.status, "•")

    fno_picks = (r.stage_outputs.get("triage") or {}).get("fno_candidates", []) or []
    eq_picks = (r.stage_outputs.get("triage") or {}).get("equity_candidates", []) or []

    return [
        "## Summary",
        "",
        f"- **Status:** {status_emoji} `{r.status_extended or r.status}`",
        f"- **Triage picks:** {len(fno_picks)} F&O / {len(eq_picks)} equity",
        f"- **Predictions persisted:** {len(r.predictions)}",
        f"- **API calls (mock or live):** {r.api_calls}",
        f"- **Tokens used:** {r.total_tokens:,}",
        f"- **Actual cost:** ${float(r.actual_cost_usd):.4f} USD",
        f"- **Projected ceiling:** ${float(r.projected_cost_usd):.4f} USD (worst-case, no caching)",
        f"- **Aggregate simulated P&L:** "
        + (f"{r.aggregate_pnl_pct:+.2f}%" if r.aggregate_pnl_pct is not None else "n/a"),
        "",
    ]


def _narrative(r: BacktestResult) -> list[str]:
    """Plain-English 'what happened' section near the top of the report.

    Distils the triage picks, judge verdict, and same-day actuals into a
    handful of sentences plus a single P&L table. Designed to be readable
    on its own — a reader who only scrolls this far gets the answer to
    'what stocks did the workflow pick and what would they have made?'
    """
    triage = (r.stage_outputs or {}).get("triage") or {}
    verdict = (r.stage_outputs or {}).get("judge_verdict") or {}
    fno = triage.get("fno_candidates", []) or []
    eq = triage.get("equity_candidates", []) or []
    allocation = verdict.get("allocation", []) or []
    deployed = [a for a in allocation if (a.get("asset_class") or "").lower() != "cash"]

    lines = ["## Executive Summary", ""]

    # 1) Stocks identified
    lines.append("**Stocks identified by the workflow**")
    lines.append("")
    if not fno and not eq:
        lines.append("- (none — Brain Triage produced no candidates)")
    else:
        if fno:
            lines.append(f"- _F&O ({len(fno)})_: " + ", ".join(
                f"`{c.get('symbol', '?')}` (rank {c.get('rank_score', 'n/a')}, "
                f"{c.get('expected_strategy_family', '')})"
                for c in fno
            ))
        if eq:
            lines.append(f"- _Equity ({len(eq)})_: " + ", ".join(
                f"`{c.get('symbol', '?')}` (rank {c.get('rank_score', 'n/a')}, "
                f"{c.get('horizon_hint', '')})"
                for c in eq
            ))
    lines.append("")

    # 2) Final allocation after the bull/bear/judge debate
    lines.append("**Final allocation (after bull/bear/judge debate)**")
    lines.append("")
    if not allocation:
        lines.append("- (no allocation produced — workflow short-circuited or failed)")
        lines.append("")
        return lines

    deployed_pct = sum(float(a.get("capital_pct") or 0) for a in deployed)
    cash_pct = sum(float(a.get("capital_pct") or 0)
                   for a in allocation
                   if (a.get("asset_class") or "").lower() == "cash")
    lines.append(
        f"- Deployed: **{deployed_pct:.1f}%** of capital across "
        f"{len(deployed)} positions; cash held: **{cash_pct:.1f}%**."
    )
    if verdict.get("expected_book_pnl_pct") is not None:
        lines.append(
            f"- Judge's expected book P&L: **{verdict['expected_book_pnl_pct']}%** "
            f"(stretch: {verdict.get('stretch_pnl_pct', 'n/a')}%, "
            f"max drawdown tolerated: {verdict.get('max_drawdown_tolerated_pct', 'n/a')}%)."
        )
    if deployed:
        lines.append("- Deployed legs:")
        for a in deployed:
            sym = a.get("underlying_or_symbol") or "?"
            lines.append(
                f"  - `{sym}` ({a.get('asset_class', '?')}) — "
                f"{a.get('capital_pct', 0):.1f}% capital · "
                f"{a.get('decision', '')} · conviction {a.get('conviction', 'n/a')}"
            )
    lines.append("")

    # 3) Profit / loss against same-day actuals
    lines.append("**Profit/loss vs same-day actuals** (close-to-close, target_date)")
    lines.append("")
    pnl_rows = [p for p in (r.pnl_estimates or [])
                if (p.get("asset_class") or "").lower() != "cash"]
    scored = [p for p in pnl_rows if p.get("simulated_pnl_pct") is not None]
    unscored = [p for p in pnl_rows if p.get("simulated_pnl_pct") is None]

    if not pnl_rows:
        lines.append("- No deployed positions — P&L is 0 by construction.")
    else:
        lines.append("| Symbol | Capital % | Direction | Day move | P&L on the leg | Contribution to book |")
        lines.append("|---|---|---|---|---|---|")
        for p in pnl_rows:
            chg = p.get("day_change_pct")
            sim = p.get("simulated_pnl_pct")
            cap = float(p.get("capital_pct") or 0)
            chg_str = f"{chg:+.2f}%" if isinstance(chg, (int, float)) else "n/a"
            sim_str = f"{sim:+.2f}%" if isinstance(sim, (int, float)) else "n/a"
            contrib = (sim * cap / 100.0) if isinstance(sim, (int, float)) else None
            contrib_str = f"{contrib:+.3f}%" if contrib is not None else "n/a"
            lines.append(
                f"| `{p.get('symbol', '?')}` | {cap:.1f}% "
                f"| {p.get('predicted_direction', '?')} | {chg_str} | {sim_str} | {contrib_str} |"
            )

    if r.aggregate_pnl_pct is not None:
        lines.append("")
        lines.append(
            f"**Book-level P&L: {r.aggregate_pnl_pct:+.3f}% of total capital** "
            f"(deployed legs only; cash legs are 0 by definition)."
        )
        # Translate to rupees on a hypothetical ₹1 lakh book for intuition.
        on_one_lakh = r.aggregate_pnl_pct * 1000  # 1% of ₹1L = ₹1000
        lines.append(
            f"On a hypothetical ₹1,00,000 book: **₹{on_one_lakh:+,.0f} net for the day**."
        )

    if unscored:
        lines.append("")
        lines.append(
            f"⚠️ {len(unscored)} position(s) could not be scored — no `price_daily` "
            f"row for the symbol on `{r.target_date}`. See the full P&L table below."
        )

    # 4) Provenance caveat for mock runs
    if r.mock_llm:
        lines += [
            "",
            "> ℹ️ **Mock-LLM run.** Picks were generated by deterministic stubs in "
            "`src.agents.backtest.mock_anthropic`, not by a real model. They do not "
            "reflect the live model's judgement on today's data. Re-run with "
            "`--live-llm` for a real backtest.",
        ]

    lines.append("")
    lines.append("---")
    lines.append("")
    return lines


def _market_inputs(r: BacktestResult) -> list[str]:
    s = r.snapshot
    if not s:
        return ["## Market Inputs", "", "_(snapshot unavailable)_", ""]
    if not s.fetch_ok:
        return [
            "## Market Inputs",
            "",
            f"⚠️ Snapshot fetch failed: `{s.fetch_error}`",
            "",
        ]

    actuals_count = len(s.actuals)
    return [
        "## Market Inputs (snapshot at as_of)",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| India VIX (latest before morning) | "
        f"{s.vix_latest:.2f} as of {s.vix_observed_at}" if s.vix_latest is not None
        else "| India VIX | n/a |",
        f"| NIFTY prev close | {s.nifty_prev_close:.2f} |" if s.nifty_prev_close is not None
        else "| NIFTY prev close | n/a |",
        f"| Raw content (24h pre-morning) | {s.raw_content_count_24h} items |",
        f"| Signals (24h pre-morning) | {s.signals_count_24h} "
        f"(bullish={s.bullish_signals_24h}, bearish={s.bearish_signals_24h}) |",
        f"| Universe sample size | {len(s.universe_sample)} instruments |",
        f"| Open positions | {len(s.open_positions)} |",
        f"| Top movers yesterday | {len(s.top_movers_yesterday)} |",
        f"| Actuals coverage | {actuals_count} symbols with EOD price for {s.target_date} |",
        "",
    ]


def _stage_traversal(r: BacktestResult) -> list[str]:
    lines = ["## Stage Traversal", ""]
    if not r.agent_runs:
        lines += ["_(no agent runs recorded)_", ""]
        return lines

    by_stage: dict[str, list[dict]] = {}
    for ar in r.agent_runs:
        by_stage.setdefault(ar["agent_name"], []).append(ar)

    lines += [
        "| Agent | Calls | Failures | Total cost USD | Total tokens |",
        "|---|---|---|---|---|",
    ]
    for agent, runs in by_stage.items():
        n = len(runs)
        failures = sum(1 for x in runs if x["status"] == "failed")
        total_cost = sum(x["cost_usd"] for x in runs)
        total_tok = sum(x["input_tokens"] + x["output_tokens"] for x in runs)
        lines.append(
            f"| `{agent}` | {n} | {failures} | ${total_cost:.4f} | {total_tok:,} |"
        )
    lines.append("")
    return lines


def _agent_runs_table(r: BacktestResult) -> list[str]:
    if not r.agent_runs:
        return []
    lines = [
        "## Per-Agent Trail",
        "",
        "| # | Agent | Model | Status | Tokens | ms | Error |",
        "|---|---|---|---|---|---|---|",
    ]
    for i, ar in enumerate(r.agent_runs, 1):
        err = (ar.get("error") or "")[:50]
        lines.append(
            f"| {i} | `{ar['agent_name']}` | `{ar['model_used']}` "
            f"| {ar['status']} | {ar['input_tokens']+ar['output_tokens']:,} "
            f"| {ar['duration_ms']} | {err} |"
        )
    lines.append("")
    return lines


def _decision_flow(r: BacktestResult) -> list[str]:
    """Chronological narrative of how each agent's output fed the next."""
    lines = ["## Decision Flow", ""]

    triage = (r.stage_outputs or {}).get("triage") or {}
    if triage:
        skip = triage.get("skip_today")
        regime = triage.get("regime_note", "")
        fno = triage.get("fno_candidates", []) or []
        eq = triage.get("equity_candidates", []) or []
        lines.append("### 1. Brain Triage (gatekeeper)")
        lines.append("")
        lines.append(
            f"- Decision: **{'SKIP TODAY' if skip else 'PROCEED'}** "
            f"with {len(fno)} F&O candidate(s) and {len(eq)} equity candidate(s)."
        )
        if regime:
            lines.append(f"- Regime note: _{regime}_")
        if fno:
            lines.append(f"- F&O picks:")
            for c in fno:
                lines.append(
                    f"  - `{c.get('symbol', '?')}` (rank {c.get('rank_score', 'n/a')}) — "
                    f"{c.get('expected_strategy_family', '')} · "
                    f"_{c.get('primary_driver', '')}_"
                )
        if eq:
            lines.append(f"- Equity picks:")
            for c in eq:
                lines.append(
                    f"  - `{c.get('symbol', '?')}` (rank {c.get('rank_score', 'n/a')}, "
                    f"horizon {c.get('horizon_hint', '')}) — "
                    f"_{c.get('primary_driver', '')}_"
                )
        lines.append("")

    # News finder + editor go/no-go per candidate
    news_findings = (r.stage_outputs or {}).get("news_findings") or []
    editor_verdicts = (r.stage_outputs or {}).get("editor_verdicts") or []
    if news_findings:
        lines.append("### 2. News Finder → Editor (per candidate)")
        lines.append("")
        lines.append("| Symbol | Sentiment | Score | Go/no-go | Editor grade | Editor decision |")
        lines.append("|---|---|---|---|---|---|")
        for nf in news_findings:
            sym = (nf.get("instrument") or {}).get("symbol") or "?"
            summary = nf.get("summary_json") or {}
            ev = next(
                (v for v in editor_verdicts
                 if v.get("instrument_symbol") == sym),
                {},
            )
            lines.append(
                f"| `{sym}` | {summary.get('sentiment', '?')} "
                f"| {summary.get('score', 'n/a')} "
                f"| {summary.get('go_no_go_hint', '?')} "
                f"| {ev.get('credibility_grade', 'n/a')} "
                f"| {'GO' if ev.get('go_no_go_for_brain') else 'NO-GO'} |"
            )
        lines.append("")

    # Explorer aggregator per candidate
    explorer_aggs = (r.stage_outputs or {}).get("explorer_aggregates") or []
    if explorer_aggs:
        lines.append("### 3. Historical Explorer pod (per candidate)")
        lines.append("")
        lines.append("| Symbol | Pattern score | Horizon | Regime fit | TLDR |")
        lines.append("|---|---|---|---|---|")
        for ea in explorer_aggs:
            lines.append(
                f"| `{ea.get('symbol', '?')}` "
                f"| {ea.get('tradable_pattern_score', 'n/a')} "
                f"| {ea.get('dominant_horizon', '?')} "
                f"| {ea.get('regime_consistency_with_today', '?')} "
                f"| {(ea.get('tldr') or '')[:80]} |"
            )
        lines.append("")

    # F&O Expert + Equity Expert thesis per candidate
    fno_full = (r.stage_outputs or {}).get("fno_candidates_full") or []
    eq_full = (r.stage_outputs or {}).get("equity_candidates_full") or []
    if fno_full or eq_full:
        lines.append("### 4. Expert thesis (per candidate)")
        lines.append("")
        if fno_full:
            lines.append("**F&O experts**")
            lines.append("")
            for f in fno_full:
                lines.append(
                    f"- `{f.get('symbol', '?')}` — {f.get('strategy', '?')} "
                    f"({f.get('direction', '?')}, conv={f.get('conviction', 'n/a')}, "
                    f"refused={f.get('refused', '?')}) · "
                    f"_{(f.get('thesis') or '')[:200]}_"
                )
            lines.append("")
        if eq_full:
            lines.append("**Equity experts**")
            lines.append("")
            for e in eq_full:
                lines.append(
                    f"- `{e.get('symbol', '?')}` — {e.get('decision', '?')} "
                    f"(conv={e.get('conviction', 'n/a')}, "
                    f"refused={e.get('refused', '?')}, "
                    f"horizon={e.get('horizon', '?')}) · "
                    f"target {e.get('target', '?')} / stop {e.get('stop', '?')} · "
                    f"_{(e.get('thesis') or '')[:200]}_"
                )
            lines.append("")

    # CEO bull/bear/judge debate
    bull = (r.stage_outputs or {}).get("bull_brief") or {}
    bear = (r.stage_outputs or {}).get("bear_brief") or {}
    judge = (r.stage_outputs or {}).get("judge_verdict") or {}
    if bull or bear or judge:
        lines.append("### 5. CEO debate (Bull vs Bear → Judge)")
        lines.append("")
        if bull:
            lines.append(f"- **Bull** ({bull.get('stance', '?')}, conviction "
                         f"{bull.get('conviction', 'n/a')}): _{bull.get('core_thesis', '')}_")
        if bear:
            lines.append(f"- **Bear** ({bear.get('stance', '?')}, conviction "
                         f"{bear.get('conviction', 'n/a')}): _{bear.get('core_thesis', '')}_")
        if judge:
            csc = judge.get("calibration_self_check") or {}
            lines.append(
                f"- **Judge verdict** "
                f"(bull={csc.get('bullish_argument_grade', '?')}, "
                f"bear={csc.get('bearish_argument_grade', '?')}, "
                f"confidence {csc.get('confidence_in_allocation', 'n/a')}): "
                f"_{judge.get('decision_summary', '')}_"
            )
            disagreements = judge.get("disagreement_loci") or []
            if disagreements:
                lines.append("- Disagreements resolved:")
                for d in disagreements:
                    lines.append(
                        f"  - **{d.get('topic', '?')}** → judge leaned "
                        f"`{d.get('judge_lean', '?')}` "
                        f"({d.get('lean_strength', '?')}). "
                        f"Decisive: _{d.get('decisive_evidence', '')}_"
                    )
        lines.append("")

    if not (triage or news_findings or explorer_aggs or fno_full or eq_full or bull or bear or judge):
        lines.append("_(no stage outputs captured)_")
        lines.append("")
    return lines


def _per_agent_detail(r: BacktestResult, *, full_prompts: bool = False) -> list[str]:
    """One block per agent_run with prompt summary, raw output, key fields."""
    lines = ["## Per-Agent Detail", ""]
    if full_prompts:
        lines.append("_Full-length prompts and outputs (no truncation)._")
        lines.append("")
    if not r.agent_runs:
        lines.append("_(no agent runs)_")
        lines.append("")
        return lines

    import json
    from collections import defaultdict
    from src.agents.personas import PERSONA_MANIFEST

    # Index calls by tool_name in chronological order so we can pop the
    # i-th call per tool when matching to the i-th agent_run of that name.
    calls_by_tool: dict[str, list[dict]] = defaultdict(list)
    for c in r.api_call_log or []:
        if c.get("tool_name"):
            calls_by_tool[c["tool_name"]].append(c)
    cursor: dict[str, int] = defaultdict(int)

    def _output_tool_for(agent_name: str, persona_version: str) -> str | None:
        defn = PERSONA_MANIFEST.get(agent_name, {}).get(persona_version, {})
        return defn.get("output_tool")

    for i, ar in enumerate(r.agent_runs, 1):
        tool = _output_tool_for(ar["agent_name"], ar["persona_version"])
        call = None
        if tool and cursor[tool] < len(calls_by_tool[tool]):
            call = calls_by_tool[tool][cursor[tool]]
            cursor[tool] += 1

        lines.append(f"### {i}. `{ar['agent_name']}` (`{ar['model_used']}`)")
        lines.append("")
        lines.append(
            f"- Status: **{ar['status']}** · "
            f"{ar['input_tokens']+ar['output_tokens']:,} tokens · "
            f"${ar['cost_usd']:.4f} · {ar['duration_ms']}ms"
            + (f" · error: `{(ar.get('error') or '')[:120]}`" if ar.get("error") else "")
        )

        sys_limit = None if full_prompts else 6  # lines
        user_limit = None if full_prompts else 2_000
        out_limit = None if full_prompts else 4_000

        if call and call.get("system_prompt"):
            sys_lines = call["system_prompt"].strip().splitlines()
            head = "\n".join(sys_lines if sys_limit is None else sys_lines[:sys_limit])
            label = "**System prompt:**" if full_prompts else "**System prompt (first 6 lines):**"
            lines += ["", label, "", "```", head, "```"]

        if call and call.get("user_prompt"):
            up = call["user_prompt"]
            shown = up if user_limit is None or len(up) <= user_limit else up[:user_limit] + "\n…[truncated]"
            note = "" if user_limit is None or len(up) <= user_limit else f" — truncated to {user_limit//1000}k"
            lines += [
                "",
                f"**User prompt** (length: {len(up):,} chars{note}):",
                "",
                "```json", shown, "```",
            ]

        out = ar.get("output") or (call or {}).get("response_payload")
        if out:
            try:
                pretty = json.dumps(out, indent=2, default=str, ensure_ascii=False)
            except (TypeError, ValueError):
                pretty = str(out)
            if out_limit is not None and len(pretty) > out_limit:
                pretty = pretty[:out_limit] + "\n…[truncated]"
            lines += ["", "**Output payload:**", "", "```json", pretty, "```"]

        lines.append("")
    return lines


def _judge_verdict(r: BacktestResult) -> list[str]:
    verdict = (r.stage_outputs or {}).get("judge_verdict") or {}
    if not verdict:
        return ["## CEO Judge Verdict", "", "_(no verdict produced)_", ""]

    lines = [
        "## CEO Judge Verdict",
        "",
        f"**Decision summary:** {verdict.get('decision_summary', '')}",
        "",
        f"- Expected book P&L: {verdict.get('expected_book_pnl_pct', 'n/a')}%",
        f"- Stretch P&L: {verdict.get('stretch_pnl_pct', 'n/a')}%",
        f"- Max drawdown tolerated: {verdict.get('max_drawdown_tolerated_pct', 'n/a')}%",
        "",
        "### Allocation",
        "",
        "| Asset class | Symbol | Capital % | Decision | Horizon | Conviction |",
        "|---|---|---|---|---|---|",
    ]
    for alloc in verdict.get("allocation", []) or []:
        lines.append(
            f"| {alloc.get('asset_class', '')} "
            f"| `{alloc.get('underlying_or_symbol', '')}` "
            f"| {alloc.get('capital_pct', 0):.1f}% "
            f"| {alloc.get('decision', '')} "
            f"| {alloc.get('horizon', '')} "
            f"| {alloc.get('conviction', 'n/a')} |"
        )
    lines.append("")

    ks = verdict.get("kill_switches", []) or []
    if ks:
        lines += ["### Kill Switches", ""]
        for k in ks:
            lines.append(
                f"- **{k.get('action', '?')}** when "
                f"`{k.get('trigger', '?')}` "
                f"(monitor: `{k.get('monitoring_metric', '?')}`)"
            )
        lines.append("")

    csc = verdict.get("calibration_self_check") or {}
    if csc:
        lines += [
            "### Calibration Self-Check",
            "",
            f"- Bullish argument grade: **{csc.get('bullish_argument_grade', '?')}**",
            f"- Bearish argument grade: **{csc.get('bearish_argument_grade', '?')}**",
            f"- Confidence: {csc.get('confidence_in_allocation', 'n/a')}",
            f"- Regret scenario: {csc.get('regret_scenario', '')}",
            "",
        ]
    return lines


def _validators_section(r: BacktestResult) -> list[str]:
    if not r.validator_outcomes:
        return [
            "## Cross-Agent Validators",
            "",
            "_(no validators ran)_",
            "",
        ]
    lines = [
        "## Cross-Agent Validators",
        "",
        "| Validator | Outcome | Detail |",
        "|---|---|---|",
    ]
    for vo in r.validator_outcomes:
        outcome = vo.get("outcome", "?")
        emoji = {"passed": "✅", "caveat": "⚠️", "rejected": "❌", "missing": "❔"}.get(outcome, "•")
        detail = (vo.get("error") or "")[:200]
        lines.append(f"| `{vo.get('validator', '?')}` | {emoji} {outcome} | {detail} |")
    lines.append("")
    return lines


def _backtest_pnl(r: BacktestResult) -> list[str]:
    if not r.pnl_estimates:
        return ["## Backtest P&L", "", "_(no allocations to score)_", ""]

    lines = [
        "## Backtest P&L (close-to-close, target_date)",
        "",
        "Method: each non-cash allocation's `decision` is mapped to a "
        "directional bet; the day's actual `change_pct` from `price_daily` "
        "is multiplied by the inferred sign and an asset-class leverage "
        "(F&O = 2×, equity = 1×). Cash = 0%. EOD numbers — no intraday slippage.",
        "",
        "| Symbol | Asset class | Capital % | Direction | Day chg % | Simulated P&L % | Notes |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in r.pnl_estimates:
        chg = row.get("day_change_pct")
        sim = row.get("simulated_pnl_pct")
        chg_str = f"{chg:+.2f}%" if isinstance(chg, (int, float)) else "n/a"
        sim_str = f"{sim:+.2f}%" if isinstance(sim, (int, float)) else "n/a"
        lines.append(
            f"| `{row.get('symbol', '')}` "
            f"| {row.get('asset_class', '')} "
            f"| {row.get('capital_pct', 0):.1f}% "
            f"| {row.get('predicted_direction', 'n/a')} "
            f"| {chg_str} | {sim_str} | {row.get('notes', '')} |"
        )

    if r.aggregate_pnl_pct is not None:
        lines += [
            "",
            f"**Capital-weighted aggregate:** {r.aggregate_pnl_pct:+.3f}% "
            f"(over deployed capital only — cash legs excluded).",
            "",
        ]
    return lines


def _cost_summary(r: BacktestResult) -> list[str]:
    by_model: Counter[str] = Counter()
    by_agent: dict[str, float] = {}
    for ar in r.agent_runs:
        by_model[ar["model_used"]] += ar["cost_usd"]
        by_agent[ar["agent_name"]] = by_agent.get(ar["agent_name"], 0.0) + ar["cost_usd"]

    lines = ["## Cost Breakdown", ""]
    if not r.agent_runs:
        lines += ["_(no cost data)_", ""]
        return lines

    lines += [
        f"- **Actual:** ${float(r.actual_cost_usd):.4f}",
        f"- **Projected ceiling (no caching):** ${float(r.projected_cost_usd):.4f}",
        f"- **Headroom:** "
        f"{(1 - float(r.actual_cost_usd) / max(float(r.projected_cost_usd), 1e-9)) * 100:.1f}% under ceiling",
        "",
        "### By model",
        "",
        "| Model | Cost USD |",
        "|---|---|",
    ]
    for model, cost in by_model.most_common():
        lines.append(f"| `{model}` | ${cost:.4f} |")
    lines += [
        "",
        "### By agent",
        "",
        "| Agent | Cost USD |",
        "|---|---|",
    ]
    for agent, cost in sorted(by_agent.items(), key=lambda kv: -kv[1]):
        lines.append(f"| `{agent}` | ${cost:.4f} |")
    lines.append("")
    return lines


def _footer(r: BacktestResult) -> list[str]:
    return [
        "---",
        "",
        f"_Backtest produced by `src.agents.backtest`. "
        f"Mode: `mock_llm={r.mock_llm}` `persist_to_db={r.persist_to_db}`. "
        f"This run did not send Telegram messages, file GitHub issues, or "
        f"emit broker calls. {'No DB writes were committed.' if not r.persist_to_db else ''}_",
        "",
    ]
