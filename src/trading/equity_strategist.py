"""LLM-driven decision brain for the equity paper-trading layer.

Produces three kinds of decisions:

* ``decide_morning_allocation`` — once per trading day at 09:10 IST. Decides
  what fraction of available cash to deploy at open and how to split it across
  BUY-rated candidates from recent signals + watchlist.
* ``decide_intraday_action`` — periodically during market hours (~ 09:45–14:30).
  Reviews current holdings + fresh signals + cash and proposes
  HOLD/SELL/BUY actions. Bounded per day by ``equity_strategy_max_intraday_calls``.
* ``decide_eod_squareoff`` — at 15:20 IST, just before close, asks the LLM
  which intraday-flavoured positions to close out vs hold as delivery.

Each invocation persists one ``strategy_decisions`` row capturing the inputs,
the LLM's reasoning, and the structured action list. The runner consumes the
return value and dispatches each action through ``TradingEngine``.

This module never touches the trading engine directly — it only *decides*.
That keeps the LLM call replayable in dryrun without unintended side-effects.
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from anthropic import AsyncAnthropic
from loguru import logger
from sqlalchemy import desc, select
from tenacity import retry, stop_after_attempt, wait_exponential

from sqlalchemy import text

from src.config import get_settings
from src.db import session_scope
from src.dryrun.side_effects import get_dryrun_run_id
from src.models.instrument import Instrument
from src.models.llm_audit_log import LLMAuditLog
from src.models.portfolio import Holding, Portfolio
from src.models.signal import Signal
from src.models.strategy_decision import StrategyDecision
from src.models.watchlist import Watchlist, WatchlistItem
from src.services.price_service import PriceService

DECISION_MORNING = "morning_allocation"
DECISION_INTRADAY = "intraday_action"
DECISION_EOD = "eod_squareoff"

# Hard cap on equity candidates fed to the LLM. Past ~40 names the prompt
# bloats faster than decision quality improves, and a wide universe was the
# reason the FNO block originally fell off the truncation cliff.
_CANDIDATE_LIMIT = 30


def _rank_score(c: dict[str, Any]) -> tuple[int, int, float, float]:
    """Sort key for candidate filtering.

    Source priority dominates so signal-driven names always beat watchlist
    fillers; within a source, higher convergence (``distinct_sources_48h``)
    wins, then analyst credibility, then raw confidence. None values are
    treated as zero — a missing field is weaker than an explicit low value.
    """
    src_priority = {
        "signal": 3,
        "watchlist": 2,
        "default_universe": 1,
    }.get(c.get("source") or "", 0)
    return (
        src_priority,
        int(c.get("distinct_sources_48h") or 0),
        float(c.get("top_analyst_credibility") or 0.0),
        float(c.get("confidence") or 0.0),
    )


SYSTEM_PROMPT = """You are an Indian markets paper-trading strategist (BSE/NSE).

You manage ONE common pool of paper capital that funds FOUR strategy
buckets (the "brains"):

  - ``equity``           — your own equity LLM brain (BUY/SELL on stocks)
  - ``fno_directional``  — long_call, long_put (single-leg directional)
  - ``fno_spread``       — bull_call_spread, bear_put_spread, iron_condor
  - ``fno_volatility``   — straddle (volatility-selling/buying)

Your morning job (decision_type='morning_allocation') has TWO outputs:
(1) a per-bucket capital ALLOCATION (fractions of the pool that sum to 1.0)
based on today's market regime; (2) the usual list of executable actions.
Both must appear in your JSON. The allocation determines today's per-
strategy ceilings — F&O entries at 09:15 IST size against their bucket's
slice. Diversification is not optional: an equity-only allocation when
fno_candidates contain composite_score≥60 setups is a failure mode.

CONTEXT YOU WILL RECEIVE
- ``market`` — VIX value+regime, NIFTY 50 day change %, FII/DII net flow.
- ``portfolio_context`` — sector exposure breakdown, pending limit/SL orders,
  today's realised P&L so far, count of open intraday positions.
- ``candidates`` — equity buys: ltp, source (signal/watchlist/default), signal
  confidence + convergence count, top analyst credibility, recent news titles
  (≤3, last 7 days), 5-day return %, distance from 200-DMA %.
- ``fno_candidates`` — Phase 3 PROCEED options ideas for today: underlying
  symbol, direction, strategy (long_call/long_put/spread/iron_condor/straddle),
  composite_score (0-100), iv_regime, contract label, target/stop premiums,
  one-line llm_thesis. These are auto-executed at 09:15 IST by a separate
  job, so your role is to surface them in a unified plan and reserve cash
  if you want to add more on top.
- ``holdings`` — for each: avg_buy_price, ltp, pnl_pct, days_held,
  is_intraday flag, original entry_reason, recent news.

DECISION FRAMEWORK
1. Read the regime first. VIX>18 (regime='high') = trim sizes & prefer
   defensives (FMCG/IT/Pharma). VIX<12 (regime='low') = momentum-friendly,
   tolerate full deployment. NIFTY day_change > +0.8% with strong FII inflow
   = bullish breadth; < -0.8% with DII selling = risk-off.
2. Score each candidate on: (a) signal convergence_count (≥3 sources is a
   strong signal), (b) analyst credibility (>0.7 is high quality), (c) news
   substance (concrete catalyst > vague commentary), (d) trend alignment
   (5d_return positive AND price > 200-DMA suggests momentum; deeply
   negative 5d on a strong-conviction signal is mean-reversion).
3. Sector concentration: do not push any sector above 35% of NAV (60% if
   risk_profile=aggressive).
4. Position sizing: respect ``per_position_cap_rupees`` strictly. Convert to
   integer quantity using approx_price=ltp.
5. Risk dial:
   - safe       → deploy ≤50% of cash, ≤3 positions, prefer convergence_count≥3
   - balanced   → deploy ≤85% of cash, ≤5 positions, OK with convergence_count≥2
   - aggressive → deploy ≤100% of cash, ≤6 positions, OK with single-source signals

HARD RULES (any action that violates these is auto-skipped by the gate;
self-skip rather than waste a slot — name the rule in your reasoning):
A. SUB-SCALE FRICTION — when ``cash_available`` < Rs 200,000:
   - Required confidence to ENTER >= 0.75 (no exceptions).
   - Required expected move (target-entry)/entry >= 2.0% AT ENTRY price.
   - At Rs 40K, costs alone are ~1.5% per round-trip; the math doesn't work
     for sub-2% setups, so don't take them.
B. HIGH-VIX REGIME — when vix_regime='high' OR vix_value >= 17:
   - Equity entries: max 2 per morning_allocation, confidence >= 0.75.
   - The thesis must survive an overnight gap. If you intend to flatten at
     EOD, do not enter — costs ~1.5% per round-trip eat the edge.
   - F&O: do NOT propose naked long_call / long_put; prefer debit spreads
     (bull_call_spread / bear_put_spread) or short_strangle / iron_condor.
C. EOD POLICY (replaces the old blanket high-VIX flatten):
   - SELL only positions where confidence < 0.75 OR pnl_pct in [-0.5%, +1%].
   - HOLD positions where confidence >= 0.80 AND multi-session catalyst
     intact AND no adverse news today; set overnight stop = entry * 0.985.
D. PORTFOLIO-AWARE: never propose a BUY for a symbol already in
   ``holdings`` (use the existing position) and never propose a SELL for a
   symbol you do not hold. F&O: reject same-strategy duplicates and
   opposing directional legs (long_call vs long_put) on same underlying.
E. CONCENTRATION: when proposing >6 single-leg long F&O calls with similar
   bullish theses, replace with one NIFTY/BANKNIFTY call of equivalent
   notional — independent ideas, not 6 copies of one trade.

OUTPUT RULES
1. Output ONLY valid JSON. No markdown, no preamble.
2. Each action has ``asset_class``: "EQUITY" or "FNO".
   - EQUITY: ``instrument_id`` is mandatory and must come from ``candidates``.
     Quantities are integers; qty * approx_price must respect
     ``per_position_cap_rupees`` and remaining cash. The risk layer will
     clamp oversize qty down — but try to stay within the cap on first try.
   - FNO: surface options ideas from ``fno_candidates``. Use ``symbol`` =
     underlying symbol, ``qty`` = number of lots, and put the strategy +
     contract label in ``reason``. ``instrument_id`` is optional. These are
     informational — Phase 4 entry job executes them — so listing them
     unifies the morning view.
3. Aim for a MIX when both lists have substance: at least one FNO action
   when ``fno_candidates`` is non-empty and contains a composite_score ≥ 60
   item. Equity-only output on a day with strong option setups is wrong.
4. Skipping a marginal trade is preferred over a forced one. Empty actions
   array is a valid decision when no edge is clear.
5. Reasoning per action: one sentence naming the dominant driver
   (convergence / news catalyst / momentum / mean-reversion / risk / IV).
6. ``reasoning`` (top-level): 2-4 sentences linking regime → strategy →
   equity vs options balance → picks.
"""


def _float_or_none(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _strip_code_fence(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[: -3]
    return text.strip()


class EquityStrategist:
    """LLM brain that emits structured action lists; never executes trades."""

    _CALLER = "equity_strategy"
    _TEMPERATURE = 0.2

    def __init__(self) -> None:
        self.settings = get_settings()
        self.client = AsyncAnthropic(api_key=self.settings.anthropic_api_key)
        self.price_service = PriceService()

    # ------------------------------------------------------------------ public

    async def decide_morning_allocation(
        self,
        portfolio_id: uuid.UUID,
        as_of: datetime | None = None,
        dryrun_run_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        """Plan how to deploy today's cash across fresh BUY candidates."""
        return await self._decide(
            decision_type=DECISION_MORNING,
            portfolio_id=portfolio_id,
            as_of=as_of,
            dryrun_run_id=dryrun_run_id,
            model=self.settings.equity_strategy_morning_model,
        )

    async def decide_intraday_action(
        self,
        portfolio_id: uuid.UUID,
        as_of: datetime | None = None,
        dryrun_run_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        """Re-evaluate holdings and signals; propose HOLD/SELL/BUY actions."""
        return await self._decide(
            decision_type=DECISION_INTRADAY,
            portfolio_id=portfolio_id,
            as_of=as_of,
            dryrun_run_id=dryrun_run_id,
            model=self.settings.equity_strategy_intraday_model,
        )

    async def decide_eod_squareoff(
        self,
        portfolio_id: uuid.UUID,
        as_of: datetime | None = None,
        dryrun_run_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        """Decide which positions to close before market close."""
        return await self._decide(
            decision_type=DECISION_EOD,
            portfolio_id=portfolio_id,
            as_of=as_of,
            dryrun_run_id=dryrun_run_id,
            model=self.settings.equity_strategy_intraday_model,
        )

    # ----------------------------------------------------------------- helpers

    async def _decide(
        self,
        *,
        decision_type: str,
        portfolio_id: uuid.UUID,
        as_of: datetime | None,
        dryrun_run_id: uuid.UUID | None,
        model: str,
    ) -> dict[str, Any]:
        as_of_eff = as_of or datetime.now(timezone.utc)
        run_id = dryrun_run_id if dryrun_run_id is not None else get_dryrun_run_id()

        snapshot = await self._build_input_snapshot(portfolio_id, decision_type, as_of_eff)
        prompt = await self._build_prompt(
            decision_type, snapshot, portfolio_id=portfolio_id, as_of=as_of_eff
        )

        parsed, tokens_in, tokens_out, latency_ms, raw = await self._call_llm(prompt, model)
        actions = self._normalise_actions(parsed)
        reasoning = (parsed or {}).get("reasoning") or ""

        # Defense-in-depth: re-check every action against the same hard
        # rules described in the prompt. Violations are downgraded to HOLD
        # and tagged with ``gate_violation`` so the runner sees them but
        # does not execute them. The mismatch rate (LLM proposed → gate
        # blocked) is a quality signal for prompt iteration.
        # The gate is defense-in-depth — if it raises we MUST notice. Logging
        # at WARNING is the floor; the EOD digest also surfaces gate-error
        # counts for the day so a regression is visible inside one session
        # rather than sitting silent until the next post-mortem.
        gate_error: str | None = None
        try:
            from src.trading.strategy_gate import filter_equity_actions
            outcome = await filter_equity_actions(
                actions,
                snapshot=snapshot,
                portfolio_id=portfolio_id,
                decision_type=decision_type,
            )
            actions = outcome.merge_into_actions()
            actions_skipped = len(outcome.skipped)
        except Exception as exc:
            logger.warning(
                f"strategist: equity gate raised — failing OPEN "
                f"(actions pass through unchecked): {exc!r}"
            )
            gate_error = repr(exc)
            actions_skipped = 0

        await self._write_audit_log(
            caller_ref_id=portfolio_id,
            prompt=prompt,
            response=raw,
            response_parsed=parsed,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=latency_ms,
            model=model,
        )

        # Persist the LLM-decided per-bucket allocation block so the F&O
        # entry executor at 09:15 IST can read it back when sizing trades.
        # Defaults are filled in when the morning brain didn't return a
        # well-formed allocation — keeps the downstream contract stable.
        actions_json: dict[str, Any] = {"actions": actions, "reasoning": reasoning}
        if gate_error:
            actions_json["gate_error"] = gate_error
        if decision_type == DECISION_MORNING:
            from src.trading.budget_allocator import (
                stamp_allocations_into_actions_json,
            )
            actions_json = stamp_allocations_into_actions_json(
                actions_json, (parsed or {}).get("allocations")
            )

        decision_id: uuid.UUID | None = None
        async with session_scope() as session:
            row = StrategyDecision(
                portfolio_id=portfolio_id,
                decision_type=decision_type,
                as_of=as_of_eff,
                risk_profile=self.settings.equity_strategy_risk_profile,
                budget_available=snapshot.get("cash_available"),
                input_summary=snapshot,
                llm_model=model,
                llm_reasoning=reasoning[:8000] if reasoning else None,
                actions_json=actions_json,
                actions_skipped=actions_skipped,
                dryrun_run_id=run_id,
            )
            session.add(row)
            await session.flush()
            decision_id = row.id

        logger.info(
            f"equity strategist {decision_type}: portfolio={portfolio_id} "
            f"actions={len(actions)} model={model} run_id={run_id}"
        )
        return {
            "decision_id": decision_id,
            "decision_type": decision_type,
            "as_of": as_of_eff,
            "actions": actions,
            "reasoning": reasoning,
            "snapshot": snapshot,
        }

    async def _build_input_snapshot(
        self, portfolio_id: uuid.UUID, decision_type: str, as_of: datetime
    ) -> dict[str, Any]:
        """Collect everything the LLM needs to make an informed call.

        Beyond the bare cash/holdings/candidates, this also pulls:
          * market regime (VIX, NIFTY day change, FII/DII flow)
          * portfolio composition (sector exposure, pending orders, day pnl)
          * per-candidate enrichment (news titles, analyst credibility,
            convergence count, 5-day return, % distance from 200-DMA)
          * per-holding enrichment (entry reason from original trade,
            days held, recent news)
        Each enrichment is wrapped so a missing table or empty result
        degrades gracefully rather than failing the whole prompt.
        """
        # Portfolio + cash
        async with session_scope() as session:
            portfolio = await session.get(Portfolio, portfolio_id)
            if portfolio is None:
                raise ValueError(f"Portfolio {portfolio_id} not found")
            cash = float(portfolio.current_cash or 0)
            invested = float(portfolio.invested_value or 0)
            current_value = float(portfolio.current_value or 0)
            day_pnl = float(portfolio.day_pnl or 0)

        market = await self._market_context(as_of)
        portfolio_context = await self._portfolio_context(portfolio_id, as_of)
        fno_candidates = await self._fno_phase3_candidates(as_of)
        fno_open_positions = await self._fno_open_positions(as_of)
        today_equity_trades = await self._today_equity_trades(portfolio_id, as_of)

        # Holdings with LTPs + enrichment
        holdings_view: list[dict[str, Any]] = []
        async with session_scope() as session:
            result = await session.execute(
                select(Holding, Instrument)
                .join(Instrument, Instrument.id == Holding.instrument_id)
                .where(Holding.portfolio_id == portfolio_id)
            )
            rows = list(result.all())
        for h, inst in rows:
            ltp = await self.price_service.latest_price(h.instrument_id)
            view = {
                "instrument_id": str(h.instrument_id),
                "symbol": inst.symbol,
                "company": inst.company_name,
                "sector": inst.sector,
                "qty": int(h.quantity),
                "avg_buy_price": float(h.avg_buy_price),
                "ltp": float(ltp) if ltp is not None else None,
                "pnl_pct": float(h.pnl_pct) if h.pnl_pct is not None else None,
                "first_buy_date": h.first_buy_date.isoformat() if h.first_buy_date else None,
                "days_held": (
                    (as_of.date() - h.first_buy_date.date()).days
                    if h.first_buy_date else None
                ),
                "is_intraday": (
                    h.first_buy_date is not None
                    and h.first_buy_date.date() == as_of.date()
                ),
            }
            view.update(await self._enrich_holding(portfolio_id, h.instrument_id, as_of))
            holdings_view.append(view)

        # Candidate buys: fresh signals + watchlist with LTP
        candidates: list[dict[str, Any]] = []
        if decision_type in (DECISION_MORNING, DECISION_INTRADAY):
            since = as_of - timedelta(hours=24 if decision_type == DECISION_MORNING else 6)
            async with session_scope() as session:
                # Recent BUY signals (deduped per instrument by latest)
                sig_q = (
                    select(Signal, Instrument)
                    .join(Instrument, Instrument.id == Signal.instrument_id)
                    .where(
                        Signal.action.in_(["BUY", "WATCH"]),
                        Signal.signal_date >= since,
                        Signal.status == "active",
                    )
                    .order_by(desc(Signal.signal_date))
                    .limit(60)
                )
                sig_rows = list((await session.execute(sig_q)).all())

                # Watchlist instruments
                wl_q = (
                    select(WatchlistItem, Instrument)
                    .join(Watchlist, Watchlist.id == WatchlistItem.watchlist_id)
                    .join(Instrument, Instrument.id == WatchlistItem.instrument_id)
                )
                wl_rows = list((await session.execute(wl_q)).all())

            seen: set[str] = set()
            for sig, inst in sig_rows:
                if str(inst.id) in seen:
                    continue
                seen.add(str(inst.id))
                ltp = await self.price_service.latest_price(inst.id)
                if ltp is None:
                    continue
                cand = {
                    "instrument_id": str(inst.id),
                    "symbol": inst.symbol,
                    "company": inst.company_name,
                    "sector": inst.sector,
                    "ltp": float(ltp),
                    "source": "signal",
                    "action": sig.action,
                    "confidence": float(sig.confidence) if sig.confidence is not None else None,
                    "convergence_score": int(sig.convergence_score or 1),
                    "target_price": float(sig.target_price) if sig.target_price else None,
                    "stop_loss": float(sig.stop_loss) if sig.stop_loss else None,
                    "reasoning": (sig.reasoning or "")[:300],
                    "signal_age_min": int((as_of - sig.signal_date).total_seconds() // 60),
                }
                cand.update(await self._enrich_candidate(inst.id, float(ltp), as_of))
                candidates.append(cand)
            for wi, inst in wl_rows:
                if str(inst.id) in seen:
                    continue
                seen.add(str(inst.id))
                ltp = await self.price_service.latest_price(inst.id)
                if ltp is None:
                    continue
                cand = {
                    "instrument_id": str(inst.id),
                    "symbol": inst.symbol,
                    "company": inst.company_name,
                    "sector": inst.sector,
                    "ltp": float(ltp),
                    "source": "watchlist",
                    "target_buy_price": float(wi.target_buy_price) if wi.target_buy_price else None,
                }
                cand.update(await self._enrich_candidate(inst.id, float(ltp), as_of))
                candidates.append(cand)

            # Default-universe fallback: if signals + watchlist yielded nothing,
            # show the LLM a small set of liquid F&O instruments with prices so
            # the morning job can still propose something on a quiet news day.
            if not candidates and decision_type == DECISION_MORNING:
                async with session_scope() as session:
                    fallback_rows = list((await session.execute(
                        select(Instrument)
                        .where(
                            Instrument.is_fno == True,  # noqa: E712
                            Instrument.is_active == True,  # noqa: E712
                        )
                        .order_by(Instrument.symbol.asc())
                        .limit(40)
                    )).scalars())
                for inst in fallback_rows:
                    if str(inst.id) in seen:
                        continue
                    ltp = await self.price_service.latest_price(inst.id)
                    if ltp is None:
                        continue
                    seen.add(str(inst.id))
                    cand = {
                        "instrument_id": str(inst.id),
                        "symbol": inst.symbol,
                        "company": inst.company_name,
                        "sector": inst.sector,
                        "ltp": float(ltp),
                        "source": "default_universe",
                    }
                    cand.update(await self._enrich_candidate(inst.id, float(ltp), as_of))
                    candidates.append(cand)
                if candidates:
                    logger.info(
                        f"strategist: no signals/watchlist candidates — "
                        f"using {len(candidates)} default-universe fallbacks"
                    )

            if not candidates:
                logger.warning(
                    "strategist: no candidates with prices available — "
                    "check price_ticks/price_daily and watchlist contents"
                )

        # Pre-filter: rank then take top-N. Done after enrichment so the
        # ranker can read distinct_sources_48h / analyst credibility from
        # the enriched dict. Keeps the LLM input bounded regardless of
        # signal volume on a noisy news day.
        if len(candidates) > _CANDIDATE_LIMIT:
            candidates.sort(key=_rank_score, reverse=True)
            dropped = len(candidates) - _CANDIDATE_LIMIT
            candidates = candidates[:_CANDIDATE_LIMIT]
            logger.info(
                f"strategist: trimmed candidates to top {_CANDIDATE_LIMIT} "
                f"(dropped {dropped})"
            )

        mode = self.settings.equity_strategy_mode
        if mode == "lumpsum":
            pos_cap_pct = self.settings.equity_strategy_pos_cap_pct_lumpsum
            cap_basis = max(current_value + cash, cash)
        else:
            pos_cap_pct = self.settings.equity_strategy_pos_cap_pct_sip
            cap_basis = max(self.settings.equity_strategy_daily_budget, cash)

        # Unified strategy budget block — shows the LLM the common pool size
        # and the *previous* day's allocation (if any) so today's split is
        # an informed choice rather than blind. Defaults fill in on day-one
        # before any allocation row exists.
        from src.trading.budget_allocator import default_plan, today_allocations
        try:
            prior_plan = await today_allocations(as_of)
        except Exception:
            prior_plan = default_plan()
        strategy_budget = {
            "total_pool": prior_plan.total_budget,
            "previous_allocations": prior_plan.allocations,
            "previous_rupee_caps": prior_plan.rupee_caps,
            "previous_source": prior_plan.source,
            "buckets": [
                "equity",
                "fno_directional",
                "fno_spread",
                "fno_volatility",
            ],
        }

        # Order matters: prompt JSON is hard-truncated at the byte budget in
        # _build_prompt to keep token cost bounded. Put short, mandatory
        # context (regime, portfolio, holdings, fno_candidates) BEFORE the
        # long ``candidates`` list, so a fat equity universe can never push
        # the FNO block off the end. We learned this the hard way: the
        # original layout had fno_candidates last, the equity list ate the
        # 24k budget, and the LLM correctly reported "no FNO candidates".
        return {
            "as_of": as_of.isoformat(),
            "decision_type": decision_type,
            "mode": mode,
            "risk_profile": self.settings.equity_strategy_risk_profile,
            "cash_available": cash,
            "invested_value": invested,
            "current_value": current_value,
            "day_pnl": day_pnl,
            "daily_budget": self.settings.equity_strategy_daily_budget,
            "strategy_budget": strategy_budget,
            "per_position_cap_pct": pos_cap_pct,
            "per_position_cap_rupees": round(cap_basis * pos_cap_pct, 2),
            "max_intraday_calls": self.settings.equity_strategy_max_intraday_calls,
            "market": market,
            "portfolio_context": portfolio_context,
            "holdings": holdings_view,
            "today_equity_trades": today_equity_trades,
            "fno_open_positions": fno_open_positions,
            "fno_candidates": fno_candidates,
            "candidates": candidates,
        }

    # ----------------------------------------------------- enrichment helpers

    async def _market_context(self, as_of: datetime) -> dict[str, Any]:
        """Pull VIX, NIFTY day change, and FII/DII flow for regime framing.

        All queries filter by ``as_of`` so historical replays see the
        regime that was visible at decision time, not today's regime.
        """
        ctx: dict[str, Any] = {}
        try:
            async with session_scope() as session:
                row = (await session.execute(
                    text(
                        "SELECT vix_value, regime, timestamp FROM vix_ticks "
                        "WHERE timestamp <= :asof "
                        "ORDER BY timestamp DESC LIMIT 1"
                    ),
                    {"asof": as_of},
                )).first()
                if row:
                    ctx["vix_value"] = float(row[0]) if row[0] is not None else None
                    ctx["vix_regime"] = row[1]
                    ctx["vix_as_of"] = row[2].isoformat() if row[2] else None
        except Exception as exc:
            logger.debug(f"market_context: vix unavailable: {exc}")

        try:
            async with session_scope() as session:
                row = (await session.execute(
                    text(
                        "SELECT pd.change_pct, pd.close, pd.date "
                        "FROM price_daily pd JOIN instruments i "
                        "  ON i.id = pd.instrument_id "
                        "WHERE i.symbol IN ('NIFTY 50','NIFTY','NIFTY50') "
                        "  AND pd.date <= :asof "
                        "ORDER BY pd.date DESC LIMIT 1"
                    ),
                    {"asof": as_of.date()},
                )).first()
                if row:
                    ctx["nifty_change_pct"] = float(row[0]) if row[0] is not None else None
                    ctx["nifty_close"] = float(row[1]) if row[1] is not None else None
                    ctx["nifty_close_date"] = row[2].isoformat() if row[2] else None
        except Exception as exc:
            logger.debug(f"market_context: nifty unavailable: {exc}")

        try:
            async with session_scope() as session:
                row = (await session.execute(
                    text(
                        "SELECT fii_flow_cr, dii_flow_cr, timestamp "
                        "FROM market_sentiment "
                        "WHERE timestamp <= :asof "
                        "ORDER BY timestamp DESC LIMIT 1"
                    ),
                    {"asof": as_of},
                )).first()
                if row:
                    ctx["fii_flow_cr"] = float(row[0]) if row[0] is not None else None
                    ctx["dii_flow_cr"] = float(row[1]) if row[1] is not None else None
                    ctx["flow_as_of"] = row[2].isoformat() if row[2] else None
        except Exception as exc:
            logger.debug(f"market_context: flows unavailable: {exc}")

        return ctx

    async def _portfolio_context(
        self, portfolio_id: uuid.UUID, as_of: datetime
    ) -> dict[str, Any]:
        """Sector exposure, pending orders, and today's realised P&L."""
        ctx: dict[str, Any] = {}
        try:
            async with session_scope() as session:
                rows = list((await session.execute(
                    text(
                        "SELECT i.sector, "
                        "       COALESCE(SUM(h.current_value), 0) AS exposure, "
                        "       COUNT(*) AS positions "
                        "FROM holdings h JOIN instruments i "
                        "  ON i.id = h.instrument_id "
                        "WHERE h.portfolio_id = :pid AND h.quantity > 0 "
                        "GROUP BY i.sector "
                        "ORDER BY exposure DESC"
                    ),
                    {"pid": str(portfolio_id)},
                )).all())
                ctx["sector_exposure"] = [
                    {
                        "sector": r[0] or "Unknown",
                        "exposure": float(r[1]) if r[1] is not None else 0.0,
                        "positions": int(r[2]),
                    }
                    for r in rows
                ]
        except Exception as exc:
            logger.debug(f"portfolio_context: sector exposure failed: {exc}")
            ctx["sector_exposure"] = []

        try:
            async with session_scope() as session:
                rows = list((await session.execute(
                    text(
                        "SELECT po.trade_type, po.order_type, po.quantity, "
                        "       po.limit_price, po.trigger_price, i.symbol "
                        "FROM pending_orders po JOIN instruments i "
                        "  ON i.id = po.instrument_id "
                        "WHERE po.portfolio_id = :pid AND po.status = 'pending' "
                        "ORDER BY po.created_at DESC LIMIT 20"
                    ),
                    {"pid": str(portfolio_id)},
                )).all())
                ctx["pending_orders"] = [
                    {
                        "symbol": r[5],
                        "side": r[0],
                        "type": r[1],
                        "qty": int(r[2]) if r[2] is not None else 0,
                        "limit_price": float(r[3]) if r[3] is not None else None,
                        "trigger_price": float(r[4]) if r[4] is not None else None,
                    }
                    for r in rows
                ]
        except Exception as exc:
            logger.debug(f"portfolio_context: pending orders failed: {exc}")
            ctx["pending_orders"] = []

        try:
            day_start = as_of.replace(hour=0, minute=0, second=0, microsecond=0)
            async with session_scope() as session:
                row = (await session.execute(
                    text(
                        "SELECT "
                        "  COUNT(*) FILTER (WHERE trade_type='BUY') AS buys, "
                        "  COUNT(*) FILTER (WHERE trade_type='SELL') AS sells, "
                        "  COALESCE(SUM(pnl) FILTER (WHERE pnl IS NOT NULL), 0) AS day_pnl "
                        "FROM trades "
                        "WHERE portfolio_id = :pid AND executed_at >= :since"
                    ),
                    {"pid": str(portfolio_id), "since": day_start},
                )).first()
                if row:
                    ctx["today_buys"] = int(row[0])
                    ctx["today_sells"] = int(row[1])
                    ctx["today_realised_pnl"] = float(row[2]) if row[2] is not None else 0.0
        except Exception as exc:
            logger.debug(f"portfolio_context: today trades failed: {exc}")

        return ctx

    async def _fno_phase3_candidates(self, as_of: datetime) -> list[dict[str, Any]]:
        """Today's PROCEED options ideas for the LLM to balance against equities.

        Pulled directly from ``fno_candidates`` rather than going through
        ``entry_engine.propose_entries`` — that path requires a populated
        live chain and is the source of truth for the 09:15 auto-fire. Here
        we only need enough signal for the LLM to surface them in the
        morning plan.
        """
        out: list[dict[str, Any]] = []
        try:
            async with session_scope() as session:
                rows = list((await session.execute(
                    text(
                        "SELECT i.symbol, fc.composite_score, fc.iv_regime, "
                        "       fc.oi_structure, fc.llm_thesis, fc.run_date "
                        "FROM fno_candidates fc JOIN instruments i "
                        "  ON i.id = fc.instrument_id "
                        "WHERE fc.run_date = :rd "
                        "  AND fc.phase = 3 "
                        "  AND fc.llm_decision = 'PROCEED' "
                        "  AND fc.dryrun_run_id IS NULL "
                        "ORDER BY fc.composite_score DESC NULLS LAST "
                        "LIMIT 20"
                    ),
                    {"rd": as_of.date()},
                )).all())
                for r in rows:
                    out.append({
                        "underlying": r[0],
                        "composite_score": float(r[1]) if r[1] is not None else None,
                        "iv_regime": r[2] or "n/a",
                        "oi_structure": r[3] or "tbd",
                        "thesis": (r[4] or "")[:240],
                    })
        except Exception as exc:
            logger.debug(f"fno_phase3_candidates failed: {exc}")
        return out

    async def _fno_open_positions(self, as_of: datetime) -> list[dict[str, Any]]:
        """Live F&O paper book — every position the LLM can decide to close.

        Pulled from ``fno_signals`` with status in the LIVE set used by
        Phase 4's position manager. Each row carries the ``signal_id``;
        the LLM must echo it back in an FNO SELL action so the runner
        knows which position to close. We deliberately surface the
        per-leg structure (strikes/option_type/lots) and the net
        entry/target/stop premiums so the model can reason about both
        directional move and time decay without a separate MTM call.
        """
        out: list[dict[str, Any]] = []
        try:
            async with session_scope() as session:
                rows = list((await session.execute(
                    text(
                        "SELECT fs.id, i.symbol, fs.strategy_type, fs.expiry_date, "
                        "       fs.legs, fs.entry_premium_net, fs.target_premium_net, "
                        "       fs.stop_premium_net, fs.status, fs.proposed_at, "
                        "       fs.filled_at "
                        "FROM fno_signals fs JOIN instruments i "
                        "  ON i.id = fs.underlying_id "
                        "WHERE fs.status IN ('paper_filled','active','scaled_out_50') "
                        "  AND fs.dryrun_run_id IS NULL "
                        "ORDER BY fs.filled_at DESC NULLS LAST "
                        "LIMIT 30"
                    ),
                )).all())
                for r in rows:
                    filled_at = r[10] or r[9]
                    days_held = None
                    if filled_at is not None:
                        days_held = (as_of.date() - filled_at.date()).days
                    out.append({
                        "signal_id": str(r[0]),
                        "symbol": r[1],
                        "strategy": r[2],
                        "expiry_date": r[3].isoformat() if r[3] else None,
                        "legs": r[4] or [],
                        "entry_premium_net": float(r[5]) if r[5] is not None else None,
                        "target_premium_net": float(r[6]) if r[6] is not None else None,
                        "stop_premium_net": float(r[7]) if r[7] is not None else None,
                        "status": r[8],
                        "filled_at": filled_at.isoformat() if filled_at else None,
                        "days_held": days_held,
                        "is_today": (filled_at.date() == as_of.date()) if filled_at else False,
                    })
        except Exception as exc:
            logger.debug(f"_fno_open_positions failed: {exc}")
        return out

    async def _today_equity_trades(
        self, portfolio_id: uuid.UUID, as_of: datetime
    ) -> list[dict[str, Any]]:
        """Today's equity executions for this portfolio — the intraday LLM's ledger.

        ``holdings`` already shows net positions, but the intraday LLM also
        needs the per-trade record (especially when it has rebalanced — a
        BUY followed by a SELL same-day collapses in holdings). Each row
        names ``trade_id`` so a future SELL action could reference it.
        """
        out: list[dict[str, Any]] = []
        try:
            async with session_scope() as session:
                rows = list((await session.execute(
                    text(
                        "SELECT t.id, i.symbol, t.trade_type, t.quantity, "
                        "       t.price, t.total_cost, t.executed_at, t.entry_reason "
                        "FROM trades t JOIN instruments i "
                        "  ON i.id = t.instrument_id "
                        "WHERE t.portfolio_id = :pid "
                        "  AND date(t.executed_at) = :asof_date "
                        "ORDER BY t.executed_at ASC"
                    ),
                    {
                        "pid": str(portfolio_id),
                        "asof_date": as_of.date(),
                    },
                )).all())
                for r in rows:
                    out.append({
                        "trade_id": str(r[0]),
                        "symbol": r[1],
                        "side": r[2],
                        "qty": int(r[3]),
                        "price": float(r[4]) if r[4] is not None else None,
                        "total_cost": float(r[5]) if r[5] is not None else None,
                        "executed_at": r[6].isoformat() if r[6] else None,
                        "entry_reason": (r[7] or "")[:200],
                    })
        except Exception as exc:
            logger.debug(f"_today_equity_trades failed: {exc}")
        return out

    async def _enrich_candidate(
        self, instrument_id: uuid.UUID, ltp: float, as_of: datetime
    ) -> dict[str, Any]:
        """Per-candidate news titles, top analyst score, price stats, convergence."""
        out: dict[str, Any] = {}
        try:
            async with session_scope() as session:
                rows = list((await session.execute(
                    text(
                        "SELECT DISTINCT rc.title, rc.published_at "
                        "FROM raw_content rc JOIN signals s "
                        "  ON s.content_id = rc.id "
                        "WHERE s.instrument_id = :iid "
                        "  AND rc.published_at >= :since "
                        "ORDER BY rc.published_at DESC LIMIT 3"
                    ),
                    {"iid": str(instrument_id), "since": as_of - timedelta(days=7)},
                )).all())
                out["recent_news"] = [
                    {
                        "title": (r[0] or "")[:160],
                        "published_at": r[1].isoformat() if r[1] else None,
                    }
                    for r in rows
                ]
        except Exception as exc:
            logger.debug(f"enrich_candidate news failed: {exc}")
            out["recent_news"] = []

        try:
            async with session_scope() as session:
                row = (await session.execute(
                    text(
                        "SELECT MAX(a.credibility_score), MAX(a.hit_rate), "
                        "       SUM(a.total_signals) "
                        "FROM analysts a JOIN signals s ON s.analyst_id = a.id "
                        "WHERE s.instrument_id = :iid AND s.signal_date >= :since"
                    ),
                    {
                        "iid": str(instrument_id),
                        "since": as_of - timedelta(days=30),
                    },
                )).first()
                if row and row[0] is not None:
                    out["top_analyst_credibility"] = float(row[0])
                    out["top_analyst_hit_rate"] = float(row[1]) if row[1] is not None else None
                    out["analyst_total_signals"] = int(row[2]) if row[2] else None
        except Exception as exc:
            logger.debug(f"enrich_candidate analyst failed: {exc}")

        try:
            async with session_scope() as session:
                row = (await session.execute(
                    text(
                        "SELECT COUNT(DISTINCT source_id) "
                        "FROM signals "
                        "WHERE instrument_id = :iid "
                        "  AND signal_date >= :since "
                        "  AND action IN ('BUY','WATCH') "
                        "  AND status = 'active'"
                    ),
                    {
                        "iid": str(instrument_id),
                        "since": as_of - timedelta(hours=48),
                    },
                )).first()
                if row:
                    out["distinct_sources_48h"] = int(row[0])
        except Exception as exc:
            logger.debug(f"enrich_candidate convergence failed: {exc}")

        try:
            async with session_scope() as session:
                rows = list((await session.execute(
                    text(
                        "SELECT close FROM price_daily "
                        "WHERE instrument_id = :iid AND date <= :asof "
                        "ORDER BY date DESC LIMIT 200"
                    ),
                    {"iid": str(instrument_id), "asof": as_of.date()},
                )).all())
                closes = [float(r[0]) for r in rows if r[0] is not None]
                if len(closes) >= 6:
                    five_d = (closes[0] - closes[5]) / closes[5] * 100
                    out["return_5d_pct"] = round(five_d, 2)
                if closes:
                    dma_window = closes[: min(len(closes), 200)]
                    dma = sum(dma_window) / len(dma_window)
                    out["dma_200"] = round(dma, 2)
                    out["pct_from_200dma"] = round((ltp - dma) / dma * 100, 2)
        except Exception as exc:
            logger.debug(f"enrich_candidate price stats failed: {exc}")

        return out

    async def _enrich_holding(
        self, portfolio_id: uuid.UUID, instrument_id: uuid.UUID, as_of: datetime
    ) -> dict[str, Any]:
        """Original entry reason from the opening trade + most recent news headline."""
        out: dict[str, Any] = {}
        try:
            async with session_scope() as session:
                row = (await session.execute(
                    text(
                        "SELECT entry_reason, executed_at FROM trades "
                        "WHERE portfolio_id = :pid AND instrument_id = :iid "
                        "  AND trade_type = 'BUY' AND status = 'open' "
                        "ORDER BY executed_at ASC LIMIT 1"
                    ),
                    {"pid": str(portfolio_id), "iid": str(instrument_id)},
                )).first()
                if row:
                    out["entry_reason"] = (row[0] or "")[:280]
                    out["entry_at"] = row[1].isoformat() if row[1] else None
        except Exception as exc:
            logger.debug(f"enrich_holding entry trade failed: {exc}")

        try:
            async with session_scope() as session:
                row = (await session.execute(
                    text(
                        "SELECT rc.title, rc.published_at "
                        "FROM raw_content rc JOIN signals s "
                        "  ON s.content_id = rc.id "
                        "WHERE s.instrument_id = :iid "
                        "  AND rc.published_at >= :since "
                        "ORDER BY rc.published_at DESC LIMIT 1"
                    ),
                    {"iid": str(instrument_id), "since": as_of - timedelta(days=3)},
                )).first()
                if row:
                    out["latest_headline"] = (row[0] or "")[:160]
                    out["headline_at"] = row[1].isoformat() if row[1] else None
        except Exception as exc:
            logger.debug(f"enrich_holding news failed: {exc}")

        return out

    async def _build_prompt(
        self,
        decision_type: str,
        snapshot: dict[str, Any],
        *,
        portfolio_id: uuid.UUID | None = None,
        as_of: datetime | None = None,
    ) -> str:
        if decision_type == DECISION_MORNING:
            instruction = (
                "ROLE: Pre-market allocator AND budget splitter. Market opens "
                "at 09:15 IST. You produce TWO things in one JSON output:\n"
                " (A) `allocations` — fractions of the common capital pool "
                "(sum to 1.0) for the four buckets {equity, fno_directional, "
                "fno_spread, fno_volatility} based on today's regime.\n"
                " (B) `actions` — the BUY/HOLD calls to execute now.\n\n"
                "ALLOCATION CHECKLIST (decide first, before actions):\n"
                "1. VIX regime drives the spread/directional/volatility split:\n"
                "   - high   → tilt to fno_spread (defined-risk) and equity, "
                "     trim fno_directional, allow fno_volatility for premium "
                "     selling (straddle).\n"
                "   - neutral → balanced split, your discretion.\n"
                "   - low    → favour fno_directional + equity momentum, "
                "     trim spread/volatility (low IV makes premium selling poor).\n"
                "2. Catalyst supply: if `fno_candidates` is empty, push the "
                "   F&O share toward zero and concentrate in equity. If 5+ "
                "   strong fno_candidates exist, allow F&O share up to 60%.\n"
                "3. NIFTY day move: > +0.8% with FII inflow → bullish, lean "
                "   fno_directional + equity. < -0.8% with DII selling → "
                "   risk-off, lean fno_spread (defined risk).\n"
                "4. Floor: every bucket gets at least 0.05 unless its catalyst "
                "   pool is empty. Cap: no single bucket > 0.65.\n\n"
                "ACTION CHECKLIST:\n"
                "5. Rank equity candidates by (distinct_sources_48h DESC, "
                "   top_analyst_credibility DESC, confidence DESC). Prefer "
                "   names with concrete recent_news catalysts.\n"
                "6. Trend filter: if pct_from_200dma < -15 AND only 1 source, "
                "   skip (falling-knife). If return_5d_pct > +12 with no fresh "
                "   catalyst, skip (chase risk).\n"
                "7. Sector cap: do not push any sector above 35% of NAV "
                "   (60% if risk_profile='aggressive').\n"
                "8. Set `deploy_now_pct` ∈ [0,1]. Reserve cash is fine.\n"
                "9. Options mix: when `fno_candidates` has any item with "
                "   composite_score ≥ 60, INCLUDE at least one FNO action. "
                "   Use underlying symbol, qty=lots (default 1), strategy in "
                "   `reason`. Phase 4 entry job at 09:15 executes them.\n\n"
                "OUTPUT: `allocations` block + `actions` list. Empty actions "
                "array is acceptable if no candidate clears the bar — but "
                "`allocations` is mandatory and must always sum to ~1.0."
            )
        elif decision_type == DECISION_INTRADAY:
            instruction = (
                "ROLE: Intraday risk manager + opportunistic trader across "
                "BOTH books — equity holdings AND open F&O option positions. "
                "Market is open. You manage the unified book made of:\n"
                "  - `holdings` (equity, with avg_buy_price + ltp + pnl_pct)\n"
                "  - `today_equity_trades` (every BUY/SELL since 00:00 IST)\n"
                "  - `fno_open_positions` (every paper-filled options trade "
                "    still live, with signal_id, legs, entry/target/stop "
                "    premiums, days_held)\n"
                "  - new equity `candidates` and `fno_candidates` you can add\n\n"
                "DECISION CHECKLIST:\n"
                "1. EQUITY holdings: SELL if pnl_pct >= +3% AND no fresh "
                "   bullish news (lock in); SELL if pnl_pct <= -2% AND "
                "   latest_headline turns bearish or thesis broken (cut "
                "   loss); else HOLD.\n"
                "2. F&O OPEN POSITIONS: review every fno_open_positions row. "
                "   Propose SELL (asset_class='FNO', signal_id=<row.signal_id>) "
                "   when: (a) the underlying has reversed against the option "
                "   direction with >0.8% intraday move AND iv_regime has "
                "   spiked, (b) days_held >= 1 with no further catalyst (theta "
                "   bleed on long premium), or (c) a stronger fno_candidate "
                "   on the same underlying replaces the thesis. SELL is a "
                "   manual close; the runner stamps status='closed_manual'. "
                "   For multi-leg strategies (iron_condor/straddle/spreads) "
                "   close the whole position via the leg-1 signal_id.\n"
                "3. Rotation: if a NEW candidate has distinct_sources_48h >= "
                "   3 AND a holding is flat/red AND swap improves convergence "
                "   weighted exposure, propose SELL old + BUY new.\n"
                "4. Re-deploy reserve cash from morning ONLY when a higher "
                "   conviction setup appears (multi-source + recent_news "
                "   catalyst). Never spend reserve on watchlist-only items "
                "   intraday.\n"
                "5. Honour pending_orders — do not duplicate them.\n"
                "6. Avoid churn: if no edge changed since last decision, "
                "   return empty actions. But ALWAYS scan fno_open_positions "
                "   — silently holding a position that meets a SELL trigger "
                "   is a failure.\n\n"
                "OUTPUT: HOLD/SELL/BUY actions across asset_class IN "
                "{EQUITY, FNO}. EQUITY SELL needs instrument_id+qty; "
                "EQUITY BUY needs instrument_id+qty+approx_price; "
                "FNO SELL needs signal_id (from fno_open_positions); "
                "FNO BUY (informational, Phase 4 auto-fires) needs symbol. "
                "Empty array is acceptable but only after explicit review."
            )
        else:  # EOD
            instruction = (
                "ROLE: Square-off arbiter. It is ~15:20 IST, 10 min before "
                "close. Decide which intraday positions to close and which "
                "to convert to delivery (hold overnight).\n\n"
                "REVISED EOD POLICY (post-2026-05-05 review — "
                "do NOT blanket-flatten on high-VIX):\n"
                "1. SELL when (a) confidence < 0.75 OR (b) pnl_pct in "
                "   [-0.5%, +1%] (noise band — costs eat the edge) OR "
                "   (c) the entry catalyst has been invalidated today.\n"
                "2. HOLD when (a) confidence >= 0.80 AND (b) catalyst is "
                "   multi-session (Brent thesis, sectoral rotation, M&A) "
                "   AND (c) no adverse news today. For these, set the "
                "   overnight mental stop at entry * 0.985 (-1.5%).\n"
                "3. risk_profile influences only the borderline cases:\n"
                "   - safe       → tilt toward SELL on borderline holds.\n"
                "   - balanced   → use the rules above as-is.\n"
                "   - aggressive → tilt toward HOLD on borderline holds.\n"
                "4. High-VIX is CONTEXT, not an automatic flatten trigger. "
                "   The previous override that closed all intraday positions "
                "   in high-VIX paired with same-day entries was a policy "
                "   bug — entries already require >=0.75 confidence; "
                "   accepted entries should be allowed to ride if the "
                "   thesis holds.\n"
                "5. Non-intraday holdings (is_intraday=false) are out of "
                "   scope — leave them alone.\n\n"
                "OUTPUT: SELL actions only for positions to close. "
                "qty=current quantity. `reasoning` should cite which clause "
                "(1a/1b/1c, 2, etc.) drove the decision per position."
            )

        # Dynamic prompt enrichment: open book + recent self-track-record +
        # versioned lessons. Built fresh per call so the LLM always sees
        # current positions and the most recent post-mortems. The block is
        # appended AFTER the instructions but BEFORE the JSON-shape and
        # snapshot — that keeps the directive text on top while still giving
        # the model a chance to read the gates before it commits to actions.
        enrichment_block = ""
        if portfolio_id is not None:
            try:
                from src.trading.prompt_context import build_full_enrichment
                enrichment_block = await build_full_enrichment(
                    portfolio_id=portfolio_id,
                    asset_class="EQUITY",
                    as_of=as_of,
                    outcomes_window_days=10,
                    lessons_lookback_days=60,
                    lessons_limit=8,
                )
            except Exception as exc:
                logger.debug(f"strategist: enrichment block skipped: {exc}")

        head = f"{instruction}\n\n"
        if enrichment_block:
            head = f"{head}{enrichment_block}\n\n"
        return head + (
            "Return JSON of shape:\n"
            "{\n"
            "  \"reasoning\": \"2-4 sentences on overall plan and risk view\",\n"
            "  \"deploy_now_pct\": 0.0,  // morning only; omit for intraday/eod\n"
            "  \"allocations\": {        // morning only; fractions sum to 1.0\n"
            "      \"equity\": 0.50,\n"
            "      \"fno_directional\": 0.25,\n"
            "      \"fno_spread\": 0.15,\n"
            "      \"fno_volatility\": 0.10\n"
            "  },\n"
            "  \"actions\": [\n"
            "    {\n"
            "      \"asset_class\": \"EQUITY|FNO\",\n"
            "      \"instrument_id\": \"uuid (required for EQUITY)\",\n"
            "      \"signal_id\": \"uuid (required for FNO SELL)\",\n"
            "      \"symbol\": \"NSE symbol or FNO underlying\",\n"
            "      \"action\": \"BUY|SELL|HOLD\",\n"
            "      \"qty\": 0,\n"
            "      \"approx_price\": 0.0,\n"
            "      \"reason\": \"one short sentence\"\n"
            "    }\n"
            "  ]\n"
            "}\n\n"
            "Inputs:\n"
            f"{json.dumps(snapshot, default=str)[:120000]}"
        )

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=20))
    async def _call_llm(
        self, prompt: str, model: str
    ) -> tuple[dict[str, Any] | None, int, int, int, str]:
        # Opus 4.7 deprecated `temperature` — omit it and let the model use
        # its default. Sonnet/Haiku still accept it but the marginal benefit
        # of temperature=0.2 here is negligible vs. portability.
        #
        # System prompt is wrapped in a cache_control block so retries within
        # the 5-minute TTL skip re-billing the system tokens. The user-side
        # prompt is dynamic (snapshot changes per call) so it is NOT cached.
        # The win is small for our cadence (one morning + ~6 intraday calls
        # 30+ min apart) but the cost of adding it is one extra dict layer.
        t0 = time.monotonic()
        msg = await self.client.messages.create(
            model=model,
            max_tokens=2000,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": prompt}],
        )
        latency_ms = int((time.monotonic() - t0) * 1000)
        raw = "".join(
            block.text for block in msg.content if getattr(block, "type", None) == "text"
        )
        tokens_in = msg.usage.input_tokens or 0
        tokens_out = msg.usage.output_tokens or 0
        try:
            parsed = json.loads(_strip_code_fence(raw))
        except json.JSONDecodeError:
            logger.warning(f"strategist LLM returned non-JSON: {raw[:200]}")
            parsed = None
        return parsed, tokens_in, tokens_out, latency_ms, raw

    @staticmethod
    def _normalise_actions(parsed: dict[str, Any] | None) -> list[dict[str, Any]]:
        """Coerce the LLM's actions into a clean, executable shape.

        Accepts ``asset_class`` ∈ {EQUITY, FNO}. EQUITY actions need a
        non-empty ``instrument_id`` (the runner executes them via the
        equity engine). FNO actions are informational — the runner appends
        them to the digest only — so an empty ``instrument_id`` is fine.
        """
        if not parsed:
            return []
        out: list[dict[str, Any]] = []
        for raw in parsed.get("actions") or []:
            if not isinstance(raw, dict):
                continue
            action = (raw.get("action") or "").upper()
            if action not in ("BUY", "SELL", "HOLD"):
                continue
            asset_class = (raw.get("asset_class") or "EQUITY").upper()
            if asset_class not in ("EQUITY", "FNO"):
                asset_class = "EQUITY"
            qty = raw.get("qty")
            try:
                qty_int = int(qty) if qty is not None else 0
            except (TypeError, ValueError):
                qty_int = 0
            if action != "HOLD" and qty_int <= 0 and asset_class == "EQUITY":
                continue
            out.append({
                "instrument_id": str(raw.get("instrument_id") or "").strip(),
                "symbol": (raw.get("symbol") or "").strip(),
                "asset_class": asset_class,
                "action": action,
                "qty": qty_int,
                "approx_price": _float_or_none(raw.get("approx_price")),
                "reason": (raw.get("reason") or "")[:400],
                "signal_id": raw.get("signal_id"),
            })
        return out

    async def _write_audit_log(
        self,
        *,
        caller_ref_id: Any,
        prompt: str,
        response: str,
        response_parsed: dict | None,
        tokens_in: int | None,
        tokens_out: int | None,
        latency_ms: int | None,
        model: str,
    ) -> None:
        try:
            async with session_scope() as session:
                session.add(LLMAuditLog(
                    caller=self._CALLER,
                    caller_ref_id=caller_ref_id,
                    model=model,
                    temperature=self._TEMPERATURE,
                    prompt=prompt[:32000],
                    response=response,
                    response_parsed=response_parsed,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    latency_ms=latency_ms,
                ))
        except Exception as exc:
            logger.error(f"strategist audit log write failed: {exc}")
