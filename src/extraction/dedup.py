"""Near-duplicate detection via SimHash."""
from __future__ import annotations

import re

from simhash import Simhash


def compute_simhash(text: str) -> int:
    """Return a 64-bit SimHash fingerprint of `text`."""
    tokens = _tokenize(text)
    return Simhash(tokens, f=64).value


def hamming_distance(a: int, b: int) -> int:
    """Bit-level Hamming distance between two 64-bit SimHash ints."""
    return bin(a ^ b).count("1")


def is_near_duplicate(h1: int, h2: int, threshold: int = 3) -> bool:
    """Return True if two SimHashes differ by < `threshold` bits."""
    return hamming_distance(h1, h2) < threshold


_TOKEN_RE = re.compile(r"[A-Za-z\u0900-\u097F]{2,}")


def _tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text or "")]
