"""Per-key usage metering.

Records what a billing system would need: requests per key, and LLM token
counts where a request ran a model turn. Nothing here charges anyone.

`TurnUsage` is a plain mutable object rather than a ContextVar-held value, for
a reason that has already bitten this codebase once. A chat turn makes several
LLM rounds, and `/chat/stream` runs the graph in a *worker thread*, so context
set in the request thread is not visible inside the graph — which is exactly
why `api/sessions.py` sets its token callback inside the worker. A
ContextVar-only accumulator would therefore record zero tokens for every
streamed request, i.e. for the main chat path, and would look like it worked.
An object created in the request thread and mutated by the worker is immune to
that, and it is testable.

Compare `agent/graph.py:_TurnClock`, which documents the same constraint.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any

from auth import store

logger = logging.getLogger("api.usage")


@dataclass
class TurnUsage:
    """Token counters accumulated across every LLM round of one request."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    llm_rounds: int = 0
    provider: str | None = None
    model: str | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add_round(self, metadata: dict[str, Any] | None) -> None:
        """Fold one LLM response's usage in. Tolerates every provider shape."""
        if not metadata:
            return
        usage = metadata.get("token_usage") or {}
        if not isinstance(usage, dict):
            usage = _as_dict(usage)

        prompt = _first_int(usage, ("prompt_tokens", "input_tokens"))
        completion = _first_int(usage, ("completion_tokens", "output_tokens"))
        total = _first_int(usage, ("total_tokens",))

        with self._lock:
            self.llm_rounds += 1
            self.prompt_tokens += prompt
            self.completion_tokens += completion
            self.total_tokens += total or (prompt + completion)
            if metadata.get("model"):
                self.model = str(metadata["model"])
            provider = metadata.get("routed_provider") or metadata.get("provider")
            if provider:
                self.provider = str(provider)

    def as_row(self) -> dict[str, Any]:
        return {
            "prompt_tokens": self.prompt_tokens or None,
            "completion_tokens": self.completion_tokens or None,
            "total_tokens": self.total_tokens or None,
            "llm_rounds": self.llm_rounds or None,
            "provider": self.provider,
            "model": self.model,
        }


def _as_dict(value: Any) -> dict[str, Any]:
    """LangChain's usage_metadata is sometimes an object, not a mapping."""
    if hasattr(value, "model_dump"):
        try:
            return dict(value.model_dump())
        except Exception:  # noqa: BLE001 - metering must never break a request
            return {}
    if hasattr(value, "__dict__"):
        return {k: v for k, v in vars(value).items() if not k.startswith("_")}
    return {}


def _first_int(source: dict[str, Any], keys: tuple[str, ...]) -> int:
    for key in keys:
        raw = source.get(key)
        if raw is None:
            continue
        try:
            return int(raw)
        except (TypeError, ValueError):
            continue
    return 0


def record_event(
    *,
    key_id: int,
    tenant_id: int,
    route: str,
    method: str,
    status_code: int,
    latency_ms: float,
    usage: TurnUsage | None = None,
) -> None:
    """Write one usage row. Failures are logged, never raised.

    Metering is bookkeeping: if it breaks, the customer's request should still
    succeed and the problem should show up in the log, not in their response.
    """
    tokens = usage.as_row() if usage else {}
    try:
        with store.session() as conn:
            conn.execute(
                """
                INSERT INTO usage_events (
                    key_id, tenant_id, created_at, route, method, status_code,
                    latency_ms, prompt_tokens, completion_tokens, total_tokens,
                    llm_rounds, provider, model
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    key_id,
                    tenant_id,
                    store.utc_now(),
                    route,
                    method,
                    status_code,
                    round(latency_ms, 2),
                    tokens.get("prompt_tokens"),
                    tokens.get("completion_tokens"),
                    tokens.get("total_tokens"),
                    tokens.get("llm_rounds"),
                    tokens.get("provider"),
                    tokens.get("model"),
                ),
            )
            if usage and usage.total_tokens:
                conn.execute(
                    """
                    UPDATE usage_daily
                    SET prompt_tokens     = prompt_tokens + ?,
                        completion_tokens = completion_tokens + ?,
                        total_tokens      = total_tokens + ?
                    WHERE key_id = ? AND day = ?
                    """,
                    (
                        usage.prompt_tokens,
                        usage.completion_tokens,
                        usage.total_tokens,
                        key_id,
                        store.utc_day(),
                    ),
                )
            conn.commit()
    except Exception:  # noqa: BLE001
        logger.exception("Failed to record usage for key_id=%s", key_id)


def usage_summary(key_id: int, days: int = 30) -> list[dict[str, Any]]:
    """Per-day totals for one key — what a billing job would read."""
    with store.session() as conn:
        return [
            dict(row)
            for row in conn.execute(
                """
                SELECT day, requests, prompt_tokens, completion_tokens, total_tokens
                FROM usage_daily
                WHERE key_id = ?
                ORDER BY day DESC
                LIMIT ?
                """,
                (key_id, days),
            )
        ]


def recent_events(key_id: int, limit: int = 20) -> list[dict[str, Any]]:
    with store.session() as conn:
        return [
            dict(row)
            for row in conn.execute(
                """
                SELECT created_at, route, method, status_code, latency_ms,
                       total_tokens, provider, model
                FROM usage_events
                WHERE key_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (key_id, limit),
            )
        ]
