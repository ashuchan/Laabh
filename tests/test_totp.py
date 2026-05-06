"""Tests for src.utils.totp — generic RFC 6238 wrapper."""
from __future__ import annotations

import pyotp
import pytest

from src.utils.totp import generate_totp


def test_returns_six_digit_string():
    code = generate_totp("JBSWY3DPEHPK3PXP")
    assert isinstance(code, str)
    assert len(code) == 6
    assert code.isdigit()


def test_matches_pyotp_directly():
    """Sanity check — confirms we don't mangle the seed in any unexpected way."""
    secret = "JBSWY3DPEHPK3PXP"
    assert generate_totp(secret) == pyotp.TOTP(secret).now()


def test_strips_spaces_in_seed():
    """Brokers display the seed with spaces every 4 chars; both forms must produce the same code."""
    raw = "JBSWY3DPEHPK3PXP"
    spaced = "JBSW Y3DP EHPK 3PXP"
    assert generate_totp(raw) == generate_totp(spaced)


def test_lowercase_seed_normalised_to_upper():
    raw = "JBSWY3DPEHPK3PXP"
    assert generate_totp(raw) == generate_totp(raw.lower())


def test_empty_secret_raises_value_error():
    with pytest.raises(ValueError):
        generate_totp("")


def test_custom_digits_and_interval():
    """Caller can override the default 6-digit / 30s window for non-standard providers."""
    secret = "JBSWY3DPEHPK3PXP"
    code = generate_totp(secret, digits=8, interval_sec=60)
    assert len(code) == 8
    assert code.isdigit()
