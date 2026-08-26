"""Gemini-primary / Groq-fallback chat model with process-wide 429 cooldown.

The graph keeps calling ``get_llm().bind_tools(TOOLS).invoke/stream(...)``.
This wrapper is the only place that branches on provider. Both LangChain
clients return ``AIMessage`` with ``tool_calls``, so ToolNode stays unchanged.

Cooldown is process-wide (one ``threading.Lock``), not per session: a Gemini
429 is a project quota, so every in-flight turn should skip Gemini.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from langchain_core.messages import AIMessage

logger = logging.getLogger("agent.timing")

BUSY_MESSAGE = "System busy, please try again."
PROVIDER_TROUBLE_MESSAGE = "Sorry, having trouble right now, try again."

# Last-resort only (both providers 429). Extra Groq attempts after the first
# failure; delays stay well under ~5s. Does not change Gemini cooldown timing.
_LAST_RESORT_EXTRA_ATTEMPTS = 2
_LAST_RESORT_DELAY_S = 1.5

# RPM/TPM: default 75s, clamp retry-after into this window.
_RPM_DEFAULT_S = 75.0
_RPM_MIN_S = 30.0
_RPM_MAX_S = 90.0

_PACIFIC = ZoneInfo("America/Los_Angeles")

_RETRY_AFTER_RE = re.compile(
    r"(?:retry[-_ ]?(?:after|in|delay)|retryDelay)[:\s\"]*([0-9]+(?:\.[0-9]+)?)\s*"
    r"(ms|s|sec|secs|second|seconds)?",
    re.IGNORECASE,
)

_lock = threading.Lock()
_gemini_cooling_until: float | None = None
_gemini_cooldown_kind: str | None = None


def reset_gemini_cooldown() -> None:
    """Clear process-wide Gemini cooldown. Tests only."""
    global _gemini_cooling_until, _gemini_cooldown_kind
    with _lock:
        _gemini_cooling_until = None
        _gemini_cooldown_kind = None


def gemini_cooldown_snapshot() -> dict[str, Any]:
    """Return cooldown state for ``describe()`` / ``/health``."""
    with _lock:
        until = _gemini_cooling_until
        kind = _gemini_cooldown_kind
    if until is None:
        return {"cooling": False, "kind": None, "remaining_s": 0.0}
    remaining = until - time.monotonic()
    if remaining <= 0:
        reset_gemini_cooldown()
        return {"cooling": False, "kind": None, "remaining_s": 0.0}
    return {"cooling": True, "kind": kind, "remaining_s": remaining}


def describe_cooldown() -> str:
    snap = gemini_cooldown_snapshot()
    if not snap["cooling"]:
        return "cooldown=off"
    return f"cooldown={snap['kind']} remaining_s={snap['remaining_s']:.0f}"


def seconds_until_pacific_midnight(now: datetime | None = None) -> float:
    """Seconds until next midnight America/Los_Angeles (Gemini RPD reset)."""
    current = now or datetime.now(_PACIFIC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=_PACIFIC)
    else:
        current = current.astimezone(_PACIFIC)
    tomorrow = (current + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return max(60.0, (tomorrow - current).total_seconds())


def _error_text(exc: BaseException) -> str:
    parts: list[str] = [str(exc), type(exc).__name__]
    for attr in ("body", "message", "status", "status_code", "code"):
        value = getattr(exc, attr, None)
        if value is not None:
            parts.append(str(value))
    response = getattr(exc, "response", None)
    if response is not None:
        for attr in ("text", "reason", "status_code"):
            value = getattr(response, attr, None)
            if value is not None:
                parts.append(str(value)[:800])
        headers = getattr(response, "headers", None)
        if headers:
            try:
                parts.append(str(dict(headers))[:400])
            except Exception:
                pass
    cause = getattr(exc, "__cause__", None)
    if cause is not None and cause is not exc:
        parts.append(str(cause)[:400])
    return " ".join(parts)


def is_transient_provider_error(exc: BaseException) -> bool:
    """True for overload / 5xx that should fail over, not abort the turn.

    Gemini 503 UNAVAILABLE ("high demand") is the live case: it is not a 429,
    so the quota cooldown path must not claim it, but Groq can still answer.
    Auth and client errors (401/400) stay hard failures.
    """
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(exc, "code", None)
    if status in {408, 500, 502, 503, 504}:
        return True
    status_name = str(getattr(exc, "status", "") or "").upper()
    if status_name in {"UNAVAILABLE", "INTERNAL", "DEADLINE_EXCEEDED", "ABORTED"}:
        return True
    text = _error_text(exc).lower()
    return (
        "unavailable" in text
        or "high demand" in text
        or "overloaded" in text
        or "503 " in text
        or text.startswith("503")
        or "temporarily unavailable" in text
    )


def is_fallback_error(exc: BaseException) -> bool:
    """Errors the router should absorb (Groq, then a paired fallback message)."""
    return is_rate_limit_error(exc) or is_transient_provider_error(exc)


def is_rate_limit_error(exc: BaseException) -> bool:
    """True only for HTTP 429 / RESOURCE_EXHAUSTED / rate_limit_exceeded."""
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(exc, "code", None)
    if status == 429:
        return True
    status_name = str(getattr(exc, "status", "") or "").upper()
    if status_name in {"RESOURCE_EXHAUSTED", "TOO_MANY_REQUESTS"}:
        return True
    name = type(exc).__name__.lower()
    if "ratelimit" in name or "resourceexhausted" in name:
        return True
    text = _error_text(exc).lower()
    return (
        " 429" in f" {text}"
        or "429 " in text
        or text.startswith("429")
        or "resource_exhausted" in text
        or "resource exhausted" in text
        or "rate_limit_exceeded" in text
        or "rate limit" in text
        or "too many requests" in text
    )


def classify_quota_kind(exc: BaseException) -> str:
    """Return ``rpd`` or ``rpm_tpm`` from a 429 body."""
    text = _error_text(exc).lower()
    daily = (
        "perday" in text
        or "per_day" in text
        or "per day" in text
        or "generaterequestsperday" in text
        or re.search(r"\brpd\b", text) is not None
        or re.search(r"\btpd\b", text) is not None
        or "tokens per day" in text
        or "requests per day" in text
        or "daily quota" in text
        or "quota exceeded for metric" in text
        and "day" in text
    )
    if daily:
        return "rpd"
    return "rpm_tpm"


def _parse_retry_after_seconds(exc: BaseException) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) if response is not None else None
    if headers:
        try:
            mapping = {str(k).lower(): v for k, v in dict(headers).items()}
        except Exception:
            mapping = {}
        raw = mapping.get("retry-after") or mapping.get("retry-after-ms")
        if raw is not None:
            try:
                value = float(str(raw).strip())
                if "retry-after-ms" in mapping and mapping.get("retry-after-ms") == raw:
                    value = value / 1000.0
                return value
            except ValueError:
                pass
    match = _RETRY_AFTER_RE.search(_error_text(exc))
    if not match:
        return None
    value = float(match.group(1))
    unit = (match.group(2) or "s").lower()
    if unit == "ms":
        value /= 1000.0
    return value


def cooldown_seconds_for(exc: BaseException) -> tuple[str, float]:
    kind = classify_quota_kind(exc)
    if kind == "rpd":
        return kind, seconds_until_pacific_midnight()
    parsed = _parse_retry_after_seconds(exc)
    seconds = _RPM_DEFAULT_S if parsed is None else parsed
    seconds = max(_RPM_MIN_S, min(_RPM_MAX_S, seconds))
    return kind, seconds


def _set_gemini_cooldown(kind: str, seconds: float) -> None:
    global _gemini_cooling_until, _gemini_cooldown_kind
    with _lock:
        _gemini_cooling_until = time.monotonic() + seconds
        _gemini_cooldown_kind = kind


def gemini_is_cooling() -> bool:
    snap = gemini_cooldown_snapshot()
    return bool(snap["cooling"])


def _tool_names(message: Any) -> list[str] | None:
    tool_calls = getattr(message, "tool_calls", None) or []
    names = [
        tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", None)
        for tc in tool_calls
    ]
    return names or None


def is_system_busy(message: Any) -> bool:
    meta = getattr(message, "response_metadata", None) or {}
    return bool(isinstance(meta, dict) and meta.get("system_busy"))


def system_busy_text(message: Any) -> str:
    meta = getattr(message, "response_metadata", None) or {}
    if isinstance(meta, dict) and meta.get("busy_text"):
        return str(meta["busy_text"])
    return BUSY_MESSAGE


def _stamp(message: Any, *, tried: str, served: str, fallback: bool) -> Any:
    if message is None:
        return message
    meta = getattr(message, "response_metadata", None)
    if not isinstance(meta, dict):
        meta = {}
        try:
            message.response_metadata = meta
        except Exception:
            return message
    meta["routed_provider"] = served
    meta["tried_provider"] = tried
    meta["fallback"] = fallback
    return message


def _busy_message(*, tried: str, fallback: bool, text: str = BUSY_MESSAGE) -> AIMessage:
    # Quota exhaustion keeps empty content so /chat/stream does not emit the
    # notice as tokens (frontend stays on "thinking…" until the done event).
    # Provider outages put the text in content so session history is a complete
    # turn, not a stranded user message.
    content = "" if text == BUSY_MESSAGE else text
    return AIMessage(
        content=content,
        response_metadata={
            "routed_provider": "none",
            "tried_provider": tried,
            "fallback": fallback,
            "system_busy": True,
            "provider_error": text != BUSY_MESSAGE,
            "busy_text": text,
        },
    )


def provider_trouble_message(*, tried: str = "unknown", fallback: bool = True) -> AIMessage:
    """Paired assistant turn when the model call fails for a non-quota reason."""
    return _busy_message(
        tried=tried, fallback=fallback, text=PROVIDER_TROUBLE_MESSAGE
    )


def _log_exhausted(*, tried: str, groq_attempts: int, t0: float) -> None:
    logger.warning(
        "llm_route_exhausted tried=%s groq_attempts=%s duration_ms=%.1f",
        tried,
        groq_attempts,
        (time.perf_counter() - t0) * 1000,
    )


def _log_route(
    *,
    tried: str,
    served: str,
    fallback: bool,
    t0: float,
    message: Any = None,
) -> None:
    snap = gemini_cooldown_snapshot()
    logger.info(
        "llm_route tried=%s served=%s fallback=%s cooldown_kind=%s duration_ms=%.1f tool_calls=%s",
        tried,
        served,
        str(fallback).lower(),
        snap["kind"],
        (time.perf_counter() - t0) * 1000,
        _tool_names(message),
    )


def _log_429(provider: str, exc: BaseException, kind: str, seconds: float) -> None:
    logger.warning(
        "llm_route_429 provider=%s kind=%s cooling_s=%.0f body=%s",
        provider,
        kind,
        seconds,
        _error_text(exc)[:400],
    )


class RoutingChatModel:
    """Duck-typed chat model: ``bind_tools`` / ``invoke`` / ``stream``."""

    def __init__(
        self,
        gemini: Any | None,
        groq: Any | None,
        *,
        gemini_model: str = "",
        groq_model: str = "",
    ) -> None:
        self.gemini = gemini
        self.groq = groq
        self.gemini_model = gemini_model
        self.groq_model = groq_model

    def bind_tools(self, tools: Any, **kwargs: Any) -> "RoutingChatModel":
        return RoutingChatModel(
            self.gemini.bind_tools(tools, **kwargs) if self.gemini is not None else None,
            self.groq.bind_tools(tools, **kwargs) if self.groq is not None else None,
            gemini_model=self.gemini_model,
            groq_model=self.groq_model,
        )

    def invoke(self, messages: Any, **kwargs: Any) -> AIMessage:
        t0 = time.perf_counter()
        cooling = gemini_is_cooling()
        try_gemini = self.gemini is not None and not cooling
        tried = "gemini" if try_gemini else "groq"
        gemini_429: BaseException | None = None
        gemini_transient: BaseException | None = None

        if try_gemini:
            try:
                result = self.gemini.invoke(messages, **kwargs)
            except Exception as exc:
                if is_rate_limit_error(exc):
                    gemini_429 = exc
                elif is_transient_provider_error(exc):
                    gemini_transient = exc
                else:
                    raise
            else:
                stamped = _stamp(
                    result, tried="gemini", served="gemini", fallback=False
                )
                _log_route(
                    tried="gemini",
                    served="gemini",
                    fallback=False,
                    t0=t0,
                    message=stamped,
                )
                return stamped

        if gemini_429 is not None:
            kind, seconds = cooldown_seconds_for(gemini_429)
            _set_gemini_cooldown(kind, seconds)
            _log_429("gemini", gemini_429, kind, seconds)
            return self._invoke_groq(
                messages, tried="gemini", fallback=True, t0=t0, **kwargs
            )

        if gemini_transient is not None:
            logger.warning(
                "llm_route_transient provider=gemini err=%s",
                _error_text(gemini_transient)[:400],
            )
            return self._invoke_groq(
                messages,
                tried="gemini",
                fallback=True,
                t0=t0,
                give_up_text=PROVIDER_TROUBLE_MESSAGE,
                **kwargs,
            )

        return self._invoke_groq(
            messages, tried=tried, fallback=cooling, t0=t0, **kwargs
        )

    def stream(self, messages: Any, **kwargs: Any) -> Iterator[Any]:
        t0 = time.perf_counter()
        cooling = gemini_is_cooling()
        try_gemini = self.gemini is not None and not cooling
        if try_gemini:
            yield from self._stream_gemini_then_groq(messages, t0=t0, **kwargs)
            return
        yield from self._stream_groq(
            messages,
            tried="groq" if self.gemini is None else "gemini",
            fallback=cooling,
            t0=t0,
            **kwargs,
        )

    def _give_up(
        self,
        *,
        tried: str,
        groq_attempts: int,
        t0: float,
        text: str = BUSY_MESSAGE,
    ) -> AIMessage:
        busy = _busy_message(tried=tried, fallback=True, text=text)
        _log_exhausted(tried=tried, groq_attempts=groq_attempts, t0=t0)
        _log_route(tried=tried, served="none", fallback=True, t0=t0, message=busy)
        return busy

    def _invoke_groq(
        self,
        messages: Any,
        *,
        tried: str,
        fallback: bool,
        t0: float,
        give_up_text: str = BUSY_MESSAGE,
        **kwargs: Any,
    ) -> AIMessage:
        if self.groq is None:
            return self._give_up(
                tried=tried, groq_attempts=0, t0=t0, text=give_up_text
            )
        attempts = 1 + _LAST_RESORT_EXTRA_ATTEMPTS
        saw_transient = give_up_text != BUSY_MESSAGE
        for attempt in range(1, attempts + 1):
            if attempt > 1:
                time.sleep(_LAST_RESORT_DELAY_S)
            try:
                result = self.groq.invoke(messages, **kwargs)
            except Exception as exc:
                if is_rate_limit_error(exc):
                    _log_429("groq", exc, classify_quota_kind(exc), 0)
                    continue
                if is_transient_provider_error(exc):
                    saw_transient = True
                    logger.warning(
                        "llm_route_transient provider=groq err=%s",
                        _error_text(exc)[:400],
                    )
                    continue
                raise
            stamped = _stamp(result, tried=tried, served="groq", fallback=fallback)
            _log_route(
                tried=tried, served="groq", fallback=fallback, t0=t0, message=stamped
            )
            return stamped
        text = PROVIDER_TROUBLE_MESSAGE if saw_transient else BUSY_MESSAGE
        return self._give_up(tried=tried, groq_attempts=attempts, t0=t0, text=text)

    def _stream_gemini_then_groq(
        self, messages: Any, *, t0: float, **kwargs: Any
    ) -> Iterator[Any]:
        assert self.gemini is not None
        yielded = 0
        gemini_429: BaseException | None = None
        gemini_transient: BaseException | None = None
        try:
            for chunk in self.gemini.stream(messages, **kwargs):
                yielded += 1
                yield _stamp(chunk, tried="gemini", served="gemini", fallback=False)
        except Exception as exc:
            if yielded:
                raise
            if is_rate_limit_error(exc):
                gemini_429 = exc
            elif is_transient_provider_error(exc):
                gemini_transient = exc
            else:
                raise
        if gemini_429 is not None:
            kind, seconds = cooldown_seconds_for(gemini_429)
            _set_gemini_cooldown(kind, seconds)
            _log_429("gemini", gemini_429, kind, seconds)
            yield from self._stream_groq(
                messages, tried="gemini", fallback=True, t0=t0, **kwargs
            )
            return
        if gemini_transient is not None:
            logger.warning(
                "llm_route_transient provider=gemini err=%s",
                _error_text(gemini_transient)[:400],
            )
            yield from self._stream_groq(
                messages,
                tried="gemini",
                fallback=True,
                t0=t0,
                give_up_text=PROVIDER_TROUBLE_MESSAGE,
                **kwargs,
            )
            return
        _log_route(tried="gemini", served="gemini", fallback=False, t0=t0)

    def _stream_groq(
        self,
        messages: Any,
        *,
        tried: str,
        fallback: bool,
        t0: float,
        give_up_text: str = BUSY_MESSAGE,
        **kwargs: Any,
    ) -> Iterator[Any]:
        if self.groq is None:
            yield self._give_up(
                tried=tried, groq_attempts=0, t0=t0, text=give_up_text
            )
            return
        attempts = 1 + _LAST_RESORT_EXTRA_ATTEMPTS
        saw_transient = give_up_text != BUSY_MESSAGE
        for attempt in range(1, attempts + 1):
            if attempt > 1:
                time.sleep(_LAST_RESORT_DELAY_S)
            yielded = 0
            last: Any = None
            try:
                for chunk in self.groq.stream(messages, **kwargs):
                    yielded += 1
                    last = chunk
                    yield _stamp(chunk, tried=tried, served="groq", fallback=fallback)
            except Exception as exc:
                if yielded:
                    raise
                if is_rate_limit_error(exc):
                    _log_429("groq", exc, classify_quota_kind(exc), 0)
                    continue
                if is_transient_provider_error(exc):
                    saw_transient = True
                    logger.warning(
                        "llm_route_transient provider=groq err=%s",
                        _error_text(exc)[:400],
                    )
                    continue
                raise
            _log_route(
                tried=tried, served="groq", fallback=fallback, t0=t0, message=last
            )
            return
        text = PROVIDER_TROUBLE_MESSAGE if saw_transient else BUSY_MESSAGE
        yield self._give_up(tried=tried, groq_attempts=attempts, t0=t0, text=text)
