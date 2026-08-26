"""Session access on top of the LangGraph checkpointer.

The checkpointer is the single source of truth for a session, so the HTTP
cart and confirm endpoints always see exactly what the agent sees. Sessions
live in process memory and disappear on restart (Milestone 1 scope).

**Every key here is namespaced by tenant** (`t<tenant_id>:<session_id>`).
Session ids are supplied by the caller, so without a namespace one tenant could
read or write another's cart by sending a session id it guessed or reused — and
`min_length=1` was the only validation. Namespacing makes that unrepresentable
rather than merely checked: a caller can only ever address ids inside its own
prefix, so there is no ownership table to keep in sync and nothing to forget.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage

from agent.currency import sanitize_currency
from agent.graph import (
    get_graph,
    mark_turn_start,
    session_config,
    set_token_callback,
    set_turn_usage,
)
from agent.llm_router import is_system_busy, provider_trouble_message, system_busy_text

logger = logging.getLogger("agent.timing")

# Location posted before the first graph turn (browser geolocation).
# Keyed by the namespaced thread id, like the checkpointer itself.
_pending: dict[str, dict[str, Any]] = {}

# One lock per thread_id so two HTTP requests cannot interleave graph.invoke
# on the same session (that is how consecutive user turns with no assistant
# reply show up under a concurrent burst against one session_id).
_thread_locks: dict[str, threading.Lock] = {}
_thread_locks_guard = threading.Lock()

# Used when no tenant is in play (direct library use, benchmarks, tests).
DEFAULT_NAMESPACE = "local"


def _lock_for(tid: str) -> threading.Lock:
    with _thread_locks_guard:
        lock = _thread_locks.get(tid)
        if lock is None:
            lock = threading.Lock()
            _thread_locks[tid] = lock
        return lock


def thread_id(session_id: str, namespace: str | None = None) -> str:
    """Checkpointer key for a session, scoped to its tenant."""
    return f"{namespace or DEFAULT_NAMESPACE}:{session_id}"

WELCOME_NO_LOCATION = (
    "Hey! What are you in the mood for today, and which area of Karachi "
    "should I search near?"
)


def _merged_values(tid: str, values: dict[str, Any] | None) -> dict[str, Any]:
    merged = dict(values or {})
    pending = _pending.get(tid) or {}
    for key, value in pending.items():
        merged.setdefault(key, value)
    return merged


def get_values(session_id: str, namespace: str | None = None) -> dict[str, Any]:
    """Current state for a session, or {} if it has no history yet."""
    tid = thread_id(session_id, namespace)
    with _lock_for(tid):
        snapshot = get_graph().get_state(session_config(tid))
        values = dict(snapshot.values) if snapshot and snapshot.values else {}
        return _merged_values(tid, values)


def session_exists(session_id: str, namespace: str | None = None) -> bool:
    return bool(get_values(session_id, namespace))


def update_values(
    session_id: str, values: dict[str, Any], namespace: str | None = None
) -> dict[str, Any]:
    """Write state outside a model turn (used by POST /confirm)."""
    tid = thread_id(session_id, namespace)
    with _lock_for(tid):
        graph = get_graph()
        snapshot = graph.get_state(session_config(tid))
        if snapshot and snapshot.values:
            graph.update_state(session_config(tid), values)
            _pending.pop(tid, None)
            snapshot = graph.get_state(session_config(tid))
            stored = dict(snapshot.values) if snapshot and snapshot.values else {}
            return _merged_values(tid, stored)
        pending = _pending.setdefault(tid, {})
        pending.update(values)
        return _merged_values(tid, {})


def set_location(
    session_id: str, location: str, namespace: str | None = None
) -> dict[str, Any]:
    """Set OrderState.location, including before the first chat turn."""
    return update_values(session_id, {"location": location}, namespace)


def welcome_reply(session_id: str, namespace: str | None = None) -> str:
    """Opening line. No LLM call — page load must not wait on Groq."""
    location = get_values(session_id, namespace).get("location")
    if location:
        return (
            f"Hey! I've got you in {location}. What are you in the mood for, "
            "and how many people are we feeding?"
        )
    return WELCOME_NO_LOCATION


def send_message(
    session_id: str,
    message: str,
    namespace: str | None = None,
    usage: Any = None,
) -> dict[str, Any]:
    """Run one conversational turn and return the resulting state.

    `usage`, when given, is a TurnUsage the graph accumulates token counts into.
    It is registered here rather than by the caller because this runs in
    whichever thread actually drives the graph — see iter_chat_events.
    """
    mark_turn_start()
    if usage is not None:
        set_turn_usage(usage)
    tid = thread_id(session_id, namespace)
    t0 = time.perf_counter()
    graph = get_graph()
    with _lock_for(tid):
        extra = _pending.pop(tid, {})
        payload: dict[str, Any] = {"messages": [HumanMessage(content=message)]}
        payload.update(extra)
        try:
            result = graph.invoke(payload, session_config(tid))
        except Exception:
            # Last-resort pairing if something other than the LLM call blows
            # up after the user message was checkpointed.
            logger.exception("chat_turn_failed session_id=%s", tid)
            graph.update_state(
                session_config(tid),
                {"messages": [provider_trouble_message(tried="unknown")]},
            )
            snapshot = graph.get_state(session_config(tid))
            result = dict(snapshot.values) if snapshot and snapshot.values else {}
    logger.info(
        "chat_turn_wall_ms=%.1f session_id=%s",
        (time.perf_counter() - t0) * 1000,
        tid,
    )
    return result


def iter_chat_events(
    session_id: str,
    message: str,
    namespace: str | None = None,
    usage: Any = None,
):
    """Yield ('token', str) then ('done', state) or ('error', exc).

    Tokens are user-facing text from the final LLM round. Tool rounds are silent.
    """
    import queue
    import threading

    events: queue.Queue = queue.Queue()

    def on_token(piece: str) -> None:
        events.put(("token", piece))

    def worker() -> None:
        # Both of these are context-local, and context does not cross into a
        # thread we start ourselves — so they are registered in here, not by
        # the caller. The usage object itself was created in the request thread
        # and is merely mutated here, which is what makes token metering work
        # for streamed requests.
        set_token_callback(on_token)
        try:
            values = send_message(session_id, message, namespace, usage)
            events.put(("done", values))
        except Exception as exc:  # noqa: BLE001 — stream must close
            events.put(("error", exc))

    threading.Thread(target=worker, daemon=True).start()
    while True:
        kind, payload = events.get()
        yield kind, payload
        if kind in ("done", "error"):
            return


def last_turn_is_busy(values: dict[str, Any]) -> bool:
    """True when the last assistant turn is the last-resort system-busy notice."""
    for message in reversed(values.get("messages") or []):
        if not isinstance(message, AIMessage):
            continue
        if is_system_busy(message):
            return True
        content = message.content
        if isinstance(content, list):
            content = " ".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict)
            )
        if str(content).strip() or (getattr(message, "tool_calls", None) or []):
            return False
    return False


def latest_reply(values: dict[str, Any]) -> str:
    """Text of the last assistant message, ignoring tool-call-only turns."""
    for message in reversed(values.get("messages") or []):
        if not isinstance(message, AIMessage):
            continue
        if is_system_busy(message):
            return sanitize_currency(system_busy_text(message))
        content = message.content
        if isinstance(content, list):
            content = " ".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict)
            )
        text = sanitize_currency(str(content).strip())
        if text:
            return text
    return ""
