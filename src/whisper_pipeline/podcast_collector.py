"""RSS-based podcast discovery and audio download."""
from __future__ import annotations

import subprocess
from pathlib import Path

import feedparser
from loguru import logger

PODCAST_FEEDS = [
    {
        "name": "The Market Podcast",
        "url": "https://feed.podbean.com/marketpodcast/feed.xml",
        "language": "en",
    },
    {
        "name": "Paisa Vaisa",
        "url": "https://feeds.megaphone.fm/paisa-vaisa",
        "language": "hi-en",
    },
    {
        "name": "Marcellus Podcast",
        "url": "https://anchor.fm/s/marcellus/podcast/rss",
        "language": "en",
    },
]

PODCAST_BASE = Path("/data/whisper/podcasts")


class PodcastCollector:
    """Downloads podcast episodes for Whisper transcription."""

    async def collect_latest(self) -> list[tuple[str, Path]]:
        """Download latest episode from each configured podcast feed.

        Returns list of (podcast_name, audio_path).
        """
        results = []
        for feed_cfg in PODCAST_FEEDS:
            try:
                paths = await self._fetch_feed(feed_cfg)
                results.extend(paths)
            except Exception as exc:
                logger.error(f"podcast collection failed for {feed_cfg['name']}: {exc}")
        return results

    async def _fetch_feed(self, cfg: dict) -> list[tuple[str, Path]]:
        name = cfg["name"]
        feed_url = cfg["url"]
        feed = feedparser.parse(feed_url)
        if not feed.entries:
            return []

        latest = feed.entries[0]
        audio_url = None
        for link in getattr(latest, "enclosures", []):
            if "audio" in link.get("type", ""):
                audio_url = link.href
                break

        if not audio_url:
            return []

        out_dir = PODCAST_BASE / name.replace(" ", "_").lower()
        out_dir.mkdir(parents=True, exist_ok=True)
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).strftime("%Y%m%d")
        out_path = out_dir / f"episode_{ts}.mp3"

        if out_path.exists():
            logger.debug(f"podcast episode already downloaded: {out_path}")
            return [(name, out_path)]

        cmd = ["yt-dlp", "-o", str(out_path), audio_url]
        try:
            subprocess.run(cmd, capture_output=True, check=True, timeout=600)
            logger.info(f"podcast episode downloaded: {out_path}")
            return [(name, out_path)]
        except Exception as exc:
            logger.error(f"podcast download failed for {name}: {exc}")
            return []
