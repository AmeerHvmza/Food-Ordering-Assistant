"""Conversation state for the ordering assistant."""

from __future__ import annotations

from typing import Annotated, Any, TypedDict

from langgraph.graph.message import add_messages

from agent.currency import sanitize_currency
from db.fees import estimate_fees


MAX_SHOWCASE_ITEMS = 24


def take_latest(existing: Any, incoming: Any) -> Any:
    """Last write wins, but two writes in one step are not an error.

    The default LastValue channel raises InvalidUpdateError when a single
    graph step writes a key twice, which happens whenever the model emits
    parallel tool calls ("2 parathas and 3 cup chai" -> two tool calls in one
    step). For scalars like location or restaurant_id, taking the later write
    is the sane resolution.
    """
    return incoming


def empty_showcase(step: int) -> dict[str, Any]:
    """A stamped 'no cards' showcase.

    Tools clear cards with this rather than None so the merge reducer stays
    total: a sibling tool that found nothing must not be able to wipe the cards
    a parallel tool did find, whichever order the writes land in.
    """
    return {"kind": "empty", "title": None, "items": [], "step": step}


def merge_showcase(existing: Any, incoming: Any) -> Any:
    """Replace across tool rounds, merge within one round.

    showcase holds the cards the chat UI paints for the current turn. Parallel
    tool calls share a step stamp (see showcase_step), so two searches in one
    step are additive - the user asked for parathas *and* chai and should see
    both - while a later round replaces stale cards outright.
    """
    if incoming is None:
        return None
    if not isinstance(incoming, dict):
        return existing
    if not isinstance(existing, dict):
        return incoming
    if incoming.get("step") != existing.get("step"):
        return incoming

    items = [dict(item) for item in existing.get("items") or []]
    seen = {(item.get("kind"), item.get("id")) for item in items}
    for item in incoming.get("items") or []:
        key = (item.get("kind"), item.get("id"))
        if key in seen:
            continue
        seen.add(key)
        items.append(dict(item))

    # A single search contributes at most 12 cards; cap the merge so a round
    # with many parallel searches cannot grow the card strip without bound.
    items = items[:MAX_SHOWCASE_ITEMS]

    kinds = {
        kind
        for kind in (existing.get("kind"), incoming.get("kind"))
        if kind and kind != "empty"
    }
    titles = [
        title
        for title in (existing.get("title"), incoming.get("title"))
        if title
    ]
    return {
        "kind": kinds.pop() if len(kinds) == 1 else "mixed",
        "title": " + ".join(dict.fromkeys(titles)) or None,
        "items": items,
        "step": existing.get("step"),
    }


def apply_cart_write(existing: Any, incoming: Any) -> list[dict]:
    """Apply a cart delta, or replace the cart outright with a list.

    Cart writes are deltas ({"op": "add"/"remove"}) rather than whole carts on
    purpose. Every parallel tool call reads the same pre-step state, so two
    tools each returning a full cart would silently drop one item no matter
    which write landed last. Deltas compose.

    A plain list still means "replace" so the graph can be seeded and
    lock_restaurant can empty the cart on a restaurant switch.
    """
    if incoming is None:
        return []
    if isinstance(incoming, list):
        return [dict(line) for line in incoming]
    if not isinstance(incoming, dict):
        return [dict(line) for line in existing or []]

    cart = [dict(line) for line in existing or []]
    op = incoming.get("op")

    if op == "add":
        line = dict(incoming.get("line") or {})
        item_id = line.get("item_id")
        qty = int(incoming.get("qty") or 1)
        for current in cart:
            if current.get("item_id") == item_id:
                current["qty"] = int(current.get("qty") or 0) + qty
                break
        else:
            line["qty"] = qty
            cart.append(line)
        return cart

    if op == "remove":
        item_id = incoming.get("item_id")
        qty = incoming.get("qty")
        match = next(
            (line for line in cart if line.get("item_id") == item_id), None
        )
        if match is None:
            return cart
        if qty is None or int(match.get("qty") or 0) <= int(qty):
            return [line for line in cart if line.get("item_id") != item_id]
        match["qty"] = int(match["qty"]) - int(qty)
        return cart

    return cart


class OrderState(TypedDict, total=False):
    """One ordering session.

    Milestone 1 locks a single restaurant per session; restaurant_id is set by
    lock_restaurant and cleared by unlock_restaurant, by search_restaurants
    when the cart is empty, or on a confirmed switch.

    Every key a tool can write carries a reducer: the model emits parallel tool
    calls for multi-item requests, and an unannotated key rejects two writes in
    the same step.
    """

    messages: Annotated[list, add_messages]
    location: Annotated[str | None, take_latest]
    restaurant_id: Annotated[int | None, take_latest]
    restaurant_name: Annotated[str | None, take_latest]
    party_size: Annotated[int | None, take_latest]
    craving: Annotated[str | None, take_latest]
    budget: Annotated[float | None, take_latest]
    deal_sensitive: Annotated[bool | None, take_latest]
    cart: Annotated[list[dict], apply_cart_write]
    order_summary: Annotated[dict | None, take_latest]
    # restaurant/dish cards for the chat UI
    showcase: Annotated[dict | None, merge_showcase]


PREFERENCE_FIELDS = (
    "location",
    "party_size",
    "craving",
    "budget",
    "deal_sensitive",
)

SNAPSHOT_FIELDS = (
    "location",
    "restaurant_id",
    "restaurant_name",
    "party_size",
    "craving",
    "budget",
    "deal_sensitive",
    "cart",
    "order_summary",
    "showcase",
)


def showcase_step(state: dict[str, Any] | None) -> int:
    """Identity for the current tool round.

    Tools receive the state as it was when the step began, so every tool call
    in one round sees the same message count, and the next round sees more.
    That makes it a free step stamp for merge_showcase.
    """
    return len((state or {}).get("messages") or [])


def cart_subtotal(cart: list[dict] | None) -> float:
    """Sum price * qty over cart lines, ignoring unpriced items."""
    total = 0.0
    for line in cart or []:
        price = line.get("price")
        qty = line.get("qty") or 0
        if isinstance(price, (int, float)):
            total += float(price) * int(qty)
    return round(total, 2)


def cart_item_count(cart: list[dict] | None) -> int:
    return sum(int(line.get("qty") or 0) for line in cart or [])


def serialize_messages(messages: list[Any] | None) -> list[dict[str, str]]:
    """Flatten LangChain messages to {role, content} for API responses."""
    roles = {"human": "user", "ai": "assistant", "tool": "tool", "system": "system"}
    out: list[dict[str, str]] = []
    for message in messages or []:
        msg_type = getattr(message, "type", None)
        if msg_type is None:
            continue
        content = getattr(message, "content", "")
        if isinstance(content, list):
            content = " ".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict)
            )
        out.append(
            {
                "role": roles.get(msg_type, msg_type),
                "content": sanitize_currency(str(content)),
            }
        )
    return out


def public_showcase(showcase: Any) -> dict[str, Any] | None:
    """Drop the internal step stamp and report "no cards" as None."""
    if not isinstance(showcase, dict):
        return None
    items = showcase.get("items") or []
    if not items:
        return None
    return {
        "kind": showcase.get("kind"),
        "title": showcase.get("title"),
        "items": items,
    }


def serialize_state(values: dict[str, Any] | None) -> dict[str, Any]:
    """Public OrderState snapshot: no raw LangChain objects.

    Milestone 6 wraps this contract with auth, so keep the shape stable.
    """
    values = values or {}
    snapshot: dict[str, Any] = {
        field: values.get(field) for field in SNAPSHOT_FIELDS
    }
    snapshot["cart"] = values.get("cart") or []
    snapshot["cart_subtotal"] = cart_subtotal(snapshot["cart"])
    snapshot["cart_totals"] = estimate_fees(snapshot["cart"])
    snapshot["showcase"] = public_showcase(values.get("showcase"))
    snapshot["messages"] = serialize_messages(values.get("messages"))
    return snapshot
