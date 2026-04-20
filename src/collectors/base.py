"""Abstract base class for all data collectors."""
from __future__ import annotations

import hashlib
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from loguru import logger
from sqlalchemy import update

from src.db import session_scope
from src.models.source import DataSource, JobLog


@dataclass
class CollectorResult:
    """Summary of a single collector run."""

    items_fetched: int = 0
    items_new: int = 0
    errors: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []


class BaseCollector(ABC):
    """Base class for all collectors. Subclasses implement `_collect`."""

    job_name: str = "base_collector"

    def __init__(self, source_id: str | None = None) -> None:
        """Store the associated data_source UUID (may be None for price-only collectors)."""
        self.source_id = source_id

    @abstractmethod
    async def _collect(self) -> CollectorResult:
        """Perform the actual collection. Must be implemented by subclasses."""

    async def run(self) -> CollectorResult:
        """Run the collector, log the outcome to `job_log`, and update source stats."""
        start = time.monotonic()
        logger.info(f"[{self.job_name}] starting")
        result = CollectorResult()
        error_msg: str | None = None

        try:
            result = await self._collect()
            status = "completed"
        except Exception as exc:
            logger.exception(f"[{self.job_name}] failed: {exc}")
            error_msg = str(exc)
            status = "failed"
            result.errors.append(error_msg)

        duration_ms = int((time.monotonic() - start) * 1000)

        async with session_scope() as session:
            session.add(JobLog(
                job_name=self.job_name,
                source_id=self.source_id,
                status=status,
                items_processed=result.items_fetched,
                duration_ms=duration_ms,
                error_message=error_msg,
                metadata_={"new": result.items_new},
            ))
            if self.source_id:
                if status == "completed":
                    await session.execute(
                        update(DataSource)
                        .where(DataSource.id == self.source_id)
                        .values(
                            last_polled_at=datetime.utcnow(),
                            last_success_at=datetime.utcnow(),
                            consecutive_errors=0,
                            last_error=None,
                            total_items_fetched=DataSource.total_items_fetched + result.items_new,
                        )
                    )
                else:
                    await session.execute(
                        update(DataSource)
                        .where(DataSource.id == self.source_id)
                        .values(
                            last_polled_at=datetime.utcnow(),
                            consecutive_errors=DataSource.consecutive_errors + 1,
                            last_error=error_msg,
                        )
                    )

        logger.info(
            f"[{self.job_name}] {status} — fetched={result.items_fetched} "
            f"new={result.items_new} duration={duration_ms}ms"
        )
        return result

    @staticmethod
    def content_hash(title: str | None, url: str | None) -> str:
        """SHA-256 hash of title + url for exact-dup detection."""
        payload = f"{title or ''}|{url or ''}".encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def text_hash(text: str) -> str:
        """SHA-256 hash of an arbitrary string."""
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    @staticmethod
    def _now() -> datetime:
        return datetime.utcnow()

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} source_id={self.source_id}>"

    # Subclasses may override to return extra metadata for logging
    def extra_metadata(self) -> dict[str, Any]:
        return {}
