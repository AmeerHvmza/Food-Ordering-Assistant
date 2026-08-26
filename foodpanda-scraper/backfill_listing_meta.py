"""Backfill listing metadata for restaurants inserted without it.

The Gulshan/Jauhar menu pass ran from a dry-run text file, which only carries
name, code, url, review count and pin. Those rows landed with NULL address,
rating, cuisine, delivery_time and image_url, so the agent's area search
(address LIKE '%area%') could not see them.

Refetches listing fields from fd-api and fills only empty columns. Also
records the discovery area in `delivery_areas`, because a vendor's street
address is where its kitchen is, not the zone it delivers to.

Idempotent and resumable: rows that already have address + delivery_areas are
skipped, and existing values are never overwritten.
"""

from __future__ import annotations

import argparse
import logging
import random
import sys
import time
from pathlib import Path

import config
import discover_gulshan_jauhar as disco
from scraperdb.database import (
    fill_missing_listing_meta,
    get_connection,
    init_db,
    set_delivery_areas,
)
from scraper import api_client

logger = logging.getLogger("backfill_listing_meta")

BASELINE_MAX_ID = 84
MAX_CONSECUTIVE_403 = 5

# Gentler than the menu pass: this is a long tail of listing-only requests and
# fd-api starts 403ing after roughly 35-45 of them at the scrape delay.
MIN_DELAY_SEC = 3.0
MAX_DELAY_SEC = 6.0

# Fine-grained discovery pins collapse to the area names a user actually says.
PIN_TO_AREA = {
    "Gulshan-e-Iqbal NIPA / Block 5": "Gulshan-e-Iqbal",
    "Gulshan-e-Iqbal Hasan Square": "Gulshan-e-Iqbal",
    "Gulistan-e-Jauhar Chowk": "Gulistan-e-Jauhar",
    "Gulistan-e-Jauhar east / Block 18-19": "Gulistan-e-Jauhar",
    "Gulistan-e-Jauhar Johar Mor / Block 19": "Gulistan-e-Jauhar",
    "Gulistan-e-Jauhar Rabia City / Block 18": "Gulistan-e-Jauhar",
    "Gulistan-e-Jauhar north / Blocks 1-4": "Gulistan-e-Jauhar",
}

DRY_RUN_FILES = (
    "gulshan_jauhar_dry_run.txt",
    "gulshan_jauhar_dry_run_247.txt",
)


def _rate_limit() -> None:
    time.sleep(random.uniform(MIN_DELAY_SEC, MAX_DELAY_SEC))


def load_pin_map() -> dict[str, str]:
    """code -> discovery pin label, from the dry-run files."""
    pins: dict[str, str] = {}
    here = Path(__file__).resolve().parent
    for filename in DRY_RUN_FILES:
        path = here / filename
        for code, vendor in disco.load_dry_run(path).items():
            pins.setdefault(code, vendor.get("discovered_from") or "")
    return pins


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default=config.DEFAULT_DB_PATH)
    parser.add_argument(
        "--min-id",
        type=int,
        default=BASELINE_MAX_ID,
        help="Only touch restaurants with id greater than this.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what is missing; do not call the API or write.",
    )
    parser.add_argument(
        "--from-disco",
        action="store_true",
        help=(
            "Read listing fields from the disco listing feed instead of "
            "per-vendor fd-api calls. disco.deliveryhero.io uses different "
            "headers and stays available when pk.fd-api.com is 403ing."
        ),
    )
    return parser.parse_args(argv)


def disco_listing_pool() -> dict[str, dict]:
    """Listing fields for every vendor at the Gulshan/Jauhar pins, by code."""
    pins = disco.PINS + disco.EXTRA_JAUHAR_PINS
    logger.info("Crawling %s pins via disco listing feed", len(pins))
    pool = disco.discover(pins)
    logger.info("disco returned %s unique vendors", len(pool))
    return pool


def run(args: argparse.Namespace) -> int:
    conn = get_connection(args.db_path)
    init_db(conn)
    pin_map = load_pin_map()
    coords = disco.pin_coordinates()

    rows = conn.execute(
        """
        SELECT id, name, url, address, delivery_areas
        FROM restaurants
        WHERE id > ?
        ORDER BY id
        """,
        (args.min_id,),
    ).fetchall()

    pending = [
        row
        for row in rows
        if not (row["address"] or "").strip()
        or not (row["delivery_areas"] or "").strip()
    ]
    logger.info(
        "%s restaurants above id %s, %s need backfill",
        len(rows),
        args.min_id,
        len(pending),
    )
    if args.dry_run:
        for row in pending[:20]:
            code = api_client.extract_vendor_code(row["url"] or "") or "?"
            logger.info(
                "  id=%s %s code=%s pin=%s",
                row["id"],
                row["name"],
                code,
                pin_map.get(code.lower(), "unknown"),
            )
        conn.close()
        return 0

    listing_pool = disco_listing_pool() if args.from_disco else {}

    filled = 0
    areas_set = 0
    failed = 0
    blocked = 0
    try:
        for idx, row in enumerate(pending, start=1):
            code = (api_client.extract_vendor_code(row["url"] or "") or "").lower()
            pin = pin_map.get(code, "")
            area = PIN_TO_AREA.get(pin)
            lat, lng = coords.get(pin, (None, None))

            if area and not (row["delivery_areas"] or "").strip():
                set_delivery_areas(conn, row["id"], area)
                areas_set += 1

            if (row["address"] or "").strip():
                continue

            meta = listing_pool.get(code)
            if meta is None and not args.from_disco:
                meta = api_client.fetch_vendor_meta(code, lat=lat, lng=lng)
                if not meta:
                    failed += 1
                    if api_client.LAST_MENU_STATUS == 403:
                        blocked += 1
                        if blocked >= MAX_CONSECUTIVE_403:
                            logger.error(
                                "fd-api 403 x%s (PerimeterX). Stopping; "
                                "retry with --from-disco.",
                                blocked,
                            )
                            break
                    _rate_limit()
                    continue
            if meta is None:
                logger.warning(
                    "id=%s %s (%s) not in the disco feed", row["id"], row["name"], code
                )
                failed += 1
                continue

            blocked = 0
            written = fill_missing_listing_meta(conn, row["id"], meta)
            if written:
                filled += 1
            logger.info(
                "[%s/%s] id=%s %s -> %s | area=%s",
                idx,
                len(pending),
                row["id"],
                row["name"],
                ", ".join(written) or "nothing missing",
                area or "unknown",
            )
            if not args.from_disco:
                _rate_limit()
    finally:
        remaining = conn.execute(
            "SELECT COUNT(*) AS n FROM restaurants "
            "WHERE id > ? AND (address IS NULL OR address = '')",
            (args.min_id,),
        ).fetchone()["n"]
        conn.close()

    print(f"Filled listing meta for {filled} restaurants")
    print(f"Set delivery_areas for {areas_set} restaurants")
    print(f"Meta fetch failures: {failed}")
    print(f"Still missing address: {remaining}")
    return 0 if remaining == 0 else 1


def main(argv: list[str] | None = None) -> int:
    disco.setup_logging()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    return run(parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
