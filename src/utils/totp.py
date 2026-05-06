"""RFC 6238 TOTP code generation.

Thin wrapper around `pyotp` so callers don't import it directly and so any
TOTP-using broker (Angel One, Dhan, ...) shares the same input-cleaning rules.
This module knows nothing about brokers; it just turns a base32 seed into the
6-digit code valid for the current 30-second window.
"""
from __future__ import annotations

import pyotp


def generate_totp(secret: str, *, digits: int = 6, interval_sec: int = 30) -> str:
    """Return the current TOTP code for ``secret``.

    The seed is RFC 4648 base32. Spaces (broker UIs commonly insert one every
    4 chars when displaying the seed) and case are normalised so callers can
    paste the seed verbatim into ``.env``.
    """
    if not secret:
        raise ValueError("TOTP secret is empty")
    cleaned = secret.replace(" ", "").upper()
    return pyotp.TOTP(cleaned, digits=digits, interval=interval_sec).now()
