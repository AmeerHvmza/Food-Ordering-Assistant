"""Live freshness checks at lock and confirm. Snapshot stays primary.

Two Foodpanda fd-api calls per conversation at most: vendor meta on lock,
menu re-fetch before confirm. Browsing never hits the network.
"""

from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from db import fees, geo

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRAPER_ROOT = REPO_ROOT / "foodpanda-scraper"

LOCK_TIMEOUT_SEC = 3.0
MENU_TIMEOUT_SEC = 5.0
BREAKER_THRESHOLD = 3
BREAKER_COOLDOWN_SEC = 600.0
PRICE_CHANGE_PKR = 1.0

_api_client: Any = None
_breaker_lock = threading.Lock()
_consecutive_403 = 0
_breaker_until = 0.0


OpenStatus = Literal["open", "closed", "inconclusive"]
CartStatus = Literal["ok", "changes", "inconclusive"]


@dataclass(frozen=True)
class OpenCheck:
    status: OpenStatus
    reason: str = ""


@dataclass
class PriceChange:
    item_id: Any
    name: str
    qty: int
    old_price: float
    new_price: float

    @property
    def unit_delta(self) -> float:
        return round(self.new_price - self.old_price, 2)

    @property
    def line_delta(self) -> float:
        return round(self.unit_delta * self.qty, 2)


@dataclass
class UnavailableLine:
    item_id: Any
    name: str
    qty: int
    price: float | None
    why: str = "not available right now"


@dataclass
class CartCheck:
    status: CartStatus
    reason: str = ""
    unavailable: list[UnavailableLine] = field(default_factory=list)
    price_changes: list[PriceChange] = field(default_factory=list)
    priced_cart: list[dict[str, Any]] | None = None


def reset_circuit_breaker() -> None:
    global _consecutive_403, _breaker_until
    with _breaker_lock:
        _consecutive_403 = 0
        _breaker_until = 0.0


def breaker_is_open() -> bool:
    return time.monotonic() < _breaker_until


def _record_403() -> None:
    global _consecutive_403, _breaker_until
    with _breaker_lock:
        _consecutive_403 += 1
        if _consecutive_403 >= BREAKER_THRESHOLD:
            _breaker_until = time.monotonic() + BREAKER_COOLDOWN_SEC


def _record_success() -> None:
    global _consecutive_403
    with _breaker_lock:
        _consecutive_403 = 0


def extract_vendor_code(url: str) -> str | None:
    parts = (url or "").rstrip("/").split("/")
    try:
        idx = parts.index("restaurant")
        if idx + 1 < len(parts):
            code = (parts[idx + 1] or "").strip()
            return code or None
    except ValueError:
        pass
    return None


def centroid_for_area(location: str) -> tuple[float, float] | None:
    """Map a free-typed area string to a KARACHI_AREAS centroid. Never Saddar default."""
    if not (location or "").strip():
        return None
    terms = set(geo.area_search_terms(location))
    normalized = geo.normalize_area_text(location)
    if not normalized:
        return None
    for name, lat, lng in geo.KARACHI_AREAS:
        name_norm = geo.normalize_area_text(name)
        aliases = geo.AREA_ALIASES.get(name, ())
        haystack = {name_norm, *aliases}
        if any(term in normalized or normalized in term for term in haystack):
            return lat, lng
        if terms & haystack:
            return lat, lng
        if any(term in name_norm for term in terms if len(term) >= 4):
            return lat, lng
    return None


def pin_for(
    state: dict[str, Any], restaurant: dict[str, Any] | None
) -> tuple[float, float] | None:
    pin = centroid_for_area(str(state.get("location") or ""))
    if pin:
        return pin
    if restaurant:
        pin = centroid_for_area(str(restaurant.get("delivery_areas") or ""))
        if pin:
            return pin
    return None


def normalize_item_name(name: str) -> str:
    lowered = (name or "").casefold()
    chars = [ch if ch.isalnum() else " " for ch in lowered]
    return " ".join("".join(chars).split())


def parse_price(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "")
    for token in ("PKR", "Rs.", "Rs", ",", " "):
        text = text.replace(token, "")
    text = text.strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _api_client_mod() -> Any:
    global _api_client
    if _api_client is not None:
        return _api_client
    root = str(SCRAPER_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    from scraper import api_client as client  # noqa: WPS433

    _api_client = client
    return client


def live_vendor_meta(
    vendor_code: str, lat: float, lng: float, timeout: float = LOCK_TIMEOUT_SEC
) -> dict[str, Any] | None:
    return _api_client_mod().fetch_vendor_meta(
        vendor_code, lat, lng, timeout=timeout
    )


def live_menu(
    vendor_code: str, lat: float, lng: float, timeout: float = MENU_TIMEOUT_SEC
) -> list[dict[str, Any]] | None:
    return _api_client_mod().fetch_menu(vendor_code, lat, lng, timeout=timeout)


def last_http_status() -> int | None:
    return getattr(_api_client_mod(), "LAST_MENU_STATUS", None)


def interpret_vendor_meta(
    meta: dict[str, Any] | None, http_status: int | None
) -> OpenCheck:
    if meta is None:
        if http_status == 403:
            _record_403()
            return OpenCheck(
                "inconclusive",
                "Foodpanda blocked the availability check (HTTP 403)",
            )
        return OpenCheck("inconclusive", "availability check failed")
    _record_success()
    if meta.get("is_temporary_closed") is True:
        return OpenCheck("closed", "temporarily closed")
    if meta.get("is_delivery_available") is False:
        return OpenCheck("closed", "not delivering right now")
    if meta.get("is_delivery_enabled") is False:
        return OpenCheck("closed", "delivery is not enabled")
    if (
        meta.get("is_delivery_available") is True
        or meta.get("is_delivery_enabled") is True
    ):
        return OpenCheck("open")
    return OpenCheck(
        "inconclusive", "availability fields missing from live response"
    )


def check_open(
    restaurant: dict[str, Any], state: dict[str, Any]
) -> OpenCheck:
    if breaker_is_open():
        return OpenCheck(
            "inconclusive",
            "skipped live check (Foodpanda recently blocked us)",
        )
    code = extract_vendor_code(str(restaurant.get("url") or ""))
    if not code:
        return OpenCheck("inconclusive", "no Foodpanda vendor code on this row")
    pin = pin_for(state, restaurant)
    if pin is None:
        return OpenCheck(
            "inconclusive",
            "no delivery pin for the live check (need a known Karachi area)",
        )
    lat, lng = pin
    try:
        meta = live_vendor_meta(code, lat, lng, timeout=LOCK_TIMEOUT_SEC)
    except Exception:
        return OpenCheck("inconclusive", "availability check failed")
    return interpret_vendor_meta(meta, last_http_status())


def flatten_live_menu(categories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for category in categories or []:
        for item in category.get("items") or []:
            items.append(item)
    return items


def diff_cart_against_menu(
    cart: list[dict[str, Any]], categories: list[dict[str, Any]]
) -> CartCheck:
    live_items = flatten_live_menu(categories)
    if not live_items:
        return CartCheck(
            "inconclusive",
            "live menu was empty — not treated as sold-out",
        )

    by_name: dict[str, list[dict[str, Any]]] = {}
    for item in live_items:
        key = normalize_item_name(str(item.get("name") or ""))
        if key:
            by_name.setdefault(key, []).append(item)

    unavailable: list[UnavailableLine] = []
    price_changes: list[PriceChange] = []
    priced_cart: list[dict[str, Any]] = []

    for line in cart:
        copied = dict(line)
        key = normalize_item_name(str(line.get("name") or ""))
        candidates = by_name.get(key) or []
        available = [c for c in candidates if not c.get("is_sold_out")]
        if not available:
            why = "sold out right now" if candidates else "not available right now"
            unavailable.append(
                UnavailableLine(
                    item_id=line.get("item_id"),
                    name=str(line.get("name") or ""),
                    qty=int(line.get("qty") or 1),
                    price=parse_price(line.get("price")),
                    why=why,
                )
            )
            continue

        old = parse_price(line.get("price"))
        live_prices = [
            p for p in (parse_price(c.get("price")) for c in available) if p is not None
        ]
        if old is not None and live_prices:
            if all(abs(old - price) >= PRICE_CHANGE_PKR for price in live_prices):
                new = min(live_prices, key=lambda price: abs(price - old))
                price_changes.append(
                    PriceChange(
                        item_id=line.get("item_id"),
                        name=str(line.get("name") or ""),
                        qty=int(line.get("qty") or 1),
                        old_price=old,
                        new_price=new,
                    )
                )
                copied["price"] = new
            else:
                matching = [
                    price
                    for price in live_prices
                    if abs(old - price) < PRICE_CHANGE_PKR
                ]
                copied["price"] = matching[0] if matching else old
        priced_cart.append(copied)

    if unavailable or price_changes:
        return CartCheck(
            "changes",
            priced_cart=priced_cart,
            unavailable=unavailable,
            price_changes=price_changes,
        )
    return CartCheck("ok", priced_cart=priced_cart)


def precheck_cart(
    state: dict[str, Any], restaurant: dict[str, Any] | None
) -> CartCheck:
    cart = list(state.get("cart") or [])
    if breaker_is_open():
        return CartCheck(
            "inconclusive",
            "skipped live check (Foodpanda recently blocked us)",
        )
    if restaurant is None:
        return CartCheck("inconclusive", "restaurant missing from the snapshot")
    code = extract_vendor_code(str(restaurant.get("url") or ""))
    if not code:
        return CartCheck("inconclusive", "no Foodpanda vendor code on this row")
    pin = pin_for(state, restaurant)
    if pin is None:
        return CartCheck(
            "inconclusive",
            "no delivery pin for the live check (need a known Karachi area)",
        )
    lat, lng = pin
    try:
        categories = live_menu(code, lat, lng, timeout=MENU_TIMEOUT_SEC)
    except Exception:
        return CartCheck("inconclusive", "live menu check failed")
    if categories is None:
        status = last_http_status()
        if status == 403:
            _record_403()
            return CartCheck(
                "inconclusive",
                "Foodpanda blocked the menu check (HTTP 403)",
            )
        return CartCheck("inconclusive", "live menu check failed")
    _record_success()
    return diff_cart_against_menu(cart, categories)


def accepted_cart(state: dict[str, Any], check: CartCheck) -> list[dict[str, Any]]:
    """Cart after the user accepts a diff: drop unavailable, use live prices."""
    if check.priced_cart is not None:
        return [dict(line) for line in check.priced_cart]
    return [dict(line) for line in (state.get("cart") or [])]


def freshness_block(
    *,
    checked: bool,
    kind: str,
    reason: str = "",
) -> dict[str, Any]:
    from datetime import datetime, timezone

    return {
        "checked": checked,
        "kind": kind,
        "reason": reason or None,
        "at": datetime.now(timezone.utc).isoformat(),
    }


def format_cart_diff(check: CartCheck, state: dict[str, Any]) -> str:
    name = state.get("restaurant_name") or "this restaurant"
    lines = [
        f"Live menu check found changes at {name}. Do not finalize yet. "
        "Tell the user plainly and wait for a decision, then call "
        "confirm_order again with accept_changes=true if they still want "
        "to proceed (unavailable items will be dropped; prices update to live)."
    ]
    for item in check.unavailable:
        lines.append(
            f"- UNAVAILABLE: {item.name} x{item.qty} ({item.why}). "
            f"item_id={item.item_id}"
        )
    for change in check.price_changes:
        direction = "up" if change.unit_delta > 0 else "down"
        lines.append(
            f"- PRICE {direction}: {change.name} x{change.qty} was "
            f"PKR {change.old_price:,.0f} now PKR {change.new_price:,.0f} "
            f"(unit {change.unit_delta:+,.0f}, line {change.line_delta:+,.0f}). "
            f"item_id={change.item_id}"
        )
    remaining = accepted_cart(state, check)
    if remaining:
        totals = fees.estimate_fees(remaining)
        lines.append(
            f"If they proceed, remaining items total "
            f"PKR {totals['total']:,.0f} (incl. fees)."
        )
    else:
        lines.append("If they drop the unavailable items, the cart is empty.")
    return "\n".join(lines)
