"""Tests for collector utilities and the RSS parsing path."""
from __future__ import annotations

from src.collectors.base import BaseCollector


def test_content_hash_is_deterministic() -> None:
    h1 = BaseCollector.content_hash("Title", "https://x.com/a")
    h2 = BaseCollector.content_hash("Title", "https://x.com/a")
    assert h1 == h2
    assert len(h1) == 64


def test_content_hash_differs_on_url() -> None:
    assert BaseCollector.content_hash("t", "a") != BaseCollector.content_hash("t", "b")


def test_rss_parse_published_handles_missing() -> None:
    from src.collectors.rss_collector import _parse_published

    class E:
        pass

    assert _parse_published(E()) is None
