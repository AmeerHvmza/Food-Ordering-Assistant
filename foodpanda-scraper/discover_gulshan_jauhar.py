"""Discover all restaurants that deliver to Gulshan-e-Iqbal and Gulistan-e-Jauhar.

No review-count or rating cutoff. Does not modify existing restaurant rows
or IDs. Menus go through scrape_menu → fetch_menu → normalize_menu
(extract_item_prices). See plans/GULSHAN_JAUHAR_PLAN.md.
"""

from __future__ import annotations

import argparse
import logging
import random
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

import config
from scraperdb.database import (
    count_menu_items,
    count_restaurants,
    get_connection,
    init_db,
    insert_restaurant_with_menu,
)
from scraper import api_client, areas
from scraper.menu import scrape_menu

logger = logging.getLogger("discover_gulshan_jauhar")

LISTING_LIMIT = 48
MAX_DEFAULT_PAGES = 15

# Two pins per area. Disco returns vendors that deliver to the point.
PINS: list[tuple[str, float, float]] = areas.GULSHAN_JAUHAR_PINS

# Extra Jauhar pins (not re-crawling Gulshan or the two original Jauhar pins).
EXTRA_JAUHAR_PINS: list[tuple[str, float, float]] = areas.EXTRA_JAUHAR_PINS

DRY_RUN_PATH = Path(__file__).resolve().parent / "gulshan_jauhar_dry_run.txt"
ALL_PIN_LABELS = [name for name, _lat, _lng in PINS + EXTRA_JAUHAR_PINS]


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


def _ingest_page(
    by_code: dict[str, dict[str, Any]],
    vendors: list[dict[str, Any]] | None,
    pin_name: str,
    lat: float,
    lng: float,
) -> int:
    """Add new codes from one listing page. Returns how many vendors the API sent."""
    if not vendors:
        return 0
    for vendor in vendors:
        code = (vendor.get("code") or "").lower()
        if not code or code in by_code:
            continue
        record = dict(vendor)
        record["latitude"] = vendor.get("latitude") or lat
        record["longitude"] = vendor.get("longitude") or lng
        record["discovered_from"] = pin_name
        by_code[code] = record
    return len(vendors)


def parse_dry_run_line(line: str) -> dict[str, Any] | None:
    text = line.strip()
    if not text or text.startswith("Would insert") or text.startswith("reviews"):
        return None
    reviews_s, rest = text.split(None, 1)
    url_idx = rest.rfind("  http")
    if url_idx < 0:
        url_idx = rest.rfind(" http")
    if url_idx < 0:
        return None
    left, url = rest[:url_idx].rstrip(), rest[url_idx:].strip()
    pin_name = None
    remainder = left
    for label in sorted(ALL_PIN_LABELS, key=len, reverse=True):
        prefix = f"{label}  "
        if remainder.startswith(prefix):
            pin_name = label
            remainder = remainder[len(prefix) :]
            break
    if pin_name is None or "  " not in remainder:
        return None
    name, code = remainder.rsplit("  ", 1)
    try:
        reviews = int(reviews_s)
    except ValueError:
        reviews = 0
    return {
        "review_number": reviews,
        "discovered_from": pin_name,
        "name": name,
        "code": code,
        "url": url,
    }


def load_dry_run(path: Path) -> dict[str, dict[str, Any]]:
    by_code: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return by_code
    for line in path.read_text(encoding="utf-8").splitlines():
        vendor = parse_dry_run_line(line)
        if not vendor:
            continue
        code = (vendor.get("code") or "").lower()
        if code:
            by_code[code] = vendor
    return by_code


def discover(
    pins: list[tuple[str, float, float]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Vendors that deliver to the given pins, keyed by lowercase code."""
    by_code: dict[str, dict[str, Any]] = {}
    for pin_name, lat, lng in pins or PINS:
        before = len(by_code)
        for page in range(MAX_DEFAULT_PAGES):
            offset = page * LISTING_LIMIT
            vendors = api_client.fetch_vendors(
                lat, lng, limit=LISTING_LIMIT, offset=offset, sort=None
            )
            _rate_limit()
            n = _ingest_page(by_code, vendors, pin_name, lat, lng)
            if n < LISTING_LIMIT:
                break
        vendors = api_client.fetch_vendors(
            lat, lng, limit=LISTING_LIMIT, offset=0, sort="rating_desc"
        )
        _rate_limit()
        _ingest_page(by_code, vendors, pin_name, lat, lng)
        logger.info(
            "%s: +%s unique (pool=%s)",
            pin_name,
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
        description=(
            "Append restaurants that deliver to Gulshan-e-Iqbal and "
            "Gulistan-e-Jauhar (no review cutoff)."
        ),
    )
    parser.add_argument("--db-path", default=config.DEFAULT_DB_PATH)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Discover and print the insert list; do not fetch menus.",
    )
    parser.add_argument(
        "--extra-jauhar-only",
        action="store_true",
        help=(
            "Crawl only the extra Jauhar pins. Merge with --merge-dry-run "
            "(default: gulshan_jauhar_dry_run.txt). Do not re-crawl Gulshan "
            "or the original two Jauhar pins."
        ),
    )
    parser.add_argument(
        "--merge-dry-run",
        default=str(DRY_RUN_PATH),
        help="Previous dry-run file to keep and dedup against.",
    )
    parser.add_argument(
        "--from-dry-run",
        default=None,
        help="Skip disco crawl; scrape menus for vendors listed in this file.",
    )
    return parser.parse_args(argv)


def pin_coordinates() -> dict[str, tuple[float, float]]:
    return {name: (lat, lng) for name, lat, lng in PINS + EXTRA_JAUHAR_PINS}


def attach_pin_coords(vendors: dict[str, dict[str, Any]]) -> None:
    coords = pin_coordinates()
    for vendor in vendors.values():
        lat_lng = coords.get(vendor.get("discovered_from") or "")
        if not lat_lng:
            continue
        vendor["latitude"] = vendor.get("latitude") or lat_lng[0]
        vendor["longitude"] = vendor.get("longitude") or lat_lng[1]


def hydrate_listing_fields(vendor: dict[str, Any]) -> bool:
    """Fill address/rating/cuisine/eta/image from fd-api.

    A dry-run line only carries name, code, url, reviews and pin, so a vendor
    rebuilt from that file would otherwise insert with those columns NULL and
    become invisible to the agent's area search.
    """
    if vendor.get("address") and vendor.get("rating") is not None:
        return True
    meta = api_client.fetch_vendor_meta(
        vendor.get("code") or "",
        lat=vendor.get("latitude"),
        lng=vendor.get("longitude"),
    )
    if not meta:
        logger.warning("No listing meta for %s", vendor.get("name"))
        return False
    for field in ("rating", "cuisine", "address", "delivery_time", "image_url"):
        if not vendor.get(field):
            vendor[field] = meta.get(field)
    if not vendor.get("review_number"):
        vendor["review_number"] = meta.get("review_number")
    return True


def run(args: argparse.Namespace) -> int:
    started = time.perf_counter()
    conn = get_connection(args.db_path)
    init_db(conn)
    already = existing_vendor_codes(conn)
    baseline_restaurants = count_restaurants(conn)
    baseline_items = count_menu_items(conn)
    logger.info(
        "Starting Gulshan/Jauhar discovery existing=%s items=%s",
        baseline_restaurants,
        baseline_items,
    )

    seed: dict[str, dict[str, Any]] = {}
    new_codes: list[str] = []
    if args.from_dry_run:
        pool = load_dry_run(Path(args.from_dry_run))
        attach_pin_coords(pool)
        logger.info(
            "Loaded %s vendors from %s (no disco crawl)",
            len(pool),
            args.from_dry_run,
        )
    elif args.extra_jauhar_only:
        seed = load_dry_run(Path(args.merge_dry_run))
        logger.info(
            "Loaded %s vendors from %s; crawling %s extra Jauhar pins only",
            len(seed),
            args.merge_dry_run,
            len(EXTRA_JAUHAR_PINS),
        )
        fresh = discover(EXTRA_JAUHAR_PINS)
        new_codes = [code for code in fresh if code not in seed]
        pool = dict(seed)
        for code in new_codes:
            pool[code] = fresh[code]
        logger.info(
            "Extra Jauhar pins added %s new codes (%s already in prior dry-run)",
            len(new_codes),
            len(fresh) - len(new_codes),
        )
    else:
        pool = discover(PINS)

    if args.from_dry_run:
        # Keep the dry-run file order (used to defer recent 403s to the end).
        targets = [v for code, v in pool.items() if code not in already]
    else:
        targets = [
            v
            for code, v in sorted(pool.items(), key=lambda kv: kv[1].get("name") or "")
            if code not in already
        ]
    skipped_existing = sum(1 for code in pool if code in already)
    logger.info(
        "Discovery: %s unique vendors, %s already in DB, %s to insert",
        len(pool),
        skipped_existing,
        len(targets),
    )
    if args.dry_run:
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
        header = (
            f"Would insert {len(targets)} restaurants "
            f"(existing {baseline_restaurants} untouched; "
            f"{skipped_existing} overlapping URLs skipped; "
            f"pool {len(pool)})"
        )
        print(header)
        print("Pin breakdown (would-insert):")
        pin_counts = Counter(v.get("discovered_from") or "?" for v in targets)
        for pin, n in pin_counts.most_common():
            print(f"  {n:4}  {pin}")
        if args.extra_jauhar_only:
            extra_labels = {name for name, _lat, _lng in EXTRA_JAUHAR_PINS}
            extra_new = [
                v for v in targets if v.get("discovered_from") in extra_labels
            ]
            print(
                f"New from extra Jauhar pins after dedup vs prior dry-run + DB: "
                f"{len(extra_new)}"
            )
        print("reviews  pin  name  code  url")
        lines = [header, "reviews  pin  name  code  url"]
        for vendor in targets:
            line = (
                f"{_review_count(vendor):6}  "
                f"{vendor.get('discovered_from')}  "
                f"{vendor.get('name')}  "
                f"{vendor.get('code')}  "
                f"{vendor.get('url')}"
            )
            print(line)
            lines.append(line)
        report = Path(__file__).resolve().parent / "gulshan_jauhar_dry_run.txt"
        report.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"Wrote {report}")
        conn.close()
        return 0

    inserted = 0
    failed = 0
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
                if api_client.LAST_MENU_STATUS == 403:
                    break
                logger.warning("Retry %s for %s", attempt, name)
                _rate_limit()
            item_total = sum(len(c.get("items") or []) for c in categories)
            if not categories or item_total == 0:
                logger.error("No menu for %s — not inserting", name)
                failed += 1
                if api_client.LAST_MENU_STATUS == 403:
                    blocked += 1
                    if blocked >= 5:
                        logger.error(
                            "fd-api returned 403 five times (PerimeterX). "
                            "Stopping so the block does not last longer. "
                            "Re-run: python discover_gulshan_jauhar.py"
                        )
                        break
                else:
                    blocked = 0
                _rate_limit()
                continue
            blocked = 0
            if args.from_dry_run:
                hydrate_listing_fields(restaurant)
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
