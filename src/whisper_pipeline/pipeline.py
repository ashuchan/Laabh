"""Whisper pipeline orchestrator: download → transcribe → filter → extract → store."""
from __future__ import annotations

import asyncio
from pathlib import Path

from loguru import logger
from sqlalchemy import select

from src.db import session_scope
from src.models.content import RawContent
from src.models.source import DataSource
from src.whisper_pipeline.chunk_processor import ChunkProcessor
from src.whisper_pipeline.financial_filter import filter_chunk
from src.whisper_pipeline.stream_recorder import AUDIO_BASE, StreamRecorder
from src.whisper_pipeline.transcriber import Transcriber
from src.whisper_pipeline.vod_downloader import VODDownloader


class WhisperPipeline:
    """Coordinates live-chunk and VOD transcription, filtering, and storage."""

    def __init__(self) -> None:
        self._transcriber = Transcriber()
        self._processor = ChunkProcessor()

    async def process_pending_chunks(self) -> int:
        """Find unprocessed live audio chunks and run them through the pipeline."""
        if not AUDIO_BASE.exists():
            return 0

        chunks = list(AUDIO_BASE.rglob("chunk_*.webm"))
        processed = 0
        for chunk_path in sorted(chunks):
            if self._is_already_processed(chunk_path):
                continue
            try:
                success = await self._process_chunk(chunk_path)
                if success:
                    processed += 1
                    self._mark_processed(chunk_path)
            except Exception as exc:
                logger.error(f"chunk processing failed {chunk_path}: {exc}")

        if processed:
            logger.info(f"whisper pipeline: processed {processed} live chunks")
        return processed

    async def run_batch_vod(self) -> int:
        """Download and transcribe today's completed YouTube VODs."""
        downloader = VODDownloader()
        paths = await downloader.download_today_vods()
        processed = 0
        for path in paths:
            try:
                await self._process_audio_file(path, url=str(path), title=path.stem)
                processed += 1
            except Exception as exc:
                logger.error(f"VOD processing failed {path}: {exc}")
        logger.info(f"batch VOD: processed {processed} videos")
        return processed

    async def _process_chunk(self, path: Path) -> bool:
        transcript = await self._transcriber.transcribe(path)
        if not transcript:
            return False

        # Financial filter — skip non-financial chunks
        if not await filter_chunk(transcript):
            return True  # processed but skipped

        # Store as RawContent chunk for LLM extraction
        source = await self._get_youtube_source(path)
        if source is None:
            return False

        await self._processor.store_chunks(
            source_id=source.id,
            transcript=transcript,
            url=f"file://{path}",
            title=path.parent.parent.name,  # channel name
        )
        return True

    async def _process_audio_file(self, path: Path, url: str, title: str) -> None:
        # If it's a text file (captions), use directly
        if path.suffix == ".txt":
            transcript = path.read_text(encoding="utf-8")
        else:
            transcript = await self._transcriber.transcribe(path)
        if not transcript:
            return

        if not await filter_chunk(transcript):
            return

        source = await self._get_youtube_source(path)
        if source is None:
            return

        await self._processor.store_chunks(
            source_id=source.id,
            transcript=transcript,
            url=url,
            title=title,
        )

    async def _get_youtube_source(self, path: Path) -> DataSource | None:
        """Find the DataSource matching this audio file's channel directory."""
        channel_name = path.parent.parent.name  # /data/whisper/live/{channel}/{date}/
        async with session_scope() as session:
            result = await session.execute(
                select(DataSource).where(
                    DataSource.type.in_(["youtube_live", "youtube_vod"]),
                    DataSource.status == "active",
                )
            )
            sources = result.scalars().all()

        for source in sources:
            normalized = source.name.replace(" ", "_").lower()
            if normalized == channel_name:
                return source

        # Return first YouTube source as fallback
        if sources:
            return sources[0]
        return None

    def _is_already_processed(self, path: Path) -> bool:
        marker = path.with_suffix(".done")
        return marker.exists()

    def _mark_processed(self, path: Path) -> None:
        path.with_suffix(".done").touch()
