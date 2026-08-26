"""SQLite connection helpers and insert helpers."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def get_connection(db_path: str) -> sqlite3.Connection:
    """Open a SQLite connection with foreign keys enabled."""
    conn = sqlite3.connect(db_path, timeout=120)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create tables from schema.sql if they do not exist."""
    schema = _SCHEMA_PATH.read_text(encoding="utf-8")
    conn.executescript(schema)
    _ensure_column(conn, "restaurants", "image_url", "TEXT")
    _ensure_column(conn, "restaurants", "review_count", "INTEGER")
    _ensure_column(conn, "restaurants", "updated_at", "TEXT")
    # Area the vendor was discovered delivering to. A street address is where
    # the kitchen sits, which is not the same as its delivery zone.
    _ensure_column(conn, "restaurants", "delivery_areas", "TEXT")
    # Daily refresh (Milestone 2). Coordinates are the vendor's own, so a menu
    # fetch queries inside its delivery zone: fd-api returns an empty menu for
    # a lat/lng the vendor does not deliver to.
    _ensure_column(conn, "restaurants", "latitude", "REAL")
    _ensure_column(conn, "restaurants", "longitude", "REAL")
    # Absence tracking. A vendor missing from one day's feed is usually just
    # closed, so absence is counted and flagged, never acted on destructively.
    _ensure_column(conn, "restaurants", "last_seen_at", "TEXT")
    _ensure_column(conn, "restaurants", "missing_streak", "INTEGER DEFAULT 0")
    _ensure_column(conn, "restaurants", "availability", "TEXT DEFAULT 'listed'")
    _ensure_column(conn, "menu_items", "updated_at", "TEXT")
    _ensure_column(conn, "menu_items", "original_price", "TEXT")
    _ensure_column(conn, "scrape_runs", "broken_prices", "INTEGER")
    _ensure_column(conn, "scrape_runs", "blocked", "INTEGER DEFAULT 0")
    _ensure_column(conn, "scrape_runs", "run_label", "TEXT")
    conn.commit()
    logger.info("Database schema initialized")


def _insert_menu_item(
    conn: sqlite3.Connection,
    category_id: int,
    item: dict[str, Any],
    updated_at: str | None = None,
) -> None:
    """Insert one menu row, including optional original/strikethrough price."""
    values = (
        category_id,
        item.get("name") or "Unknown",
        item.get("price"),
        item.get("original_price"),
        item.get("description"),
        item.get("image_url"),
    )
    if updated_at is None:
        conn.execute(
            """
            INSERT INTO menu_items
                (category_id, name, price, original_price, description, image_url)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            values,
        )
        return
    conn.execute(
        """
        INSERT INTO menu_items
            (category_id, name, price, original_price, description, image_url, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (*values, updated_at),
    )


def _ensure_column(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    col_type: str,
) -> None:
    """Add a column if an older DB is missing it."""
    cols = {
        row[1]
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
        logger.info("Added column %s.%s", table, column)


def clear_all_data(conn: sqlite3.Connection) -> None:
    """Delete all scraped rows (for a fresh top-rated re-scrape)."""
    conn.execute("DELETE FROM menu_items")
    conn.execute("DELETE FROM menu_categories")
    conn.execute("DELETE FROM restaurants")
    conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('restaurants','menu_categories','menu_items')")
    conn.commit()
    logger.info("Cleared all restaurant/menu data")


def restaurant_exists(conn: sqlite3.Connection, url: str) -> bool:
    """Return True if a restaurant with this URL is already stored."""
    row = conn.execute(
        "SELECT 1 FROM restaurants WHERE url = ? LIMIT 1",
        (url,),
    ).fetchone()
    return row is not None


def insert_restaurant_with_menu(
    conn: sqlite3.Connection,
    restaurant: dict[str, Any],
    categories: list[dict[str, Any]],
) -> int | None:
    """
    Insert a restaurant and its full menu in one transaction.

    Uses INSERT OR IGNORE on unique restaurant URL. If the URL already
    exists, skips menu inserts to keep re-runs idempotent and returns None.

    Returns the new restaurant id on insert, or None if skipped.
    """
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO restaurants
            (name, url, rating, cuisine, address, delivery_time, image_url, review_count)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            restaurant.get("name") or "Unknown",
            restaurant["url"],
            restaurant.get("rating"),
            restaurant.get("cuisine"),
            restaurant.get("address"),
            restaurant.get("delivery_time"),
            restaurant.get("image_url"),
            restaurant.get("review_number"),
        ),
    )

    if cur.rowcount == 0:
        logger.info(
            "Restaurant already in DB (skipping menu): %s",
            restaurant.get("url"),
        )
        conn.commit()
        return None

    restaurant_id = cur.lastrowid
    item_count = 0

    for category in categories:
        cat_cur = conn.execute(
            """
            INSERT INTO menu_categories (restaurant_id, category_name)
            VALUES (?, ?)
            """,
            (restaurant_id, category.get("category_name") or "Uncategorized"),
        )
        category_id = cat_cur.lastrowid

        for item in category.get("items") or []:
            _insert_menu_item(conn, category_id, item)
            item_count += 1

    conn.commit()
    logger.info(
        "Saved restaurant id=%s (%s) with %s menu items across %s categories",
        restaurant_id,
        restaurant.get("name"),
        item_count,
        len(categories),
    )
    return restaurant_id


def update_listing_stats(
    conn: sqlite3.Connection,
    restaurant_id: int,
    review_count: int,
    rating: float | None = None,
) -> None:
    """Update listing-level stats without touching menu rows."""
    if rating is None:
        conn.execute(
            "UPDATE restaurants SET review_count = ? WHERE id = ?",
            (review_count, restaurant_id),
        )
    else:
        conn.execute(
            "UPDATE restaurants SET review_count = ?, rating = ? WHERE id = ?",
            (review_count, rating, restaurant_id),
        )


_LISTING_META_FIELDS = (
    "rating",
    "cuisine",
    "address",
    "delivery_time",
    "image_url",
)


def fill_missing_listing_meta(
    conn: sqlite3.Connection,
    restaurant_id: int,
    meta: dict[str, Any],
) -> list[str]:
    """Fill only NULL/empty listing columns for one restaurant.

    Never overwrites a value that is already there, so re-running this cannot
    disturb rows that were scraped with complete metadata.

    Returns the column names that were written.
    """
    row = conn.execute(
        "SELECT rating, cuisine, address, delivery_time, image_url "
        "FROM restaurants WHERE id = ?",
        (restaurant_id,),
    ).fetchone()
    if row is None:
        return []

    updates: dict[str, Any] = {}
    for field in _LISTING_META_FIELDS:
        current = row[field]
        if current is not None and str(current).strip():
            continue
        value = meta.get(field)
        if value is None or not str(value).strip():
            continue
        updates[field] = value
    if not updates:
        return []

    assignments = ", ".join(f"{field} = ?" for field in updates)
    conn.execute(
        f"UPDATE restaurants SET {assignments} WHERE id = ?",
        (*updates.values(), restaurant_id),
    )
    conn.commit()
    return list(updates)


def set_delivery_areas(
    conn: sqlite3.Connection,
    restaurant_id: int,
    areas: str,
) -> None:
    """Record which discovery areas this vendor delivers to."""
    conn.execute(
        "UPDATE restaurants SET delivery_areas = ? WHERE id = ?",
        (areas, restaurant_id),
    )
    conn.commit()


def count_menu_items(conn: sqlite3.Connection) -> int:
    """Return total number of menu items in the database."""
    row = conn.execute("SELECT COUNT(*) AS n FROM menu_items").fetchone()
    return int(row["n"]) if row else 0


def count_restaurants(conn: sqlite3.Connection) -> int:
    """Return total number of restaurants in the database."""
    row = conn.execute("SELECT COUNT(*) AS n FROM restaurants").fetchone()
    return int(row["n"]) if row else 0


def list_restaurants_for_refresh(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """All restaurants currently in the DB — the daily job's target set.

    Selects every column the refresh may need to fall back on, so a vendor that
    dropped out of today's feed can be re-queried at its own coordinates and
    re-saved without inventing NULLs.
    """
    return list(
        conn.execute(
            """
            SELECT id, name, url, rating, review_count, cuisine, address,
                   delivery_time, image_url, delivery_areas, latitude,
                   longitude, missing_streak, availability, last_seen_at
            FROM restaurants
            ORDER BY id
            """
        )
    )


# The project's standing broken-price diagnostic. Defined once so the daily
# job, verify_new_menu_prices.py and the audit scripts cannot drift apart.
BROKEN_PRICE_SQL = """
SELECT COUNT(*) AS n FROM menu_items
WHERE price IS NULL
   OR TRIM(price) = ''
   OR price LIKE '%Rs%Rs%'
   OR LOWER(price) LIKE '%from%'
"""


def count_broken_prices(conn: sqlite3.Connection) -> int:
    """Menu rows with a NULL, empty, concatenated or 'from' price. Must be 0."""
    row = conn.execute(BROKEN_PRICE_SQL).fetchone()
    return int(row["n"]) if row else 0


def count_restaurants_without_menu(conn: sqlite3.Connection) -> int:
    """Restaurants that would render with an empty menu."""
    row = conn.execute(
        """
        SELECT COUNT(*) AS n FROM restaurants r
        WHERE NOT EXISTS (
            SELECT 1 FROM menu_categories mc
            JOIN menu_items mi ON mi.category_id = mc.id
            WHERE mc.restaurant_id = r.id
        )
        """
    ).fetchone()
    return int(row["n"]) if row else 0


def merge_delivery_areas(existing: str | None, observed: list[str]) -> str | None:
    """Union today's observed areas with what is already recorded.

    Never narrows: a vendor absent from a pin today may simply be closed, which
    is not evidence that it stopped delivering there.
    """
    current = [
        part.strip()
        for part in (existing or "").split(",")
        if part.strip()
    ]
    merged = sorted({*current, *[area for area in observed if area.strip()]})
    return ", ".join(merged) if merged else None


def mark_vendor_seen(
    conn: sqlite3.Connection,
    restaurant_id: int,
    seen_at: str,
    areas: list[str],
) -> None:
    """Vendor was in a listing feed today: reset absence, widen its areas."""
    row = conn.execute(
        "SELECT delivery_areas FROM restaurants WHERE id = ?", (restaurant_id,)
    ).fetchone()
    merged = merge_delivery_areas(row["delivery_areas"] if row else None, areas)
    conn.execute(
        """
        UPDATE restaurants
        SET last_seen_at = ?,
            missing_streak = 0,
            availability = 'listed',
            delivery_areas = COALESCE(?, delivery_areas)
        WHERE id = ?
        """,
        (seen_at, merged, restaurant_id),
    )


def mark_vendor_missing(
    conn: sqlite3.Connection,
    restaurant_id: int,
    threshold: int,
) -> int:
    """No evidence the vendor exists today. Increment the streak, flag at the
    threshold, and touch nothing else.

    Callers must only reach here when the listing feeds *and* the per-vendor
    lookup both came back empty. A vendor that is merely closed still answers
    the per-vendor endpoint, so it never lands here.
    """
    conn.execute(
        "UPDATE restaurants SET missing_streak = COALESCE(missing_streak, 0) + 1 "
        "WHERE id = ?",
        (restaurant_id,),
    )
    row = conn.execute(
        "SELECT missing_streak FROM restaurants WHERE id = ?", (restaurant_id,)
    ).fetchone()
    streak = int(row["missing_streak"] or 0) if row else 0
    if streak >= threshold:
        conn.execute(
            "UPDATE restaurants SET availability = 'unlisted' WHERE id = ?",
            (restaurant_id,),
        )
    return streak


def replace_restaurant_menu(
    conn: sqlite3.Connection,
    restaurant_id: int,
    restaurant: dict[str, Any],
    categories: list[dict[str, Any]],
    updated_at: str,
) -> int:
    """Replace one restaurant's listing fields and menu in a single transaction.

    Keeps restaurants.id and url. Callers must only invoke this after a
    successful scrape; a failure should skip the call so yesterday remains.

    Every listing column is COALESCEd. A field missing from today's response
    means "no observation", not "no value" — the daily refresh often has only a
    stub for a vendor that dropped out of the listing feed, and writing that
    stub literally would erase address/cuisine/image that took a backfill pass
    to collect.
    """
    item_count = 0
    try:
        conn.execute(
            """
            UPDATE restaurants
            SET name = COALESCE(NULLIF(?, ''), name),
                rating = COALESCE(?, rating),
                cuisine = COALESCE(NULLIF(?, ''), cuisine),
                address = COALESCE(NULLIF(?, ''), address),
                delivery_time = COALESCE(NULLIF(?, ''), delivery_time),
                image_url = COALESCE(NULLIF(?, ''), image_url),
                review_count = COALESCE(?, review_count),
                latitude = COALESCE(?, latitude),
                longitude = COALESCE(?, longitude),
                updated_at = ?
            WHERE id = ?
            """,
            (
                restaurant.get("name"),
                restaurant.get("rating"),
                restaurant.get("cuisine"),
                restaurant.get("address"),
                restaurant.get("delivery_time"),
                restaurant.get("image_url"),
                restaurant.get("review_number"),
                restaurant.get("latitude"),
                restaurant.get("longitude"),
                updated_at,
                restaurant_id,
            ),
        )
        conn.execute(
            """
            DELETE FROM menu_items
            WHERE category_id IN (
                SELECT id FROM menu_categories WHERE restaurant_id = ?
            )
            """,
            (restaurant_id,),
        )
        conn.execute(
            "DELETE FROM menu_categories WHERE restaurant_id = ?",
            (restaurant_id,),
        )
        for category in categories:
            cat_cur = conn.execute(
                """
                INSERT INTO menu_categories (restaurant_id, category_name)
                VALUES (?, ?)
                """,
                (restaurant_id, category.get("category_name") or "Uncategorized"),
            )
            category_id = cat_cur.lastrowid
            for item in category.get("items") or []:
                _insert_menu_item(conn, category_id, item, updated_at=updated_at)
                item_count += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    logger.info(
        "Refreshed restaurant id=%s (%s) with %s menu items",
        restaurant_id,
        restaurant.get("name"),
        item_count,
    )
    return item_count


def update_listing_only(
    conn: sqlite3.Connection,
    restaurant_id: int,
    restaurant: dict[str, Any],
    updated_at: str,
) -> None:
    """Write listing fields when the menu scrape failed. Menu rows stay."""
    conn.execute(
        """
        UPDATE restaurants
        SET rating = COALESCE(?, rating),
            review_count = COALESCE(?, review_count),
            cuisine = COALESCE(NULLIF(?, ''), cuisine),
            address = COALESCE(NULLIF(?, ''), address),
            delivery_time = COALESCE(NULLIF(?, ''), delivery_time),
            image_url = COALESCE(NULLIF(?, ''), image_url),
            latitude = COALESCE(?, latitude),
            longitude = COALESCE(?, longitude),
            updated_at = ?
        WHERE id = ?
        """,
        (
            restaurant.get("rating"),
            restaurant.get("review_number"),
            restaurant.get("cuisine"),
            restaurant.get("address"),
            restaurant.get("delivery_time"),
            restaurant.get("image_url"),
            restaurant.get("latitude"),
            restaurant.get("longitude"),
            updated_at,
            restaurant_id,
        ),
    )
    conn.commit()


def start_scrape_run(
    conn: sqlite3.Connection,
    started_at: str,
    log_path: str,
    run_label: str = "manual",
) -> int:
    cur = conn.execute(
        """
        INSERT INTO scrape_runs (started_at, status, log_path, run_label)
        VALUES (?, 'running', ?, ?)
        """,
        (started_at, log_path, run_label),
    )
    conn.commit()
    return int(cur.lastrowid)


def finish_scrape_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    finished_at: str,
    status: str,
    ok: int,
    failed: int,
    skipped: int,
    listing_only: int,
    note: str,
    broken_prices: int | None = None,
    blocked: int = 0,
) -> None:
    conn.execute(
        """
        UPDATE scrape_runs
        SET finished_at = ?,
            status = ?,
            restaurants_ok = ?,
            restaurants_failed = ?,
            restaurants_skipped = ?,
            listing_only = ?,
            note = ?,
            broken_prices = ?,
            blocked = ?
        WHERE id = ?
        """,
        (
            finished_at,
            status,
            ok,
            failed,
            skipped,
            listing_only,
            note,
            broken_prices,
            blocked,
            run_id,
        ),
    )
    conn.commit()


def recent_scrape_runs(conn: sqlite3.Connection, limit: int = 5) -> list[sqlite3.Row]:
    """Most recent runs, newest first — backs `daily_scrape.py --status`."""
    return list(
        conn.execute(
            """
            SELECT id, started_at, finished_at, status, run_label,
                   restaurants_ok, restaurants_failed, restaurants_skipped,
                   listing_only, broken_prices, blocked, log_path, note
            FROM scrape_runs
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        )
    )
