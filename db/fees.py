"""Typical Foodpanda Pakistan checkout extras for the cart summary.

These are not live quotes. The disco listing feed for this Karachi snapshot
returned ``minimum_delivery_fee = 99.0`` on every matched vendor (2026-08-13).
A platform/service fee is charged on Foodpanda.pk checkout in addition to
delivery; the listing API exposes ``is_service_fee_enabled`` but that amount
was never persisted, so we use the common PKR 8.99 platform fee shown on
Pakistan checkout around 2025–2026.

Label both as estimates in the UI. Real checkout can differ (distance, MOV,
vouchers, GST). See policies.md §7.3–7.5.
"""

from __future__ import annotations

from typing import Any


DELIVERY_FEE_PKR = 99.0
PLATFORM_FEE_PKR = 8.99
MINIMUM_ORDER_PKR = 249.0

FEE_NOTE = (
    "Delivery PKR 99 and platform fee PKR 8.99 are typical Foodpanda Pakistan "
    "checkout extras from the Karachi listing snapshot, not a live quote. "
    "GST, small-order fees and vouchers are not applied here."
)


def _subtotal(cart: list[dict] | None) -> float:
    total = 0.0
    for line in cart or []:
        price = line.get("price")
        qty = line.get("qty") or 0
        if isinstance(price, (int, float)):
            total += float(price) * int(qty)
    return round(total, 2)


def estimate_fees(cart: list[dict] | None) -> dict[str, Any]:
    """Return a Foodpanda-style totals breakdown for a cart."""
    subtotal = _subtotal(cart)
    delivery = DELIVERY_FEE_PKR if cart else 0.0
    platform = PLATFORM_FEE_PKR if cart else 0.0
    total = round(subtotal + delivery + platform, 2)
    below_mov = bool(cart) and subtotal < MINIMUM_ORDER_PKR
    return {
        "subtotal": subtotal,
        "delivery_fee": delivery,
        "platform_fee": platform,
        "total": total,
        "currency": "PKR",
        "minimum_order": MINIMUM_ORDER_PKR,
        "below_minimum_order": below_mov,
        "fee_note": FEE_NOTE,
    }
