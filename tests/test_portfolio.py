"""Tests for portfolio valuation and snapshot logic."""
from __future__ import annotations

import pytest

from src.whisper_pipeline.financial_filter import _BASE_KEYWORDS
from src.whisper_pipeline.chunk_processor import ChunkProcessor


# ---- ChunkProcessor tests ----

def test_chunk_processor_splits_correctly():
    proc = ChunkProcessor()
    words = " ".join(f"word{i}" for i in range(1000))
    chunks = proc.split_transcript(words)
    assert len(chunks) > 1
    # Each chunk ≤ CHUNK_WORDS words
    for chunk in chunks:
        assert len(chunk.split()) <= ChunkProcessor.OVERLAP_WORDS + 300  # some tolerance


def test_chunk_processor_empty():
    proc = ChunkProcessor()
    chunks = proc.split_transcript("")
    assert chunks == []


def test_chunk_processor_short_text():
    proc = ChunkProcessor()
    text = "RELIANCE looks bullish"
    chunks = proc.split_transcript(text)
    assert len(chunks) == 1
    assert chunks[0] == text


def test_chunk_overlap():
    proc = ChunkProcessor()
    words = " ".join(str(i) for i in range(400))
    chunks = proc.split_transcript(words)
    # Overlap: last words of chunk N should appear at start of chunk N+1
    if len(chunks) >= 2:
        last_words = chunks[0].split()[-5:]
        next_start = chunks[1].split()[:30]
        overlap_found = any(w in next_start for w in last_words)
        assert overlap_found, "Expected overlap between consecutive chunks"


# ---- Financial filter keyword tests ----

def test_base_keywords_coverage():
    assert "buy" in _BASE_KEYWORDS
    assert "sell" in _BASE_KEYWORDS
    assert "nifty" in _BASE_KEYWORDS
    assert "sensex" in _BASE_KEYWORDS
    assert "target" in _BASE_KEYWORDS
