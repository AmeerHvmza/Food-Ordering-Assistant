"""Pre-M2 audit: what the daily refresh has to keep current, and its baseline.

Prints per-column NULL/empty counts, delivery_areas coverage, the standing
broken-price diagnostic, and any recorded scrape_runs history.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

DB = Path(__file__).resolve().parents[1] / "foodpanda-scraper" / "foodpanda.db"

LISTING_COLUMNS = (
    "name",
    "url",
    "rating",
    "review_count",
    "cuisine",
    "address",
    "delivery_time",
    "image_url",
    "delivery_areas",
    "updated_at",
)

BROKEN_PRICE_SQL = """
SELECT COUNT(*) FROM menu_items
WHERE price IS NULL
   OR TRIM(price) = ''
   OR price LIKE '%Rs%Rs%'
   OR LOWER(price) LIKE '%from%'
"""


def main() -> int:
    if not DB.exists():
        print(f"missing db: {DB}")
        return 1
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row

    total = conn.execute("SELECT COUNT(*) FROM restaurants").fetchone()[0]
    items = conn.execute("SELECT COUNT(*) FROM menu_items").fetchone()[0]
    cats = conn.execute("SELECT COUNT(*) FROM menu_categories").fetchone()[0]
    print(f"restaurants={total} categories={cats} menu_items={items}")

    print("\n-- missing (NULL or blank) per column, of %s rows" % total)
    for col in LISTING_COLUMNS:
        missing = conn.execute(
            f"SELECT COUNT(*) FROM restaurants "
            f"WHERE {col} IS NULL OR TRIM(CAST({col} AS TEXT)) = ''"
        ).fetchone()[0]
        flag = "  <-- gap" if missing else ""
        print(f"  {col:<16} missing={missing:>4}{flag}")

    print("\n-- delivery_areas values")
    for row in conn.execute(
        "SELECT COALESCE(delivery_areas,'(null)') AS area, COUNT(*) AS n "
        "FROM restaurants GROUP BY area ORDER BY n DESC"
    ):
        print(f"  {row['n']:>4}  {row['area']}")

    print("\n-- restaurants with zero menu items")
    empty = conn.execute(
        """
        SELECT COUNT(*) FROM restaurants r
        WHERE NOT EXISTS (
            SELECT 1 FROM menu_categories mc
            JOIN menu_items mi ON mi.category_id = mc.id
            WHERE mc.restaurant_id = r.id
        )
        """
    ).fetchone()[0]
    print(f"  {empty}")

    broken = conn.execute(BROKEN_PRICE_SQL).fetchone()[0]
    print(f"\n-- broken-price diagnostic (must stay 0): {broken}")

    print("\n-- scrape_runs history")
    try:
        rows = list(
            conn.execute(
                "SELECT id, started_at, finished_at, status, restaurants_ok, "
                "restaurants_failed, listing_only, note FROM scrape_runs "
                "ORDER BY id DESC LIMIT 10"
            )
        )
    except sqlite3.OperationalError as exc:
        print(f"  table missing: {exc}")
        rows = []
    if not rows:
        print("  (never run)")
    for row in rows:
        print(f"  #{row['id']} {row['started_at']} -> {row['finished_at']} "
              f"{row['status']} ok={row['restaurants_ok']} "
              f"failed={row['restaurants_failed']} note={row['note']}")

    print("\n-- updated_at spread (menu freshness)")
    for row in conn.execute(
        "SELECT COALESCE(SUBSTR(updated_at,1,10),'(never)') AS day, COUNT(*) AS n "
        "FROM restaurants GROUP BY day ORDER BY day"
    ):
        print(f"  {row['n']:>4}  {row['day']}")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
