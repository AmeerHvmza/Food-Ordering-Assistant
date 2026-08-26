"""Normalize Foodpanda menu prices to a single numeric string.

Deal items expose a current/discounted amount and a strikethrough original.
The expansion scrape stored the concatenated display string
(`from Rs. 399Rs. 549`) instead of the structured variation price, and
sometimes a second empty-price row for the same name.

Stored `price` is always what the customer pays (discounted/current).
`original_price` is the strikethrough amount when present.
"""

from __future__ import annotations

import re
from typing import Any

# First amount is the payable/discounted price; a second amount is the
# crossed-out original. Matches CAST(REPLACE(REPLACE(..., 'PKR'), ',')) AS REAL
# plus Rs./₹ prefixes the original 29 restaurants never had.
_AMOUNT_RE = re.compile(
    r"(?:Rs\.?|PKR|₹)\s*([\d,]+(?:\.\d+)?)",
    re.IGNORECASE,
)
_BARE_NUMBER_RE = re.compile(r"^[\d,]+(?:\.\d+)?$")


def format_numeric_price(value: float) -> str:
    """Store like the original 29 restaurants: '300' or '964.75'."""
    rounded = round(float(value), 2)
    if rounded == int(rounded):
        return str(int(rounded))
    return f"{rounded:.2f}"


def parse_price_parts(raw: Any) -> tuple[str | None, str | None]:
    """Return (current_price, original_price) as numeric strings.

    Prefer a bare number (API `product_variations[].price`). Fall back to the
    first 'Rs. N' in a display string as the payable amount; a second amount
    is treated as the strikethrough original.
    """
    if raw is None:
        return None, None
    if isinstance(raw, bool):
        return None, None
    if isinstance(raw, (int, float)):
        return format_numeric_price(float(raw)), None

    text = str(raw).strip()
    if not text:
        return None, None

    stripped = (
        text.replace("PKR", "").replace("₹", "").replace(",", "").strip()
    )
    if _BARE_NUMBER_RE.match(stripped.replace("PKR", "").strip()):
        # Already a clean numeric / CAST-REPLACE-able string with no Rs.
        try:
            return format_numeric_price(float(stripped)), None
        except ValueError:
            pass

    amounts = [
        float(m.replace(",", ""))
        for m in _AMOUNT_RE.findall(text)
    ]
    if not amounts:
        # Last resort: CAST/REPLACE style on the whole string.
        cleaned = text.replace("PKR", "").replace("Rs.", "").replace("Rs", "")
        cleaned = cleaned.replace("₹", "").replace(",", "").replace("from", "")
        cleaned = cleaned.strip()
        try:
            return format_numeric_price(float(cleaned)), None
        except ValueError:
            return None, None

    current = format_numeric_price(amounts[0])
    original = format_numeric_price(amounts[1]) if len(amounts) > 1 else None
    if original == current:
        original = None
    return current, original


def clean_stored_price(raw: Any) -> str | None:
    """Numeric string for menu_items.price, or None if unusable."""
    current, _original = parse_price_parts(raw)
    return current


def extract_item_prices(product: dict[str, Any]) -> tuple[str | None, str | None]:
    """Current and original price from an fd-api product object.

    Structured fields on product_variations[] win over display_price. When
    several variations exist, the cheapest current price is stored (the
    'from Rs. X' amount a customer can actually pay).
    """
    variations = product.get("product_variations") or []
    priced: list[tuple[float, float | None]] = []
    for variation in variations:
        if not isinstance(variation, dict):
            continue
        current_raw = variation.get("price")
        if current_raw is None:
            continue
        try:
            current = float(current_raw)
        except (TypeError, ValueError):
            parsed, _ = parse_price_parts(current_raw)
            if parsed is None:
                continue
            current = float(parsed)
        original_raw = variation.get("price_before_discount")
        original: float | None = None
        if original_raw is not None:
            try:
                original = float(original_raw)
            except (TypeError, ValueError):
                parsed_orig, _ = parse_price_parts(original_raw)
                original = float(parsed_orig) if parsed_orig else None
        priced.append((current, original))

    if priced:
        current, original = min(priced, key=lambda pair: pair[0])
        orig_s = (
            format_numeric_price(original)
            if original is not None and original != current
            else None
        )
        return format_numeric_price(current), orig_s

    # No structured variation price: parse display_price, never store it raw.
    display = product.get("display_price")
    return parse_price_parts(display)
