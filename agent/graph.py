"""LangGraph state graph for the ordering assistant.

    user message -> agent -> (tools -> agent)* -> reply

The agent node calls the model with a freshly built system prompt; the tool
node runs any requested tools and applies their state updates. The loop ends
when the model answers without asking for a tool.

Session persistence is the checkpointer, keyed by thread_id = session_id, so
the API layer has exactly one source of truth for a session.
"""

from __future__ import annotations

import contextvars
import logging
import threading
import time
from functools import lru_cache
from typing import Any

from langchain_core.messages import SystemMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from agent.llm import get_llm
from agent.llm_router import provider_trouble_message
from agent.prompts import build_routing_prompt, build_system_prompt
from agent.state import OrderState
from agent.tools import TOOLS

logger = logging.getLogger("agent.timing")

# Each tool call costs two steps (agent + tools), and one turn can legitimately
# chain remember -> search -> lock -> menu -> add -> view. 25 leaves room for
# about a dozen calls while still stopping a runaway loop.
RECURSION_LIMIT = 25


class _TurnClock:
    """Mutable per-turn counters. LangGraph copies the context into each node,
    so ContextVar integers would reset; a shared object keeps mutations."""

    __slots__ = ("started", "llm_rounds")

    def __init__(self) -> None:
        self.started = time.perf_counter()
        self.llm_rounds = 0


_turn_clock: contextvars.ContextVar[_TurnClock | None] = contextvars.ContextVar(
    "chat_turn_clock", default=None
)
_on_token: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "chat_on_token", default=None
)

# Per-request token accumulator (Milestone 6 metering). Same mutable-object
# reasoning as _TurnClock: a turn spans several LLM rounds and, when streaming,
# several threads, so the value has to be an object we mutate rather than one
# we rebind. None whenever the graph is driven outside the metered API.
_turn_usage: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "chat_turn_usage", default=None
)

_TOOLS_NODE = ToolNode(TOOLS)


def _metadata_for_log(message: Any) -> dict[str, Any]:
    """Pull Groq/OpenAI usage and any rate-limit headers off an AIMessage."""
    meta = dict(getattr(message, "response_metadata", None) or {})
    usage = getattr(message, "usage_metadata", None)
    extra = getattr(message, "additional_kwargs", None) or {}
    headers = (
        meta.get("headers")
        or meta.get("http_headers")
        or extra.get("headers")
        or {}
    )
    rate = {}
    if isinstance(headers, dict):
        for key, value in headers.items():
            lowered = str(key).lower()
            if "ratelimit" in lowered or "retry" in lowered or lowered.startswith("x-groq"):
                rate[str(key)] = value
    out: dict[str, Any] = {
        "model": meta.get("model_name") or meta.get("model"),
        "finish_reason": meta.get("finish_reason") or meta.get("stop_reason"),
        "token_usage": meta.get("token_usage") or usage,
        "system_fingerprint": meta.get("system_fingerprint"),
    }
    if rate:
        out["rate_limit_headers"] = rate
    # Keep unknown metadata keys so a Groq header we have not seen yet is still logged.
    extra_keys = {
        k: v
        for k, v in meta.items()
        if k not in {"model_name", "model", "finish_reason", "stop_reason", "token_usage", "system_fingerprint", "headers", "http_headers"}
    }
    if extra_keys:
        out["response_metadata_extra"] = extra_keys
    return {k: v for k, v in out.items() if v}


def _last_message_is_tool(state: OrderState) -> bool:
    history = state.get("messages") or []
    if not history:
        return False
    last = history[-1]
    return isinstance(last, ToolMessage) or getattr(last, "type", None) == "tool"


def _agent_node(state: OrderState) -> dict[str, Any]:
    """Call the model; use a short routing prompt after tool results."""
    clock = _turn_clock.get()
    round_n = 1
    if clock is not None:
        clock.llm_rounds += 1
        round_n = clock.llm_rounds
    routing = _last_message_is_tool(state)
    prompt = (
        build_routing_prompt(dict(state))
        if routing
        else build_system_prompt(dict(state))
    )
    history = state.get("messages") or []
    now = time.perf_counter()
    since_turn_ms = (now - clock.started) * 1000 if clock is not None else None
    logger.info(
        "llm_round=%s start since_turn_ms=%s prompt=%s system_prompt_chars=%s history_messages=%s",
        round_n,
        None if since_turn_ms is None else round(since_turn_ms, 1),
        "routing" if routing else "full",
        len(prompt),
        len(history),
    )

    model = get_llm().bind_tools(TOOLS)
    messages = [SystemMessage(content=prompt)]
    messages.extend(history)
    t0 = time.perf_counter()
    on_token = _on_token.get()
    try:
        if on_token:
            acc: Any = None
            for chunk in model.stream(messages):
                acc = chunk if acc is None else acc + chunk
                tool_calls = getattr(acc, "tool_calls", None) or []
                piece = getattr(chunk, "content", None)
                if not tool_calls and isinstance(piece, str) and piece:
                    on_token(piece)
            result = acc if acc is not None else model.invoke(messages)
        else:
            result = model.invoke(messages)
    except Exception as exc:
        status = getattr(exc, "status_code", None)
        body = getattr(exc, "body", None)
        logger.warning(
            "llm_call_failed type=%s status=%s body=%s err=%s",
            type(exc).__name__,
            status,
            body,
            str(exc)[:500],
        )
        # Do not re-raise: LangGraph has already checkpointed the user
        # message. Returning a paired assistant turn keeps history consistent
        # and stops FastAPI from turning this into a 502.
        return {"messages": [provider_trouble_message(tried="unknown")]}
    llm_ms = (time.perf_counter() - t0) * 1000
    tool_calls = getattr(result, "tool_calls", None) or []
    tool_names = [
        tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", None)
        for tc in tool_calls
    ]
    metadata = _metadata_for_log(result)
    logger.info(
        "llm_round=%s done duration_ms=%.1f tool_calls=%s metadata=%s",
        round_n,
        llm_ms,
        tool_names or None,
        metadata,
    )
    usage = _turn_usage.get()
    if usage is not None:
        # Metering must never cost a caller their answer.
        try:
            usage.add_round(metadata)
        except Exception:  # noqa: BLE001
            logger.warning("usage accounting failed", exc_info=True)
    return {"messages": [result]}


def _tools_node(state: OrderState) -> Any:
    last = (state.get("messages") or [None])[-1]
    tool_calls = getattr(last, "tool_calls", None) or []
    names = [
        tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", None)
        for tc in tool_calls
    ]
    clock = _turn_clock.get()
    since_turn_ms = (
        (time.perf_counter() - clock.started) * 1000 if clock is not None else None
    )
    t0 = time.perf_counter()
    result = _TOOLS_NODE.invoke(state)
    logger.info(
        "tool_round names=%s duration_ms=%.1f since_turn_ms=%s",
        names,
        (time.perf_counter() - t0) * 1000,
        None if since_turn_ms is None else round(since_turn_ms, 1),
    )
    return result


class ThreadSafeMemorySaver(MemorySaver):
    """MemorySaver with a lock around every storage call.

    The stock saver is a defaultdict with no lock. FastAPI runs sync `/v1/chat`
    in a thread pool, so concurrent turns (even on different session ids) race
    on `storage` / `writes` / `blobs`.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._lock = threading.RLock()

    def get_tuple(self, config):  # noqa: ANN001
        with self._lock:
            return super().get_tuple(config)

    def put(self, *args: Any, **kwargs: Any):  # noqa: ANN001
        with self._lock:
            return super().put(*args, **kwargs)

    def put_writes(self, *args: Any, **kwargs: Any) -> None:
        with self._lock:
            super().put_writes(*args, **kwargs)

    def list(self, *args: Any, **kwargs: Any):  # noqa: ANN001
        with self._lock:
            return list(super().list(*args, **kwargs))

    def delete_thread(self, thread_id: str) -> None:
        with self._lock:
            super().delete_thread(thread_id)


def build_graph(checkpointer: Any | None = None):
    """Compile the graph. Pass a checkpointer to override the default."""
    builder = StateGraph(OrderState)
    builder.add_node("agent", _agent_node)
    builder.add_node("tools", _tools_node)
    builder.set_entry_point("agent")
    builder.add_conditional_edges(
        "agent",
        tools_condition,
        {"tools": "tools", END: END},
    )
    builder.add_edge("tools", "agent")
    return builder.compile(checkpointer=checkpointer or ThreadSafeMemorySaver())


@lru_cache(maxsize=1)
def get_graph():
    """Process-wide compiled graph.

    MemorySaver keeps sessions in memory only: restarting the API drops every
    conversation. Milestone 6 should swap in a persistent checkpointer.
    """
    return build_graph()


def session_config(session_id: str) -> dict[str, Any]:
    return {
        "configurable": {"thread_id": session_id},
        "recursion_limit": RECURSION_LIMIT,
    }


def mark_turn_start() -> None:
    """Reset per-turn timing counters. Called by sessions.send_message."""
    _turn_clock.set(_TurnClock())


def set_token_callback(callback: Any) -> Any:
    """Receive streamed text tokens for the current turn (same thread)."""
    return _on_token.set(callback)


def set_turn_usage(usage: Any) -> Any:
    """Collect this turn's token counts into `usage` (same thread).

    Must be called from the thread that drives the graph, which for streamed
    requests is the worker, not the request thread.
    """
    return _turn_usage.set(usage)
