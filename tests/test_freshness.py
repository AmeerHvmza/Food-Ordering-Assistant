"""Hybrid V2 freshness checks — stubbed network only."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from agent.freshness import (
    OpenCheck,
    accepted_cart,
    check_open,
    centroid_for_area,
    diff_cart_against_menu,
    extract_vendor_code,
    interpret_vendor_meta,
    normalize_item_name,
    pin_for,
    reset_circuit_breaker,
)
from agent.tools import build_order_summary, prepare_confirm


def _menu(*items: dict) -> list[dict]:
    return [{"category_name": "Mains", "items": list(items)}]


class PinAndCodeTests(unittest.TestCase):
    def test_vendor_code_from_url(self) -> None:
        self.assertEqual(
            extract_vendor_code(
                "https://foodpanda.pk/restaurant/pv2x/rehmat-e-shereen-garden"
            ),
            "pv2x",
        )
        self.assertIsNone(extract_vendor_code("https://example.com/x"))

    def test_jauhar_centroid_is_not_saddar(self) -> None:
        pin = centroid_for_area("Jauhar")
        self.assertIsNotNone(pin)
        saddar = centroid_for_area("Saddar")
        self.assertNotEqual(pin, saddar)

    def test_unknown_area_does_not_fall_back_to_saddar(self) -> None:
        self.assertIsNone(centroid_for_area("Islamabad Blue Area"))
        self.assertIsNone(pin_for({"location": "Islamabad Blue Area"}, {"url": "x"}))

    def test_delivery_areas_fallback_when_session_location_missing(self) -> None:
        pin = pin_for(
            {"location": None},
            {"delivery_areas": "Gulistan-e-Jauhar"},
        )
        self.assertEqual(pin, centroid_for_area("Gulistan-e-Jauhar"))


class OpenCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_circuit_breaker()

    def test_temporary_closed_is_closed(self) -> None:
        check = interpret_vendor_meta(
            {"is_temporary_closed": True, "is_delivery_available": True},
            200,
        )
        self.assertEqual(check.status, "closed")

    def test_not_delivering_is_closed(self) -> None:
        check = interpret_vendor_meta(
            {"is_temporary_closed": False, "is_delivery_available": False},
            200,
        )
        self.assertEqual(check.status, "closed")

    def test_delivering_is_open(self) -> None:
        check = interpret_vendor_meta(
            {"is_temporary_closed": False, "is_delivery_available": True},
            200,
        )
        self.assertEqual(check.status, "open")

    def test_none_is_inconclusive_not_closed(self) -> None:
        check = interpret_vendor_meta(None, 500)
        self.assertEqual(check.status, "inconclusive")

    def test_missing_fields_on_200_are_inconclusive(self) -> None:
        check = interpret_vendor_meta({"name": "X"}, 200)
        self.assertEqual(check.status, "inconclusive")

    def test_breaker_trips_after_three_403s(self) -> None:
        restaurant = {
            "url": "https://foodpanda.pk/restaurant/aaaa/x",
            "name": "X",
        }
        state = {"location": "Saddar"}
        with patch("agent.freshness.live_vendor_meta", return_value=None), patch(
            "agent.freshness.last_http_status", return_value=403
        ):
            for _ in range(3):
                check_open(restaurant, state)
            skipped = check_open(restaurant, state)
        self.assertEqual(skipped.status, "inconclusive")
        self.assertIn("skipped", skipped.reason)


class CartDiffTests(unittest.TestCase):
    def test_name_normalize_folds_punctuation(self) -> None:
        self.assertEqual(
            normalize_item_name("Chicken-Cheese  Paratha"),
            normalize_item_name("chicken cheese paratha"),
        )

    def test_missing_item_is_unavailable(self) -> None:
        cart = [{"item_id": 1, "name": "Biryani", "price": 500, "qty": 1}]
        check = diff_cart_against_menu(cart, _menu({"name": "Karahi", "price": "600"}))
        self.assertEqual(check.status, "changes")
        self.assertEqual(check.unavailable[0].name, "Biryani")

    def test_sold_out_flag_is_unavailable(self) -> None:
        cart = [{"item_id": 1, "name": "Biryani", "price": 500, "qty": 1}]
        check = diff_cart_against_menu(
            cart,
            _menu({"name": "Biryani", "price": "500", "is_sold_out": True}),
        )
        self.assertEqual(check.status, "changes")
        self.assertEqual(check.unavailable[0].why, "sold out right now")

    def test_price_up_is_reported(self) -> None:
        cart = [{"item_id": 1, "name": "Biryani", "price": 500, "qty": 2}]
        check = diff_cart_against_menu(
            cart, _menu({"name": "Biryani", "price": "520"})
        )
        self.assertEqual(check.status, "changes")
        change = check.price_changes[0]
        self.assertEqual(change.old_price, 500)
        self.assertEqual(change.new_price, 520)
        self.assertEqual(change.line_delta, 40)

    def test_sub_rupee_delta_ignored(self) -> None:
        cart = [{"item_id": 1, "name": "Biryani", "price": 500.4, "qty": 1}]
        check = diff_cart_against_menu(
            cart, _menu({"name": "Biryani", "price": "500.9"})
        )
        self.assertEqual(check.status, "ok")

    def test_duplicate_live_names_do_not_invent_a_change(self) -> None:
        cart = [{"item_id": 1, "name": "Chai", "price": 80, "qty": 1}]
        check = diff_cart_against_menu(
            cart,
            _menu(
                {"name": "Chai", "price": "80"},
                {"name": "Chai", "price": "120"},
            ),
        )
        self.assertEqual(check.status, "ok")

    def test_empty_live_menu_is_inconclusive(self) -> None:
        cart = [{"item_id": 1, "name": "Biryani", "price": 500, "qty": 1}]
        check = diff_cart_against_menu(cart, [])
        self.assertEqual(check.status, "inconclusive")

    def test_accept_drops_unavailable_and_applies_live_price(self) -> None:
        state = {
            "cart": [
                {"item_id": 1, "name": "Biryani", "price": 500, "qty": 1},
                {"item_id": 2, "name": "Raita", "price": 50, "qty": 1},
            ]
        }
        check = diff_cart_against_menu(
            state["cart"],
            _menu({"name": "Biryani", "price": "550"}),
        )
        remaining = accepted_cart(state, check)
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["price"], 550)


class ConfirmPrepareTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_circuit_breaker()

    def test_prepare_blocks_on_changes_until_accepted(self) -> None:
        state = {
            "restaurant_id": 16,
            "restaurant_name": "Demo",
            "location": "Saddar",
            "cart": [{"item_id": 1, "name": "Biryani", "price": 500, "qty": 1}],
        }
        restaurant = {
            "url": "https://foodpanda.pk/restaurant/aaaa/x",
            "name": "Demo",
        }
        live = _menu({"name": "Biryani", "price": "600"})
        with patch("agent.tools._restaurant_for_lock", return_value=restaurant), patch(
            "agent.freshness.live_menu", return_value=live
        ), patch("agent.freshness.last_http_status", return_value=200):
            blocked, summary, cart = prepare_confirm(state, accept_changes=False)
            self.assertIsNotNone(blocked)
            self.assertIsNone(summary)
            self.assertIn("PRICE", blocked or "")
            blocked2, summary2, cart2 = prepare_confirm(state, accept_changes=True)
        self.assertIsNone(blocked2)
        self.assertIsNotNone(summary2)
        self.assertEqual(summary2["freshness"]["kind"], "accepted_changes")
        self.assertEqual(cart2[0]["price"], 600)
        self.assertEqual(summary2["items"][0]["price"], 600)

    def test_inconclusive_still_builds_summary(self) -> None:
        state = {
            "restaurant_id": 16,
            "restaurant_name": "Demo",
            "location": "Saddar",
            "cart": [{"item_id": 1, "name": "Biryani", "price": 500, "qty": 1}],
        }
        restaurant = {
            "url": "https://foodpanda.pk/restaurant/aaaa/x",
            "name": "Demo",
        }
        with patch("agent.tools._restaurant_for_lock", return_value=restaurant), patch(
            "agent.freshness.live_menu", return_value=None
        ), patch("agent.freshness.last_http_status", return_value=403):
            blocked, summary, _cart = prepare_confirm(state)
        self.assertIsNone(blocked)
        self.assertEqual(summary["freshness"]["kind"], "unverified")
        self.assertIn("stale", summary["disclaimer"].lower())

    def test_tool_and_builder_share_freshness_shape(self) -> None:
        state = {
            "restaurant_id": 1,
            "restaurant_name": "X",
            "cart": [{"item_id": 1, "name": "A", "price": 10, "qty": 1}],
        }
        a = build_order_summary(
            state,
            freshness={
                "checked": True,
                "kind": "ok",
                "reason": None,
                "at": "t",
            },
        )
        b = build_order_summary(
            state,
            freshness={
                "checked": True,
                "kind": "ok",
                "reason": None,
                "at": "t",
            },
        )
        self.assertEqual(a["freshness"], b["freshness"])
        self.assertEqual(a["total"], b["total"])


class LockToolTests(unittest.TestCase):
    def test_closed_does_not_lock(self) -> None:
        from langchain_core.messages import AIMessage
        from langgraph.graph import END, StateGraph
        from langgraph.prebuilt import ToolNode

        from agent.state import OrderState
        from agent.tools import TOOLS

        restaurant = {
            "id": 16,
            "name": "Closed Place",
            "url": "https://foodpanda.pk/restaurant/aaaa/x",
        }
        builder = StateGraph(OrderState)
        builder.add_node("tools", ToolNode(TOOLS))
        builder.set_entry_point("tools")
        builder.add_edge("tools", END)
        graph = builder.compile()
        seeded = {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "lock_restaurant",
                            "args": {"restaurant_id": 16},
                            "id": "c1",
                            "type": "tool_call",
                        }
                    ],
                )
            ],
            "location": "Saddar",
            "restaurant_id": None,
            "cart": [],
        }
        with patch("db.queries.get_restaurant", return_value=restaurant), patch(
            "agent.tools.check_open",
            return_value=OpenCheck("closed", "temporarily closed"),
        ):
            result = graph.invoke(seeded)
        self.assertIsNone(result.get("restaurant_id"))
        texts = [
            str(getattr(m, "content", ""))
            for m in result.get("messages") or []
            if getattr(m, "type", "") == "tool"
        ]
        self.assertTrue(any("not accepting orders" in t for t in texts))


if __name__ == "__main__":
    unittest.main()
