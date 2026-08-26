"""Groq Orpheus TTS with 200-character chunking and WAV concatenation."""

from __future__ import annotations

import io
import logging
import os
import time
import wave

from groq import APIError

from voice.chunker import ORPHEUS_CHAR_LIMIT, chunk_for_tts
from voice.client import get_groq_client

logger = logging.getLogger(__name__)

TTS_MODEL = "canopylabs/orpheus-v1-english"
DEFAULT_VOICE = "troy"


def _voice() -> str:
    return (os.getenv("TTS_VOICE") or DEFAULT_VOICE).strip() or DEFAULT_VOICE


def concat_wavs(clips: list[bytes]) -> bytes:
    """Join WAV clips into one file. All clips must share channel/width/rate."""
    if not clips:
        raise ValueError("no WAV clips to concatenate")
    if len(clips) == 1:
        return clips[0]

    frames: list[bytes] = []
    nchannels = sampwidth = framerate = None
    for blob in clips:
        with wave.open(io.BytesIO(blob), "rb") as reader:
            if nchannels is None:
                nchannels = reader.getnchannels()
                sampwidth = reader.getsampwidth()
                framerate = reader.getframerate()
            else:
                same = (
                    reader.getnchannels() == nchannels
                    and reader.getsampwidth() == sampwidth
                    and reader.getframerate() == framerate
                )
                if not same:
                    raise ValueError(
                        "WAV clips have mismatched format; pin sample_rate "
                        f"on every Orpheus call (expected {framerate} Hz)"
                    )
            frames.append(reader.readframes(reader.getnframes()))

    out = io.BytesIO()
    with wave.open(out, "wb") as writer:
        writer.setnchannels(nchannels)
        writer.setsampwidth(sampwidth)
        writer.setframerate(framerate)
        writer.writeframes(b"".join(frames))
    return out.getvalue()


def speak(text: str) -> tuple[bytes, int, int]:
    """Synthesize `text` to a single WAV.

    Returns (wav_bytes, latency_ms, chunk_count).
    """
    chunks = chunk_for_tts(text, limit=ORPHEUS_CHAR_LIMIT)
    if not chunks:
        raise ValueError("nothing to speak")

    client = get_groq_client()
    voice = _voice()
    started = time.perf_counter()
    clips: list[bytes] = []
    for index, chunk in enumerate(chunks):
        try:
            response = client.audio.speech.create(
                model=TTS_MODEL,
                voice=voice,
                input=chunk,
                response_format="wav",
            )
        except APIError:
            raise
        clips.append(response.read())
        logger.info(
            "TTS chunk %s/%s chars=%s",
            index + 1,
            len(chunks),
            len(chunk),
        )

    wav = concat_wavs(clips)
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    logger.info(
        "TTS %sms chunks=%s chars=%s voice=%s",
        elapsed_ms,
        len(chunks),
        len(text),
        voice,
    )
    return wav, elapsed_ms, len(chunks)
