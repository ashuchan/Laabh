"""Typed exceptions for chain data source adapters."""
from __future__ import annotations


class ChainSourceError(Exception):
    """Base class for all chain source errors."""


class SchemaError(ChainSourceError):
    """Response shape did not match the expected schema.

    Carries the truncated raw response payload so callers can log and file issues.
    """

    def __init__(self, message: str, raw_response: str = "") -> None:
        super().__init__(message)
        self.raw_response = raw_response[:8192]  # cap at 8 KB


class RateLimitError(ChainSourceError):
    """Source returned a rate-limit response (HTTP 429 or equivalent)."""


class AuthError(ChainSourceError):
    """Credentials are missing, invalid, or expired."""


class SourceUnavailableError(ChainSourceError):
    """Any other failure — network error, 5xx response, timeout, etc."""
