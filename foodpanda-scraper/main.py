"""CLI entry point: scrape Foodpanda restaurants + menus into SQLite."""

from __future__ import annotations

import argparse
import logging
import random
import sys
import time
from typing import Any

from playwright.sync_api import sync_playwright

import config
from scraperdb.database import (
    clear_all_data,
    count_menu_items,
    get_connection,
    init_db,
    insert_restaurant_with_menu,
    restaurant_exists,
)
from scraper.listing import get_restaurants
from scraper.menu import scrape_menu

logger = logging.getLogger("foodpanda_scraper")


def _parse_bool(value: str) -> bool:
    """Parse CLI boolean values such as True/False/1/0."""
    if isinstance(value, bool):
        return value
    lowered = str(value).strip().lower()
    if lowered in {"1", "true", "yes", "y", "on"}:
        return True
    if lowered in {"0", "false", "no", "n", "off"}:
        return False
    raise argparse.ArgumentTypeError(f"Expected boolean, got {value!r}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scrape Foodpanda.pk restaurant listings and full menus into SQLite.",
    )
    parser.add_argument("--city", default=config.DEFAULT_CITY, help="City label (logging only)")
    parser.add_argument(
        "--lat",
        type=float,
        default=config.DEFAULT_LAT,
        help="Latitude for listing search",
    )
    parser.add_argument(
        "--lng",
        type=float,
        default=config.DEFAULT_LNG,
        help="Longitude for listing search",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=config.DEFAULT_COUNT,
        help="Number of restaurants to scrape (default: 15)",
    )
    parser.add_argument(
        "--db-path",
        default=config.DEFAULT_DB_PATH,
        help="SQLite database path (default: foodpanda.db)",
    )
    parser.add_argument(
        "--headless",
        type=_parse_bool,
        default=config.DEFAULT_HEADLESS,
        help="Run Playwright headless (default: True). Use --headless=False to debug.",
    )
    parser.add_argument(
        "--top-rated",
        type=_parse_bool,
        default=True,
        help="Target Top rated restaurants (sort=rating_desc). Default: True.",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="Clear existing DB rows before scraping (needed when switching ranking).",
    )
    return parser.parse_args(argv)


def setup_logging() -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    formatter = logging.Formatter(config.LOG_FORMAT)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    file_handler = logging.FileHandler(config.LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)


def _rate_limit() -> None:
    delay = random.uniform(config.MIN_DELAY_SEC, config.MAX_DELAY_SEC)
    logger.debug("Sleeping %.2fs", delay)
    time.sleep(delay)


def run(args: argparse.Namespace) -> int:
    logger.info(
        "Starting scrape city=%s lat=%s lng=%s count=%s db=%s headless=%s top_rated=%s",
        args.city,
        args.lat,
        args.lng,
        args.count,
        args.db_path,
        args.headless,
        args.top_rated,
    )

    conn = get_connection(args.db_path)
    init_db(conn)
    if args.fresh:
        clear_all_data(conn)

    browser = None
    context = None
    page = None
    playwright_cm = None

    def ensure_page():
        """Lazily launch a single reusable Playwright browser/page."""
        nonlocal browser, context, page, playwright_cm
        if page is not None:
            return page
        logger.info("Launching Playwright (headless=%s)", args.headless)
        playwright_cm = sync_playwright().start()
        browser = playwright_cm.chromium.launch(headless=args.headless)
        context = browser.new_context(
            user_agent=config.USER_AGENT,
            locale="en-PK",
            viewport={"width": 1400, "height": 900},
        )
        page = context.new_page()
        page.set_default_timeout(config.NAV_TIMEOUT_MS)
        return page

    scraped_ok = 0
    skipped_existing = 0
    failed = 0
    requested = args.count

    try:
        restaurants = get_restaurants(
            lat=args.lat,
            lng=args.lng,
            count=args.count,
            page=None,
            top_rated=args.top_rated,
        )
        if not restaurants:
            logger.warning("API listing empty; retrying with Playwright")
            restaurants = get_restaurants(
                lat=args.lat,
                lng=args.lng,
                count=args.count,
                page=ensure_page(),
                top_rated=args.top_rated,
            )

        if not restaurants:
            logger.error("No restaurants found for the given location")
            print(f"Scraped 0/{requested} restaurants")
            print("Total menu items: 0")
            print(f"Database: {args.db_path}")
            return 1

        logger.info("Processing %s restaurants", len(restaurants))

        for idx, restaurant in enumerate(restaurants, start=1):
            name = restaurant.get("name") or "Unknown"
            url = restaurant.get("url") or ""
            logger.info("[%s/%s] Scraping %s (%s)", idx, len(restaurants), name, url)

            try:
                if url and restaurant_exists(conn, url):
                    logger.info("Already in DB, skipping: %s", url)
                    skipped_existing += 1
                    scraped_ok += 1  # counts toward requested coverage on re-run
                    _rate_limit()
                    continue

                categories: list[dict[str, Any]] = []
                try:
                    categories = scrape_menu(
                        restaurant,
                        page=None,
                        lat=args.lat,
                        lng=args.lng,
                    )
                except Exception as exc:
                    logger.warning("API menu path error for %s: %s", name, exc)

                if not categories:
                    try:
                        categories = scrape_menu(
                            restaurant,
                            page=ensure_page(),
                            lat=args.lat,
                            lng=args.lng,
                        )
                    except Exception as exc:
                        logger.error("DOM menu fallback failed for %s: %s", name, exc)
                        failed += 1
                        _rate_limit()
                        continue

                item_total = sum(len(c.get("items") or []) for c in categories)
                logger.info("Found %s menu items for %s", item_total, name)

                if not url:
                    logger.error("Restaurant missing URL, skipping: %s", name)
                    failed += 1
                    _rate_limit()
                    continue

                insert_restaurant_with_menu(conn, restaurant, categories)
                scraped_ok += 1
            except Exception as exc:
                logger.exception("Failed to scrape restaurant %s: %s", name, exc)
                failed += 1
            finally:
                _rate_limit()

    finally:
        if context is not None:
            context.close()
        if browser is not None:
            browser.close()
        if playwright_cm is not None:
            playwright_cm.stop()
        total_items = count_menu_items(conn)
        conn.close()

    print(f"Scraped {scraped_ok}/{requested} restaurants")
    print(f"Total menu items: {total_items}")
    print(f"Database: {args.db_path}")
    if skipped_existing:
        logger.info("Skipped %s already-stored restaurants", skipped_existing)
    if failed:
        logger.info("Failed restaurants: %s", failed)

    return 0 if scraped_ok > 0 else 1


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    args = parse_args(argv)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
