"""Groq Whisper STT."""

from __future__ import annotations

import io
import logging
import time

from groq import APIError

from voice.client import get_groq_client

logger = logging.getLogger(__name__)

STT_MODEL = "whisper-large-v3-turbo"

# Auto-detect maps Urdu to Hindi. Pin language to Urdu; the prompt is a
# sample of Urdu+English code-switching (English words stay Latin).
STT_LANGUAGE = "ur"
STT_PROMPT = (
    "میں pizza اور biryani آرڈر کرنا چاہتا ہوں from Saddar. "
    "This is mixed Urdu and English, not Hindi."
)


def transcribe(
    data: bytes,
    filename: str = "audio.webm",
    content_type: str = "audio/webm",
) -> tuple[str, int]:
    """Return (transcript, latency_ms). Urdu+English mix; Urdu script, not Hindi."""
    client = get_groq_client()
    started = time.perf_counter()
    buffer = io.BytesIO(data)
    buffer.name = filename
    buffer.seek(0)
    try:
        result = client.audio.transcriptions.create(
            model=STT_MODEL,
            file=(filename, buffer, content_type.split(";")[0].strip() or "audio/webm"),
            language=STT_LANGUAGE,
            prompt=STT_PROMPT,
        )
    except APIError:
        raise
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    text = (getattr(result, "text", None) or str(result) or "").strip()
    logger.info("STT %sms chars=%s file=%s", elapsed_ms, len(text), filename)
    return text, elapsed_ms
