"""Example SQL queries against a scraped foodpanda.db."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

import config


def connect(db_path: str) -> sqlite3.Connection:
    path = Path(db_path)
    if not path.exists():
        print(f"Database not found: {db_path}", file=sys.stderr)
        print("Run main.py first to scrape data.", file=sys.stderr)
        raise SystemExit(1)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def run_examples(conn: sqlite3.Connection) -> None:
    print("=== 1) Menu items priced under PKR 500 ===")
    rows = conn.execute(
        """
        SELECT r.name AS restaurant, mi.name AS item, mi.price
        FROM menu_items mi
        JOIN menu_categories mc ON mc.id = mi.category_id
        JOIN restaurants r ON r.id = mc.restaurant_id
        WHERE CAST(
            REPLACE(REPLACE(COALESCE(mi.price, ''), 'PKR', ''), ',', '')
            AS REAL
        ) < 500
          AND TRIM(COALESCE(mi.price, '')) != ''
        ORDER BY CAST(
            REPLACE(REPLACE(COALESCE(mi.price, ''), 'PKR', ''), ',', '')
            AS REAL
        ) ASC
        LIMIT 20
        """
    ).fetchall()
    for row in rows:
        print(f"  {row['restaurant']}: {row['item']} — {row['price']}")
    if not rows:
        print("  (no matching rows)")

    print("\n=== 2) Average menu size per restaurant ===")
    row = conn.execute(
        """
        SELECT
            AVG(item_count) AS avg_items,
            COUNT(*) AS restaurants
        FROM (
            SELECT r.id, COUNT(mi.id) AS item_count
            FROM restaurants r
            LEFT JOIN menu_categories mc ON mc.restaurant_id = r.id
            LEFT JOIN menu_items mi ON mi.category_id = mc.id
            GROUP BY r.id
        )
        """
    ).fetchone()
    print(
        f"  Avg items/restaurant: {row['avg_items']:.1f} "
        f"across {row['restaurants']} restaurants"
    )

    print("\n=== 3) Item counts per category (all restaurants) ===")
    rows = conn.execute(
        """
        SELECT mc.category_name, COUNT(mi.id) AS item_count
        FROM menu_categories mc
        JOIN menu_items mi ON mi.category_id = mc.id
        GROUP BY mc.category_name
        ORDER BY item_count DESC
        LIMIT 15
        """
    ).fetchall()
    for row in rows:
        print(f"  {row['category_name']}: {row['item_count']}")

    print("\n=== 4) Top-rated restaurants with menu item counts ===")
    rows = conn.execute(
        """
        SELECT
            r.name,
            r.rating,
            r.cuisine,
            COUNT(mi.id) AS items
        FROM restaurants r
        LEFT JOIN menu_categories mc ON mc.restaurant_id = r.id
        LEFT JOIN menu_items mi ON mi.category_id = mc.id
        GROUP BY r.id
        ORDER BY (r.rating IS NULL), r.rating DESC, items DESC
        LIMIT 10
        """
    ).fetchall()
    for row in rows:
        print(
            f"  {row['name']} (rating={row['rating']}, "
            f"cuisine={row['cuisine']}) — {row['items']} items"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run example queries on foodpanda.db")
    parser.add_argument(
        "--db-path",
        default=config.DEFAULT_DB_PATH,
        help="Path to SQLite database",
    )
    args = parser.parse_args()
    conn = connect(args.db_path)
    try:
        run_examples(conn)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
