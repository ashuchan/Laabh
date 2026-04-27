"""Tests for src/runday/checks/base.py."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.runday.checks.base import CheckResult, Severity, exit_code_for


def test_check_result_passed_ok():
    r = CheckResult(name="test.check", severity=Severity.OK, message="all good")
    assert r.passed is True


def test_check_result_passed_warn():
    r = CheckResult(name="test.check", severity=Severity.WARN, message="warning")
    assert r.passed is False


def test_check_result_passed_fail():
    r = CheckResult(name="test.check", severity=Severity.FAIL, message="failure")
    assert r.passed is False


def test_to_dict_contains_required_fields():
    r = CheckResult(
        name="test.check",
        severity=Severity.OK,
        message="ok",
        details={"key": "value"},
        duration_ms=42,
    )
    d = r.to_dict()
    assert d["name"] == "test.check"
    assert d["severity"] == "ok"
    assert d["message"] == "ok"
    assert d["details"] == {"key": "value"}
    assert d["duration_ms"] == 42
    assert "queried_at" in d


def test_exit_code_all_ok():
    results = [
        CheckResult(name="a", severity=Severity.OK, message=""),
        CheckResult(name="b", severity=Severity.OK, message=""),
    ]
    assert exit_code_for(results) == 0


def test_exit_code_warn():
    results = [
        CheckResult(name="a", severity=Severity.OK, message=""),
        CheckResult(name="b", severity=Severity.WARN, message=""),
    ]
    assert exit_code_for(results) == 10


def test_exit_code_fail():
    results = [
        CheckResult(name="a", severity=Severity.OK, message=""),
        CheckResult(name="b", severity=Severity.WARN, message=""),
        CheckResult(name="c", severity=Severity.FAIL, message=""),
    ]
    assert exit_code_for(results) == 20


def test_exit_code_empty():
    assert exit_code_for([]) == 0


def test_severity_string_values():
    assert Severity.OK.value == "ok"
    assert Severity.WARN.value == "warn"
    assert Severity.FAIL.value == "fail"
