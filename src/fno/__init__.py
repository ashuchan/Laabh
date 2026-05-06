"""F&O package — shared constants live here so callers across position
manager, gate, prompt enrichment and entry executor read from one source."""

# Statuses that count as "the position is still live" — used by the position
# manager (mark-to-market, stops, hard exit), the prompt-context open-book
# block (so the LLM sees only what's actually open), and the strategy gate
# (to dedupe new proposals against open ones). Keeping these in one place
# prevents the lists from drifting out of sync as new statuses are added.
LIVE_FNO_STATUSES: tuple[str, ...] = (
    "paper_filled",
    "active",
    "scaled_out_50",
)
