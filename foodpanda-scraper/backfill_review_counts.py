"""Listing-only backfill of restaurants.review_count (no menu re-scrape).

Matches existing rows by vendor code parsed from restaurants.url.
Disco listing feed first; per-vendor fd-api for any leftover rows.
Also refreshes rating so R and v come from the same observation.

# Needs periodic re-run after a fresh scrape if review_count is missing.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path
from typing import Any

import config
from scraperdb.database import get_connection, init_db, update_listing_stats
from scraper import api_client

logger = logging.getLogger("backfill_review_counts")

DISCO_PAGE_SIZE = 48
DISCO_MAX_OFFSET = 480
FD_API_DELAY_SEC = 1.5


def _parse_review_count(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def collect_listing_index(
    lat: float,
    lng: float,
) -> dict[str, dict[str, Any]]:
    """Paginate disco (rating_desc + default) keyed by vendor code."""
    index: dict[str, dict[str, Any]] = {}
    for sort in ("rating_desc", None):
        for offset in range(0, DISCO_MAX_OFFSET, DISCO_PAGE_SIZE):
            page = api_client.fetch_vendors(
                lat,
                lng,
                limit=DISCO_PAGE_SIZE,
                offset=offset,
                sort=sort,
            )
            if not page:
                break
            added = 0
            for vendor in page:
                code = vendor.get("code")
                if not code or code in index:
                    continue
                index[code] = vendor
                added += 1
            logger.info(
                "disco sort=%s offset=%s: %s new (index=%s)",
                sort or "default",
                offset,
                added,
                len(index),
            )
            if len(page) < DISCO_PAGE_SIZE:
                break
    return index


def backfill(db_path: str, lat: float, lng: float) -> int:
    conn = get_connection(db_path)
    try:
        init_db(conn)
        rows = conn.execute(
            "SELECT id, name, url, rating FROM restaurants ORDER BY id"
        ).fetchall()
        if not rows:
            logger.error("No restaurants in %s", db_path)
            return 1

        index = collect_listing_index(lat, lng)
        updated = 0
        missing: list[tuple[int, str, str]] = []

        for row in rows:
            code = api_client.extract_vendor_code(row["url"])
            vendor = index.get(code) if code else None
            source = "disco"
            if vendor is None and code:
                logger.info("Not in disco feed, trying fd-api: %s (%s)", row["name"], code)
                time.sleep(FD_API_DELAY_SEC)
                vendor = api_client.fetch_vendor_meta(code, lat, lng)
                source = "fd-api"

            review_count = _parse_review_count(
                (vendor or {}).get("review_number")
            )
            if vendor is None or review_count is None:
                missing.append((row["id"], code or "", row["name"]))
                logger.warning("No review_count for id=%s %s", row["id"], row["name"])
                continue

            rating = vendor.get("rating")
            try:
                rating = float(rating) if rating is not None else None
            except (TypeError, ValueError):
                rating = None

            update_listing_stats(conn, row["id"], review_count, rating)
            updated += 1
            logger.info(
                "id=%s %s review_count=%s rating=%s via %s",
                row["id"],
                row["name"],
                review_count,
                rating,
                source,
            )

        conn.commit()

        nulls = conn.execute(
            "SELECT COUNT(*) AS n FROM restaurants WHERE review_count IS NULL"
        ).fetchone()["n"]
        total = conn.execute("SELECT COUNT(*) AS n FROM restaurants").fetchone()["n"]
        logger.info(
            "Backfill done: updated=%s total=%s null_review_count=%s missing=%s",
            updated,
            total,
            nulls,
            len(missing),
        )
        if missing:
            for rid, code, name in missing:
                logger.error("UNFILLED id=%s code=%s %s", rid, code, name)
            return 1
        if nulls != 0:
            logger.error("Expected 0 NULL review_count, got %s", nulls)
            return 1
        return 0
    finally:
        conn.close()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format=config.LOG_FORMAT,
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default=config.DEFAULT_DB_PATH)
    parser.add_argument("--lat", type=float, default=config.DEFAULT_LAT)
    parser.add_argument("--lng", type=float, default=config.DEFAULT_LNG)
    args = parser.parse_args()

    db_path = str(Path(args.db_path))
    if not Path(db_path).exists():
        logger.error("Database not found: %s", db_path)
        return 1
    return backfill(db_path, args.lat, args.lng)


if __name__ == "__main__":
    raise SystemExit(main())
