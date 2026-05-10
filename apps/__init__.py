"""Streamlit entry-points (decision_inspector, backtest_dashboard).

This package isn't installed via ``pip install -e .`` — it's intentionally
excluded from ``[tool.setuptools.packages.find]`` because it's an
entry-point directory, not a library. Each script prepends the project
root to ``sys.path`` so it can import sibling modules from this package.
"""
