"""Parallel tool calls in one graph step must merge, not crash.

Real failure: "2 parathas and 3 cup chai, do it for me" made the model emit two
search_menu calls in a single step. Both wrote OrderState['showcase'], and an
unannotated key rejects two writes per step:

    InvalidUpdateError: At key 'showcase': Can receive only one value per step.

Two parallel add_to_cart calls are worse than a crash: each one builds the full
cart from the same pre-step state, so a last-write-wins reducer silently drops
one of the two items.
"""

from __future__ import annotations

import unittest

from langchain_core.messages import AIMessage
from langgraph.graph import END, StateGraph
from langgraph.prebuilt import ToolNode

from agent.state import OrderState, serialize_state
from agent.tools import TOOLS
from db import queries


def _tool_step(state: dict, calls: list[dict]) -> dict:
    """Run one tools-node step with `calls` executed in parallel."""
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


def _locked_state() -> dict:
    """A session locked to a restaurant that has both parathas and chai."""
    with queries.session() as conn:
        row = conn.execute(
            """
            SELECT r.id, r.name
            FROM restaurants r
            WHERE EXISTS (
                SELECT 1 FROM menu_categories mc
                JOIN menu_items mi ON mi.category_id = mc.id
                WHERE mc.restaurant_id = r.id
                  AND LOWER(mi.name) LIKE '%paratha%'
            ) AND EXISTS (
                SELECT 1 FROM menu_categories mc
                JOIN menu_items mi ON mi.category_id = mc.id
                WHERE mc.restaurant_id = r.id
                  AND LOWER(mi.name) LIKE '%chai%'
            )
            LIMIT 1
            """
        ).fetchone()
    if row is None:
        raise unittest.SkipTest("snapshot has no restaurant with paratha + chai")
    return {
        "messages": [],
        "location": "Jauhar",
        "restaurant_id": row["id"],
        "restaurant_name": row["name"],
        "cart": [],
    }


def _menu_item_ids(restaurant_id: int, keyword: str, limit: int = 1) -> list[int]:
    with queries.session() as conn:
        rows = conn.execute(
            """
            SELECT mi.id
            FROM menu_items mi
            JOIN menu_categories mc ON mc.id = mi.category_id
            WHERE mc.restaurant_id = ?
              AND LOWER(mi.name) LIKE ?
              AND TRIM(COALESCE(mi.price, '')) != ''
            ORDER BY mi.id
            LIMIT ?
            """,
            (restaurant_id, f"%{keyword}%", limit),
        ).fetchall()
    return [row["id"] for row in rows]


class ParallelShowcaseTests(unittest.TestCase):
    def test_two_menu_searches_in_one_step(self) -> None:
        state = _locked_state()
        result = _tool_step(
            state,
            [
                _call("search_menu", {"query": "paratha"}, "c1"),
                _call("search_menu", {"query": "chai"}, "c2"),
            ],
        )
        showcase = result.get("showcase")
        self.assertIsNotNone(showcase, "parallel searches produced no showcase")
        names = " ".join(
            (item.get("name") or "").lower()
            for item in showcase.get("items") or []
        )
        # Both cravings must survive the merge, not just whichever landed last.
        self.assertIn("paratha", names)
        self.assertIn("chai", names)

    def test_empty_result_does_not_wipe_a_real_showcase(self) -> None:
        state = _locked_state()
        result = _tool_step(
            state,
            [
                _call("search_menu", {"query": "paratha"}, "c1"),
                _call(
                    "search_menu",
                    {"query": "zzzznosuchdish", "category": "zzzznosuchcat"},
                    "c2",
                ),
            ],
        )
        showcase = result.get("showcase")
        self.assertIsNotNone(showcase)
        self.assertTrue(showcase.get("items"))


class ShowcaseStepTests(unittest.TestCase):
    """Merging must be per tool round, not for all time."""

    def test_a_later_round_replaces_stale_cards(self) -> None:
        state = _locked_state()
        first = _tool_step(state, [_call("search_menu", {"query": "paratha"}, "c1")])
        self.assertTrue(first["showcase"]["items"])

        # Feed the first round's output back in, as the next turn would.
        second = _tool_step(first, [_call("search_menu", {"query": "chai"}, "c2")])
        names = " ".join(
            (item.get("name") or "").lower()
            for item in second["showcase"]["items"]
        )
        self.assertIn("chai", names)
        self.assertNotIn("paratha", names)

    def test_switching_restaurant_clears_cards(self) -> None:
        state = _locked_state()
        searched = _tool_step(
            state, [_call("search_menu", {"query": "paratha"}, "c1")]
        )
        with queries.session() as conn:
            other = conn.execute(
                "SELECT id FROM restaurants WHERE id != ? LIMIT 1",
                (state["restaurant_id"],),
            ).fetchone()["id"]
        locked = _tool_step(
            searched,
            [
                _call(
                    "lock_restaurant",
                    {"restaurant_id": other, "confirm_switch": True},
                    "c2",
                )
            ],
        )
        self.assertFalse(locked["showcase"]["items"])
        self.assertIsNone(serialize_state(locked)["showcase"])

    def test_internal_step_stamp_is_not_public(self) -> None:
        state = _locked_state()
        result = _tool_step(state, [_call("search_menu", {"query": "chai"}, "c1")])
        public = serialize_state(result)["showcase"]
        self.assertIsNotNone(public)
        self.assertNotIn("step", public)
        self.assertTrue(public["items"])


class ParallelCartTests(unittest.TestCase):
    def test_two_adds_in_one_step_keep_both_items(self) -> None:
        state = _locked_state()
        paratha = _menu_item_ids(state["restaurant_id"], "paratha")
        chai = _menu_item_ids(state["restaurant_id"], "chai")
        self.assertTrue(paratha and chai)

        result = _tool_step(
            state,
            [
                _call("add_to_cart", {"item_id": paratha[0], "qty": 2}, "c1"),
                _call("add_to_cart", {"item_id": chai[0], "qty": 3}, "c2"),
            ],
        )
        cart = {line["item_id"]: line["qty"] for line in result.get("cart") or []}
        self.assertEqual(cart.get(paratha[0]), 2, f"paratha lost: {cart}")
        self.assertEqual(cart.get(chai[0]), 3, f"chai lost: {cart}")

    def test_parallel_adds_onto_an_existing_cart(self) -> None:
        state = _locked_state()
        paratha = _menu_item_ids(state["restaurant_id"], "paratha")
        chai = _menu_item_ids(state["restaurant_id"], "chai")
        state["cart"] = [
            {"item_id": -1, "name": "Pre-existing", "price": 100.0, "qty": 1}
        ]
        result = _tool_step(
            state,
            [
                _call("add_to_cart", {"item_id": paratha[0], "qty": 2}, "c1"),
                _call("add_to_cart", {"item_id": chai[0], "qty": 3}, "c2"),
            ],
        )
        cart = {line["item_id"]: line["qty"] for line in result.get("cart") or []}
        self.assertEqual(cart.get(-1), 1, f"existing line lost: {cart}")
        self.assertEqual(cart.get(paratha[0]), 2, f"paratha lost: {cart}")
        self.assertEqual(cart.get(chai[0]), 3, f"chai lost: {cart}")

    def test_same_item_added_twice_in_one_step_sums(self) -> None:
        state = _locked_state()
        paratha = _menu_item_ids(state["restaurant_id"], "paratha")
        result = _tool_step(
            state,
            [
                _call("add_to_cart", {"item_id": paratha[0], "qty": 2}, "c1"),
                _call("add_to_cart", {"item_id": paratha[0], "qty": 3}, "c2"),
            ],
        )
        cart = {line["item_id"]: line["qty"] for line in result.get("cart") or []}
        self.assertEqual(cart.get(paratha[0]), 5, f"quantities not summed: {cart}")


if __name__ == "__main__":
    unittest.main()
