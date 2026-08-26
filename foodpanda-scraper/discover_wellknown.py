"""Discover well-known Karachi restaurants and append menus to foodpanda.db.

Does not modify existing restaurant rows or IDs. Discovery filters on
review_number before any menu request. See plans/KARACHI_WELLKNOWN_PLAN.md.
"""

from __future__ import annotations

import argparse
import logging
import random
import sys
import time
from typing import Any

import config
from scraperdb.database import (
    count_menu_items,
    count_restaurants,
    get_connection,
    init_db,
    insert_restaurant_with_menu,
)
from scraper import api_client
from scraper.areas import WELLKNOWN_PINS
from scraper.menu import scrape_menu

logger = logging.getLogger("discover_wellknown")

REVIEW_CUTOFF = 3208
LISTING_PAGES = 3
LISTING_LIMIT = 48

AREAS: list[tuple[str, float, float]] = WELLKNOWN_PINS


def _rate_limit() -> None:
    time.sleep(random.uniform(config.MIN_DELAY_SEC, config.MAX_DELAY_SEC))


def _review_count(vendor: dict[str, Any]) -> int:
    raw = vendor.get("review_number")
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


def setup_logging() -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    formatter = logging.Formatter(config.LOG_FORMAT)
    if not root.handlers:
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(formatter)
        root.addHandler(console)
        file_handler = logging.FileHandler(config.LOG_FILE, encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)


def existing_vendor_codes(conn) -> set[str]:
    codes: set[str] = set()
    for (url,) in conn.execute("SELECT url FROM restaurants"):
        code = api_client.extract_vendor_code(url or "")
        if code:
            codes.add(code.lower())
    return codes


def discover() -> dict[str, dict[str, Any]]:
    """Return vendors keyed by code, already filtered to REVIEW_CUTOFF."""
    by_code: dict[str, dict[str, Any]] = {}
    for area, lat, lng in AREAS:
        before = len(by_code)
        for sort in (None, "rating_desc"):
            pages = LISTING_PAGES if sort is None else 1
            for page in range(pages):
                offset = page * LISTING_LIMIT
                vendors = api_client.fetch_vendors(
                    lat, lng, limit=LISTING_LIMIT, offset=offset, sort=sort
                )
                _rate_limit()
                if not vendors:
                    break
                for vendor in vendors:
                    code = (vendor.get("code") or "").lower()
                    if not code or code in by_code:
                        continue
                    if _review_count(vendor) < REVIEW_CUTOFF:
                        continue
                    vendor = dict(vendor)
                    vendor["latitude"] = vendor.get("latitude") or lat
                    vendor["longitude"] = vendor.get("longitude") or lng
                    by_code[code] = vendor
                if len(vendors) < LISTING_LIMIT:
                    break
        logger.info(
            "%s: +%s well-known (pool=%s)",
            area,
            len(by_code) - before,
            len(by_code),
        )
    return by_code


def scrape_one_menu(
    restaurant: dict[str, Any],
    lat: float,
    lng: float,
) -> list[dict[str, Any]]:
    """API only. foodpanda.pk DOM is PerimeterX-blocked; Playwright cannot help."""
    try:
        categories = scrape_menu(restaurant, page=None, lat=lat, lng=lng)
    except Exception as exc:
        logger.warning("API menu error for %s: %s", restaurant.get("name"), exc)
        return []
    return categories or []


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Append well-known Karachi restaurants (review_count >= 3208).",
    )
    parser.add_argument("--db-path", default=config.DEFAULT_DB_PATH)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Discover and print the insert list; do not fetch menus.",
    )
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    started = time.perf_counter()
    conn = get_connection(args.db_path)
    init_db(conn)
    already = existing_vendor_codes(conn)
    baseline_restaurants = count_restaurants(conn)
    baseline_items = count_menu_items(conn)
    logger.info(
        "Starting well-known discovery cutoff=%s existing=%s items=%s",
        REVIEW_CUTOFF,
        baseline_restaurants,
        baseline_items,
    )

    pool = discover()
    targets = [
        v for code, v in sorted(pool.items(), key=lambda kv: -_review_count(kv[1]))
        if code not in already
    ]
    skipped_existing = sum(1 for code in pool if code in already)
    logger.info(
        "Discovery: %s at cutoff, %s already in DB, %s to insert",
        len(pool),
        skipped_existing,
        len(targets),
    )
    if args.dry_run:
        for vendor in targets:
            print(
                f"{_review_count(vendor):6}  {vendor.get('name')}  "
                f"{vendor.get('code')}  {vendor.get('url')}"
            )
        print(f"Would insert {len(targets)} restaurants (existing {baseline_restaurants} untouched)")
        conn.close()
        return 0

    inserted = 0
    failed = 0
    empty = 0
    blocked = 0
    try:
        for idx, restaurant in enumerate(targets, start=1):
            name = restaurant.get("name") or "Unknown"
            lat = restaurant.get("latitude") or config.DEFAULT_LAT
            lng = restaurant.get("longitude") or config.DEFAULT_LNG
            logger.info(
                "[%s/%s] %s (%s reviews) @ %s,%s",
                idx,
                len(targets),
                name,
                _review_count(restaurant),
                lat,
                lng,
            )
            categories: list[dict[str, Any]] = []
            for attempt in (1, 2):
                categories = scrape_one_menu(restaurant, lat, lng)
                if categories:
                    break
                logger.warning("Retry %s for %s", attempt, name)
                _rate_limit()
            item_total = sum(len(c.get("items") or []) for c in categories)
            if not categories or item_total == 0:
                logger.error("No menu for %s — not inserting", name)
                empty += 1
                failed += 1
                if api_client.LAST_MENU_STATUS == 403:
                    blocked += 1
                    if blocked >= 5:
                        logger.error(
                            "fd-api returned 403 five times (PerimeterX). "
                            "Stopping so the block does not last longer. "
                            "Re-run: python discover_wellknown.py"
                        )
                        break
                else:
                    blocked = 0
                _rate_limit()
                continue
            blocked = 0
            new_id = insert_restaurant_with_menu(conn, restaurant, categories)
            if new_id is None:
                skipped_existing += 1
            else:
                inserted += 1
            _rate_limit()
    finally:
        restaurants = count_restaurants(conn)
        items = count_menu_items(conn)
        conn.close()

    elapsed = time.perf_counter() - started
    print(f"Inserted {inserted} new restaurants ({failed} failed/empty)")
    print(f"Skipped existing overlap: {skipped_existing}")
    print(f"Database now: {restaurants} restaurants, {items} menu items")
    print(f"Baseline was: {baseline_restaurants} restaurants, {baseline_items} items")
    print(f"Elapsed: {elapsed / 60:.1f} min")
    print(f"Database: {args.db_path}")
    return 0 if inserted or not targets else 1


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    return run(parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
