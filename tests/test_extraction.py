"""Tests for dedup (SimHash) and numeric coercion helpers."""
from __future__ import annotations

from src.extraction.dedup import compute_simhash, hamming_distance, is_near_duplicate
from src.extraction.llm_extractor import _num, _strip_code_fence


def test_simhash_near_duplicate() -> None:
    a = compute_simhash("Reliance industries reports strong Q4 earnings today")
    b = compute_simhash("Reliance industries reports strong Q4 earnings today.")
    assert is_near_duplicate(a, b, threshold=5)


def test_simhash_distinct_texts() -> None:
    a = compute_simhash("Reliance misses earnings estimates sharply")
    b = compute_simhash("Titan posts record jewellery sales this festive quarter")
    assert hamming_distance(a, b) > 10


def test_num_coerces_strings() -> None:
    assert _num("12.5") == 12.5
    assert _num(None) is None
    assert _num("abc") is None


def test_strip_code_fence() -> None:
    assert _strip_code_fence("```json\n{\"a\":1}\n```") == '{"a":1}'
    assert _strip_code_fence('{"a":1}') == '{"a":1}'
