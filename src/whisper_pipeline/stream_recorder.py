"""Live stream audio capture via yt-dlp (market hours only)."""
from __future__ import annotations

import asyncio
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger
from sqlalchemy import select

from src.config import get_settings
from src.db import session_scope
from src.models.source import DataSource

AUDIO_BASE = Path("/data/whisper/live")


class StreamRecorder:
    """Manages yt-dlp subprocesses for each configured YouTube live channel."""

    _procs: dict[str, subprocess.Popen] = {}  # source_id → process

    async def start_all(self) -> None:
        """Start recordings for all active YouTube live sources."""
        async with session_scope() as session:
            result = await session.execute(
                select(DataSource).where(
                    DataSource.type == "youtube_live",
                    DataSource.status == "active",
                )
            )
            sources = result.scalars().all()

        for source in sources:
            await self._start_source(source)

    async def stop_all(self) -> None:
        """Gracefully stop all running recorders."""
        for source_id, proc in list(self._procs.items()):
            try:
                proc.terminate()
                proc.wait(timeout=10)
            except Exception as exc:
                logger.warning(f"could not stop recorder {source_id}: {exc}")
            finally:
                del self._procs[source_id]
        logger.info("all live recorders stopped")

    async def _start_source(self, source: DataSource) -> None:
        stream_url = source.config.get("url")
        if not stream_url:
            logger.warning(f"source {source.id} has no URL configured")
            return

        channel_name = source.name.replace(" ", "_").lower()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        out_dir = AUDIO_BASE / channel_name / today
        out_dir.mkdir(parents=True, exist_ok=True)
        out_pattern = str(out_dir / "chunk_%(epoch)s.%(ext)s")

        cmd = [
            "yt-dlp",
            "--live-from-start",
            "-f", "bestaudio",
            "--downloader", "ffmpeg",
            "--downloader-args",
            "ffmpeg:-f segment -segment_time 60 -reset_timestamps 1",
            "-o", out_pattern,
            stream_url,
        ]

        source_id = str(source.id)
        if source_id in self._procs:
            old = self._procs[source_id]
            if old.poll() is None:
                logger.debug(f"recorder already running for {source.name}")
                return
            del self._procs[source_id]

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self._procs[source_id] = proc
            logger.info(f"recorder started for {source.name} (pid {proc.pid})")
        except FileNotFoundError:
            logger.error("yt-dlp not found — install with: pip install yt-dlp")
        except Exception as exc:
            logger.error(f"failed to start recorder for {source.name}: {exc}")

    async def supervise(self) -> None:
        """Restart any crashed recorder processes (call periodically)."""
        for source_id, proc in list(self._procs.items()):
            if proc.poll() is not None:
                logger.warning(f"recorder {source_id} crashed — restarting")
                del self._procs[source_id]
                async with session_scope() as session:
                    source = await session.get(DataSource, source_id)
                if source:
                    await self._start_source(source)

    @staticmethod
    def cleanup_old_chunks(days: int = 7) -> int:
        """Delete audio chunks older than `days` days. Returns count deleted."""
        if not AUDIO_BASE.exists():
            return 0
        now = datetime.now(timezone.utc).timestamp()
        deleted = 0
        for audio_file in AUDIO_BASE.rglob("chunk_*.webm"):
            age_days = (now - audio_file.stat().st_mtime) / 86400
            if age_days > days:
                audio_file.unlink(missing_ok=True)
                deleted += 1
        if deleted:
            logger.info(f"cleaned up {deleted} old audio chunks")
        return deleted
