"""Download completed YouTube VODs for batch transcription."""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from loguru import logger
from sqlalchemy import select

from src.db import session_scope
from src.models.source import DataSource

VOD_BASE = Path("/data/whisper/vod")


class VODDownloader:
    """Fetches YouTube captions (free) or downloads audio for Whisper transcription."""

    async def download_today_vods(self) -> list[Path]:
        """Download today's completed streams from configured YouTube VOD sources."""
        async with session_scope() as session:
            result = await session.execute(
                select(DataSource).where(
                    DataSource.type.in_(["youtube_vod", "youtube_live"]),
                    DataSource.status == "active",
                )
            )
            sources = result.scalars().all()

        paths: list[Path] = []
        for source in sources:
            url = source.config.get("url") or source.config.get("channel_url")
            if not url:
                continue
            try:
                result = await self._download_source(source.name, url)
                if result:
                    paths.append(result)
            except Exception as exc:
                logger.error(f"VOD download failed for {source.name}: {exc}")
        return paths

    async def _download_source(self, name: str, url: str) -> Path | None:
        # Try youtube-transcript-api first (free, fast)
        transcript = await self._try_captions(url)
        if transcript:
            channel_dir = VOD_BASE / name.replace(" ", "_").lower()
            channel_dir.mkdir(parents=True, exist_ok=True)
            from datetime import datetime, timezone
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            txt_path = channel_dir / f"transcript_{ts}.txt"
            txt_path.write_text(transcript, encoding="utf-8")
            logger.info(f"caption transcript saved: {txt_path}")
            return txt_path

        # Fallback: download audio with yt-dlp
        return await self._download_audio(name, url)

    async def _try_captions(self, url: str) -> str | None:
        try:
            from youtube_transcript_api import YouTubeTranscriptApi
            video_id = self._extract_video_id(url)
            if not video_id:
                return None
            transcript = YouTubeTranscriptApi.get_transcript(
                video_id, languages=["hi", "en"]
            )
            return " ".join(item["text"] for item in transcript)
        except Exception:
            return None

    async def _download_audio(self, name: str, url: str) -> Path | None:
        out_dir = VOD_BASE / name.replace(" ", "_").lower()
        out_dir.mkdir(parents=True, exist_ok=True)
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        out_path = out_dir / f"audio_{ts}.webm"

        cmd = [
            "yt-dlp",
            "--format", "bestaudio",
            "--output", str(out_path),
            "--no-playlist",
            url,
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=600)
            if result.returncode == 0 and out_path.exists():
                logger.info(f"VOD audio downloaded: {out_path}")
                return out_path
            logger.error(f"yt-dlp failed: {result.stderr.decode()[:200]}")
        except subprocess.TimeoutExpired:
            logger.error(f"yt-dlp timeout for {url}")
        except FileNotFoundError:
            logger.error("yt-dlp not found")
        return None

    def _extract_video_id(self, url: str) -> str | None:
        import re
        patterns = [
            r"youtube\.com/watch\?v=([^&]+)",
            r"youtu\.be/([^?]+)",
            r"youtube\.com/live/([^?]+)",
        ]
        for pat in patterns:
            m = re.search(pat, url)
            if m:
                return m.group(1)
        return None
