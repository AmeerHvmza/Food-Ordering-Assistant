"""Chat model selection: Gemini primary with Groq fallback, or a forced provider.

Default (GOOGLE_API_KEY / GEMINI_API_KEY present, LLM_PROVIDER unset): Gemini
``gemini-3.1-flash-lite`` with Groq ``openai/gpt-oss-20b`` on 429.

Set LLM_PROVIDER to force one backend:
  gemini / google / google_genai — routing (requires a Google key)
  groq / openai / anthropic      — that provider only (debug / legacy)

LLM_MODEL overrides the primary model. GROQ_FALLBACK_MODEL overrides the
fallback. Gemini thinking is pinned off (thinking_budget=0).
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Any

from langchain.chat_models import init_chat_model

from agent.llm_router import RoutingChatModel, describe_cooldown

logger = logging.getLogger("agent.timing")

# Probe order when LLM_PROVIDER is not set and no Google key is present.
PROVIDERS = (
    ("openai", "OPENAI_API_KEY", "gpt-4o-mini"),
    ("anthropic", "ANTHROPIC_API_KEY", "claude-sonnet-4-5"),
    ("groq", "GROQ_API_KEY", "openai/gpt-oss-120b"),
)

DEFAULT_TEMPERATURE = 0.3
DEFAULT_GEMINI_MODEL = "gemini-3.1-flash-lite"
DEFAULT_GROQ_FALLBACK_MODEL = "openai/gpt-oss-20b"

_ROUTING_ALIASES = {"gemini", "google", "google_genai"}
_SINGLE_PROVIDERS = {"openai", "anthropic", "groq"}


class NoProviderConfigured(RuntimeError):
    """No usable LLM credentials were found."""


def _google_api_key() -> str:
    return (os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY") or "").strip()


def _ensure_google_api_key_env() -> None:
    """langchain-google-genai reads GOOGLE_API_KEY; accept GEMINI_API_KEY too."""
    if os.getenv("GOOGLE_API_KEY"):
        return
    gemini_key = (os.getenv("GEMINI_API_KEY") or "").strip()
    if gemini_key:
        os.environ["GOOGLE_API_KEY"] = gemini_key


def _temperature() -> float:
    return float(os.getenv("LLM_TEMPERATURE", DEFAULT_TEMPERATURE))


def gemini_model_name() -> str:
    return (
        (os.getenv("GEMINI_MODEL") or os.getenv("LLM_MODEL") or "").strip()
        or DEFAULT_GEMINI_MODEL
    )


def groq_fallback_model_name() -> str:
    return (os.getenv("GROQ_FALLBACK_MODEL") or "").strip() or DEFAULT_GROQ_FALLBACK_MODEL


def use_gemini_routing() -> bool:
    """True when Gemini should be primary (with Groq fallback if keyed)."""
    requested = (os.getenv("LLM_PROVIDER") or "").strip().lower()
    if requested in _SINGLE_PROVIDERS:
        return False
    if requested in _ROUTING_ALIASES:
        if not _google_api_key():
            raise NoProviderConfigured(
                "LLM_PROVIDER=gemini but GOOGLE_API_KEY / GEMINI_API_KEY is not set."
            )
        return True
    if requested:
        return False
    return bool(_google_api_key())


def resolve_provider() -> tuple[str, str]:
    """Return (provider, model) from the environment (non-routing path)."""
    requested = (os.getenv("LLM_PROVIDER") or "").strip().lower()
    override_model = (os.getenv("LLM_MODEL") or "").strip()

    if requested:
        for provider, key_name, default_model in PROVIDERS:
            if provider != requested:
                continue
            if not os.getenv(key_name):
                raise NoProviderConfigured(
                    f"LLM_PROVIDER={provider} but {key_name} is not set."
                )
            return provider, override_model or default_model
        supported = ", ".join(p for p, _, _ in PROVIDERS) + ", gemini"
        raise NoProviderConfigured(
            f"Unknown LLM_PROVIDER={requested!r}. Supported: {supported}."
        )

    for provider, key_name, default_model in PROVIDERS:
        if os.getenv(key_name):
            return provider, override_model or default_model

    raise NoProviderConfigured(
        "No LLM credentials found. Set GOOGLE_API_KEY (Gemini primary) or one of: "
        + ", ".join(key for _, key, _ in PROVIDERS)
        + " (copy .env.example to .env)."
    )


_GROQ_RETRY_PATCHED = False


def _install_groq_retry_logging() -> None:
    """Log HTTP status / body before Groq SDK sleeps to retry.

    Clients are constructed with max_retries=0 so this should not fire; it is
    a diagnostic safety net if a retry still occurs.
    """
    global _GROQ_RETRY_PATCHED
    if _GROQ_RETRY_PATCHED:
        return
    try:
        from groq._base_client import AsyncAPIClient, SyncAPIClient
    except ImportError:
        return

    def _cause(response: Any) -> dict[str, Any]:
        if response is None:
            return {
                "status": None,
                "kind": "connection_or_timeout",
            }
        headers = {str(k).lower(): v for k, v in dict(response.headers).items()}
        body = ""
        try:
            body = (response.text or "")[:500]
        except Exception:
            body = "<unreadable>"
        return {
            "status": getattr(response, "status_code", None),
            "reason": getattr(response, "reason_phrase", None),
            "kind": "http_error",
            "retry_after": headers.get("retry-after") or headers.get("retry-after-ms"),
            "ratelimit_remaining_tokens": headers.get("x-ratelimit-remaining-tokens"),
            "ratelimit_remaining_requests": headers.get("x-ratelimit-remaining-requests"),
            "ratelimit_reset_tokens": headers.get("x-ratelimit-reset-tokens"),
            "body": body,
        }

    def _wrap(orig):  # noqa: ANN001
        def wrapped(
            self,  # noqa: ANN001
            *,
            retries_taken: int,
            max_retries: int,
            options: Any,
            response: Any,
        ) -> None:
            logger.warning(
                "groq_retry_cause retries_taken=%s max_retries=%s url=%s %s",
                retries_taken,
                max_retries,
                getattr(options, "url", None),
                _cause(response),
            )
            return orig(
                self,
                retries_taken=retries_taken,
                max_retries=max_retries,
                options=options,
                response=response,
            )

        return wrapped

    SyncAPIClient._sleep_for_retry = _wrap(SyncAPIClient._sleep_for_retry)
    AsyncAPIClient._sleep_for_retry = _wrap(AsyncAPIClient._sleep_for_retry)
    _GROQ_RETRY_PATCHED = True


def _init_gemini():
    _ensure_google_api_key_env()
    return init_chat_model(
        gemini_model_name(),
        model_provider="google_genai",
        temperature=_temperature(),
        max_retries=0,
        thinking_budget=0,
        include_thoughts=False,
    )


def _init_groq(model: str):
    _install_groq_retry_logging()
    return init_chat_model(
        model,
        model_provider="groq",
        temperature=_temperature(),
        max_retries=0,
    )


@lru_cache(maxsize=1)
def get_llm():
    """Build the chat model once per process."""
    if use_gemini_routing():
        gemini = _init_gemini()
        groq = None
        groq_model = groq_fallback_model_name()
        if os.getenv("GROQ_API_KEY"):
            groq = _init_groq(groq_model)
        return RoutingChatModel(
            gemini,
            groq,
            gemini_model=gemini_model_name(),
            groq_model=groq_model if groq is not None else "",
        )

    provider, model = resolve_provider()
    if provider == "groq":
        return RoutingChatModel(
            None,
            _init_groq(model),
            groq_model=model,
        )
    return init_chat_model(
        model,
        model_provider=provider,
        temperature=_temperature(),
    )


def describe() -> str:
    if use_gemini_routing():
        groq_label = groq_fallback_model_name() if os.getenv("GROQ_API_KEY") else "none"
        return (
            f"gemini:{gemini_model_name()}|groq:{groq_label} {describe_cooldown()}"
        )
    provider, model = resolve_provider()
    return f"{provider}:{model}"
