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


SYSTEM_PROMPT = """You are an Indian equity paper-trading strategist (BSE/NSE).

You decide how to deploy a small daily cash budget across stocks based on
fresh BUY/SELL signals, current holdings, market regime, and a risk dial.

CONTEXT YOU WILL RECEIVE
- ``market`` — VIX value+regime, NIFTY 50 day change %, FII/DII net flow.
- ``portfolio_context`` — sector exposure breakdown, pending limit/SL orders,
  today's realised P&L so far, count of open intraday positions.
- ``candidates`` — for each: ltp, source (signal/watchlist/default), signal
  confidence + convergence count, top analyst credibility, recent news titles
  (≤3, last 7 days), 5-day return %, distance from 200-DMA %.
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

OUTPUT RULES
1. Output ONLY valid JSON. No markdown, no preamble.
2. Quantities must be integers; qty * approx_price must respect
   ``per_position_cap_rupees`` and remaining cash.
3. Only act on instruments that appear in the candidates list with non-null
   ltp. Do not invent symbols. ``instrument_id`` is mandatory.
4. Skipping a marginal trade is preferred over a forced one. Empty actions
   array is a valid decision when no edge is clear.
5. Reasoning per action: one sentence naming the dominant driver
   (convergence / news catalyst / momentum / mean-reversion / risk).
6. ``reasoning`` (top-level): 2-4 sentences linking regime → strategy → picks.
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
        prompt = self._build_prompt(decision_type, snapshot)

        parsed, tokens_in, tokens_out, latency_ms, raw = await self._call_llm(prompt, model)
        actions = self._normalise_actions(parsed)
        reasoning = (parsed or {}).get("reasoning") or ""

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
                actions_json={"actions": actions, "reasoning": reasoning},
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

        mode = self.settings.equity_strategy_mode
        if mode == "lumpsum":
            pos_cap_pct = self.settings.equity_strategy_pos_cap_pct_lumpsum
            cap_basis = max(current_value + cash, cash)
        else:
            pos_cap_pct = self.settings.equity_strategy_pos_cap_pct_sip
            cap_basis = max(self.settings.equity_strategy_daily_budget, cash)

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
            "per_position_cap_pct": pos_cap_pct,
            "per_position_cap_rupees": round(cap_basis * pos_cap_pct, 2),
            "max_intraday_calls": self.settings.equity_strategy_max_intraday_calls,
            "market": market,
            "portfolio_context": portfolio_context,
            "holdings": holdings_view,
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

    def _build_prompt(self, decision_type: str, snapshot: dict[str, Any]) -> str:
        if decision_type == DECISION_MORNING:
            instruction = (
                "ROLE: Pre-market allocator. The market opens at 09:15 IST. "
                "Decide how much of `cash_available` to deploy now and how to "
                "split it across BUY candidates.\n\n"
                "DECISION CHECKLIST:\n"
                "1. Read `market` first. If vix_regime='high' or "
                "   nifty_change_pct < -0.8, lean cautious (deploy ≤60%). If "
                "   vix_regime='low' and FII flow positive, lean fuller "
                "   (deploy ≥80% in balanced/aggressive).\n"
                "2. Rank candidates by (distinct_sources_48h DESC, "
                "   top_analyst_credibility DESC, confidence DESC). Prefer "
                "   names with concrete recent_news catalysts over vague "
                "   commentary.\n"
                "3. Trend filter: if pct_from_200dma < -15 AND only 1 source, "
                "   skip (falling-knife risk). If return_5d_pct > +12 with no "
                "   fresh catalyst, skip (chase risk).\n"
                "4. Sector cap: do not push any sector (incl. existing "
                "   `portfolio_context.sector_exposure`) above 35% (60% "
                "   aggressive).\n"
                "5. Set `deploy_now_pct` ∈ [0,1] = your chosen deployment "
                "   ratio. Reserve cash is fine — name it in `reasoning`.\n\n"
                "OUTPUT: BUY actions only. qty is integer; qty*ltp ≤ "
                "per_position_cap_rupees. Empty actions array is acceptable "
                "if no candidate clears the bar."
            )
        elif decision_type == DECISION_INTRADAY:
            instruction = (
                "ROLE: Intraday risk manager + opportunistic trader. Market "
                "is open. Re-evaluate every holding and any fresh signal.\n\n"
                "DECISION CHECKLIST:\n"
                "1. For each holding: SELL if pnl_pct ≥ +3% AND no fresh "
                "   bullish news (lock in); SELL if pnl_pct ≤ -2% AND "
                "   latest_headline turns bearish or thesis broken (cut "
                "   loss); else HOLD.\n"
                "2. Rotation: if a NEW candidate has distinct_sources_48h ≥ "
                "   3 AND a holding is flat/red AND swap improves convergence "
                "   weighted exposure, propose SELL old + BUY new.\n"
                "3. Re-deploy reserve cash from morning ONLY when a higher "
                "   conviction setup appears (multi-source + recent_news "
                "   catalyst). Never spend reserve on watchlist-only items "
                "   intraday.\n"
                "4. Honour pending_orders — do not duplicate them.\n"
                "5. Avoid churn: if no edge changed since last decision, "
                "   return empty actions.\n\n"
                "OUTPUT: HOLD/SELL/BUY actions. SELL needs instrument_id+qty; "
                "BUY needs instrument_id+qty+approx_price. Empty array OK."
            )
        else:  # EOD
            instruction = (
                "ROLE: Square-off arbiter. It is ~15:20 IST, 10 min before "
                "close. Decide which intraday positions to close and which "
                "to convert to delivery (hold overnight).\n\n"
                "DECISION CHECKLIST:\n"
                "1. Default by `risk_profile`:\n"
                "   - safe       → CLOSE all is_intraday=true positions.\n"
                "   - balanced   → CLOSE losers (pnl_pct < 0) and small "
                "     winners (<+1%); hold only conviction picks (entry_reason "
                "     cites multi-source/strong catalyst).\n"
                "   - aggressive → CLOSE only clearly broken theses; hold "
                "     anything still trending with the thesis intact.\n"
                "2. Override down (more selling) if vix_regime='high' OR "
                "   nifty_change_pct < -0.8 today (overnight gap risk).\n"
                "3. Override up (more holding) if a fresh strong bullish "
                "   catalyst landed in latest_headline within last 2h.\n"
                "4. Non-intraday holdings (is_intraday=false) are out of "
                "   scope — leave them alone.\n\n"
                "OUTPUT: SELL actions only for positions to close. "
                "qty=current quantity. `reasoning` should explicitly cite "
                "risk_profile and which override (if any) was applied."
            )

        return (
            f"{instruction}\n\n"
            "Return JSON of shape:\n"
            "{\n"
            "  \"reasoning\": \"2-4 sentences on overall plan and risk view\",\n"
            "  \"deploy_now_pct\": 0.0,  // morning only; omit for intraday/eod\n"
            "  \"actions\": [\n"
            "    {\n"
            "      \"instrument_id\": \"uuid\",\n"
            "      \"symbol\": \"NSE symbol\",\n"
            "      \"action\": \"BUY|SELL|HOLD\",\n"
            "      \"qty\": 0,\n"
            "      \"approx_price\": 0.0,\n"
            "      \"reason\": \"one short sentence\"\n"
            "    }\n"
            "  ]\n"
            "}\n\n"
            "Inputs:\n"
            f"{json.dumps(snapshot, default=str)[:24000]}"
        )

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=20))
    async def _call_llm(
        self, prompt: str, model: str
    ) -> tuple[dict[str, Any] | None, int, int, int, str]:
        # Opus 4.7 deprecated `temperature` — omit it and let the model use
        # its default. Sonnet/Haiku still accept it but the marginal benefit
        # of temperature=0.2 here is negligible vs. portability.
        t0 = time.monotonic()
        msg = await self.client.messages.create(
            model=model,
            max_tokens=2000,
            system=SYSTEM_PROMPT,
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
        """Coerce the LLM's actions into a clean, executable shape."""
        if not parsed:
            return []
        out: list[dict[str, Any]] = []
        for raw in parsed.get("actions") or []:
            if not isinstance(raw, dict):
                continue
            action = (raw.get("action") or "").upper()
            if action not in ("BUY", "SELL", "HOLD"):
                continue
            qty = raw.get("qty")
            try:
                qty_int = int(qty) if qty is not None else 0
            except (TypeError, ValueError):
                qty_int = 0
            if action != "HOLD" and qty_int <= 0:
                continue
            out.append({
                "instrument_id": str(raw.get("instrument_id") or "").strip(),
                "symbol": (raw.get("symbol") or "").strip(),
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
