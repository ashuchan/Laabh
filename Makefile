.PHONY: test test-unit test-integration \
        test-integration-openalgo test-integration-tradingagents \
        test-integration-nautilus test-unit-strategies \
        test-unit-features test-integration-all

# ── unit tests (no external services needed) ────────────────────────────────

test-unit-strategies:
	pytest tests/test_iron_fly.py tests/test_short_straddle.py -v --tb=short

test-unit-features:
	pytest tests/test_feature_pipeline.py tests/test_regime_gate.py -v --tb=short

test-unit:
	pytest tests/ -m "not integration" -v --tb=short

# ── integration tests (require live services / credentials) ─────────────────

test-integration-openalgo:
	pytest tests/test_integration_openalgo.py -m integration -v --tb=short

test-integration-tradingagents:
	pytest tests/test_integration_tradingagents.py -m integration -v --tb=short

test-integration-nautilus:
	pytest tests/test_integration_nautilus.py -m integration -v --tb=short

test-integration-all:
	pytest tests/ -m integration -v --tb=short
	pytest tests/ -m "not integration" -v --tb=short

# ── convenience targets ──────────────────────────────────────────────────────

test: test-unit

lint:
	ruff check src/ tests/

typecheck:
	mypy src/ --ignore-missing-imports
