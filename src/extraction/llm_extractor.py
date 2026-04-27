"""LLM-based signal extraction using the Anthropic Claude API."""
from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any

from anthropic import AsyncAnthropic
from loguru import logger
from sqlalchemy import select, update
from tenacity import retry, stop_after_attempt, wait_exponential

from src.config import get_settings
from src.db import session_scope
from src.extraction.entity_matcher import EntityMatcher
from src.extraction.prompts import (
    FILING_EXTRACTION_PROMPT,
    NEWS_EXTRACTION_PROMPT,
    PROMPT_VERSION,
    SYSTEM_PROMPT,
)
from src.models.content import RawContent
from src.models.llm_audit_log import LLMAuditLog
from src.models.signal import Signal
from src.models.source import DataSource


class LLMExtractor:
    """Run Claude over unprocessed `raw_content` rows and create `signals` rows."""

    _CALLER = "phase1.extractor"
    _TEMPERATURE = 0.0

    def __init__(self) -> None:
        self.settings = get_settings()
        self.client = AsyncAnthropic(api_key=self.settings.anthropic_api_key)
        self.model = self.settings.anthropic_model
        self.matcher = EntityMatcher()

    async def process_pending(self, limit: int = 20) -> int:
        """Process up to `limit` unprocessed raw_content items. Returns count of signals created."""
        async with session_scope() as session:
            rows = await session.execute(
                select(RawContent, DataSource)
                .join(DataSource, DataSource.id == RawContent.source_id)
                .where(RawContent.is_processed == False)  # noqa: E712
                .limit(limit)
            )
            batch = list(rows.all())

        total_signals = 0
        for content, source in batch:
            try:
                count = await self._process_one(content, source)
                total_signals += count
            except Exception as exc:
                logger.exception(f"extraction failed id={content.id}: {exc}")
                async with session_scope() as session:
                    await session.execute(
                        update(RawContent)
                        .where(RawContent.id == content.id)
                        .values(
                            is_processed=True,
                            processed_at=datetime.utcnow(),
                            processing_error=str(exc)[:500],
                        )
                    )
        return total_signals

    async def _process_one(self, content: RawContent, source: DataSource) -> int:
        """Extract signals from a single content item and persist them."""
        text = (content.content_text or "")[:6000]
        if not text or len(text) < 40:
            await self._mark_processed(content.id, result=None)
            return 0

        prompt = self._build_prompt(content, source, text)
        extraction, tokens_in, tokens_out, latency_ms, raw_response = await self._call_llm(prompt)

        await self._write_audit_log(
            caller_ref_id=content.id,
            prompt=prompt,
            response=raw_response,
            response_parsed=extraction,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=latency_ms,
        )

        await self._mark_processed(
            content.id,
            result=extraction,
            model=self.model,
            tokens=tokens_in + tokens_out if tokens_in and tokens_out else None,
        )

        signals = (extraction or {}).get("signals") or []
        if not signals:
            return 0

        created = 0
        async with session_scope() as session:
            for sig in signals:
                sym = (sig.get("stock_symbol") or "").strip()
                if not sym:
                    continue
                inst_id = await self.matcher.match(session, sym)
                if inst_id is None:
                    continue
                session.add(Signal(
                    content_id=content.id,
                    instrument_id=inst_id,
                    source_id=content.source_id,
                    action=(sig.get("action") or "WATCH").upper(),
                    timeframe=sig.get("timeframe") or "short_term",
                    entry_price=_num(sig.get("entry_price")),
                    target_price=_num(sig.get("target_price")),
                    stop_loss=_num(sig.get("stop_loss")),
                    confidence=_num(sig.get("confidence")),
                    reasoning=(sig.get("reasoning") or "")[:2000],
                    analyst_name_raw=sig.get("analyst_name"),
                ))
                created += 1
        return created

    def _build_prompt(
        self, content: RawContent, source: DataSource, text: str
    ) -> str:
        if source.type == "bse_filing" or source.type == "nse_announcement":
            return FILING_EXTRACTION_PROMPT.format(
                company_name=content.author or "",
                symbol=content.author or "",
                filing_type=content.media_type or "filing",
                date=content.published_at or "",
                content=text,
            )
        return NEWS_EXTRACTION_PROMPT.format(
            source_name=source.name,
            title=content.title or "",
            published_at=content.published_at or "",
            content=text,
        )

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=20))
    async def _call_llm(
        self, prompt: str
    ) -> tuple[dict[str, Any] | None, int, int, int, str]:
        """Call Claude and return (parsed, tokens_in, tokens_out, latency_ms, raw_text)."""
        t0 = time.monotonic()
        msg = await self.client.messages.create(
            model=self.model,
            max_tokens=2000,
            temperature=self._TEMPERATURE,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        latency_ms = int((time.monotonic() - t0) * 1000)
        raw = "".join(
            block.text for block in msg.content if getattr(block, "type", None) == "text"
        )
        tokens_in = msg.usage.input_tokens or 0
        tokens_out = msg.usage.output_tokens or 0
        try:
            parsed = json.loads(_strip_code_fence(raw))
        except json.JSONDecodeError:
            logger.warning(f"LLM returned non-JSON (first 200 chars): {raw[:200]}")
            parsed = None
        return parsed, tokens_in, tokens_out, latency_ms, raw

    async def _write_audit_log(
        self,
        caller_ref_id: Any,
        prompt: str,
        response: str,
        response_parsed: dict | None,
        tokens_in: int | None,
        tokens_out: int | None,
        latency_ms: int | None,
    ) -> None:
        """Persist one row to llm_audit_log (non-blocking — exceptions are logged, not raised)."""
        try:
            async with session_scope() as session:
                session.add(LLMAuditLog(
                    caller=self._CALLER,
                    caller_ref_id=caller_ref_id,
                    model=self.model,
                    temperature=self._TEMPERATURE,
                    prompt=prompt,
                    response=response,
                    response_parsed=response_parsed,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    latency_ms=latency_ms,
                ))
        except Exception as exc:
            logger.error(f"llm_audit_log write failed: {exc}")

    async def _mark_processed(
        self,
        content_id: Any,
        result: dict | None,
        model: str | None = None,
        tokens: int | None = None,
    ) -> None:
        async with session_scope() as session:
            await session.execute(
                update(RawContent)
                .where(RawContent.id == content_id)
                .values(
                    is_processed=True,
                    processed_at=datetime.utcnow(),
                    extraction_result={**(result or {}), "_prompt_version": PROMPT_VERSION},
                    extraction_model=model,
                    extraction_tokens=tokens,
                )
            )


def _num(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _strip_code_fence(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s[3:]
        if s.endswith("```"):
            s = s[: -3]
    return s.strip()
