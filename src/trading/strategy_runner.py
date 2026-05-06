"""Glue between the LLM strategist and the trading engine.

The strategist is pure-decision; the runner is impure: it executes the
proposed actions, sends a Telegram message per fill, and updates the
``strategy_decisions`` row with the executed/skipped counts so the daily
report can explain what the brain proposed vs. what actually happened.

There is one entry point per scheduler hook:

* :func:`run_morning_allocation`  — 09:10 IST job
* :func:`run_intraday_action`     — every ~hour 09:45–14:30 IST
* :func:`run_eod_squareoff`       — 15:20 IST job

Each performs:
    1. Bootstrap or top up the strategy portfolio (lumpsum vs. SIP).
    2. Ask the strategist to decide.
    3. Execute non-HOLD actions through ``TradingEngine``.
    4. Send a per-fill Telegram message and a digest summary.
    5. Update the decision row with executed/skipped tallies.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from loguru import logger
from sqlalchemy import select, update

from src.config import get_settings
from src.db import session_scope
from src.models.instrument import Instrument
from src.models.portfolio import Holding, Portfolio
from src.models.strategy_decision import StrategyDecision
from src.models.trade import Trade
from src.services.notification_service import NotificationService
from src.services.price_service import PriceService
from src.trading.engine import TradingEngine
from src.trading.equity_strategist import (
    DECISION_EOD,
    DECISION_INTRADAY,
    DECISION_MORNING,
    EquityStrategist,
)
from src.trading.risk_manager import RiskError, RiskManager

STRATEGY_PORTFOLIO_NAME = "Equity Strategy"


# ---------------------------------------------------------------- bootstrap


async def ensure_strategy_portfolio() -> uuid.UUID:
    """Return the id of the persistent equity-strategy portfolio.

    Creates it on first run with the configured initial capital. In ``sip``
    mode the initial cash is the daily budget; in ``lumpsum`` it's the
    configured lumpsum capital.
    """
    settings = get_settings()
    mode = settings.equity_strategy_mode
    initial = (
        settings.equity_strategy_lumpsum_capital
        if mode == "lumpsum"
        else settings.equity_strategy_daily_budget
    )

    async with session_scope() as session:
        result = await session.execute(
            select(Portfolio).where(Portfolio.name == STRATEGY_PORTFOLIO_NAME)
        )
        portfolio = result.scalar_one_or_none()
        if portfolio is None:
            portfolio = Portfolio(
                name=STRATEGY_PORTFOLIO_NAME,
                initial_capital=float(initial),
                current_cash=float(initial),
            )
            session.add(portfolio)
            await session.flush()
            logger.info(
                f"created {STRATEGY_PORTFOLIO_NAME} portfolio "
                f"({mode}, ₹{initial:,.0f})"
            )
        return portfolio.id


async def topup_daily_budget(portfolio_id: uuid.UUID) -> float:
    """Apply the morning cash refill rule for the strategy mode.

    * ``sip``: add ``equity_strategy_daily_budget`` to ``current_cash``;
      un-deployed cash from prior days rolls forward.
    * ``lumpsum``: no-op — capital is set once at bootstrap.

    Returns the new ``current_cash`` balance.
    """
    settings = get_settings()
    if settings.equity_strategy_mode != "sip":
        async with session_scope() as session:
            p = await session.get(Portfolio, portfolio_id)
            return float(p.current_cash if p else 0)

    budget = Decimal(str(settings.equity_strategy_daily_budget))
    async with session_scope() as session:
        p = await session.get(Portfolio, portfolio_id)
        if p is None:
            raise ValueError(f"Portfolio {portfolio_id} not found")
        new_cash = Decimal(str(p.current_cash or 0)) + budget
        p.current_cash = float(new_cash)
        new_initial = Decimal(str(p.initial_capital or 0)) + budget
        p.initial_capital = float(new_initial)
        logger.info(
            f"sip topup: +₹{budget} → cash=₹{new_cash} (initial=₹{new_initial})"
        )
        return float(new_cash)


# ---------------------------------------------------------------- execution


async def _execute_actions(
    portfolio_id: uuid.UUID,
    decision_id: uuid.UUID,
    actions: list[dict[str, Any]],
) -> tuple[int, int, list[str]]:
    """Run each action through TradingEngine. Returns (executed, skipped, lines).

    FNO BUY actions are executed via ``fno.entry_executor.auto_enter`` (the
    same Phase 4 pipeline the 09:15 IST cron uses). It's idempotent — a
    candidate that already has an FNOSignal today is skipped — so calling
    it from intraday firings is safe and lets later runs pick up fills
    that the 09:15 sweep missed. To avoid re-running ``auto_enter`` once
    per FNO BUY action in the batch, the result is memoised inside this
    call and reused for subsequent FNO BUY lines.

    On equity BUY actions, qty is clamped to ``RiskManager.max_buy_qty``
    instead of erroring out on a 10% position-cap breach. The runner
    intentionally sizes down rather than skipping — the LLM's pick is
    treated as the direction, and the risk layer enforces the size.
    """
    engine = TradingEngine()
    risk = RiskManager()
    price_service = PriceService()
    executed = 0
    skipped = 0
    lines: list[str] = []
    fno_auto_enter_result: dict | None = None
    fno_fills_after: dict[str, dict] | None = None

    for action in actions:
        kind = action["action"]
        if kind == "HOLD":
            continue

        asset_class = (action.get("asset_class") or "EQUITY").upper()
        if asset_class == "FNO":
            symbol = action.get("symbol") or "?"
            reason = (action.get("reason") or "").strip()
            # FNO SELL is a manual-close on a specific FNOSignal; FNO BUY
            # stays informational because Phase 4's entry job is the
            # canonical entry path and double-firing would dupe positions.
            if kind == "SELL":
                fno_signal_id = action.get("signal_id")
                if not fno_signal_id:
                    skipped += 1
                    lines.append(
                        f"⚠️ Skipped FNO SELL {symbol}: no signal_id provided"
                    )
                    continue
                try:
                    from src.fno.position_manager import close_fno_signal
                    result = await close_fno_signal(
                        fno_signal_id,
                        reason=f"[strategy:{decision_id}] {reason}"[:400],
                    )
                except Exception as exc:
                    skipped += 1
                    lines.append(
                        f"⚠️ Skipped FNO SELL {symbol}: {exc!r}"
                    )
                    continue
                if "skipped" in result:
                    skipped += 1
                    lines.append(
                        f"⚠️ Skipped FNO SELL {symbol}: {result['skipped']}"
                    )
                    continue
                executed += 1
                pnl = result.get("pnl", 0)
                pnl_sign = "+" if pnl >= 0 else ""
                lines.append(
                    f"✅ FNO SELL {result.get('symbol', symbol)} (manual close) "
                    f"@ Rs{result.get('exit_price', 0):,.2f} "
                    f"P&L {pnl_sign}Rs{pnl:,.2f} - {reason[:120]}"
                )
                continue
            # FNO BUY — fire the Phase 4 entry pipeline (idempotent, so
            # re-running it here on top of the 09:15 cron is safe). We call
            # auto_enter once per batch and reuse its result for subsequent
            # FNO BUY actions. Then we look up today's FNOSignal rows for
            # this symbol to report whether the LLM's specific pick filled.
            if fno_auto_enter_result is None:
                try:
                    from src.fno.entry_executor import auto_enter
                    fno_auto_enter_result = await auto_enter()
                except Exception as exc:
                    fno_auto_enter_result = {"error": repr(exc)}
                    logger.warning(f"auto_enter failed: {exc!r}")
                fno_fills_after = await _fno_fills_today_by_symbol()

            err = fno_auto_enter_result.get("error") if fno_auto_enter_result else None
            fill = (fno_fills_after or {}).get(symbol.upper()) if not err else None
            reason_tail = f" - {reason[:120]}" if reason else ""
            if err:
                skipped += 1
                lines.append(
                    f"⚠️ FNO BUY {symbol} skipped: auto_enter error {err[:160]}"
                )
            elif fill:
                executed += 1
                premium = fill.get("entry_premium_net") or 0.0
                lines.append(
                    f"✅ FNO BUY {symbol} {fill.get('strategy_type', '')} "
                    f"- net Rs {float(premium):,.0f} ({fill.get('status', '?')})"
                    f"{reason_tail}"
                )
            else:
                skipped += 1
                lines.append(
                    f"⚠️ FNO BUY {symbol} not filled: no Phase 3 PROCEED "
                    f"candidate / chain unavailable{reason_tail}"
                )
            continue

        instrument_id_raw = action.get("instrument_id")
        if not instrument_id_raw:
            skipped += 1
            continue
        try:
            instrument_id = uuid.UUID(instrument_id_raw)
        except (TypeError, ValueError):
            skipped += 1
            continue

        qty = int(action.get("qty") or 0)
        if qty <= 0:
            skipped += 1
            continue

        ltp = await price_service.latest_price(instrument_id)
        if ltp is None:
            approx = action.get("approx_price")
            ltp = float(approx) if approx else None
        if ltp is None or ltp <= 0:
            logger.warning(
                f"skip {kind} {action.get('symbol')}: no LTP available"
            )
            skipped += 1
            lines.append(
                f"⚠️ Skipped {kind} {action.get('symbol')} qty={qty}: no LTP"
            )
            continue

        clamp_note: str | None = None
        if kind == "BUY":
            allowed = await risk.max_buy_qty(
                str(portfolio_id), str(instrument_id), Decimal(str(ltp))
            )
            if allowed <= 0:
                skipped += 1
                lines.append(
                    f"⚠️ Skipped BUY {action.get('symbol')}: no room "
                    f"(cash/position cap exhausted)"
                )
                continue
            if qty > allowed:
                logger.info(
                    f"clamped BUY {action.get('symbol')} qty {qty}→{allowed} "
                    f"(position/cash cap)"
                )
                clamp_note = f" (clamped {qty}→{allowed})"
                qty = allowed

        try:
            trade = await engine.execute_market_order(
                portfolio_id=portfolio_id,
                instrument_id=instrument_id,
                trade_type=kind,
                quantity=qty,
                current_ltp=Decimal(str(ltp)),
                signal_id=action.get("signal_id"),
                reason=f"[strategy:{decision_id}] {action.get('reason') or ''}"[:1000],
            )
        except (RiskError, Exception) as exc:
            skipped += 1
            logger.warning(
                f"strategy action skipped: {kind} {action.get('symbol')} "
                f"qty={qty}: {exc}"
            )
            lines.append(
                f"⚠️ Skipped {kind} {action.get('symbol')} qty={qty}: {exc}"
            )
            continue

        executed += 1
        lines.append(
            f"✅ {kind} {qty} {action.get('symbol')} @ ₹{trade.price:,.2f}"
            f"{clamp_note or ''} — _{action.get('reason') or 'no reason'}_"
        )
    return executed, skipped, lines


async def _update_decision_counts(
    decision_id: uuid.UUID, executed: int, skipped: int
) -> None:
    async with session_scope() as session:
        await session.execute(
            update(StrategyDecision)
            .where(StrategyDecision.id == decision_id)
            .values(actions_executed=executed, actions_skipped=skipped)
        )


def _strip_md(s: str) -> str:
    """Remove markdown emphasis chars that legacy Telegram Markdown chokes on.

    LLM-generated reasoning routinely contains stray ``_``, ``*``, ``[`` or
    ``` ` ``` characters that unbalance the parser and 400 the Telegram API.
    The digest is sent as plain text now (see ``_send_summary``); this also
    strips emphasis from individual action lines so the visible output stays
    clean rather than showing literal ``_reason text_`` underscores.
    """
    return (
        s.replace("_", "")
         .replace("*", "")
         .replace("`", "")
    )


async def _fno_fills_today_by_symbol() -> dict[str, dict]:
    """Return a {symbol_upper: latest-FNOSignal-fields} map for today's fills.

    Used by ``_execute_actions`` to confirm whether an LLM-suggested FNO BUY
    actually got entered after calling ``auto_enter``. Picks the most recent
    row when a symbol has multiple fills (FIFO would also work; for the
    digest line, latest is fine).
    """
    out: dict[str, dict] = {}
    try:
        from sqlalchemy import func, select as _select

        from src.models.fno_signal import FNOSignal
        from src.models.instrument import Instrument

        async with session_scope() as session:
            result = await session.execute(
                _select(
                    Instrument.symbol,
                    FNOSignal.strategy_type,
                    FNOSignal.entry_premium_net,
                    FNOSignal.status,
                    FNOSignal.proposed_at,
                )
                .join(Instrument, Instrument.id == FNOSignal.underlying_id)
                .where(
                    func.date(FNOSignal.proposed_at) == date.today(),
                    FNOSignal.dryrun_run_id.is_(None),
                )
                .order_by(FNOSignal.proposed_at.desc())
            )
            for r in result.all():
                key = (r.symbol or "").upper()
                if key in out:
                    continue
                out[key] = {
                    "strategy_type": r.strategy_type,
                    "entry_premium_net": r.entry_premium_net,
                    "status": r.status,
                }
    except Exception as exc:
        logger.debug(f"_fno_fills_today_by_symbol: {exc!r}")
    return out


async def _summarise_today_fno_fills() -> list[str]:
    """One-line-per-fill summary of today's Phase 4 F&O entries.

    Returns a list ready to splice into the morning digest. Empty list on
    a day with no fills or when the FNO module is disabled. The F&O paper
    book lives in ``fno_signals`` (separate from the equity portfolio's
    ``trades`` table) so this is purely a read-side merge for reporting.
    """
    out: list[str] = []
    try:
        from sqlalchemy import func, select as _select

        from src.models.fno_signal import FNOSignal
        from src.models.instrument import Instrument

        async with session_scope() as session:
            result = await session.execute(
                _select(
                    Instrument.symbol,
                    FNOSignal.strategy_type,
                    FNOSignal.entry_premium_net,
                    FNOSignal.status,
                )
                .join(Instrument, Instrument.id == FNOSignal.underlying_id)
                .where(
                    func.date(FNOSignal.proposed_at) == date.today(),
                    FNOSignal.dryrun_run_id.is_(None),
                )
                .order_by(FNOSignal.proposed_at.asc())
            )
            rows = list(result.all())
        for r in rows:
            premium = float(r.entry_premium_net) if r.entry_premium_net is not None else 0.0
            out.append(
                f"  {r.symbol} {r.strategy_type} - net Rs {premium:,.0f} ({r.status})"
            )
    except Exception as exc:
        logger.debug(f"_summarise_today_fno_fills: {exc!r}")
    return out


def _fmt_money(v: float) -> str:
    """Indian-style number with thousands separators, no currency prefix."""
    return f"{v:,.2f}"


def _pad(s: str, w: int, right: bool = False) -> str:
    s = s if len(s) <= w else s[: w - 1] + "…"
    return s.rjust(w) if right else s.ljust(w)


async def _portfolio_summary_block(portfolio_id: uuid.UUID) -> str:
    """Build the day-in-review block as a monospaced Markdown code block.

    Returns a string ready to splice into the Telegram digest. The caller
    must send with ``parse_mode='Markdown'`` (legacy) so the triple-backtick
    code block renders in monospace and the columns align.

    Capital invested today = today's equity BUY ``total_cost`` (cash debit,
    incl. charges) + today's FNO ``entry_premium_net`` (positive = debit
    paid). Credit-spread entries net out to negative premium and reduce
    the "capital invested" total accordingly — that matches how cash
    actually moves on a paper credit position.

    Realized P&L per SELL is computed against (in priority): today's average
    BUY cost-per-share for that instrument, then the residual holding's
    ``avg_buy_price``. If neither is available (overnight position fully
    sold out today and no current holding) the SELL is skipped — the
    digest is a quick snapshot, not an audit.
    """
    today_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    async with session_scope() as session:
        buy_rows = await session.execute(
            select(
                Trade.instrument_id,
                Trade.quantity,
                Trade.price,
                Trade.total_cost,
                Instrument.symbol,
            )
            .join(Instrument, Instrument.id == Trade.instrument_id)
            .where(
                Trade.portfolio_id == portfolio_id,
                Trade.trade_type == "BUY",
                Trade.executed_at >= today_start,
            )
        )
        buys = list(buy_rows.all())

        sell_rows = await session.execute(
            select(
                Trade.instrument_id,
                Trade.quantity,
                Trade.price,
                Trade.total_cost,
                Instrument.symbol,
            )
            .join(Instrument, Instrument.id == Trade.instrument_id)
            .where(
                Trade.portfolio_id == portfolio_id,
                Trade.trade_type == "SELL",
                Trade.executed_at >= today_start,
            )
        )
        sells = list(sell_rows.all())

        hold_rows = await session.execute(
            select(Holding, Instrument.symbol)
            .join(Instrument, Instrument.id == Holding.instrument_id)
            .where(Holding.portfolio_id == portfolio_id)
            .order_by(Instrument.symbol.asc())
        )
        holdings = list(hold_rows.all())

    capital_invested_today = sum(float(b.total_cost or 0) for b in buys)

    today_buy_agg: dict[uuid.UUID, tuple[int, float]] = {}
    for b in buys:
        qty, cost = today_buy_agg.get(b.instrument_id, (0, 0.0))
        today_buy_agg[b.instrument_id] = (
            qty + int(b.quantity),
            cost + float(b.total_cost or 0),
        )

    holding_avg_by_instr = {
        h.instrument_id: float(h.avg_buy_price) for (h, _sym) in holdings
    }

    earned_profit = 0.0
    incurred_loss = 0.0
    for s in sells:
        cost_per_share: float | None = None
        agg = today_buy_agg.get(s.instrument_id)
        if agg and agg[0] > 0:
            cost_per_share = agg[1] / agg[0]
        elif s.instrument_id in holding_avg_by_instr:
            cost_per_share = holding_avg_by_instr[s.instrument_id]
        if cost_per_share is None:
            continue
        pnl = float(s.total_cost or 0) - cost_per_share * int(s.quantity)
        if pnl >= 0:
            earned_profit += pnl
        else:
            incurred_loss += pnl

    # Equity rows — compute MTM on-the-fly via PriceService instead of
    # trusting holdings.pnl/current_price (those are refreshed only every
    # 5 min by the update_portfolio job, so a position bought in the
    # current firing would show 0).
    price_service = PriceService()
    rows: list[tuple[str, str, float, float, float, float, float]] = []
    # tuple: (name, type, invested, rate_at_inv, cur_rate, cur_value, pnl)
    total_invested_now = 0.0
    total_current_value = 0.0
    total_pnl_now = 0.0

    for (h, sym) in holdings:
        ltp_val = await price_service.latest_price(h.instrument_id)
        if ltp_val is None:
            ltp_val = float(h.current_price) if h.current_price is not None else float(h.avg_buy_price)
        ltp = float(ltp_val)
        avg = float(h.avg_buy_price)
        qty = int(h.quantity)
        invested = float(h.invested_value or (avg * qty))
        current_val = ltp * qty
        pnl = current_val - invested
        rows.append((sym, "EQ", invested, avg, ltp, current_val, pnl))
        total_invested_now += invested
        total_current_value += current_val
        total_pnl_now += pnl

    # F&O open positions — live in fno_signals, not holdings. Pull via
    # position_manager.open_positions_summary so the user sees the full
    # equity+options book in one place. Invested = entry_price × lots ×
    # lot_size; current value uses the latest options-chain premium.
    fno_invested_today = 0.0
    try:
        from src.fno.position_manager import open_positions_summary
        fno_positions = await open_positions_summary()
    except Exception as exc:
        logger.debug(f"open_positions_summary failed: {exc!r}")
        fno_positions = []

    for p in fno_positions:
        lots = int(p.get("lots") or 0)
        lot_size = int(p.get("lot_size") or 0)
        contracts = lots * lot_size
        entry_rate = float(p.get("entry") or 0)
        cur = p.get("current")
        cur_rate = float(cur) if cur is not None else entry_rate
        invested = entry_rate * contracts
        current_val = cur_rate * contracts
        pnl = current_val - invested
        strike = p.get("strike")
        opt = p.get("option_type") or "?"
        name = f"{p.get('symbol', '?')} {strike}{opt}" if strike is not None else f"{p.get('symbol', '?')} {opt}"
        rows.append((name, "FNO", invested, entry_rate, cur_rate, current_val, pnl))
        total_invested_now += invested
        total_current_value += current_val
        total_pnl_now += pnl

    # Today's FNO premium debits — feeds into "Capital invested today" so
    # the total reflects equity cash debit + FNO premium debit. Pulled
    # from FNOSignal.entry_premium_net (debit positive, credit negative)
    # for paper-filled rows opened today.
    try:
        from sqlalchemy import func, select as _select
        from src.models.fno_signal import FNOSignal
        async with session_scope() as session:
            res = await session.execute(
                _select(FNOSignal.entry_premium_net)
                .where(
                    func.date(FNOSignal.proposed_at) == date.today(),
                    FNOSignal.dryrun_run_id.is_(None),
                )
            )
            for (prem,) in res.all():
                fno_invested_today += float(prem or 0)
    except Exception as exc:
        logger.debug(f"fno premium today aggregation failed: {exc!r}")

    capital_invested_today_total = capital_invested_today + fno_invested_today

    # Profit/loss right now buckets — split the unrealized P&L sum into
    # gainers and losers so the "if all sold" lines match the per-row P&L.
    profit_now = sum(r[6] for r in rows if r[6] >= 0)
    loss_now = sum(r[6] for r in rows if r[6] < 0)

    # Column widths chosen to fit a typical NSE symbol + a 5-digit strike +
    # CE/PE suffix. Wider names truncate with an ellipsis (see _pad).
    name_w, type_w, money_w, rate_w = 18, 4, 12, 10
    head = (
        _pad("NAME", name_w) + " " +
        _pad("TYP", type_w) + " " +
        _pad("INVESTED", money_w, right=True) + " " +
        _pad("RATE@INV", rate_w, right=True) + " " +
        _pad("CUR_RATE", rate_w, right=True) + " " +
        _pad("CUR_VAL", money_w, right=True) + " " +
        _pad("P&L", money_w, right=True)
    )
    sep = "-" * len(head)

    table_lines: list[str] = [head, sep]
    if rows:
        for (name, typ, inv, rate_inv, cur_rate, cur_val, pnl) in rows:
            sign = "+" if pnl >= 0 else ""
            table_lines.append(
                _pad(name, name_w) + " " +
                _pad(typ, type_w) + " " +
                _pad(_fmt_money(inv), money_w, right=True) + " " +
                _pad(_fmt_money(rate_inv), rate_w, right=True) + " " +
                _pad(_fmt_money(cur_rate), rate_w, right=True) + " " +
                _pad(_fmt_money(cur_val), money_w, right=True) + " " +
                _pad(f"{sign}{_fmt_money(pnl)}", money_w, right=True)
            )
    else:
        table_lines.append("(no open positions)")
    table_lines.append(sep)
    sign_total = "+" if total_pnl_now >= 0 else ""
    table_lines.append(
        _pad("TOTAL", name_w) + " " +
        _pad("", type_w) + " " +
        _pad(_fmt_money(total_invested_now), money_w, right=True) + " " +
        _pad("", rate_w, right=True) + " " +
        _pad("", rate_w, right=True) + " " +
        _pad(_fmt_money(total_current_value), money_w, right=True) + " " +
        _pad(f"{sign_total}{_fmt_money(total_pnl_now)}", money_w, right=True)
    )

    table = "```\n" + "\n".join(table_lines) + "\n```"

    out_parts: list[str] = [
        "",
        "----- Portfolio summary (today) -----",
        f"Capital invested today: Rs {capital_invested_today_total:,.2f} "
        f"(equity Rs {capital_invested_today:,.2f} + FNO Rs {fno_invested_today:,.2f})",
        f"Holdings ({len(rows)}):",
        table,
        f"Earned profit (realized): +Rs {earned_profit:,.2f}",
        f"Incurred loss (realized): Rs {incurred_loss:,.2f}",
        f"Profit right now (if all sold): +Rs {profit_now:,.2f}",
        f"Loss right now (if all sold): Rs {loss_now:,.2f}",
        f"Net P&L right now: {'+' if total_pnl_now >= 0 else ''}Rs {total_pnl_now:,.2f}",
    ]
    return "\n".join(out_parts)


async def _send_summary(
    *,
    title: str,
    reasoning: str,
    cash_after: float,
    lines: list[str],
    portfolio_id: uuid.UUID | None = None,
) -> None:
    """Push the per-run digest to Telegram as plain text.

    Plain-text mode (``parse_mode=None``) sidesteps the long tail of
    Markdown-escaping bugs we hit when the LLM emits stray emphasis
    characters in its reasoning. Markdown formatting was cosmetic — the
    information density is the same. We also catch the send failure so a
    Telegram outage never aborts a run whose trades have already executed.

    When ``portfolio_id`` is provided, the day-in-review block (capital
    deployed, holdings list, realized + unrealized P&L) is appended after
    the action lines. Failures in that block are swallowed so a stale
    holdings table can't suppress the rest of the digest.
    """
    notifier = NotificationService()
    body_parts = [title]
    if reasoning:
        body_parts.append(_strip_md(reasoning[:600]))
    body_parts.append("")
    if lines:
        body_parts.extend(_strip_md(ln) for ln in lines)
    else:
        body_parts.append("No actions taken.")
    body_parts.append("")
    body_parts.append(f"Cash remaining: Rs {cash_after:,.2f}")
    # Headline P&L block — shared shape with the 15:40 F&O EOD message and
    # the 18:30 daily report so all three reconcile to the same numbers.
    try:
        from src.services.pnl_aggregator import daily_pnl_snapshot
        from src.services.report_formatter import format_compact_pnl_block
        snap = await daily_pnl_snapshot()
        body_parts.append("")
        body_parts.append(format_compact_pnl_block(snap))
    except Exception as exc:
        logger.warning(f"compact pnl block failed: {exc!r}")

    portfolio_block: str | None = None
    if portfolio_id is not None:
        try:
            portfolio_block = await _portfolio_summary_block(portfolio_id)
        except Exception as exc:
            logger.warning(f"portfolio summary block failed: {exc!r}")
    if portfolio_block:
        body_parts.append(portfolio_block)
    # Use legacy Markdown so the holdings ```...``` code block renders in
    # monospace and the columns align. _strip_md above keeps the LLM
    # reasoning + action lines from breaking the parser. The holdings
    # block is constructed by us, so it's safe to include verbatim.
    text = "\n".join(body_parts)
    try:
        await notifier.send_text(text[:3500], parse_mode="Markdown")
    except Exception as exc:
        logger.error(f"strategy summary telegram send failed: {exc!r}")
        # Markdown parse failures are 400s — fall back to plain text so
        # the user still gets the digest even if a stray char in the
        # reasoning unbalanced the parser.
        try:
            await notifier.send_text(text[:3500], parse_mode=None)
        except Exception as exc2:
            logger.error(f"plain-text fallback also failed: {exc2!r}")


# --------------------------------------------------------------- entrypoints


async def run_morning_allocation(
    as_of: datetime | None = None,
    dryrun_run_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Top up daily cash, ask the LLM how to deploy, and execute BUYs."""
    settings = get_settings()
    if not settings.equity_strategy_enabled:
        logger.info("equity strategy disabled — morning job skipped")
        return {"skipped": True}

    portfolio_id = await ensure_strategy_portfolio()
    # In dryrun, do not mutate the live portfolio's cash/initial_capital —
    # just read the current cash for the LLM snapshot.
    if dryrun_run_id is None:
        cash = await topup_daily_budget(portfolio_id)
    else:
        async with session_scope() as session:
            p = await session.get(Portfolio, portfolio_id)
            cash = float(p.current_cash or 0) if p else 0
        logger.info(f"dryrun: skipping sip topup, snapshot cash=₹{cash}")

    strategist = EquityStrategist()
    decision = await strategist.decide_morning_allocation(
        portfolio_id=portfolio_id, as_of=as_of, dryrun_run_id=dryrun_run_id
    )
    actions = decision["actions"]
    executed, skipped, lines = await _execute_actions(
        portfolio_id, decision["decision_id"], actions
    )
    await _update_decision_counts(decision["decision_id"], executed, skipped)

    async with session_scope() as session:
        p = await session.get(Portfolio, portfolio_id)
        cash_after = float(p.current_cash or 0) if p else cash

    # Unified digest: append today's F&O paper fills so the user sees the
    # full equity+options book in one message. The F&O book is filled by the
    # separate Phase 4 entry job at 09:15 IST; reporting it here is the
    # natural place because the equity LLM has just been told to consider
    # the same fno_candidates as inputs.
    fno_lines = await _summarise_today_fno_fills()
    if fno_lines:
        lines.append("")
        lines.append(f"📈 F&O book today ({len(fno_lines)} fills via Phase 4):")
        lines.extend(fno_lines)

    await _send_summary(
        title=f"🌅 Morning Allocation — {date.today():%d %b %Y}",
        reasoning=decision["reasoning"],
        cash_after=cash_after,
        lines=lines,
        portfolio_id=portfolio_id,
    )
    logger.info(
        f"morning allocation: executed={executed} skipped={skipped} "
        f"cash=Rs{cash_after} fno_today={len(fno_lines)}"
    )
    return {
        "decision_id": str(decision["decision_id"]),
        "executed": executed,
        "skipped": skipped,
        "cash_after": cash_after,
    }


async def run_intraday_action(
    as_of: datetime | None = None,
    dryrun_run_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Mid-session re-evaluation: hold/sell/buy based on current state."""
    settings = get_settings()
    if not settings.equity_strategy_enabled:
        return {"skipped": True}

    portfolio_id = await ensure_strategy_portfolio()
    if await _intraday_calls_today(portfolio_id) >= settings.equity_strategy_max_intraday_calls:
        logger.info("intraday call cap reached — skipping")
        return {"skipped": True, "reason": "cap_reached"}

    strategist = EquityStrategist()
    decision = await strategist.decide_intraday_action(
        portfolio_id=portfolio_id, as_of=as_of, dryrun_run_id=dryrun_run_id
    )
    actions = [a for a in decision["actions"] if a["action"] != "HOLD"]
    executed, skipped, lines = await _execute_actions(
        portfolio_id, decision["decision_id"], actions
    )
    await _update_decision_counts(decision["decision_id"], executed, skipped)

    async with session_scope() as session:
        p = await session.get(Portfolio, portfolio_id)
        cash_after = float(p.current_cash or 0) if p else 0

    await _send_summary(
        title=f"🔄 Intraday Re-eval — {datetime.now(timezone.utc):%H:%M UTC}",
        reasoning=decision["reasoning"],
        cash_after=cash_after,
        lines=lines,
        portfolio_id=portfolio_id,
    )
    return {
        "decision_id": str(decision["decision_id"]),
        "executed": executed,
        "skipped": skipped,
    }


async def run_eod_squareoff(
    as_of: datetime | None = None,
    dryrun_run_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """At ~15:20 IST, ask the LLM which intraday positions to close."""
    settings = get_settings()
    if not settings.equity_strategy_enabled:
        return {"skipped": True}

    portfolio_id = await ensure_strategy_portfolio()
    strategist = EquityStrategist()
    decision = await strategist.decide_eod_squareoff(
        portfolio_id=portfolio_id, as_of=as_of, dryrun_run_id=dryrun_run_id
    )
    sells = [a for a in decision["actions"] if a["action"] == "SELL"]
    executed, skipped, lines = await _execute_actions(
        portfolio_id, decision["decision_id"], sells
    )
    await _update_decision_counts(decision["decision_id"], executed, skipped)

    async with session_scope() as session:
        p = await session.get(Portfolio, portfolio_id)
        cash_after = float(p.current_cash or 0) if p else 0

    await _send_summary(
        title=f"🌇 EOD Square-off — {date.today():%d %b %Y}",
        reasoning=decision["reasoning"],
        cash_after=cash_after,
        lines=lines or ["No square-off needed — positions held overnight."],
        portfolio_id=portfolio_id,
    )
    return {
        "decision_id": str(decision["decision_id"]),
        "executed": executed,
        "skipped": skipped,
    }


async def _intraday_calls_today(portfolio_id: uuid.UUID) -> int:
    """Count today's intraday strategy decisions for the cap."""
    today_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    async with session_scope() as session:
        result = await session.execute(
            select(StrategyDecision.id).where(
                StrategyDecision.portfolio_id == portfolio_id,
                StrategyDecision.decision_type == DECISION_INTRADAY,
                StrategyDecision.as_of >= today_start,
            )
        )
        return len(list(result.scalars()))


__all__ = [
    "DECISION_EOD",
    "DECISION_INTRADAY",
    "DECISION_MORNING",
    "STRATEGY_PORTFOLIO_NAME",
    "ensure_strategy_portfolio",
    "run_eod_squareoff",
    "run_intraday_action",
    "run_morning_allocation",
    "topup_daily_budget",
]
