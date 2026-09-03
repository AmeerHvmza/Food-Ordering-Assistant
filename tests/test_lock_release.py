"""Unlock / reconsideration must actually clear restaurant_id.

Stub-model only — no live LLM. ToolNode tests cover the lock gate; scripted
graph turns cover the two conversations in LOCK_RELEASE_PLAN.md.
"""

from __future__ import annotations

import unittest
import uuid
from unittest.mock import patch

from langchain_core.messages import AIMessage
from langgraph.graph import END, StateGraph
from langgraph.prebuilt import ToolNode

from agent.freshness import OpenCheck
from agent.state import OrderState, serialize_state
from agent.tools import NO_LOCK_MESSAGE, TOOLS
from db import queries

_open_patch = None


def setUpModule() -> None:
    global _open_patch
    _open_patch = patch(
        "agent.tools.check_open", return_value=OpenCheck("open")
    )
    _open_patch.start()


def tearDownModule() -> None:
    if _open_patch is not None:
        _open_patch.stop()


def _tool_step(state: dict, calls: list[dict]) -> dict:
    builder = StateGraph(OrderState)
    builder.add_node("tools", ToolNode(TOOLS))
    builder.set_entry_point("tools")
    builder.add_edge("tools", END)
    graph = builder.compile()
    seeded = dict(state)
    seeded["messages"] = [
        *(state.get("messages") or []),
        AIMessage(content="", tool_calls=calls),
    ]
    return graph.invoke(seeded)


def _call(name: str, args: dict, call_id: str) -> dict:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def _tool_texts(state: dict) -> list[str]:
    return [
        str(getattr(m, "content", ""))
        for m in state.get("messages") or []
        if getattr(m, "type", "") == "tool"
    ]


_SEARCH_AREAS = (
    "Saddar",
    "Garden",
    "Gulshan-e-Iqbal",
    "Gulistan-e-Jauhar",
    "Jauhar",
)


def _fixture() -> tuple[int, str, int, str, int, str]:
    """Restaurant + menu item + a second id + an area search_restaurants hits."""
    with queries.session() as conn:
        location = None
        rows: list[dict] = []
        for area in _SEARCH_AREAS:
            rows = queries.search_restaurants(
                conn, craving="biryani", location=area, limit=8
            )
            if rows:
                location = area
                break
        if not rows:
            raise unittest.SkipTest("snapshot has no biryani search hits")
        first = rows[0]
        other = next((r for r in rows if r["id"] != first["id"]), None)
        if other is None:
            other_row = conn.execute(
                "SELECT id, name FROM restaurants WHERE id != ? LIMIT 1",
                (first["id"],),
            ).fetchone()
            if other_row is None:
                raise unittest.SkipTest("need two restaurants in the snapshot")
            other = dict(other_row)
        items = queries.search_menu(
            conn, restaurant_id=first["id"], query="biryani", limit=1
        )
        if not items:
            items = queries.search_menu(conn, restaurant_id=first["id"], limit=1)
        if not items:
            raise unittest.SkipTest("locked restaurant has no menu items")
        return (
            int(first["id"]),
            str(first["name"]),
            int(items[0]["item_id"]),
            str(items[0]["name"]),
            int(other["id"]),
            str(location),
        )


def _locked_state(*, with_cart: bool = False) -> dict:
    restaurant_id, name, item_id, item_name, _other, location = _fixture()
    cart: list[dict] = []
    if with_cart:
        cart = [
            {"item_id": item_id, "name": item_name, "qty": 1, "price": 500.0}
        ]
    return {
        "messages": [],
        "location": location,
        "craving": "biryani",
        "budget": 3000.0,
        "restaurant_id": restaurant_id,
        "restaurant_name": name,
        "cart": cart,
    }


class UnlockToolTests(unittest.TestCase):
    def test_empty_cart_unlocks_without_confirm(self) -> None:
        state = _locked_state(with_cart=False)
        result = _tool_step(state, [_call("unlock_restaurant", {}, "c1")])
        public = serialize_state(result)
        self.assertIsNone(public["restaurant_id"])
        self.assertIsNone(public["restaurant_name"])
        self.assertFalse(public["cart"])
        self.assertTrue(
            any("Unlocked" in text for text in _tool_texts(result))
        )

    def test_cart_items_refuse_without_confirm(self) -> None:
        state = _locked_state(with_cart=True)
        locked_id = state["restaurant_id"]
        result = _tool_step(state, [_call("unlock_restaurant", {}, "c1")])
        self.assertEqual(result.get("restaurant_id"), locked_id)
        self.assertEqual(len(result.get("cart") or []), 1)
        self.assertTrue(
            any("confirm_switch=true" in text for text in _tool_texts(result))
        )

    def test_cart_items_unlock_with_confirm_clears_cart(self) -> None:
        state = _locked_state(with_cart=True)
        result = _tool_step(
            state,
            [_call("unlock_restaurant", {"confirm_switch": True}, "c1")],
        )
        public = serialize_state(result)
        self.assertIsNone(public["restaurant_id"])
        self.assertIsNone(public["restaurant_name"])
        self.assertFalse(public["cart"])

    def test_unlock_when_already_unlocked_is_noop(self) -> None:
        state = {
            "messages": [],
            "location": "Saddar",
            "cart": [],
        }
        result = _tool_step(state, [_call("unlock_restaurant", {}, "c1")])
        self.assertIsNone(result.get("restaurant_id"))
        self.assertTrue(
            any("No restaurant is locked" in text for text in _tool_texts(result))
        )


class SearchWhileLockedTests(unittest.TestCase):
    def test_empty_cart_search_unlocks_and_menu_is_ungated(self) -> None:
        state = _locked_state(with_cart=False)
        searched = _tool_step(
            state, [_call("search_restaurants", {"craving": "biryani"}, "c1")]
        )
        public = serialize_state(searched)
        self.assertIsNone(public["restaurant_id"])
        self.assertIsNone(public["restaurant_name"])
        self.assertIsNotNone(public["showcase"])
        self.assertEqual(public["showcase"]["kind"], "restaurants")
        self.assertTrue(public["showcase"]["items"])
        self.assertTrue(
            any("Unlocked" in text for text in _tool_texts(searched))
        )

        menu = _tool_step(
            searched, [_call("search_menu", {"query": "biryani"}, "c2")]
        )
        self.assertTrue(
            any(NO_LOCK_MESSAGE.split(".")[0] in text for text in _tool_texts(menu))
        )

    def test_cart_items_search_refuses_and_keeps_lock(self) -> None:
        state = _locked_state(with_cart=True)
        locked_id = state["restaurant_id"]
        result = _tool_step(
            state, [_call("search_restaurants", {"craving": "biryani"}, "c1")]
        )
        self.assertEqual(result.get("restaurant_id"), locked_id)
        self.assertEqual(len(result.get("cart") or []), 1)
        public = serialize_state(result)
        self.assertIsNone(public["showcase"])
        self.assertTrue(
            any("unlock_restaurant" in text for text in _tool_texts(result))
        )
        self.assertTrue(
            any("confirm_switch=true" in text for text in _tool_texts(result))
        )


class EmptyCartSwitchTests(unittest.TestCase):
    def test_lock_other_restaurant_without_confirm_when_cart_empty(self) -> None:
        restaurant_id, name, _item_id, _item_name, other_id, location = _fixture()
        state = {
            "messages": [],
            "location": location,
            "restaurant_id": restaurant_id,
            "restaurant_name": name,
            "cart": [],
        }
        result = _tool_step(
            state, [_call("lock_restaurant", {"restaurant_id": other_id}, "c1")]
        )
        self.assertEqual(result.get("restaurant_id"), other_id)
        self.assertNotEqual(result.get("restaurant_name"), name)


class ScriptedModel:
    """Replays tool calls / replies. Same shape as scripts/smoke_test.py."""

    def __init__(self, script: list) -> None:
        self.script = script
        self.step = 0

    def bind_tools(self, tools):  # noqa: ANN001
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


class ScriptedConversationTests(unittest.TestCase):
    def setUp(self) -> None:
        import agent.graph as graph_module

        self.graph_module = graph_module
        self._real_get_llm = graph_module.get_llm

    def tearDown(self) -> None:
        self.graph_module.get_llm = self._real_get_llm
        self.graph_module.get_graph.cache_clear()

    def _drive(self, script: list, user_messages: list[str]) -> list[dict]:
        stub = ScriptedModel(script)
        self.graph_module.get_llm = lambda: stub
        self.graph_module.get_graph.cache_clear()
        from api import sessions

        session_id = f"lock-release-{uuid.uuid4().hex[:12]}"
        return [sessions.send_message(session_id, msg) for msg in user_messages]

    def test_empty_cart_other_options_unlocks_without_asking(self) -> None:
        restaurant_id, name, _item_id, _item_name, _other, location = _fixture()
        script = [
            (
                "remember_preferences",
                {
                    "craving": "biryani",
                    "location": location,
                    "budget": 3000.0,
                },
            ),
            ("search_restaurants", {"craving": "biryani", "location": location}),
            ("lock_restaurant", {"restaurant_id": restaurant_id}),
            f"Locked in {name}. Want the menu?",
            ("search_restaurants", {"craving": "biryani"}),
            "Here are some other places.",
        ]
        locked, unlocked = self._drive(
            script,
            [
                f"biryani in {location}, about 3000",
                "actually show me some other options",
            ],
        )
        self.assertEqual(locked.get("restaurant_id"), restaurant_id)
        self.assertFalse(locked.get("cart"))
        public = serialize_state(unlocked)
        self.assertIsNone(public["restaurant_id"])
        self.assertIsNone(public["restaurant_name"])
        self.assertIsNotNone(public["showcase"])
        self.assertEqual(public["showcase"]["kind"], "restaurants")
        self.assertTrue(
            any("Unlocked" in text for text in _tool_texts(unlocked))
        )
        self.assertFalse(
            any("Ask the user to confirm" in text for text in _tool_texts(unlocked))
        )

    def test_cart_items_other_options_asks_then_unlocks(self) -> None:
        restaurant_id, name, item_id, item_name, _other, location = _fixture()
        script = [
            (
                "remember_preferences",
                {
                    "craving": "biryani",
                    "location": location,
                    "budget": 3000.0,
                },
            ),
            ("search_restaurants", {"craving": "biryani", "location": location}),
            ("lock_restaurant", {"restaurant_id": restaurant_id}),
            ("search_menu", {"query": "biryani"}),
            ("add_to_cart", {"item_id": item_id, "qty": 1}),
            f"Locked in {name} with {item_name}.",
            ("unlock_restaurant", {}),
            "Switching empties your cart. Want me to go ahead?",
            ("unlock_restaurant", {"confirm_switch": True}),
            ("search_restaurants", {"craving": "biryani"}),
            "Here are some other places.",
        ]
        built, asked, released = self._drive(
            script,
            [
                f"biryani in {location}, about 3000, add one",
                "actually show me some other options",
                "yes, go ahead",
            ],
        )
        self.assertEqual(built.get("restaurant_id"), restaurant_id)
        self.assertTrue(built.get("cart"))
        self.assertEqual(asked.get("restaurant_id"), restaurant_id)
        self.assertTrue(asked.get("cart"))
        self.assertIn("empties your cart", (asked.get("messages") or [])[-1].content)
        self.assertTrue(
            any("Ask the user to confirm" in text for text in _tool_texts(asked))
        )

        public = serialize_state(released)
        self.assertIsNone(public["restaurant_id"])
        self.assertIsNone(public["restaurant_name"])
        self.assertFalse(public["cart"])
        self.assertIsNotNone(public["showcase"])
        self.assertEqual(public["showcase"]["kind"], "restaurants")
        menu = _tool_step(
            released, [_call("search_menu", {"query": "biryani"}, "after")]
        )
        self.assertTrue(
            any(NO_LOCK_MESSAGE.split(".")[0] in text for text in _tool_texts(menu))
        )


if __name__ == "__main__":
    unittest.main()
