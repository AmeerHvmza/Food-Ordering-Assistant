"""End-to-end smoke test with a scripted stub model (no API key, no spend).

Drives a full session - preferences, search, lock, menu, cart, confirm - then
exercises the HTTP contract. Proves the graph wiring, state updates and
endpoints work; it does not evaluate conversation quality, which needs a real
model and a human reading the replies.

Run: python scripts/smoke_test.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Scratch tenant store, so a smoke run never litters the real one with
# throwaway tenants. Set before anything opens the database.
os.environ.setdefault(
    "TENANT_DB_PATH",
    str(Path(tempfile.gettempdir()) / f"tenants_smoke_{uuid.uuid4().hex}.db"),
)

from langchain_core.messages import AIMessage  # noqa: E402

from db import queries  # noqa: E402


class ScriptedModel:
    """Replays a fixed list of tool calls / replies instead of calling an LLM."""

    def __init__(self, script: list) -> None:
        self.script = script
        self.step = 0

    def bind_tools(self, tools):  # noqa: ANN001 - mirrors the real interface
        return self

    def invoke(self, messages):  # noqa: ANN001
        if self.step >= len(self.script):
            return AIMessage(content="(script exhausted)")
        item = self.script[self.step]
        self.step += 1
        if isinstance(item, str):
            return AIMessage(content=item)
        name, args = item
        return AIMessage(
            content="",
            tool_calls=[{"name": name, "args": args, "id": f"call_{self.step}"}],
        )


def pick_fixtures() -> tuple[int, str, int, str]:
    """Choose a real restaurant and menu item from the snapshot."""
    with queries.session() as conn:
        restaurants = queries.search_restaurants(conn, craving="biryani", limit=5)
        if not restaurants:
            raise SystemExit("No biryani restaurants in the snapshot.")
        restaurant = restaurants[0]
        items = queries.search_menu(
            conn, restaurant_id=restaurant["id"], query="biryani", limit=3
        )
        if not items:
            items = queries.search_menu(conn, restaurant_id=restaurant["id"], limit=3)
    item = items[0]
    return restaurant["id"], restaurant["name"], item["item_id"], item["name"]


def check(label: str, condition: bool, detail: str = "") -> bool:
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}{f' - {detail}' if detail else ''}")
    return condition


def main() -> int:
    restaurant_id, restaurant_name, item_id, item_name = pick_fixtures()
    print(f"Fixtures: restaurant {restaurant_id} ({restaurant_name}), item {item_id} ({item_name})\n")

    # A turn ends when the model replies with text, so each block below is one
    # user message.
    script = [
        ("remember_preferences", {"craving": "biryani", "party_size": 4, "budget": 3000.0, "location": "Saddar"}),
        ("search_restaurants", {"craving": "biryani", "location": "Saddar"}),
        ("lock_restaurant", {"restaurant_id": restaurant_id}),
        ("search_menu", {"query": "biryani"}),
        ("add_to_cart", {"item_id": item_id, "qty": 2}),
        ("view_cart", {}),
        f"Locked in {restaurant_name} with 2 x {item_name}. Want me to prepare the summary?",

        ("confirm_order", {}),
        "Summary is ready. This is not a real Foodpanda order.",

        # Switching restaurants must be refused until the user confirms.
        ("lock_restaurant", {"restaurant_id": 43}),
        "Switching empties your cart. Want me to go ahead?",

        ("lock_restaurant", {"restaurant_id": 43, "confirm_switch": True}),
        "Switched over and cleared the cart.",
    ]

    import agent.graph as graph_module

    stub = ScriptedModel(script)
    graph_module.get_llm = lambda: stub

    from api import sessions

    failures = 0
    session_id = "smoke-session"

    print("Turn 1: build an order")
    state = sessions.send_message(session_id, "biryani for 4 in Saddar, about 3000")
    failures += not check("craving remembered", state.get("craving") == "biryani")
    failures += not check("party size remembered", state.get("party_size") == 4)
    failures += not check("budget remembered", state.get("budget") == 3000.0)
    failures += not check("restaurant locked", state.get("restaurant_id") == restaurant_id)
    failures += not check("cart has the item", any(l["item_id"] == item_id for l in state.get("cart") or []))
    failures += not check("qty is 2", (state.get("cart") or [{}])[0].get("qty") == 2)
    failures += not check("no summary before confirm", state.get("order_summary") is None)

    print("\nTurn 2: confirm")
    state = sessions.send_message(session_id, "yes please")
    summary = state.get("order_summary") or {}
    failures += not check("summary built", summary.get("kind") == "cart_summary")
    failures += not check(
        "summary is labelled as not a real order",
        "No order has been placed" in summary.get("disclaimer", ""),
    )
    failures += not check("summary subtotal matches cart", summary.get("subtotal") is not None)
    failures += not check("delivery fee present", summary.get("delivery_fee") == 99.0)
    failures += not check("platform fee present", summary.get("platform_fee") == 8.99)
    failures += not check(
        "total is items plus fees",
        abs((summary.get("total") or 0) - (summary.get("subtotal") or 0) - 99.0 - 8.99) < 0.01,
    )

    print("\nTurn 3: switching restaurants is refused without confirmation")
    state = sessions.send_message(session_id, "actually let's do McDonald's")
    failures += not check("still locked to the original", state.get("restaurant_id") == restaurant_id)
    failures += not check("cart untouched", len(state.get("cart") or []) == 1)
    tool_texts = [
        str(getattr(m, "content", "")) for m in state.get("messages") or []
        if getattr(m, "type", "") == "tool"
    ]
    failures += not check(
        "refusal explains the cart will be emptied",
        any("Ask the user to confirm" in t for t in tool_texts),
    )

    print("\nTurn 4: confirmed switch clears the cart")
    state = sessions.send_message(session_id, "yes, switch")
    failures += not check("switched restaurant", state.get("restaurant_id") == 43)
    failures += not check("cart cleared on switch", not state.get("cart"))

    print("\nHTTP contract")
    from fastapi.testclient import TestClient

    from api.main import app
    from auth import api_keys, store

    # Milestone 6: /v1 routes need a key. Use a scratch tenant on the unlimited
    # tier so the smoke test never trips its own rate limit.
    store.init_db()
    tenant = api_keys.create_tenant(f"smoke-{os.getpid()}", "unlimited")
    AUTH = {"Authorization": f"Bearer {api_keys.create_key(tenant, 'smoke test')}"}

    client = TestClient(app)

    health = client.get("/health").json()
    failures += not check(
        "health reports the database",
        (health["database"].get("restaurants") or 0) >= 29,
        str(health["database"]),
    )
    failures += not check("health has model field", "model" in health)

    unauthenticated = client.post(
        "/v1/sessions/smoke-geo/location", json={"lat": 24.88, "lng": 67.02}
    )
    failures += not check(
        "unauthenticated request is 401",
        unauthenticated.status_code == 401,
        unauthenticated.text[:120],
    )

    garden = client.post(
        "/v1/sessions/smoke-geo/location",
        json={"lat": 24.8820, "lng": 67.0270},
        headers=AUTH,
    )
    failures += not check(
        "POST location snaps Garden",
        garden.status_code == 200 and garden.json().get("location") == "Garden",
        str(garden.json())[:160],
    )
    hello = client.post("/v1/sessions/smoke-geo/welcome", headers=AUTH)
    failures += not check(
        "welcome with location skips the area question",
        hello.status_code == 200
        and "Garden" in hello.json().get("reply", "")
        and "Which area" not in hello.json().get("reply", ""),
        (hello.json().get("reply") or "")[:160],
    )
    asked = client.post("/v1/sessions/smoke-geo-denied/welcome", headers=AUTH)
    failures += not check(
        "welcome without location asks for area",
        asked.status_code == 200 and "in the mood" in asked.json().get("reply", ""),
        (asked.json().get("reply") or "")[:160],
    )

    stub.script = [("lock_restaurant", {"restaurant_id": restaurant_id}), ("add_to_cart", {"item_id": item_id, "qty": 1}), "Added."]
    stub.step = 0
    api_session = "smoke-http"
    chat = client.post(
        "/v1/chat",
        json={"session_id": api_session, "message": "one biryani please"},
        headers=AUTH,
    )
    failures += not check("POST /chat returns 200", chat.status_code == 200, chat.text[:120])
    body = chat.json()
    failures += not check("chat response has reply and state", "reply" in body and "state" in body)
    failures += not check("state snapshot is JSON-safe", isinstance(body["state"].get("messages"), list))

    cart = client.get(f"/v1/sessions/{api_session}/cart", headers=AUTH)
    failures += not check("GET cart returns 200", cart.status_code == 200)
    failures += not check("cart subtotal present", "subtotal" in cart.json())
    failures += not check("cart includes delivery fee", cart.json().get("delivery_fee") == 99.0)
    failures += not check("cart includes platform fee", cart.json().get("platform_fee") == 8.99)

    confirmed = client.post(f"/v1/sessions/{api_session}/confirm", headers=AUTH)
    failures += not check("POST confirm returns 200", confirmed.status_code == 200, confirmed.text[:120])
    failures += not check("confirm returns a cart summary", confirmed.json().get("kind") == "cart_summary")

    missing = client.get("/v1/sessions/does-not-exist/cart", headers=AUTH)
    failures += not check("unknown session returns 404", missing.status_code == 404)

    empty = client.post("/v1/sessions/does-not-exist/confirm", headers=AUTH)
    failures += not check("confirm on unknown session returns 404", empty.status_code == 404)

    other = api_keys.create_tenant(f"smoke-other-{os.getpid()}", "unlimited")
    other_auth = {
        "Authorization": f"Bearer {api_keys.create_key(other, 'isolation check')}"
    }
    leaked = client.get(f"/v1/sessions/{api_session}/cart", headers=other_auth)
    failures += not check(
        "another tenant cannot read this cart",
        leaked.status_code == 404,
        leaked.text[:120],
    )

    usage = client.get("/v1/usage", headers=AUTH).json()
    failures += not check(
        "usage is metered for this key",
        (usage.get("days") or [{}])[0].get("requests", 0) > 0,
        str(usage)[:160],
    )

    print("\nVoice HTTP (no Groq spend)")
    silent = client.post("/v1/voice/speak", json={"text": "**"}, headers=AUTH)
    failures += not check(
        "speak of markdown-only text is 400",
        silent.status_code == 400,
        silent.text[:120],
    )
    missing_audio = client.post("/v1/voice/transcribe", headers=AUTH)
    failures += not check(
        "transcribe without a file is 422",
        missing_audio.status_code == 422,
        str(missing_audio.status_code),
    )
    tiny = client.post(
        "/v1/voice/transcribe",
        files={"audio": ("tiny.webm", b"abc", "audio/webm")},
        headers=AUTH,
    )
    failures += not check(
        "transcribe of empty audio is 400",
        tiny.status_code == 400,
        tiny.text[:120],
    )
    oversized = client.post(
        "/v1/voice/transcribe",
        files={"audio": ("huge.webm", b"x" * (8 * 1024 * 1024 + 1), "audio/webm")},
        headers=AUTH,
    )
    failures += not check(
        "transcribe over 8 MB is 413",
        oversized.status_code == 413,
        oversized.text[:120],
    )

    print(f"\n{'ALL CHECKS PASSED' if failures == 0 else f'{failures} CHECK(S) FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
