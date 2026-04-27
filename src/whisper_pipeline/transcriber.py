"""Whisper model wrapper — local GPU or OpenAI cloud API."""
from __future__ import annotations

from pathlib import Path

from loguru import logger

INITIAL_PROMPT = (
    "Stock market analysis. Nifty, Sensex, BSE, NSE. "
    "RELIANCE, TCS, HDFC, INFOSYS, ICICI, ADANI, TATA. "
    "buy, sell, target, stop loss, bullish, bearish, resistance, support."
)


class Transcriber:
    """Transcribes audio files using Whisper (local or cloud).

    Prefers local GPU; falls back to OpenAI API; falls back to CPU.
    """

    def __init__(self, model_name: str | None = None, device: str | None = None) -> None:
        from src.config import get_settings
        settings = get_settings()
        self._model_name = model_name or settings.whisper_model
        self._device = device or settings.whisper_device
        self._model = None  # loaded lazily

    def _load_model(self):
        if self._model is not None:
            return
        try:
            import whisper
            self._model = whisper.load_model(self._model_name, device=self._device)
            logger.info(f"Whisper model '{self._model_name}' loaded on {self._device}")
        except Exception as exc:
            if self._device == "cuda":
                logger.warning(f"GPU load failed ({exc}), falling back to CPU")
                try:
                    import whisper
                    self._model = whisper.load_model(self._model_name, device="cpu")
                    self._device = "cpu"
                except Exception as exc2:
                    logger.error(f"Whisper local model unavailable: {exc2}")
                    self._model = None

    async def transcribe(self, audio_path: Path, language: str = "hi") -> str | None:
        """Transcribe an audio file. Returns plain text or None on failure."""
        import asyncio
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._transcribe_sync, audio_path, language)

    def _transcribe_sync(self, audio_path: Path, language: str) -> str | None:
        self._load_model()

        if self._model is not None:
            try:
                result = self._model.transcribe(
                    str(audio_path),
                    language=language,
                    task="transcribe",
                    word_timestamps=True,
                    condition_on_previous_text=True,
                    initial_prompt=INITIAL_PROMPT,
                )
                return result.get("text", "").strip()
            except Exception as exc:
                logger.error(f"local Whisper transcription failed: {exc}")

        # Fallback: OpenAI Whisper API
        from src.config import get_settings
        settings = get_settings()
        if settings.anthropic_api_key:  # reuse as proxy check — in practice use OPENAI_API_KEY
            pass

        try:
            from openai import OpenAI
            client = OpenAI()
            with open(audio_path, "rb") as f:
                result = client.audio.transcriptions.create(
                    model="whisper-1",
                    file=f,
                    language=language,
                    prompt=INITIAL_PROMPT,
                )
            return result.text.strip()
        except Exception as exc:
            logger.error(f"OpenAI Whisper API transcription failed: {exc}")
            return None
