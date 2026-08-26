"""Gemini→Groq routing: 429 fallback, shared cooldown, RPM vs RPD."""

from __future__ import annotations

import os
import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from langchain_core.messages import AIMessage

from agent.llm_router import (
    BUSY_MESSAGE,
    PROVIDER_TROUBLE_MESSAGE,
    RoutingChatModel,
    classify_quota_kind,
    cooldown_seconds_for,
    is_rate_limit_error,
    is_system_busy,
    is_transient_provider_error,
    reset_gemini_cooldown,
    seconds_until_pacific_midnight,
)

_LLM_ENV_KEYS = (
    "LLM_PROVIDER",
    "LLM_MODEL",
    "GEMINI_MODEL",
    "GROQ_FALLBACK_MODEL",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "GROQ_API_KEY",
)


class _Env:
    """Temporarily replace LLM-related env vars and restore them after."""

    def __init__(self, **values: str) -> None:
        self.values = values
        self._saved: dict[str, str | None] = {}

    def __enter__(self) -> "_Env":
        self._saved = {key: os.environ.get(key) for key in _LLM_ENV_KEYS}
        for key in _LLM_ENV_KEYS:
            os.environ.pop(key, None)
        os.environ.update(self.values)
        from agent.llm import get_llm

        get_llm.cache_clear()
        return self

    def __exit__(self, *exc: object) -> None:
        for key in _LLM_ENV_KEYS:
            os.environ.pop(key, None)
        for key, value in self._saved.items():
            if value is not None:
                os.environ[key] = value
        from agent.llm import get_llm

        get_llm.cache_clear()
        reset_gemini_cooldown()


def _rate_limit(message: str, status: int = 429) -> Exception:
    exc = Exception(message)
    exc.status_code = status  # type: ignore[attr-defined]
    return exc


class FakeModel:
    def __init__(self, responses: list) -> None:
        self.responses = list(responses)
        self.calls = 0
        self.bound_tools = None

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001
        self.bound_tools = tools
        return self

    def invoke(self, messages, **kwargs):  # noqa: ANN001
        self.calls += 1
        if not self.responses:
            return AIMessage(content="ok")
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def stream(self, messages, **kwargs):  # noqa: ANN001
        result = self.invoke(messages, **kwargs)
        yield result


class QuotaKindTests(unittest.TestCase):
    def test_detects_429_status(self) -> None:
        self.assertTrue(is_rate_limit_error(_rate_limit("nope")))

    def test_detects_resource_exhausted_name(self) -> None:
        class ResourceExhausted(Exception):
            pass

        self.assertTrue(is_rate_limit_error(ResourceExhausted("quota")))

    def test_ignores_auth_errors(self) -> None:
        exc = Exception("API key not valid")
        exc.status_code = 401  # type: ignore[attr-defined]
        self.assertFalse(is_rate_limit_error(exc))
        self.assertFalse(is_transient_provider_error(exc))

    def test_detects_gemini_high_demand_503(self) -> None:
        exc = Exception(
            "503 UNAVAILABLE. {'error': {'code': 503, 'message': "
            "'This model is currently experiencing high demand.', "
            "'status': 'UNAVAILABLE'}}"
        )
        self.assertTrue(is_transient_provider_error(exc))
        self.assertFalse(is_rate_limit_error(exc))

    def test_rpm_default_75_clamped_window(self) -> None:
        kind, seconds = cooldown_seconds_for(
            _rate_limit("RESOURCE_EXHAUSTED GenerateRequestsPerMinute")
        )
        self.assertEqual(kind, "rpm_tpm")
        self.assertEqual(seconds, 75.0)

    def test_rpm_retry_after_clamped_to_30(self) -> None:
        kind, seconds = cooldown_seconds_for(
            _rate_limit("PerMinute Please retry in 12 seconds")
        )
        self.assertEqual(kind, "rpm_tpm")
        self.assertEqual(seconds, 30.0)

    def test_rpm_retry_after_kept_inside_window(self) -> None:
        kind, seconds = cooldown_seconds_for(
            _rate_limit("PerMinute retryDelay: 58s")
        )
        self.assertEqual(kind, "rpm_tpm")
        self.assertEqual(seconds, 58.0)

    def test_rpm_retry_after_clamped_to_90(self) -> None:
        kind, seconds = cooldown_seconds_for(
            _rate_limit("tokens per minute retry in 120 seconds")
        )
        self.assertEqual(kind, "rpm_tpm")
        self.assertEqual(seconds, 90.0)

    def test_rpd_until_pacific_midnight(self) -> None:
        exc = _rate_limit(
            "429 RESOURCE_EXHAUSTED GenerateRequestsPerDay quota exceeded"
        )
        self.assertEqual(classify_quota_kind(exc), "rpd")
        kind, seconds = cooldown_seconds_for(exc)
        self.assertEqual(kind, "rpd")
        expected = seconds_until_pacific_midnight()
        self.assertAlmostEqual(seconds, expected, delta=2)

    def test_pacific_midnight_math(self) -> None:
        now = datetime(2026, 8, 19, 15, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
        # 9 hours until midnight.
        self.assertEqual(seconds_until_pacific_midnight(now), 9 * 3600)


class RoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_gemini_cooldown()

    def tearDown(self) -> None:
        reset_gemini_cooldown()

    def test_gemini_429_falls_back_to_groq(self) -> None:
        gemini = FakeModel([_rate_limit("429 rate_limit_exceeded PerMinute")])
        groq = FakeModel([AIMessage(content="from groq")])
        router = RoutingChatModel(gemini, groq).bind_tools([])
        result = router.invoke([])
        self.assertEqual(result.content, "from groq")
        self.assertEqual(gemini.calls, 1)
        self.assertEqual(groq.calls, 1)
        self.assertTrue(result.response_metadata["fallback"])
        self.assertEqual(result.response_metadata["routed_provider"], "groq")

    def test_both_429_retries_then_system_busy(self) -> None:
        gemini = FakeModel([_rate_limit("429 PerMinute")])
        groq = FakeModel(
            [
                _rate_limit("429 tokens per day TPD"),
                _rate_limit("429 tokens per day TPD"),
                _rate_limit("429 tokens per day TPD"),
            ]
        )
        router = RoutingChatModel(gemini, groq)
        with patch("agent.llm_router.time.sleep") as slept:
            result = router.invoke([])
        self.assertTrue(result.response_metadata["system_busy"])
        self.assertTrue(is_system_busy(result))
        self.assertEqual(result.content, "")
        self.assertEqual(result.response_metadata["routed_provider"], "none")
        self.assertFalse(result.tool_calls)
        self.assertEqual(gemini.calls, 1)
        self.assertEqual(groq.calls, 3)
        self.assertEqual(slept.call_count, 2)

    def test_groq_429_recovers_on_bounded_retry(self) -> None:
        gemini = FakeModel([_rate_limit("429 PerMinute")])
        groq = FakeModel(
            [_rate_limit("429 TPD"), AIMessage(content="from groq retry")]
        )
        router = RoutingChatModel(gemini, groq)
        with patch("agent.llm_router.time.sleep") as slept:
            result = router.invoke([])
        self.assertEqual(result.content, "from groq retry")
        self.assertFalse(result.response_metadata.get("system_busy"))
        self.assertEqual(result.response_metadata["routed_provider"], "groq")
        self.assertEqual(groq.calls, 2)
        self.assertEqual(slept.call_count, 1)

    def test_shared_cooldown_skips_gemini_on_next_call(self) -> None:
        gemini = FakeModel(
            [
                _rate_limit("429 PerMinute retry in 58s"),
                AIMessage(content="gemini should not run"),
            ]
        )
        groq = FakeModel(
            [AIMessage(content="groq-1"), AIMessage(content="groq-2")]
        )
        router = RoutingChatModel(gemini, groq)
        first = router.invoke([])
        second = router.invoke([])
        self.assertEqual(first.content, "groq-1")
        self.assertEqual(second.content, "groq-2")
        self.assertEqual(gemini.calls, 1)
        self.assertEqual(groq.calls, 2)
        self.assertTrue(second.response_metadata["fallback"])

    def test_non_429_does_not_fall_back(self) -> None:
        gemini = FakeModel([_rate_limit("API key invalid", status=401)])
        groq = FakeModel([AIMessage(content="should not run")])
        router = RoutingChatModel(gemini, groq)
        with self.assertRaises(Exception) as ctx:
            router.invoke([])
        self.assertIn("invalid", str(ctx.exception))
        self.assertEqual(groq.calls, 0)

    def test_gemini_503_falls_back_to_groq(self) -> None:
        gemini = FakeModel([_rate_limit("503 UNAVAILABLE high demand", status=503)])
        groq = FakeModel([AIMessage(content="from groq")])
        router = RoutingChatModel(gemini, groq)
        result = router.invoke([])
        self.assertEqual(result.content, "from groq")
        self.assertEqual(groq.calls, 1)
        self.assertTrue(result.response_metadata["fallback"])
        self.assertEqual(result.response_metadata["routed_provider"], "groq")

    def test_gemini_and_groq_503_returns_paired_fallback(self) -> None:
        gemini = FakeModel([_rate_limit("503 UNAVAILABLE", status=503)])
        groq = FakeModel(
            [
                _rate_limit("503 UNAVAILABLE", status=503),
                _rate_limit("503 UNAVAILABLE", status=503),
                _rate_limit("503 UNAVAILABLE", status=503),
            ]
        )
        router = RoutingChatModel(gemini, groq)
        with patch("agent.llm_router.time.sleep"):
            result = router.invoke([])
        self.assertEqual(result.content, PROVIDER_TROUBLE_MESSAGE)
        self.assertTrue(result.response_metadata["provider_error"])
        self.assertTrue(is_system_busy(result))
        self.assertEqual(groq.calls, 3)

    def test_stream_gemini_429_retries_groq_from_scratch(self) -> None:
        gemini = FakeModel([_rate_limit("429 PerMinute")])
        groq = FakeModel([AIMessage(content="streamed groq")])
        router = RoutingChatModel(gemini, groq)
        chunks = list(router.stream([]))
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].content, "streamed groq")
        self.assertEqual(gemini.calls, 1)
        self.assertEqual(groq.calls, 1)


class GetLlmWiringTests(unittest.TestCase):
    def tearDown(self) -> None:
        from agent.llm import get_llm

        get_llm.cache_clear()
        reset_gemini_cooldown()

    def test_both_clients_constructed_with_max_retries_zero(self) -> None:
        from agent.llm import describe, get_llm

        created: list[tuple[str, dict]] = []

        def fake_init(model, **kwargs):  # noqa: ANN001
            created.append((model, kwargs))
            return FakeModel([])

        with _Env(GOOGLE_API_KEY="gk", GROQ_API_KEY="rk"):
            with patch("agent.llm.init_chat_model", side_effect=fake_init):
                llm = get_llm()
                summary = describe()

        self.assertIsInstance(llm, RoutingChatModel)
        self.assertEqual(len(created), 2)
        gemini_kwargs = created[0][1]
        groq_kwargs = created[1][1]
        self.assertEqual(created[0][1]["model_provider"], "google_genai")
        self.assertEqual(created[1][1]["model_provider"], "groq")
        self.assertEqual(gemini_kwargs["max_retries"], 0)
        self.assertEqual(groq_kwargs["max_retries"], 0)
        self.assertEqual(gemini_kwargs["thinking_budget"], 0)
        self.assertIn("gemini-3.1-flash-lite", summary)
        self.assertIn("openai/gpt-oss-20b", summary)
        self.assertIn("cooldown=off", summary)

    def test_groq_only_also_uses_max_retries_zero(self) -> None:
        from agent.llm import get_llm

        created: list[dict] = []

        def fake_init(model, **kwargs):  # noqa: ANN001
            created.append(kwargs)
            return FakeModel([])

        with _Env(LLM_PROVIDER="groq", GROQ_API_KEY="rk"):
            with patch("agent.llm.init_chat_model", side_effect=fake_init):
                llm = get_llm()

        self.assertIsInstance(llm, RoutingChatModel)
        self.assertIsNone(llm.gemini)
        self.assertEqual(created[0]["max_retries"], 0)
        self.assertEqual(created[0]["model_provider"], "groq")


if __name__ == "__main__":
    unittest.main()
