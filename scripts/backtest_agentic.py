#!/usr/bin/env python3
"""Convenience wrapper: invokes `python -m src.agents.backtest`.

Identical CLI surface — exists so `python scripts/backtest_agentic.py ...`
works alongside `laabh-runday replay`, `weekly_postmortem.py`, etc.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make project root importable when running as a script.
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.agents.backtest.__main__ import main  # noqa: E402

if __name__ == "__main__":
    main()
