"""Step 4 price check for restaurants inserted after baseline max id."""

from __future__ import annotations

import sqlite3
import sys

from scraperdb.database import count_menu_items, count_restaurants, get_connection

SQL = """
SELECT r.id, r.name, mi.name AS item_name, mi.price
FROM menu_items mi
JOIN menu_categories mc ON mi.category_id = mc.id
JOIN restaurants r ON mc.restaurant_id = r.id
WHERE r.id > ?
  AND (
       mi.price IS NULL
    OR mi.price = ''
    OR mi.price LIKE '%Rs%Rs%'
    OR mi.price LIKE '%from%'
  )
"""


def main() -> int:
    baseline = int(sys.argv[1]) if len(sys.argv) > 1 else 84
    conn = get_connection("foodpanda.db")
    conn.row_factory = sqlite3.Row
    rows = conn.execute(SQL, (baseline,)).fetchall()
    new_r = conn.execute(
        "SELECT COUNT(*) AS n FROM restaurants WHERE id > ?", (baseline,)
    ).fetchone()["n"]
    new_i = conn.execute(
        """
        SELECT COUNT(*) AS n
        FROM menu_items mi
        JOIN menu_categories mc ON mi.category_id = mc.id
        JOIN restaurants r ON mc.restaurant_id = r.id
        WHERE r.id > ?
        """,
        (baseline,),
    ).fetchone()["n"]
    print(f"baseline_max_id={baseline}")
    print(f"restaurants_total={count_restaurants(conn)}")
    print(f"menu_items_total={count_menu_items(conn)}")
    print(f"new_restaurants={new_r}")
    print(f"new_menu_items={new_i}")
    print(f"bad_price_rows={len(rows)}")
    for row in rows[:30]:
        print(f"  {row['id']} | {row['name']} | {row['item_name']} | {row['price']}")
    conn.close()
    return 0 if not rows else 1


if __name__ == "__main__":
    raise SystemExit(main())
