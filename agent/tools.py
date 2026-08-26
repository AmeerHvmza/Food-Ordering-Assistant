"""Tool nodes for the ordering agent.

Every factual claim the assistant makes about a restaurant, dish, price or
rating has to originate in one of these tools. Read-only tools return
formatted text; tools that change the session return a Command so LangGraph
applies the state update and the ToolMessage together.

Restaurant search and menu search are deliberately separate tools. A session
locks exactly one restaurant, and separate tools let the menu and cart tools
hard-fail before a lock exists instead of relying on the model to respect a
restaurant_id argument.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any

from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState
from langgraph.types import Command

from agent.state import (
    apply_cart_write,
    cart_item_count,
    empty_showcase,
    showcase_step,
)
from db import fees, geo, name_match, queries, ranking

MAX_RESTAURANT_RESULTS = 5
MAX_MENU_RESULTS = 10
CANDIDATE_POOL = 25

# Weighted rating orders restaurants by trustworthiness, but on this dataset
# the top scores sit within ~0.005 of each other, so using it as the only sort
# key lets a high-volume chain outrank a specialist that actually matches the
# craving. Relevance decides the tier; the weighted rating orders within it.
RELEVANCE_TIERS = {
    "cuisine_or_name": 0,
    "menu_item": 1,
    "unfiltered": 2,
    "unfiltered_no_hints": 3,
}

# Bakeries still match "spicy" via nimco blurbs or "desi" via ghee. If the
# craving is not sweet, push dessert-only vendors below savoury matches.
_DESSERT_MARKERS = ("cake", "bakery", "dessert", "sweet", "pastry", "mithai")
_SAVORY_CUISINE = (
    "pakistani", "biryani", "bbq", "chinese", "burger", "pizza", "wrap",
    "samosa", "paratha", "karahi", "nihari", "broast", "tikka", "fast food",
    "indian", "grill", "roll",
)
_SWEET_CRAVING = {
    "cake", "bakery", "dessert", "sweet", "pastry", "mithai", "chocolate",
    "ice", "donut", "biscuit",
}


def _dessert_only(row: dict[str, Any]) -> bool:
    blob = f"{row.get('cuisine') or ''} {row.get('name') or ''}".lower()
    if any(marker in blob for marker in _SAVORY_CUISINE):
        return False
    return any(marker in blob for marker in _DESSERT_MARKERS)


def _craving_is_sweet(craving: str | None) -> bool:
    return bool(set(queries._terms(craving)) & _SWEET_CRAVING)


def _covers_area(row: dict[str, Any], location: str) -> bool:
    """Does this row actually claim the user's area?

    Checks the recorded delivery area first, then the address, and accepts any
    known spelling of the area: Jauhar addresses are routinely typed "Johar".
    """
    blob = geo.normalize_area_text(
        f"{row.get('delivery_areas') or ''} {row.get('address') or ''} "
        f"{row.get('name') or ''}"
    )
    return any(term in blob for term in geo.area_search_terms(location))


def _promote_name_matches(
    ranked: list[dict[str, Any]],
    craving: str | None,
) -> list[dict[str, Any]]:
    """Float high combo-score name matches so they survive the top-N cut."""
    if not craving or not ranked:
        return ranked
    names = [r.get("name") or "" for r in ranked]
    eligible = name_match.eligible_indices(
        craving, names, name_match.SEARCH_PROMOTE
    )
    promoted = [
        ranked[i]
        for i in eligible
        if name_match.combo_score(craving, names[i])
        >= name_match.SEARCH_PROMOTE.min_combo
    ]
    if not promoted:
        return ranked
    promoted.sort(
        key=lambda row: name_match.combo_score(craving, row.get("name") or ""),
        reverse=True,
    )
    promoted_ids = {r["id"] for r in promoted}
    rest = [r for r in ranked if r["id"] not in promoted_ids]
    return promoted + rest


def order_restaurant_hits(
    rows: list[dict[str, Any]],
    craving: str | None,
) -> list[dict[str, Any]]:
    """Relevance first, then rating. Dessert-only shops lose on savoury cravings."""
    sweet = _craving_is_sweet(craving)
    ordered = list(rows)
    ordered.sort(
        key=lambda r: (
            0 if sweet or not _dessert_only(r) else 1,
            RELEVANCE_TIERS.get(r.get("match_source"), 9),
            -(r.get("weighted_rating") or 0.0),
        )
    )
    return ordered


NO_LOCK_MESSAGE = (
    "No restaurant is locked for this session yet. Use search_restaurants, "
    "let the user choose, then call lock_restaurant before touching the menu "
    "or the cart."
)

NO_LOCATION_MESSAGE = (
    "BLOCKED: location is unknown. Foodpanda only lists restaurants that "
    "deliver to the user's area. Ask which Karachi area they are in "
    "(Saddar, Garden, SMCHS, Tariq Road, Burns Road, etc.), call "
    "remember_preferences with location, then search_restaurants. Do not "
    "invent nearby restaurants."
)


def _restaurant_card(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "restaurant",
        "id": row["id"],
        "name": row.get("name"),
        "image_url": row.get("image_url"),
        "subtitle": row.get("cuisine"),
        "rating": row.get("rating"),
        "review_count": row.get("review_count"),
        "rating_confidence": row.get("rating_confidence"),
        "eta": row.get("delivery_time"),
        "price": row.get("min_item_price"),
        "address": row.get("address"),
        "badge": None,
    }


def _dish_card(row: dict[str, Any], *, is_deal: bool = False) -> dict[str, Any]:
    return {
        "kind": "dish",
        "id": row.get("item_id"),
        "name": row.get("name"),
        "image_url": row.get("image_url"),
        "subtitle": (row.get("description") or "")[:120] or row.get("category_name"),
        "price": row.get("price_value"),
        "category": row.get("category_name"),
        "badge": "Deal" if is_deal or queries.looks_like_deal(row.get("category_name") or "") else None,
    }


def _looks_like_deal(category_name: str) -> bool:
    return queries.looks_like_deal(category_name)


def _money(value: float | None) -> str:
    return f"PKR {value:,.0f}" if isinstance(value, (int, float)) else "price n/a"


def _rating_phrase(row: dict[str, Any]) -> str:
    """Always pair a rating with its review count.

    A bare rating is not a trustworthy quality signal: the snapshot contains
    5.0 ratings backed by 63 reviews next to 4.9s backed by 39,949.
    """
    rating = row.get("rating")
    reviews = row.get("review_count")
    if rating is None:
        return "no rating"
    if reviews is None:
        return f"{rating} (review count unknown)"
    phrase = f"{rating} from {int(reviews):,} reviews"
    if row.get("rating_confidence") == "low":
        phrase += " [LOW CONFIDENCE: too few reviews to trust the rating]"
    return phrase


def _tool_reply(content: str, tool_call_id: str, update: dict[str, Any]) -> Command:
    payload = dict(update)
    payload["messages"] = [ToolMessage(content=content, tool_call_id=tool_call_id)]
    return Command(update=payload)


@tool
def remember_preferences(
    state: Annotated[dict, InjectedState],
    tool_call_id: Annotated[str, InjectedToolCallId],
    location: str | None = None,
    party_size: int | None = None,
    craving: str | None = None,
    budget: float | None = None,
    deal_sensitive: bool | None = None,
) -> Command:
    """Save what the user told you about their order.

    Call this as details come up in conversation. Pass only the fields you
    actually learned; omitted fields keep their previous value. budget is the
    total order budget in PKR unless the user says otherwise.
    """
    update = {
        key: value
        for key, value in {
            "location": location,
            "party_size": party_size,
            "craving": craving,
            "budget": budget,
            "deal_sensitive": deal_sensitive,
        }.items()
        if value is not None
    }
    if not update:
        return _tool_reply("Nothing new to remember.", tool_call_id, {})
    saved = ", ".join(f"{k}={v}" for k, v in update.items())
    return _tool_reply(f"Noted: {saved}", tool_call_id, update)


@tool
def search_restaurants(
    craving: str,
    state: Annotated[dict, InjectedState],
    tool_call_id: Annotated[str, InjectedToolCallId],
    location: str | None = None,
    budget: float | None = None,
    min_reviews: int | None = None,
) -> Command:
    """Find restaurants matching a craving, area and budget.

    NEVER call this until you know the user's area. Do not call this if a
    restaurant is already locked unless the user asks to browse or switch.
    craving may name a cuisine or a dish. Results are ordered by relevance
    then review-weighted rating. Does not lock.
    """
    location = location or state.get("location")
    budget = budget if budget is not None else state.get("budget")
    if not location:
        return _tool_reply(NO_LOCATION_MESSAGE, tool_call_id, {})

    with queries.session() as conn:
        rows = queries.search_restaurants(
            conn,
            craving=craving,
            location=location,
            budget=budget,
            limit=CANDIDATE_POOL,
        )
        if not rows:
            return _tool_reply(
                f"No restaurants in the snapshot deliver to '{location}' "
                f"for '{craving}'. Ask the user for a nearby Karachi area "
                "(Saddar, Garden, SMCHS, Tariq Road…) rather than showing "
                "the other side of the city.",
                tool_call_id,
                {"showcase": empty_showcase(showcase_step(state))},
            )
        m, C = ranking.compute_m_and_c(queries.dataset_rows(conn))

    ranked = order_restaurant_hits(
        ranking.rank_restaurants(rows, m=m, C=C),
        craving,
    )
    ranked = _promote_name_matches(ranked, craving)

    if min_reviews is not None:
        trusted = [r for r in ranked if (r.get("review_count") or 0) >= min_reviews]
        if trusted:
            ranked = trusted

    top = ranked[:MAX_RESTAURANT_RESULTS]
    notes: list[str] = []
    if not any(_covers_area(row, location) for row in top):
        notes.append(
            f"Nothing here records '{location}' as its delivery area or "
            "address. Only show these if the area still looks nearby; "
            "otherwise say the snapshot has no coverage there."
        )
    if any(r.get("match_source") == "menu_item" for r in top):
        notes.append("Some matches come from dish names rather than the cuisine label.")

    lines = [f"Top {len(top)} matches near '{location}' for '{craving}':"]
    for row in top:
        lines.append(
            f"- id={row['id']} | {row['name']} | {row.get('cuisine') or 'cuisine n/a'}"
            f" | rating {_rating_phrase(row)}"
            f" | delivery {row.get('delivery_time') or 'n/a'}"
            f" | from {_money(row.get('min_item_price'))}"
            f" | {row.get('address') or 'address n/a'}"
            f" | image={row.get('image_url') or 'none'}"
        )
    if notes:
        lines.append("Notes: " + " ".join(notes))
    lines.append(
        "The chat UI will render these as photo cards. Keep your spoken "
        "reply to a short recommendation, not a pasted list."
    )
    showcase = {
        "kind": "restaurants",
        "title": f"Near {location}",
        "items": [_restaurant_card(row) for row in top],
        "step": showcase_step(state),
    }
    return _tool_reply("\n".join(lines), tool_call_id, {"showcase": showcase})


@tool
def lock_restaurant(
    restaurant_id: int,
    state: Annotated[dict, InjectedState],
    tool_call_id: Annotated[str, InjectedToolCallId],
    confirm_switch: bool = False,
) -> Command:
    """Lock this session to one restaurant so the menu and cart can be used.

    If a different restaurant is already locked, this refuses unless
    confirm_switch=True. Ask the user first, warn them the cart will be
    emptied, and only then retry with confirm_switch=True.
    """
    current = state.get("restaurant_id")

    with queries.session() as conn:
        restaurant = queries.get_restaurant(conn, restaurant_id)
    if restaurant is None:
        return _tool_reply(
            f"No restaurant with id={restaurant_id} in the snapshot. "
            "Use search_restaurants and pick an id from those results.",
            tool_call_id,
            {},
        )

    if current == restaurant_id:
        return _tool_reply(
            f"Already locked to {restaurant['name']} (id={restaurant_id}).",
            tool_call_id,
            {},
        )

    if current is not None and not confirm_switch:
        return _tool_reply(
            f"Session is locked to {state.get('restaurant_name')} "
            f"(id={current}) with {cart_item_count(state.get('cart'))} item(s) "
            f"in the cart. Switching to {restaurant['name']} will empty the "
            "cart. Ask the user to confirm, then call lock_restaurant again "
            "with confirm_switch=true.",
            tool_call_id,
            {},
        )

    update: dict[str, Any] = {
        "restaurant_id": restaurant_id,
        "restaurant_name": restaurant["name"],
        "order_summary": None,
        "showcase": empty_showcase(showcase_step(state)),
    }
    message = f"Locked to {restaurant['name']} (id={restaurant_id})."
    if current is not None:
        update["cart"] = []
        message += " Previous cart cleared because the restaurant changed."
    return _tool_reply(message, tool_call_id, update)


@tool
def get_reviews(
    state: Annotated[dict, InjectedState],
    restaurant_name: str | None = None,
    restaurant_id: int | None = None,
) -> str:
    """Fetch stored customer review text for one restaurant.

    Pass restaurant_name when the user says a place by name (typical). Pass
    restaurant_id only when you already have it from search_restaurants.
    Uses session location to pick the right branch when names repeat across
    areas. Does not require a locked restaurant.
    """
    if restaurant_id is None and not restaurant_name:
        return (
            "Provide restaurant_name (what the user said) or restaurant_id "
            "from a prior search_restaurants result."
        )

    with queries.session() as conn:
        if restaurant_id is not None:
            restaurant = queries.get_restaurant(conn, restaurant_id)
            if restaurant is None:
                return (
                    f"No restaurant with id={restaurant_id} in the snapshot. "
                    "Use search_restaurants or get_reviews with restaurant_name."
                )
        else:
            assert restaurant_name is not None
            restaurant, err = queries.resolve_restaurant_by_name(
                conn,
                restaurant_name,
                location=state.get("location"),
            )
            if err:
                return err
            if restaurant is None:
                return f"No restaurant in the snapshot matches {restaurant_name!r}."
            restaurant_id = int(restaurant["id"])

        rows = queries.list_reviews(conn, restaurant_id)
        restaurant = queries.get_restaurant(conn, restaurant_id)
    assert restaurant is not None

    if not rows:
        return (
            f"No stored customer review text for {restaurant['name']} "
            f"(id={restaurant_id}). This restaurant is not in the manual "
            "review sample — cite rating/review_count only if relevant, "
            "and do not invent or paraphrase customer comments."
        )
    lines = [
        f"Stored customer reviews for {restaurant['name']} "
        f"(id={restaurant_id}, manual sample, not live Foodpanda):",
    ]
    for idx, row in enumerate(rows, start=1):
        lines.append(f"\n--- Review {idx} ---")
        lines.append(row["review_text"])
        liked = row.get("liked_dishes") or []
        if liked:
            dish_bits = []
            for dish in liked:
                name = dish.get("name") or "?"
                item_id = dish.get("item_id")
                if item_id is not None:
                    dish_bits.append(f"{name} (menu item_id={item_id})")
                else:
                    dish_bits.append(name)
            lines.append("Liked dishes: " + ", ".join(dish_bits))
        if row.get("owner_response"):
            lines.append(f"Restaurant response: {row['owner_response']}")
    lines.append(
        "\nQuote only what appears above. These are a curated subset, "
        "not all Foodpanda reviews for this place."
    )
    return "\n".join(lines)


@tool
def search_menu(
    query: str,
    state: Annotated[dict, InjectedState],
    tool_call_id: Annotated[str, InjectedToolCallId],
    max_price: float | None = None,
    category: str | None = None,
) -> Command:
    """Search the locked restaurant's menu for dishes and matching deals.

    Requires a locked restaurant. Automatically includes deal-category items
    that fit the craving, budget or party size. Returns item_id values for
    add_to_cart. Do not invent discount percentages.
    """
    restaurant_id = state.get("restaurant_id")
    if restaurant_id is None:
        return _tool_reply(NO_LOCK_MESSAGE, tool_call_id, {})

    budget = state.get("budget")
    if max_price is None and budget is not None:
        max_price = budget

    with queries.session() as conn:
        rows = queries.search_menu(
            conn,
            restaurant_id=restaurant_id,
            query=query,
            max_price=max_price,
            category=category,
            limit=MAX_MENU_RESULTS,
        )
        deals = queries.search_deals(
            conn,
            restaurant_id=restaurant_id,
            query=query or state.get("craving"),
            budget=budget,
            party_size=state.get("party_size"),
            limit=4,
        )
        categories = queries.list_categories(conn, restaurant_id)

    if not rows and not deals:
        return _tool_reply(
            f"No menu items found at {state.get('restaurant_name')}. "
            f"Available categories: {', '.join(categories[:15]) or 'none'}",
            tool_call_id,
            {"showcase": empty_showcase(showcase_step(state))},
        )

    seen: set[int] = set()
    cards: list[dict[str, Any]] = []
    lines = [f"Menu matches at {state.get('restaurant_name')}:"]

    if deals:
        lines.append("Matching deals (category names only — no invented % off):")
        for row in deals:
            seen.add(row["item_id"])
            cards.append(_dish_card(row, is_deal=True))
            lines.append(
                f"- DEAL item_id={row['item_id']} | {row['name']} | "
                f"{_money(row.get('price_value'))} | {row['category_name']}"
            )

    for row in rows:
        if row["item_id"] in seen:
            continue
        seen.add(row["item_id"])
        cards.append(_dish_card(row))
        description = (row.get("description") or "").strip().replace("\n", " ")
        if len(description) > 110:
            description = description[:107] + "..."
        lines.append(
            f"- item_id={row['item_id']} | {row['name']} | "
            f"{_money(row.get('price_value'))} | category: {row['category_name']}"
            + (f" | {description}" if description else "")
        )

    if deals:
        lines.append(
            "Pitch a matching deal first when it fits party size or budget. "
            "The chat UI shows photo cards; keep the spoken reply short."
        )
    showcase = {
        "kind": "menu",
        "title": state.get("restaurant_name") or "Menu",
        "items": cards[:12],
        "step": showcase_step(state),
    }
    return _tool_reply("\n".join(lines), tool_call_id, {"showcase": showcase})


@tool
def add_to_cart(
    item_id: int,
    state: Annotated[dict, InjectedState],
    tool_call_id: Annotated[str, InjectedToolCallId],
    qty: int = 1,
) -> Command:
    """Add a menu item to the cart by item_id from search_menu.

    Returns every cart line plus item subtotal, delivery fee, platform fee
    and total. Do not call view_cart afterwards just to confirm this add.
    """
    restaurant_id = state.get("restaurant_id")
    if restaurant_id is None:
        return _tool_reply(NO_LOCK_MESSAGE, tool_call_id, {})
    if qty < 1:
        return _tool_reply("qty must be at least 1.", tool_call_id, {})

    with queries.session() as conn:
        item = queries.get_menu_item(conn, restaurant_id, item_id)
    if item is None:
        return _tool_reply(
            f"item_id={item_id} is not on {state.get('restaurant_name')}'s menu. "
            "Use search_menu to get valid item_ids for the locked restaurant.",
            tool_call_id,
            {},
        )

    # A delta, not the whole cart: parallel add_to_cart calls all read the same
    # pre-step state, so returning full carts would drop every item but one.
    operation = {
        "op": "add",
        "qty": qty,
        "line": {
            "item_id": item_id,
            "name": item["name"],
            "price": item.get("price_value"),
            "image_url": item.get("image_url"),
        },
    }
    cart = apply_cart_write(state.get("cart"), operation)

    totals = fees.estimate_fees(cart)
    lines = [
        f"Added {qty} x {item['name']} ({_money(item.get('price_value'))}).",
        f"Cart at {state.get('restaurant_name') or 'unknown restaurant'}:",
    ]
    for line in cart:
        price = line.get("price")
        line_total = price * line["qty"] if isinstance(price, (int, float)) else None
        lines.append(
            f"- {line['name']} x{line['qty']} | {_money(price)} each | "
            f"line {_money(line_total)}"
        )
    lines.append(f"Subtotal (items): {_money(totals['subtotal'])}")
    lines.append(f"Delivery fee: {_money(totals['delivery_fee'])}")
    lines.append(f"Platform fee: {_money(totals['platform_fee'])}")
    lines.append(f"Total: {_money(totals['total'])}")
    lines.append(
        "This total covers the cart before this turn plus this one add. If you "
        "called add_to_cart more than once this turn, the other items are not "
        "in it — call view_cart once for the real total. For a single add the "
        "cart above is complete, so do not re-read it."
    )
    return _tool_reply(
        "\n".join(lines),
        tool_call_id,
        {"cart": operation, "order_summary": None},
    )


@tool
def remove_from_cart(
    item_id: int,
    state: Annotated[dict, InjectedState],
    tool_call_id: Annotated[str, InjectedToolCallId],
    qty: int | None = None,
) -> Command:
    """Remove an item from the cart. Omit qty to remove the whole line."""
    cart = [dict(line) for line in state.get("cart") or []]
    match = next((line for line in cart if line["item_id"] == item_id), None)
    if match is None:
        return _tool_reply(
            f"item_id={item_id} is not in the cart.", tool_call_id, {}
        )

    if qty is None or int(match["qty"]) <= qty:
        detail = f"Removed {match['name']} from the cart."
    else:
        detail = f"Removed {qty} x {match['name']}."

    operation = {"op": "remove", "item_id": item_id, "qty": qty}
    cart = apply_cart_write(state.get("cart"), operation)

    totals = fees.estimate_fees(cart)
    return _tool_reply(
        f"{detail} Cart: {cart_item_count(cart)} item(s), "
        f"items {_money(totals['subtotal'])}, total {_money(totals['total'])} "
        f"(incl. delivery {_money(totals['delivery_fee'])} + platform "
        f"{_money(totals['platform_fee'])}).",
        tool_call_id,
        {"cart": operation, "order_summary": None},
    )


@tool
def view_cart(state: Annotated[dict, InjectedState]) -> str:
    """Show the current cart as a Foodpanda-style checkout breakdown."""
    cart = state.get("cart") or []
    if not cart:
        return "The cart is empty."
    totals = fees.estimate_fees(cart)
    lines = [f"Cart at {state.get('restaurant_name') or 'unknown restaurant'}:"]
    for line in cart:
        price = line.get("price")
        line_total = price * line["qty"] if isinstance(price, (int, float)) else None
        lines.append(
            f"- item_id={line['item_id']} | {line['name']} x{line['qty']} | "
            f"{_money(price)} each | line total {_money(line_total)}"
        )
    lines.append(f"Subtotal (items): {_money(totals['subtotal'])}")
    lines.append(f"Delivery fee: {_money(totals['delivery_fee'])}")
    lines.append(f"Platform fee: {_money(totals['platform_fee'])}")
    lines.append(f"Total: {_money(totals['total'])}")
    if totals["below_minimum_order"]:
        lines.append(
            f"Note: items are below the typical PKR {totals['minimum_order']:.0f} "
            "vendor minimum; Foodpanda may add a small-order fee at checkout."
        )
    lines.append(totals["fee_note"])
    return "\n".join(lines)


def build_order_summary(state: dict[str, Any]) -> dict[str, Any]:
    """Build the handoff object shared by the tool and POST /confirm.

    Shaped like a Foodpanda cart checkout: line items, then delivery and
    platform fees, then total. This is still NOT a real Foodpanda order.
    """
    cart = state.get("cart") or []
    totals = fees.estimate_fees(cart)
    return {
        "kind": "cart_summary",
        "disclaimer": (
            "This is a prepared cart summary only. No order has been placed "
            "with Foodpanda or the restaurant, nothing is reserved, and prices "
            "come from a local scraped snapshot rather than live checkout."
        ),
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "restaurant": {
            "id": state.get("restaurant_id"),
            "name": state.get("restaurant_name"),
        },
        "items": [
            {
                "item_id": line["item_id"],
                "name": line["name"],
                "price": line.get("price"),
                "qty": line["qty"],
                "image_url": line.get("image_url"),
                "line_total": (
                    round(line["price"] * line["qty"], 2)
                    if isinstance(line.get("price"), (int, float))
                    else None
                ),
            }
            for line in cart
        ],
        "item_count": cart_item_count(cart),
        "subtotal": totals["subtotal"],
        "delivery_fee": totals["delivery_fee"],
        "platform_fee": totals["platform_fee"],
        "total": totals["total"],
        "currency": "PKR",
        "party_size": state.get("party_size"),
        "location": state.get("location"),
        "below_minimum_order": totals["below_minimum_order"],
        "minimum_order": totals["minimum_order"],
        "notes": totals["fee_note"],
    }


@tool
def confirm_order(
    state: Annotated[dict, InjectedState],
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> Command:
    """Finalize the cart into an order summary for the user to confirm.

    This does not place a real Foodpanda order. Say so plainly when you
    report the result.
    """
    if state.get("restaurant_id") is None:
        return _tool_reply(NO_LOCK_MESSAGE, tool_call_id, {})
    cart = state.get("cart") or []
    if not cart:
        return _tool_reply(
            "The cart is empty, so there is nothing to confirm.", tool_call_id, {}
        )

    summary = build_order_summary(state)
    lines = [
        f"Cart summary for {summary['restaurant']['name']} "
        "(NOT placed with Foodpanda):",
    ]
    for item in summary["items"]:
        lines.append(f"- {item['name']} x{item['qty']} = {_money(item['line_total'])}")
    lines.append(f"Subtotal (items): {_money(summary['subtotal'])}")
    lines.append(f"Delivery fee: {_money(summary['delivery_fee'])}")
    lines.append(f"Platform fee: {_money(summary['platform_fee'])}")
    lines.append(f"Total: {_money(summary['total'])}")
    lines.append(summary["notes"])
    lines.append(
        "Tell the user this is a cart they can recreate in the Foodpanda app; "
        "this assistant cannot place the order or track it."
    )
    return _tool_reply("\n".join(lines), tool_call_id, {"order_summary": summary})


TOOLS = [
    remember_preferences,
    search_restaurants,
    get_reviews,
    lock_restaurant,
    search_menu,
    add_to_cart,
    remove_from_cart,
    view_cart,
    confirm_order,
]
