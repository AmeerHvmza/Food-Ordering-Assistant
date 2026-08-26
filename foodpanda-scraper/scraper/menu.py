"""Scrape a single restaurant's full menu — API first, Playwright DOM fallback."""

from __future__ import annotations

import logging
from typing import Any

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

import config
from scraper import api_client
from scraper.prices import parse_price_parts

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SELECTORS — swap these after inspecting a restaurant page in DevTools.
# Keep all menu DOM selectors in this block for easy maintenance.
# ---------------------------------------------------------------------------
MENU_CATEGORY_SELECTOR = "[data-testid='menu-category'], .dish-category-header, section.menu-category"
MENU_CATEGORY_NAME_SELECTOR = "h2, h3, .category-name, [data-testid='menu-category-name']"
MENU_ITEM_SELECTOR = "[data-testid='menu-product'], .dish-card, li.menu-item, article.product"
MENU_ITEM_NAME_SELECTOR = ".dish-name, .product-name, [data-testid='menu-product-name'], h3, h4"
MENU_ITEM_PRICE_SELECTOR = ".price, .product-price, [data-testid='menu-product-price']"
MENU_ITEM_DESC_SELECTOR = ".dish-description, .product-description, [data-testid='menu-product-description'], p"
MENU_ITEM_IMAGE_SELECTOR = "img"
# ---------------------------------------------------------------------------


def scrape_menu(
    restaurant: dict[str, Any],
    page: Page | None = None,
    lat: float | None = None,
    lng: float | None = None,
) -> list[dict[str, Any]]:
    """
    Return menu categories for one restaurant.

    Each category: {"category_name": str, "items": [item dicts]}.
    Tries fd-api first; falls back to Playwright DOM if needed.
    """
    code = restaurant.get("code") or api_client.extract_vendor_code(
        restaurant.get("url") or ""
    )
    categories = api_client.fetch_menu(code or "", lat=lat, lng=lng)
    if categories is not None:
        item_total = sum(len(c.get("items") or []) for c in categories)
        logger.info(
            "API menu for %s: %s categories, %s items",
            restaurant.get("name"),
            len(categories),
            item_total,
        )
        return categories

    logger.warning(
        "Menu API unavailable for %s; falling back to Playwright DOM",
        restaurant.get("name"),
    )
    if page is None:
        logger.error("No Playwright page available for menu DOM fallback")
        return []

    return _scrape_menu_dom(page, restaurant)


def _scrape_menu_dom(
    page: Page,
    restaurant: dict[str, Any],
) -> list[dict[str, Any]]:
    """Parse categories and items from the restaurant menu page DOM."""
    url = restaurant.get("url")
    if not url:
        return []

    categories: list[dict[str, Any]] = []

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=config.NAV_TIMEOUT_MS)
        page.wait_for_timeout(2_000)

        try:
            page.wait_for_selector(
                f"{MENU_CATEGORY_SELECTOR}, {MENU_ITEM_SELECTOR}",
                timeout=config.NAV_TIMEOUT_MS,
            )
        except PlaywrightTimeoutError:
            logger.error(
                "Menu content not found on %s (selectors may need updating "
                "or page blocked).",
                url,
            )
            return []

        # Scroll to trigger lazy-loaded sections
        for _ in range(8):
            page.mouse.wheel(0, 2500)
            page.wait_for_timeout(500)

        cat_locators = page.locator(MENU_CATEGORY_SELECTOR)
        cat_count = cat_locators.count()
        logger.info("DOM menu categories found: %s", cat_count)

        if cat_count > 0:
            for i in range(cat_count):
                try:
                    section = cat_locators.nth(i)
                    name_el = section.locator(MENU_CATEGORY_NAME_SELECTOR).first
                    cat_name = "Uncategorized"
                    try:
                        if name_el.count() > 0:
                            cat_name = (name_el.inner_text(timeout=2_000) or "").strip() or cat_name
                    except Exception:
                        pass

                    items = _parse_items(section)
                    if items:
                        categories.append(
                            {"category_name": cat_name, "items": items}
                        )
                except Exception as exc:
                    logger.debug("Skipping menu category %s: %s", i, exc)
        else:
            # Flat item list without clear category wrappers
            items = _parse_items(page)
            if items:
                categories.append({"category_name": "Menu", "items": items})

    except PlaywrightTimeoutError as exc:
        logger.error("Timeout loading menu page %s: %s", url, exc)
    except Exception as exc:
        logger.error("DOM menu scrape failed for %s: %s", url, exc)

    item_total = sum(len(c.get("items") or []) for c in categories)
    logger.info(
        "DOM menu for %s: %s categories, %s items",
        restaurant.get("name"),
        len(categories),
        item_total,
    )
    return categories


def _parse_items(scope) -> list[dict[str, Any]]:
    """Extract menu items from a Playwright locator/page scope."""
    items: list[dict[str, Any]] = []
    try:
        item_locs = scope.locator(MENU_ITEM_SELECTOR)
        count = item_locs.count()
    except Exception:
        return items

    for i in range(count):
        try:
            item = item_locs.nth(i)
            name = _child_text(item, MENU_ITEM_NAME_SELECTOR)
            if not name:
                continue
            raw_price = _child_text(item, MENU_ITEM_PRICE_SELECTOR)
            price, original_price = parse_price_parts(raw_price)
            if price is None:
                continue
            description = _child_text(item, MENU_ITEM_DESC_SELECTOR)
            image_url = None
            try:
                img = item.locator(MENU_ITEM_IMAGE_SELECTOR).first
                if img.count() > 0:
                    image_url = img.get_attribute("src") or img.get_attribute(
                        "data-src"
                    )
            except Exception:
                pass

            items.append(
                {
                    "name": name,
                    "price": price,
                    "original_price": original_price,
                    "description": description,
                    "image_url": image_url,
                }
            )
        except Exception as exc:
            logger.debug("Skipping menu item %s: %s", i, exc)
    return items


def _child_text(locator, selector: str) -> str | None:
    try:
        el = locator.locator(selector).first
        if el.count() == 0:
            return None
        text = el.inner_text(timeout=2_000)
        return text.strip() if text else None
    except Exception:
        return None
