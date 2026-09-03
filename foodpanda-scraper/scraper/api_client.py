"""Direct HTTP clients for Foodpanda internal JSON APIs."""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

import requests

import config
from scraper.prices import extract_item_prices

logger = logging.getLogger(__name__)

# Last HTTP status from fetch_menu (so callers can stop on PerimeterX 403).
LAST_MENU_STATUS: int | None = None

# Rotate when PerimeterX fingerprints a single app UA (Android then iOS
# each lasted ~45 menus this scrape). Disco listing keeps USER_AGENT.
_FD_API_USER_AGENTS = (
    "okhttp/4.12.0",
    "Mozilla/5.0 (Linux; Android 13; SM-G991B) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
)
_fd_api_ua_index = 0


def _perseus_ids() -> tuple[str, str]:
    """Generate pseudo Perseush client/session ids accepted by fd-api."""
    ts = int(time.time() * 1000)
    client = f"{ts}.{uuid.uuid4().int % 10**18}.web"
    session = f"{ts}.{uuid.uuid4().int % 10**18}.sess"
    return client, session


def _disco_headers() -> dict[str, str]:
    return {
        "User-Agent": config.USER_AGENT,
        "Accept": "application/json",
        "x-disco-client-id": "web",
    }


def _fd_api_headers() -> dict[str, str]:
    global _fd_api_ua_index
    client_id, session_id = _perseus_ids()
    # Desktop and Android UAs are PerimeterX-blocked on pk.fd-api.com.
    ua = _FD_API_USER_AGENTS[_fd_api_ua_index % len(_FD_API_USER_AGENTS)]
    _fd_api_ua_index += 1
    return {
        "User-Agent": ua,
        "Accept": "application/json, text/plain, */*",
        "X-FP-API-KEY": "volo",
        "perseus-client-id": client_id,
        "perseus-session-id": session_id,
        "x-pd-language-id": "1",
    }


def normalize_vendor(raw: dict[str, Any]) -> dict[str, Any]:
    """Map a disco/fd-api vendor payload to the scraper restaurant dict."""
    cuisines = raw.get("cuisines") or []
    cuisine_names = [
        c.get("name") for c in cuisines if isinstance(c, dict) and c.get("name")
    ]
    code = raw.get("code") or ""
    url_key = raw.get("url_key") or ""
    web_path = raw.get("web_path") or raw.get("redirection_url")
    if not web_path and code:
        slug = url_key or code
        web_path = f"{config.BASE_URL}/restaurant/{code}/{slug}"

    delivery_time = raw.get("minimum_delivery_time")
    if delivery_time is not None:
        delivery_time = f"{delivery_time} min"

    rating = raw.get("rating")
    try:
        rating = float(rating) if rating is not None else None
    except (TypeError, ValueError):
        rating = None

    image = raw.get("hero_listing_image") or raw.get("hero_image") or None
    if isinstance(image, str) and "%s" in image:
        image = image.replace("%s", "400")

    try:
        latitude = float(raw["latitude"]) if raw.get("latitude") is not None else None
    except (TypeError, ValueError):
        latitude = None
    try:
        longitude = float(raw["longitude"]) if raw.get("longitude") is not None else None
    except (TypeError, ValueError):
        longitude = None

    extra = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}

    return {
        "code": code,
        "name": raw.get("name") or "Unknown",
        "url": web_path,
        "rating": rating,
        "cuisine": ", ".join(cuisine_names) if cuisine_names else None,
        "address": raw.get("address") or raw.get("address_line2"),
        "delivery_time": delivery_time,
        "image_url": image,
        "review_number": raw.get("review_number"),
        "latitude": latitude,
        "longitude": longitude,
        # Additive; scraper insert ignores unknown keys. Used by the agent's
        # lock-time freshness check, not by the nightly snapshot write.
        "is_active": raw.get("is_active"),
        "is_busy": raw.get("is_busy"),
        "is_delivery_enabled": raw.get("is_delivery_enabled"),
        "is_delivery_available": extra.get("is_delivery_available"),
        "is_temporary_closed": extra.get("is_temporary_closed"),
    }


def normalize_menu(vendor_payload: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Parse menus from an fd-api vendor response into category dicts.

    Each category: {"category_name": str,
    "items": [{"name","price","original_price","description","image_url"}]}

    `price` is the payable (discounted/current) numeric string. Empty-price
    stubs and same-name duplicates in a category are dropped.
    """
    menus = vendor_payload.get("menus") or []
    categories: list[dict[str, Any]] = []

    for menu in menus:
        for cat in menu.get("menu_categories") or []:
            cat_name = cat.get("name") or "Uncategorized"
            items: list[dict[str, Any]] = []
            seen_names: set[str] = set()
            for product in cat.get("products") or []:
                name = (product.get("name") or "Unknown").strip() or "Unknown"
                if name in seen_names:
                    continue
                price, original_price = extract_item_prices(product)
                if price is None:
                    # Preview/stub product with no variation price and no
                    # parseable display_price — do not insert an empty row.
                    continue

                image = (
                    product.get("file_path")
                    or product.get("logo_path")
                    or None
                )
                if isinstance(image, str) and "%s" in image:
                    image = image.replace("%s", "400")

                items.append(
                    {
                        "name": name,
                        "price": price,
                        "original_price": original_price,
                        "description": product.get("description") or None,
                        "image_url": image,
                        "is_sold_out": bool(product.get("is_sold_out")),
                    }
                )
                seen_names.add(name)

            if items:
                categories.append({"category_name": cat_name, "items": items})

    return categories


def fetch_vendors(
    lat: float,
    lng: float,
    limit: int = 48,
    offset: int = 0,
    sort: str | None = None,
) -> list[dict[str, Any]] | None:
    """
    Fetch restaurant listings from the disco pandora vendors API.

    Returns normalized restaurant dicts, or None if the API is unusable.
    Pass sort='rating_desc' for the Top rated restaurants ordering.
    """
    params = {
        "latitude": str(lat),
        "longitude": str(lng),
        "language_id": "1",
        "include": "characteristics",
        "dynamic_pricing": "0",
        "configuration": "Variant3",
        "country": config.COUNTRY_CODE,
        "vertical": "restaurants",
        "limit": str(limit),
        "offset": str(offset),
        "customer_type": "regular",
    }
    if sort:
        params["sort"] = sort
    try:
        resp = requests.get(
            config.DISCO_VENDORS_URL,
            headers=_disco_headers(),
            params=params,
            timeout=config.REQUEST_TIMEOUT_SEC,
        )
        if resp.status_code != 200:
            logger.warning(
                "Listing API HTTP %s: %s",
                resp.status_code,
                resp.text[:200],
            )
            return None

        payload = resp.json()
        items = (payload.get("data") or {}).get("items")
        if not isinstance(items, list):
            logger.warning("Listing API response missing data.items")
            return None

        restaurants = [normalize_vendor(item) for item in items if item.get("code")]
        restaurants = [r for r in restaurants if r.get("url")]
        logger.info(
            "Listing API returned %s vendors (sort=%s)",
            len(restaurants),
            sort or "default",
        )
        return restaurants
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        logger.warning("Listing API failed: %s", exc)
        return None


def fetch_vendor_meta(
    vendor_code: str,
    lat: float | None = None,
    lng: float | None = None,
    timeout: float | None = None,
) -> dict[str, Any] | None:
    """
    Fetch listing-level fields for one vendor from pk.fd-api.com.

    Does not request menus. Used to backfill review_number for vendors
    missing from the disco listing feed.

    Records the HTTP status in LAST_MENU_STATUS so callers can stop on a
    PerimeterX 403 instead of hammering a live block.
    """
    global LAST_MENU_STATUS
    LAST_MENU_STATUS = None
    if not vendor_code:
        return None

    lat = lat if lat is not None else config.DEFAULT_LAT
    lng = lng if lng is not None else config.DEFAULT_LNG
    url = config.FD_API_VENDOR_URL.format(vendor_code=vendor_code)
    params = {
        "latitude": str(lat),
        "longitude": str(lng),
        "language_id": "1",
    }
    wait = timeout if timeout is not None else config.REQUEST_TIMEOUT_SEC
    try:
        resp = requests.get(
            url,
            headers=_fd_api_headers(),
            params=params,
            timeout=wait,
        )
        LAST_MENU_STATUS = resp.status_code
        if resp.status_code != 200:
            logger.warning(
                "Vendor meta HTTP %s for %s: %s",
                resp.status_code,
                vendor_code,
                resp.text[:200],
            )
            return None
        payload = resp.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict) or not data.get("code"):
            logger.warning("Vendor meta missing data for %s", vendor_code)
            return None
        return normalize_vendor(data)
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        logger.warning("Vendor meta failed for %s: %s", vendor_code, exc)
        return None


def fetch_menu(
    vendor_code: str,
    lat: float | None = None,
    lng: float | None = None,
    timeout: float | None = None,
) -> list[dict[str, Any]] | None:
    """
    Fetch a vendor menu via pk.fd-api.com (include=menus).

    Returns normalized category list, or None on failure / empty shape.
    """
    if not vendor_code:
        return None

    lat = lat if lat is not None else config.DEFAULT_LAT
    lng = lng if lng is not None else config.DEFAULT_LNG
    url = config.FD_API_VENDOR_URL.format(vendor_code=vendor_code)
    params = {
        "latitude": str(lat),
        "longitude": str(lng),
        "language_id": "1",
        "include": "menus",
    }

    # Also try the classic /menu path as a secondary candidate
    candidates = [
        (url, params),
        (
            f"{config.BASE_URL}/api/v5/vendors/{vendor_code}/menu",
            {"latitude": str(lat), "longitude": str(lng)},
        ),
    ]

    global LAST_MENU_STATUS
    LAST_MENU_STATUS = None
    saw_403 = False
    wait = timeout if timeout is not None else config.REQUEST_TIMEOUT_SEC
    for endpoint, query in candidates:
        try:
            resp = requests.get(
                endpoint,
                headers=_fd_api_headers(),
                params=query,
                timeout=wait,
            )
            LAST_MENU_STATUS = resp.status_code
            if resp.status_code == 403:
                saw_403 = True
                logger.warning(
                    "Menu API %s -> HTTP %s",
                    endpoint,
                    resp.status_code,
                )
                break
            if resp.status_code != 200:
                logger.warning(
                    "Menu API %s -> HTTP %s",
                    endpoint,
                    resp.status_code,
                )
                continue

            text = resp.text or ""
            if "captcha" in text.lower() or '"appId"' in text[:120]:
                logger.debug("Menu API blocked by bot protection: %s", endpoint)
                continue

            payload = resp.json()
            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, dict):
                # Some /menu endpoints nest under data differently
                data = payload if isinstance(payload, dict) else {}

            categories = normalize_menu(data)
            if not categories and "menus" not in data:
                # Try if payload itself is menu-shaped
                categories = normalize_menu({"menus": data.get("menus") or []})

            if categories:
                logger.info(
                    "Menu API OK for %s: %s categories via %s",
                    vendor_code,
                    len(categories),
                    endpoint,
                )
                return categories

            logger.debug("Menu API returned no categories for %s", vendor_code)
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            logger.debug("Menu API error for %s at %s: %s", vendor_code, endpoint, exc)

    if saw_403:
        LAST_MENU_STATUS = 403
    logger.warning("All menu API candidates failed for vendor %s", vendor_code)
    return None


def extract_vendor_code(url: str) -> str | None:
    """Extract vendor code from a Foodpanda restaurant URL."""
    # Expected: .../restaurant/{code}/{slug}
    parts = (url or "").rstrip("/").split("/")
    try:
        idx = parts.index("restaurant")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    except ValueError:
        pass
    return None
