"""Auth, rate limiting, metering and tenant isolation for the /v1 API.

The isolation tests are the point of this file. Before Milestone 6 the
session id was supplied by the client and used directly as the checkpointer
key, with `min_length=1` as the only validation — so any caller who guessed or
reused an id read somebody else's cart. These tests pin that shut.

No network and no model: the LLM is replaced with a scripted stub.
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

# Point the tenant store at a scratch file before anything opens it.
_TENANT_DB = Path(tempfile.gettempdir()) / f"tenants_test_{uuid.uuid4().hex}.db"
os.environ["TENANT_DB_PATH"] = str(_TENANT_DB)

from fastapi.testclient import TestClient  # noqa: E402
from langchain_core.messages import AIMessage, HumanMessage  # noqa: E402

import agent.graph as graph_module  # noqa: E402
from agent.llm_router import PROVIDER_TROUBLE_MESSAGE, RoutingChatModel  # noqa: E402
from api import sessions  # noqa: E402
from api.main import app  # noqa: E402
from auth import api_keys, rate_limit, store  # noqa: E402
from auth import usage as usage_mod  # noqa: E402

KARACHI = {"lat": 24.918, "lng": 67.091}


class ScriptedModel:
    """Stands in for the chat model, with believable token usage attached."""

    def __init__(self, reply: str = "Sure thing.") -> None:
        self.reply = reply
        self.calls = 0

    def bind_tools(self, tools):  # noqa: ANN001 - mirrors the real interface
        return self

    def _message(self) -> AIMessage:
        self.calls += 1
        return AIMessage(
            content=self.reply,
            response_metadata={
                "model_name": "scripted-model",
                "token_usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "total_tokens": 120,
                },
            },
        )

    def invoke(self, messages):  # noqa: ANN001
        return self._message()

    def stream(self, messages):  # noqa: ANN001
        yield self._message()


class ApiTestCase(unittest.TestCase):
    """Fresh tenant DB, fresh limiter and fresh sessions for every test."""

    def setUp(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            Path(str(_TENANT_DB) + suffix).unlink(missing_ok=True)
        store.init_db()
        rate_limit.reset_for_tests()
        sessions._pending.clear()
        # A shared in-memory checkpointer would leak state between tests.
        graph_module.get_graph.cache_clear()

        # Restored in tearDown: leaving a stub bound would silently change what
        # every later test module is exercising.
        self._real_get_llm = graph_module.get_llm

        self.client = TestClient(app)
        self.tenant_a = api_keys.create_tenant("acme", "pro")
        self.tenant_b = api_keys.create_tenant("globex", "pro")
        self.key_a = api_keys.create_key(self.tenant_a, "a")
        self.key_b = api_keys.create_key(self.tenant_b, "b")

    def tearDown(self) -> None:
        graph_module.get_llm = self._real_get_llm
        graph_module.get_graph.cache_clear()

    def auth(self, key: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {key}"}

    def key_id_for(self, tenant_id: int) -> int:
        return api_keys.list_keys(tenant_id)[0]["id"]


class AuthenticationTests(ApiTestCase):
    def test_no_key_is_rejected(self) -> None:
        response = self.client.post(
            "/v1/chat", json={"session_id": "s1", "message": "hi"}
        )
        self.assertEqual(response.status_code, 401)
        self.assertIn("API key", response.json()["detail"])

    def test_unknown_key_is_rejected(self) -> None:
        response = self.client.get(
            "/v1/usage", headers=self.auth("fda_live_not_a_real_key")
        )
        self.assertEqual(response.status_code, 401)

    def test_revoked_key_is_rejected(self) -> None:
        self.assertEqual(
            self.client.get("/v1/usage", headers=self.auth(self.key_a)).status_code,
            200,
        )
        api_keys.revoke_key(self.key_id_for(self.tenant_a))
        self.assertEqual(
            self.client.get("/v1/usage", headers=self.auth(self.key_a)).status_code,
            401,
        )

    def test_x_api_key_header_also_works(self) -> None:
        response = self.client.get("/v1/usage", headers={"X-API-Key": self.key_a})
        self.assertEqual(response.status_code, 200)

    def test_disabled_tenant_is_forbidden(self) -> None:
        with store.session() as conn:
            conn.execute(
                "UPDATE tenants SET disabled_at = ? WHERE id = ?",
                (store.utc_now(), self.tenant_a),
            )
            conn.commit()
        response = self.client.get("/v1/usage", headers=self.auth(self.key_a))
        self.assertEqual(response.status_code, 403)

    def test_health_stays_public(self) -> None:
        self.assertEqual(self.client.get("/health").status_code, 200)

    def test_key_is_never_stored_in_plaintext(self) -> None:
        with store.session() as conn:
            rows = [dict(r) for r in conn.execute("SELECT * FROM api_keys")]
        blob = repr(rows)
        self.assertNotIn(self.key_a, blob)
        self.assertNotIn(self.key_b, blob)
        for row in rows:
            self.assertEqual(len(row["key_hash"]), 64)  # sha256 hex

    def test_unversioned_routes_are_gone(self) -> None:
        """Leaving them alive would be a complete auth bypass."""
        for method, path in (
            ("post", "/chat"),
            ("post", "/chat/stream"),
            ("get", "/session/s1/cart"),
            ("post", "/session/s1/confirm"),
        ):
            response = self.client.request(method.upper(), path, json={})
            self.assertEqual(response.status_code, 404, f"{path} still exists")


class TenantIsolationTests(ApiTestCase):
    """One key must never reach another key's session, even with its exact id."""

    def _seed_session_for_a(self, session_id: str) -> None:
        response = self.client.post(
            f"/v1/sessions/{session_id}/location",
            json=KARACHI,
            headers=self.auth(self.key_a),
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(response.json()["location"])

    def test_identical_session_id_is_a_different_session(self) -> None:
        shared_id = "guessable-1"
        self._seed_session_for_a(shared_id)

        mine = self.client.post(
            f"/v1/sessions/{shared_id}/welcome", headers=self.auth(self.key_a)
        ).json()
        theirs = self.client.post(
            f"/v1/sessions/{shared_id}/welcome", headers=self.auth(self.key_b)
        ).json()

        self.assertEqual(mine["state"]["location"], "Gulshan-e-Iqbal")
        self.assertIsNone(
            theirs["state"]["location"],
            "tenant B saw tenant A's location through a shared session id",
        )

    def test_cart_is_not_readable_across_tenants(self) -> None:
        session_id = "guessable-2"
        graph_module.get_llm = lambda: ScriptedModel()
        self.client.post(
            "/v1/chat",
            json={"session_id": session_id, "message": "hello"},
            headers=self.auth(self.key_a),
        )

        mine = self.client.get(
            f"/v1/sessions/{session_id}/cart", headers=self.auth(self.key_a)
        )
        theirs = self.client.get(
            f"/v1/sessions/{session_id}/cart", headers=self.auth(self.key_b)
        )
        self.assertEqual(mine.status_code, 200)
        self.assertEqual(
            theirs.status_code,
            404,
            "tenant B could read a cart belonging to tenant A",
        )

    def test_conversation_history_does_not_leak(self) -> None:
        session_id = "guessable-3"
        graph_module.get_llm = lambda: ScriptedModel("Tenant A secret reply")
        self.client.post(
            "/v1/chat",
            json={"session_id": session_id, "message": "my card is 4111"},
            headers=self.auth(self.key_a),
        )
        theirs = self.client.post(
            f"/v1/sessions/{session_id}/welcome", headers=self.auth(self.key_b)
        ).json()
        self.assertEqual(theirs["state"]["messages"], [])

    def test_confirm_cannot_touch_another_tenants_session(self) -> None:
        session_id = "guessable-4"
        self._seed_session_for_a(session_id)
        response = self.client.post(
            f"/v1/sessions/{session_id}/confirm", headers=self.auth(self.key_b)
        )
        self.assertEqual(response.status_code, 404)

    def test_namespaces_are_distinct_per_tenant(self) -> None:
        principals = []
        for key in (self.key_a, self.key_b):
            principal = api_keys.resolve(key)
            self.assertIsNotNone(principal)
            principals.append(principal)
        self.assertNotEqual(principals[0].namespace, principals[1].namespace)
        self.assertNotEqual(
            sessions.thread_id("x", principals[0].namespace),
            sessions.thread_id("x", principals[1].namespace),
        )

    def test_issued_session_ids_are_uuid4(self) -> None:
        response = self.client.post("/v1/sessions", headers=self.auth(self.key_a))
        self.assertEqual(response.status_code, 201)
        parsed = uuid.UUID(response.json()["session_id"])
        self.assertEqual(parsed.version, 4)


class RateLimitTests(ApiTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.tenant_free = api_keys.create_tenant("tiny", "free")
        self.key_free = api_keys.create_key(self.tenant_free)

    def test_minute_limit_returns_429_with_retry_after(self) -> None:
        limited = None
        # free tier: burst 10, 20/min
        for _ in range(40):
            response = self.client.get("/v1/usage", headers=self.auth(self.key_free))
            if response.status_code == 429:
                limited = response
                break
        self.assertIsNotNone(limited, "burst limit never triggered")
        self.assertIn("Retry-After", limited.headers)
        self.assertGreaterEqual(int(limited.headers["Retry-After"]), 1)
        self.assertEqual(limited.headers["X-RateLimit-Scope"], "minute")

    def test_successful_responses_carry_limit_headers(self) -> None:
        response = self.client.get("/v1/usage", headers=self.auth(self.key_a))
        self.assertEqual(response.status_code, 200)
        for header in (
            "X-RateLimit-Limit",
            "X-RateLimit-Remaining",
            "X-RateLimit-Reset",
        ):
            self.assertIn(header, response.headers)

    def test_daily_quota_is_enforced_and_survives_a_restart(self) -> None:
        """The daily counter lives in SQLite precisely so it is not reset by
        a process restart, which an in-memory counter would be."""
        key_id = api_keys.list_keys(self.tenant_free)[0]["id"]
        with store.session() as conn:
            conn.execute(
                """
                INSERT INTO usage_daily (key_id, tenant_id, day, requests)
                VALUES (?, ?, ?, 100)
                """,
                (key_id, self.tenant_free, store.utc_day()),
            )
            conn.commit()

        rate_limit.reset_for_tests()  # clears only the in-memory minute bucket
        response = self.client.get("/v1/usage", headers=self.auth(self.key_free))
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["X-RateLimit-Scope"], "day")

    def test_quota_is_per_key_not_global(self) -> None:
        key_id = api_keys.list_keys(self.tenant_free)[0]["id"]
        with store.session() as conn:
            conn.execute(
                "INSERT INTO usage_daily (key_id, tenant_id, day, requests) "
                "VALUES (?, ?, ?, 100)",
                (key_id, self.tenant_free, store.utc_day()),
            )
            conn.commit()
        self.assertEqual(
            self.client.get("/v1/usage", headers=self.auth(self.key_free)).status_code,
            429,
        )
        self.assertEqual(
            self.client.get("/v1/usage", headers=self.auth(self.key_a)).status_code,
            200,
        )

    def test_rejected_requests_do_not_consume_quota(self) -> None:
        key_id = api_keys.list_keys(self.tenant_free)[0]["id"]
        for _ in range(40):
            self.client.get("/v1/usage", headers=self.auth(self.key_free))
        with store.session() as conn:
            row = conn.execute(
                "SELECT requests FROM usage_daily WHERE key_id = ? AND day = ?",
                (key_id, store.utc_day()),
            ).fetchone()
        # The burst limit rejected most of them; only served requests count.
        self.assertLessEqual(row["requests"], 20)


class MeteringTests(ApiTestCase):
    def _events(self, tenant_id: int) -> list[dict]:
        return usage_mod.recent_events(self.key_id_for(tenant_id), limit=50)

    def test_every_authenticated_request_is_recorded(self) -> None:
        self.client.get("/v1/usage", headers=self.auth(self.key_a))
        events = self._events(self.tenant_a)
        self.assertTrue(events)
        self.assertEqual(events[0]["route"], "/v1/usage")
        self.assertEqual(events[0]["status_code"], 200)
        self.assertIsNotNone(events[0]["latency_ms"])

    def test_rejected_requests_are_recorded_too(self) -> None:
        """Demand is worth measuring, not just served traffic."""
        tenant = api_keys.create_tenant("throttled", "free")
        key = api_keys.create_key(tenant)
        for _ in range(40):
            self.client.get("/v1/usage", headers=self.auth(key))
        statuses = {e["status_code"] for e in self._events(tenant)}
        self.assertIn(429, statuses)

    def test_chat_records_token_usage(self) -> None:
        graph_module.get_llm = lambda: ScriptedModel()
        response = self.client.post(
            "/v1/chat",
            json={"session_id": "m1", "message": "hello"},
            headers=self.auth(self.key_a),
        )
        self.assertEqual(response.status_code, 200)
        event = self._events(self.tenant_a)[0]
        self.assertEqual(event["route"], "/v1/chat")
        self.assertEqual(event["total_tokens"], 120)
        self.assertEqual(event["model"], "scripted-model")

    def test_streamed_chat_also_records_token_usage(self) -> None:
        """The regression this exists for.

        /v1/chat/stream runs the graph in a worker thread. A ContextVar-based
        accumulator would silently record zero tokens here — for the main chat
        path — while every other test still passed.
        """
        graph_module.get_llm = lambda: ScriptedModel()
        with self.client.stream(
            "POST",
            "/v1/chat/stream",
            json={"session_id": "m2", "message": "hello"},
            headers=self.auth(self.key_a),
        ) as response:
            self.assertEqual(response.status_code, 200)
            body = "".join(response.iter_text())
        self.assertIn('"done": true', body)

        event = self._events(self.tenant_a)[0]
        self.assertEqual(event["route"], "/v1/chat/stream")
        self.assertEqual(
            event["total_tokens"],
            120,
            "streamed requests recorded no tokens — the worker thread lost the "
            "usage accumulator",
        )

    def test_usage_endpoint_reports_the_callers_own_totals(self) -> None:
        self.client.get("/v1/usage", headers=self.auth(self.key_a))
        body = self.client.get("/v1/usage", headers=self.auth(self.key_a)).json()
        self.assertEqual(body["tenant"], "acme")
        self.assertEqual(body["tier"], "pro")
        self.assertEqual(body["requests_per_day"], 10000)
        self.assertTrue(body["days"])
        self.assertGreaterEqual(body["days"][0]["requests"], 1)

    def test_tenants_cannot_see_each_others_usage(self) -> None:
        self.client.get("/v1/usage", headers=self.auth(self.key_a))
        body = self.client.get("/v1/usage", headers=self.auth(self.key_b)).json()
        self.assertEqual(body["tenant"], "globex")
        self.assertEqual(sum(d["requests"] for d in body["days"]), 1)


def _unavailable() -> Exception:
    exc = Exception("503 UNAVAILABLE. This model is currently experiencing high demand.")
    exc.status_code = 503  # type: ignore[attr-defined]
    return exc


class BoomModel:
    def bind_tools(self, tools):  # noqa: ANN001
        return self

    def invoke(self, messages):  # noqa: ANN001
        raise _unavailable()

    def stream(self, messages):  # noqa: ANN001
        raise _unavailable()
        yield  # pragma: no cover


class OkModel:
    def bind_tools(self, tools):  # noqa: ANN001
        return self

    def invoke(self, messages):  # noqa: ANN001
        return AIMessage(content="ok")

    def stream(self, messages):  # noqa: ANN001
        yield AIMessage(content="ok")


class ProviderFailureTests(ApiTestCase):
    """Gemini 503 must not 502 or leave a stranded user turn."""

    def _roles(self, session_id: str) -> list[str]:
        values = sessions.get_values(session_id, f"t{self.tenant_a}")
        roles = []
        for message in values.get("messages") or []:
            if isinstance(message, HumanMessage):
                roles.append("user")
            elif isinstance(message, AIMessage):
                roles.append("assistant")
        return roles

    def test_http_503_from_provider_is_200_with_fallback_reply(self) -> None:
        graph_module.get_llm = lambda: BoomModel()
        response = self.client.post(
            "/v1/chat",
            json={"session_id": "orphan-1", "message": "hi"},
            headers=self.auth(self.key_a),
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["reply"], PROVIDER_TROUBLE_MESSAGE)
        self.assertTrue(body["busy"])
        self.assertEqual(self._roles("orphan-1"), ["user", "assistant"])

    def test_gemini_503_served_by_groq_still_pairs_the_turn(self) -> None:
        graph_module.get_llm = lambda: RoutingChatModel(BoomModel(), OkModel())
        response = self.client.post(
            "/v1/chat",
            json={"session_id": "orphan-2", "message": "hi"},
            headers=self.auth(self.key_a),
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["reply"], "ok")
        self.assertEqual(self._roles("orphan-2"), ["user", "assistant"])

    def test_concurrent_chat_returns_only_200_or_429(self) -> None:
        class Flaky:
            def __init__(self) -> None:
                self.n = 0
                self._lock = threading.Lock()

            def bind_tools(self, tools):  # noqa: ANN001
                return self

            def invoke(self, messages):  # noqa: ANN001
                with self._lock:
                    self.n += 1
                    n = self.n
                if n in {3, 7}:
                    raise _unavailable()
                return AIMessage(content=f"ok-{n}")

            def stream(self, messages):  # noqa: ANN001
                yield self.invoke(messages)

        graph_module.get_llm = lambda: Flaky()
        free = api_keys.create_tenant("burst", "free")
        key = api_keys.create_key(free)

        def hit(i: int) -> int:
            return self.client.post(
                "/v1/chat",
                json={"session_id": f"burst-{i}", "message": "hi"},
                headers={"Authorization": f"Bearer {key}"},
            ).status_code

        with ThreadPoolExecutor(max_workers=21) as pool:
            futures = [pool.submit(hit, i) for i in range(21)]
            codes = [fut.result() for fut in as_completed(futures)]

        unexpected = [c for c in codes if c not in {200, 429}]
        self.assertFalse(unexpected, f"unexpected status codes: {codes}")
        self.assertIn(429, codes)
        self.assertIn(200, codes)

    def test_same_session_concurrent_turns_stay_paired(self) -> None:
        graph_module.get_llm = lambda: OkModel()
        session_id = "same-session"

        def hit() -> int:
            return self.client.post(
                "/v1/chat",
                json={"session_id": session_id, "message": "hi"},
                headers=self.auth(self.key_a),
            ).status_code

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(hit) for _ in range(8)]
            codes = [fut.result() for fut in as_completed(futures)]
        self.assertEqual(set(codes), {200}, codes)
        roles = self._roles(session_id)
        self.assertEqual(len(roles), 16)
        self.assertEqual(roles, ["user", "assistant"] * 8)


class TierConfigTests(unittest.TestCase):
    def test_tiers_load_and_are_ordered_sensibly(self) -> None:
        from auth.tiers import get_tier, tier_names

        self.assertEqual(tier_names(), ["free", "pro", "unlimited"])
        self.assertEqual(get_tier("free").requests_per_day, 100)
        self.assertEqual(get_tier("pro").requests_per_day, 10000)
        self.assertLess(
            get_tier("free").requests_per_day, get_tier("pro").requests_per_day
        )

    def test_unknown_tier_fails_loudly(self) -> None:
        """A renamed tier must not silently inherit a generous allowance."""
        from auth.tiers import TierConfigError, get_tier

        with self.assertRaises(TierConfigError):
            get_tier("enterprise-platinum")


if __name__ == "__main__":
    unittest.main()
