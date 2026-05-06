"""Post-LLM rule gate — defense-in-depth for equity + F&O proposals.

The prompts already describe the hard rules (see ``prompt_context``), but
prompt compliance is best-effort. This gate re-checks each proposed action
against the same rules and downgrades violations from "execute" to "skip".

Each violation is logged with a structured ``gate_violation:<rule_id>``
reason so the runner can count rule-vs-LLM mismatches over time — that
mismatch rate is itself a quality signal for prompt iteration.

Two entry points:
  * ``filter_equity_actions`` — runs after ``EquityStrategist._normalise_actions``
  * ``filter_fno_proposals``  — runs before ``entry_executor._enter_one``
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from loguru import logger
from sqlalchemy import text

from src.db import session_scope
from src.fno import LIVE_FNO_STATUSES


# Threshold constants — single source of truth, mirrored in the prompt text.
SUB_SCALE_CASH_THRESHOLD = 200_000.0
SUB_SCALE_MIN_EXPECTED_MOVE_PCT = 2.0
SUB_SCALE_MIN_CONFIDENCE = 0.75
HIGH_VIX_THRESHOLD = 17.0
HIGH_VIX_MIN_CONFIDENCE = 0.75
HIGH_VIX_MAX_NEW_ENTRIES = 2
FNO_MAX_PREMIUM_DRAWDOWN_PCT = 0.45
FNO_NAKED_LONG_STRATEGIES = {"long_call", "long_put"}


@dataclass
class GateOutcome:
    """Result of running an action set through the gate."""
    accepted: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)

    @property
    def violations(self) -> list[str]:
        return [a.get("gate_violation") or "" for a in self.skipped]

    def merge_into_actions(self) -> list[dict[str, Any]]:
        """Return the original list with violators tagged as HOLD+gate_violation.

        Preserves the LLM's intent record while neutralising execution.
        """
        out = list(self.accepted)
        for s in self.skipped:
            tagged = dict(s)
            tagged["action"] = "HOLD"
            tagged.setdefault("reason", "")
            tagged["reason"] = f"[gate] {tagged.get('gate_violation','blocked')}: {tagged['reason']}"
            out.append(tagged)
        return out


# ---------------------------------------------------------------------------
# Equity gate
# ---------------------------------------------------------------------------

async def filter_equity_actions(
    actions: list[dict[str, Any]],
    *,
    snapshot: dict[str, Any] | None = None,
    portfolio_id: uuid.UUID | str | None = None,
    decision_type: str | None = None,
) -> GateOutcome:
    """Apply HARD RULES A–E to a list of normalised equity actions.

    ``snapshot`` is the same input snapshot fed to the LLM — it carries
    cash, VIX regime, and the candidate enrichment fields the gate needs.
    Missing fields degrade gracefully: a rule that can't be evaluated is
    a pass, not a fail.
    """
    out = GateOutcome()
    snap = snapshot or {}
    cash = float(snap.get("cash_available") or 0.0)
    market = snap.get("market") or {}
    vix_regime = (market.get("vix_regime") or "").lower()
    vix_value = float(market.get("vix_value") or 0.0)
    is_high_vix = (vix_regime == "high") or (vix_value >= HIGH_VIX_THRESHOLD)
    is_sub_scale = cash > 0 and cash < SUB_SCALE_CASH_THRESHOLD

    # Lookup tables for portfolio-aware checks.
    held_iids = await _held_instrument_ids(portfolio_id) if portfolio_id else set()

    # candidate enrichment by instrument_id, so we can read confidence and
    # estimate expected_move_pct from target/stop.
    cand_by_iid: dict[str, dict[str, Any]] = {}
    for c in (snap.get("candidates") or []):
        iid = str(c.get("instrument_id") or "").strip()
        if iid:
            cand_by_iid[iid] = c

    # Two-pass evaluation. Pass 1 applies portfolio-aware (Rule D), sub-scale
    # (Rule A) and high-VIX confidence (Rule B confidence sub-rule) — these
    # are *eligibility* checks. Pass 2 applies the high-VIX entry-count cap
    # *after* sorting eligible BUYs by confidence DESC, so a low-conviction
    # entry never burns a slot ahead of a strong one. Order in the input
    # actions list still influences ties; that's deliberate (the LLM
    # ordering is a tie-break signal).
    high_vix_eligible_buys: list[dict[str, Any]] = []

    for a in actions:
        if a.get("asset_class") != "EQUITY":
            out.accepted.append(a)
            continue
        action = (a.get("action") or "").upper()
        iid = str(a.get("instrument_id") or "").strip()

        # Rule D — portfolio-aware
        if action == "BUY" and iid and iid in held_iids:
            _reject(out, a, "D_already_held",
                    "BUY for symbol already in holdings")
            continue
        if action == "SELL" and iid and iid not in held_iids:
            _reject(out, a, "D_sell_without_holding",
                    "SELL for a symbol not in holdings")
            continue

        # Rule A — sub-scale friction. Apply only to NEW buys.
        if action == "BUY" and is_sub_scale:
            cand = cand_by_iid.get(iid, {})
            confidence = _safe_float(cand.get("confidence"))
            target = _safe_float(cand.get("target_price"))
            entry = _safe_float(a.get("approx_price")) or _safe_float(cand.get("ltp"))
            if confidence is not None and confidence < SUB_SCALE_MIN_CONFIDENCE:
                _reject(out, a, "A_sub_scale_confidence",
                        f"confidence {confidence:.2f} < {SUB_SCALE_MIN_CONFIDENCE}")
                continue
            if target and entry:
                move_pct = (target - entry) / entry * 100.0
                if move_pct < SUB_SCALE_MIN_EXPECTED_MOVE_PCT:
                    _reject(out, a, "A_sub_scale_move",
                            f"expected move {move_pct:.2f}% < "
                            f"{SUB_SCALE_MIN_EXPECTED_MOVE_PCT}%")
                    continue

        # Rule B (confidence sub-rule) — high VIX requires conviction.
        # Apply BEFORE the count cap so a low-confidence entry never burns
        # a slot that a high-confidence entry could fill.
        if action == "BUY" and is_high_vix:
            cand = cand_by_iid.get(iid, {})
            confidence = _safe_float(cand.get("confidence"))
            if confidence is not None and confidence < HIGH_VIX_MIN_CONFIDENCE:
                _reject(out, a, "B_high_vix_confidence",
                        f"high-VIX confidence {confidence:.2f} < "
                        f"{HIGH_VIX_MIN_CONFIDENCE}")
                continue
            high_vix_eligible_buys.append(a)
            continue  # decision deferred to the count-cap pass

        out.accepted.append(a)

    # Pass 2 — high-VIX count cap. Confidence DESC, with the LLM-provided
    # input order as the tie-break (stable sort). Top N pass through;
    # the rest are rejected with B_high_vix_count.
    if high_vix_eligible_buys:
        def _conf_key(action: dict[str, Any]) -> float:
            cand = cand_by_iid.get(
                str(action.get("instrument_id") or "").strip(), {}
            )
            return _safe_float(cand.get("confidence")) or 0.0

        ranked = sorted(high_vix_eligible_buys, key=_conf_key, reverse=True)
        for i, a in enumerate(ranked):
            if i < HIGH_VIX_MAX_NEW_ENTRIES:
                out.accepted.append(a)
            else:
                _reject(out, a, "B_high_vix_count",
                        f">{HIGH_VIX_MAX_NEW_ENTRIES} entries in high-VIX regime")

    if out.skipped:
        logger.info(
            f"strategy_gate(equity): kept {len(out.accepted)} / "
            f"skipped {len(out.skipped)} actions; violations="
            f"{[a.get('gate_violation') for a in out.skipped]}"
        )
    return out


# ---------------------------------------------------------------------------
# F&O gate — runs against EntryProposal-shaped dicts
# ---------------------------------------------------------------------------

@dataclass
class FNOProposalView:
    """Minimal projection of an EntryProposal for the gate to operate on.

    Keeps the gate decoupled from the EntryProposal dataclass so tests and
    replays can construct synthetic inputs.
    """
    instrument_id: str
    symbol: str
    expiry_date: Any
    strategy_name: str
    entry_premium: Decimal | float
    stop_premium: Decimal | float | None
    direction: str = "neutral"
    # Per-proposal regime context. The gate's regime check is OR-combined
    # across this and the call-site VIX value, so a missing VIX read during
    # replay still triggers the naked-long block when the candidate's
    # iv_regime is 'high'/'elevated'.
    iv_regime: str | None = None


async def filter_fno_proposals(
    proposals: list[FNOProposalView],
    *,
    vix_value: float | None = None,
    iv_regime: str | None = None,
) -> tuple[list[FNOProposalView], list[tuple[FNOProposalView, str]]]:
    """Apply F&O hard rules to entry proposals.

    Returns ``(accepted, rejected)`` where ``rejected`` carries
    ``(proposal, gate_violation_code)`` pairs for telemetry.
    """
    accepted: list[FNOProposalView] = []
    rejected: list[tuple[FNOProposalView, str]] = []

    # Call-site regime — applies to every proposal in this batch.
    batch_high_vix = (vix_value is not None and vix_value >= HIGH_VIX_THRESHOLD) or (
        (iv_regime or "").lower() in ("high", "elevated")
    )

    open_book = await _open_fno_book_index()

    for p in proposals:
        sname = (p.strategy_name or "").lower()

        # Rule 1: regime gate — block naked longs in high-VIX/IV.
        # Per-proposal iv_regime acts as a fallback when call-site VIX
        # isn't available (e.g. replay against a session with no vix_ticks
        # row yet) so the gate never silently passes naked longs.
        per_prop_high = (p.iv_regime or "").lower() in ("high", "elevated")
        if (batch_high_vix or per_prop_high) and sname in FNO_NAKED_LONG_STRATEGIES:
            rejected.append((p, "F1_high_vix_naked_long"))
            continue

        # Rule 2: stop discipline. Drawdown must be <= 45% of entry premium.
        try:
            entry_d = Decimal(str(p.entry_premium))
            stop_d = Decimal(str(p.stop_premium)) if p.stop_premium is not None else None
        except Exception:
            entry_d, stop_d = Decimal("0"), None
        if entry_d > 0 and stop_d is not None:
            drawdown = (entry_d - stop_d) / entry_d
            if drawdown > Decimal(str(FNO_MAX_PREMIUM_DRAWDOWN_PCT)):
                rejected.append((p, "F2_stop_drawdown_exceeded"))
                continue

        # Rule 3a: same strategy_type already open on same underlying+expiry.
        same_key = (p.instrument_id, str(p.expiry_date), sname)
        if same_key in open_book["same"]:
            rejected.append((p, "F3a_duplicate_strategy"))
            continue

        # Rule 3b: opposing direction already open on same underlying+expiry.
        opposing_dirs = open_book["directions"].get(
            (p.instrument_id, str(p.expiry_date)), set()
        )
        my_dir = _direction_of(sname, p.direction)
        if my_dir in ("bullish", "bearish"):
            if "bullish" in opposing_dirs and my_dir == "bearish":
                rejected.append((p, "F3b_opposing_direction"))
                continue
            if "bearish" in opposing_dirs and my_dir == "bullish":
                rejected.append((p, "F3b_opposing_direction"))
                continue

        accepted.append(p)

    if rejected:
        logger.info(
            f"strategy_gate(fno): kept {len(accepted)} / rejected {len(rejected)} "
            f"proposals; violations={[v for _, v in rejected]}"
        )
    return accepted, rejected


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _safe_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _reject(out: GateOutcome, action: dict[str, Any], code: str, detail: str) -> None:
    """Mark an action as rejected by the gate."""
    rec = dict(action)
    rec["gate_violation"] = code
    rec["gate_detail"] = detail
    out.skipped.append(rec)


def _direction_of(strategy_name: str, declared: str | None) -> str:
    # Only honour an *informative* declared direction. 'neutral' is the
    # FNOProposalView default and means "not set" — fall through to the
    # strategy-name heuristic, otherwise a long_call with direction='neutral'
    # would silently disable the F3b opposing-leg check.
    if declared and declared in ("bullish", "bearish"):
        return declared
    s = (strategy_name or "").lower()
    if "call" in s and "spread" not in s and "bear" not in s:
        return "bullish"
    if "put" in s and "spread" not in s and "bull" not in s:
        return "bearish"
    if "bull" in s:
        return "bullish"
    if "bear" in s:
        return "bearish"
    return "neutral"


async def _held_instrument_ids(
    portfolio_id: uuid.UUID | str,
) -> set[str]:
    """Return the set of instrument_ids the portfolio currently holds."""
    try:
        async with session_scope() as session:
            rows = list((await session.execute(
                text(
                    "SELECT instrument_id::text FROM holdings "
                    "WHERE portfolio_id = :pid AND quantity > 0"
                ),
                {"pid": str(portfolio_id)},
            )).all())
            return {r[0] for r in rows}
    except Exception as exc:
        logger.debug(f"_held_instrument_ids failed: {exc}")
        return set()


async def _open_fno_book_index() -> dict[str, Any]:
    """Index of open F&O signals for cross-checking new proposals.

    Returns:
        ``same``       — set of (underlying_id, expiry_date_str, strategy_lc).
        ``directions`` — dict[(underlying_id, expiry_date_str)] -> set of
                         {'bullish','bearish','neutral'} from open positions.
    """
    same: set[tuple[str, str, str]] = set()
    directions: dict[tuple[str, str], set[str]] = {}
    try:
        async with session_scope() as session:
            rows = list((await session.execute(
                text(
                    "SELECT underlying_id::text, expiry_date, strategy_type "
                    "FROM fno_signals "
                    "WHERE status = ANY(:statuses) "
                    "  AND dryrun_run_id IS NULL"
                ),
                {"statuses": list(LIVE_FNO_STATUSES)},
            )).all())
        for uid, expiry, stype in rows:
            stype_lc = (stype or "").lower()
            key = (uid, str(expiry))
            same.add((uid, str(expiry), stype_lc))
            directions.setdefault(key, set()).add(_direction_of(stype_lc, None))
    except Exception as exc:
        logger.debug(f"_open_fno_book_index failed: {exc}")
    return {"same": same, "directions": directions}
