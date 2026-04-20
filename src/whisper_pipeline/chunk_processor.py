"""Split transcripts into processable chunks and store in DB."""
from __future__ import annotations

import uuid
from pathlib import Path

from loguru import logger
from sqlalchemy import select

from src.db import session_scope
from src.models.content import RawContent
from src.models.source import DataSource

CHUNK_WORDS = 300  # ~60-second speech at average speaking pace


class ChunkProcessor:
    """Splits long transcripts into overlapping chunks for LLM processing."""

    OVERLAP_WORDS = 30  # overlap to avoid splitting a signal at chunk boundary

    def split_transcript(self, text: str) -> list[str]:
        """Return list of overlapping word-chunk strings."""
        words = text.split()
        if not words:
            return []

        chunks: list[str] = []
        step = CHUNK_WORDS - self.OVERLAP_WORDS
        for i in range(0, len(words), step):
            chunk = " ".join(words[i: i + CHUNK_WORDS])
            if chunk:
                chunks.append(chunk)
        return chunks

    async def store_chunks(
        self,
        source_id: uuid.UUID,
        transcript: str,
        url: str,
        title: str,
    ) -> int:
        """Split and store transcript chunks as RawContent rows. Returns count stored."""
        chunks = self.split_transcript(transcript)
        stored = 0
        for i, chunk in enumerate(chunks):
            import hashlib
            content_hash = hashlib.sha256(f"{url}#{i}#{chunk[:50]}".encode()).hexdigest()

            async with session_scope() as session:
                # Skip duplicates
                result = await session.execute(
                    select(RawContent).where(RawContent.content_hash == content_hash)
                )
                if result.scalar_one_or_none():
                    continue

                row = RawContent(
                    source_id=source_id,
                    url=url,
                    title=f"{title} [chunk {i + 1}/{len(chunks)}]",
                    content_text=chunk,
                    content_hash=content_hash,
                    is_processed=False,
                    media_type="transcript",
                )
                session.add(row)
                stored += 1

        logger.info(f"stored {stored} transcript chunks from {url}")
        return stored
