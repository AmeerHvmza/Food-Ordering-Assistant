"""Process-wide Groq SDK client for STT and TTS.

Chat may use OpenAI or Anthropic; voice is Groq-only and needs GROQ_API_KEY
even when the LLM is another provider. One client per process — do not
construct Groq() inside request handlers.
"""

from __future__ import annotations

import os
from functools import lru_cache

from groq import Groq


class NoGroqKey(RuntimeError):
    """GROQ_API_KEY is missing; STT/TTS cannot run."""


@lru_cache(maxsize=1)
def get_groq_client() -> Groq:
    key = (os.getenv("GROQ_API_KEY") or "").strip()
    if not key:
        raise NoGroqKey(
            "GROQ_API_KEY is not set. Voice uses Groq Whisper and Orpheus "
            "even if the chat LLM is OpenAI or Anthropic."
        )
    return Groq(api_key=key)
