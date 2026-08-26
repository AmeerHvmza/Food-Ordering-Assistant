"""Scrape restaurant listings for a lat/lng — API first, Playwright DOM fallback."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urljoin

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

import config
from scraper import api_client

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SELECTORS — swap these after inspecting the live DOM in DevTools.
# Foodpanda markup changes often; keep all listing selectors in this block.
# ---------------------------------------------------------------------------
LISTING_CARD_SELECTOR = "ul.vendor-list li a, a[href*='/restaurant/']"
LISTING_NAME_SELECTOR = ".name, [data-testid='vendor-name'], .vendor-name"
LISTING_RATING_SELECTOR = ".rating, [data-testid='vendor-rating'], .b-rating"
LISTING_CUISINE_SELECTOR = ".cuisines, [data-testid='vendor-cuisines'], .vendor-characteristic"
LISTING_ADDRESS_SELECTOR = ".address, [data-testid='vendor-address']"
LISTING_DELIVERY_TIME_SELECTOR = ".delivery-time, [data-testid='vendor-delivery-time'], .extra-info"
# ---------------------------------------------------------------------------

# Disco API sort value that matches Foodpanda's "Top rated restaurants" ordering.
TOP_RATED_SORT = "rating_desc"


def get_restaurants(
    lat: float,
    lng: float,
    count: int = 15,
    page: Page | None = None,
    top_rated: bool = True,
) -> list[dict[str, Any]]:
    """
    Fetch restaurants for a location.

    When top_rated=True (default), request the disco listing with
    sort=rating_desc so results match the homepage "Top rated restaurants"
    carousel, then take the top `count`.
    """
    return scrape_listings(
        lat=lat,
        lng=lng,
        count=count,
        page=page,
        top_rated=top_rated,
    )


def scrape_listings(
    lat: float,
    lng: float,
    count: int,
    page: Page | None = None,
    top_rated: bool = True,
) -> list[dict[str, Any]]:
    """
    Return up to `count` restaurant dicts for the given coordinates.

    Tries the disco JSON API first; falls back to Playwright DOM if needed.
    """
    sort = TOP_RATED_SORT if top_rated else None
    # Fetch a wider page so client-side rating sort still has enough candidates
    # if the API sort param is ignored on some regions.
    fetch_limit = max(count, 48) if top_rated else max(count, 48)

    api_results = api_client.fetch_vendors(
        lat, lng, limit=fetch_limit, offset=0, sort=sort
    )
    if api_results:
        if top_rated:
            api_results = _sort_by_rating(api_results)
            logger.info(
                "Top-rated mode: using sort=%s + local rating sort; taking top %s",
                TOP_RATED_SORT,
                count,
            )
        return api_results[:count]

    logger.warning("Listing API unavailable; falling back to Playwright DOM")
    if page is None:
        logger.error("No Playwright page available for listing DOM fallback")
        return []

    restaurants = _scrape_listings_dom(page, lat, lng, max(count, 48))
    if top_rated:
        restaurants = _sort_by_rating(restaurants)
    return restaurants[:count]


def _sort_by_rating(restaurants: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort by rating desc, then review_number desc when available."""
    return sorted(
        restaurants,
        key=lambda r: (
            r.get("rating") is not None,
            float(r["rating"]) if r.get("rating") is not None else -1.0,
            int(r["review_number"]) if r.get("review_number") is not None else 0,
        ),
        reverse=True,
    )


def _scrape_listings_dom(
    page: Page,
    lat: float,
    lng: float,
    count: int,
) -> list[dict[str, Any]]:
    """Parse restaurant cards from the listing page DOM."""
    url = config.listing_page_url(lat, lng)
    restaurants: list[dict[str, Any]] = []
    seen_urls: set[str] = set()

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=config.NAV_TIMEOUT_MS)
        page.wait_for_timeout(2_000)
        try:
            page.wait_for_selector(
                LISTING_CARD_SELECTOR,
                timeout=config.NAV_TIMEOUT_MS,
            )
        except PlaywrightTimeoutError:
            logger.error(
                "Listing cards not found (selector=%s). "
                "Page may be blocked or selectors need updating.",
                LISTING_CARD_SELECTOR,
            )
            return []

        for _ in range(5):
            page.mouse.wheel(0, 3000)
            page.wait_for_timeout(800)

        cards = page.locator(LISTING_CARD_SELECTOR)
        total = cards.count()
        logger.info("DOM listing found %s candidate links", total)

        for i in range(total):
            if len(restaurants) >= count:
                break
            try:
                card = cards.nth(i)
                href = card.get_attribute("href") or ""
                if "/restaurant/" not in href:
                    continue
                full_url = urljoin(config.BASE_URL + "/", href)
                if full_url in seen_urls:
                    continue
                seen_urls.add(full_url)

                name = _safe_text(card, LISTING_NAME_SELECTOR) or card.inner_text()[:80]
                rating_text = _safe_text(card, LISTING_RATING_SELECTOR)
                rating = _parse_rating(rating_text)
                cuisine = _safe_text(card, LISTING_CUISINE_SELECTOR)
                address = _safe_text(card, LISTING_ADDRESS_SELECTOR)
                delivery_time = _safe_text(card, LISTING_DELIVERY_TIME_SELECTOR)

                restaurants.append(
                    {
                        "code": api_client.extract_vendor_code(full_url),
                        "name": (name or "Unknown").strip(),
                        "url": full_url,
                        "rating": rating,
                        "cuisine": cuisine,
                        "address": address,
                        "delivery_time": delivery_time,
                        "image_url": None,
                    }
                )
            except Exception as exc:
                logger.debug("Skipping listing card %s: %s", i, exc)
                continue
    except PlaywrightTimeoutError as exc:
        logger.error("Timeout loading listing page: %s", exc)
    except Exception as exc:
        logger.error("DOM listing scrape failed: %s", exc)

    logger.info("DOM listing collected %s restaurants", len(restaurants))
    return restaurants[:count]


def _safe_text(locator, selector: str) -> str | None:
    try:
        el = locator.locator(selector).first
        if el.count() == 0:
            return None
        text = el.inner_text(timeout=2_000)
        return text.strip() if text else None
    except Exception:
        return None


def _parse_rating(text: str | None) -> float | None:
    if not text:
        return None
    try:
        token = text.strip().split()[0].replace(",", ".")
        return float(token)
    except (ValueError, IndexError):
        return None
