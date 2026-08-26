"""Add Chicken Fajita Pizza from Pizza Yumm's, view cart, confirm price.

Uses the scripted stub model so this does not spend a Groq call. Asserts the
DB now yields a non-zero PKR unit price and the reply has no ₹ symbol.

Run: python scripts/test_deal_cart.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langchain_core.messages import AIMessage  # noqa: E402

from db import queries  # noqa: E402


class ScriptedModel:
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


def main() -> int:
    with queries.session() as conn:
        rest = conn.execute(
            """
            SELECT id, name FROM restaurants
            WHERE name LIKE '%Pizza Yumm%' OR id = 69
            ORDER BY id
            LIMIT 1
            """
        ).fetchone()
        if rest is None:
            print("Pizza Yumm's not in the snapshot.")
            return 1
        restaurant_id = int(rest["id"])
        restaurant_name = rest["name"]
        item = conn.execute(
            """
            SELECT mi.id AS item_id, mi.name, mi.price,
                   CAST(REPLACE(REPLACE(COALESCE(mi.price, ''), 'PKR', ''), ',', '') AS REAL)
                   AS price_value
            FROM menu_items mi
            JOIN menu_categories mc ON mc.id = mi.category_id
            WHERE mc.restaurant_id = ?
              AND mi.name = 'Chicken Fajita Pizza'
            LIMIT 1
            """,
            (restaurant_id,),
        ).fetchone()
        items = [dict(item)] if item else []
    if not items:
        print(f"No Chicken Fajita Pizza on restaurant {restaurant_id}.")
        return 1
    item = items[0]
    item_id = item["item_id"]
    price_value = item.get("price_value")
    print(
        f"Fixture: {restaurant_name} (id={restaurant_id}) "
        f"item_id={item_id} price_value={price_value} raw={item.get('price')}"
    )
    if not isinstance(price_value, (int, float)) or price_value <= 0:
        print("FAIL: expected a positive numeric price_value from the DB.")
        return 1

    script = [
        ("remember_preferences", {"location": "Gulistan-e-Jauhar", "craving": "pizza"}),
        ("lock_restaurant", {"restaurant_id": restaurant_id}),
        ("search_menu", {"query": "Chicken Fajita Pizza"}),
        ("add_to_cart", {"item_id": item_id, "qty": 1}),
        ("view_cart", {}),
        "Added Chicken Fajita Pizza for ₹ 1. Just kidding — PKR only.",
    ]

    import agent.graph as graph_module
    from agent.graph import get_graph

    stub = ScriptedModel(script)
    graph_module.get_llm = lambda: stub
    get_graph.cache_clear()

    from api import sessions
    from agent.currency import sanitize_currency

    session_id = "deal-price-cart"
    state = sessions.send_message(
        session_id,
        "I'm in Jauhar. Add Chicken Fajita Pizza from Pizza Yumm's and show the cart.",
    )
    cart = state.get("cart") or []
    ok = True

    def check(label: str, cond: bool, detail: str = "") -> None:
        nonlocal ok
        print(f"  [{'PASS' if cond else 'FAIL'}] {label}{f' - {detail}' if detail else ''}")
        ok = ok and cond

    check("restaurant locked", state.get("restaurant_id") == restaurant_id)
    check("cart has one line", len(cart) == 1)
    unit = (cart[0].get("price") if cart else None)
    check("cart unit price is a number", isinstance(unit, (int, float)), repr(unit))
    check("cart unit price is non-zero", isinstance(unit, (int, float)) and unit > 0, repr(unit))
    reply = sessions.latest_reply(state)
    check("reply has no rupee symbol", "₹" not in reply, reply[:160])
    check(
        "sanitizer would strip a stray rupee symbol",
        sanitize_currency("\u20b9399") == "Rs.399",
    )
    check("reply mentions PKR or Rs.", "PKR" in reply or "Rs." in reply or "Rs" in reply, reply[:200])

    print(f"\nReply:\n{reply}\n")
    print(f"Cart: {cart}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
