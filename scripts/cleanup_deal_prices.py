"""One-shot cleanup of expansion-batch deal-price rows.

Does not re-scrape. Empty-price duplicate stubs are deleted; concatenated
display strings like 'from Rs. 399Rs. 549' are re-parsed to the payable
numeric amount (first Rs. N). Original 29 restaurants (ids 16-44) are left
untouched if their prices are already clean numerics.

Run: python scripts/cleanup_deal_prices.py
"""

from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "foodpanda-scraper"))

from scraperdb.database import get_connection, init_db  # noqa: E402
from scraper.prices import parse_price_parts  # noqa: E402

DB = ROOT / "foodpanda-scraper" / "foodpanda.db"
CLEAN_RE = re.compile(r"^\d+(?:\.\d+)?$")

BROKEN_WHERE = """
    TRIM(COALESCE(mi.price, '')) = ''
    OR LOWER(COALESCE(mi.price, '')) LIKE '%from%'
    OR mi.price LIKE '%Rs.%Rs.%'
"""


def _count(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    return int(conn.execute(sql, params).fetchone()[0])


def broken_count(conn: sqlite3.Connection) -> int:
    return _count(
        conn,
        f"""
        SELECT COUNT(*) FROM menu_items mi
        WHERE {BROKEN_WHERE}
        """,
    )


def report(conn: sqlite3.Connection, label: str) -> dict[str, int]:
    stats = {
        "menu_items": _count(conn, "SELECT COUNT(*) FROM menu_items"),
        "broken": broken_count(conn),
        "empty": _count(
            conn,
            "SELECT COUNT(*) FROM menu_items WHERE TRIM(COALESCE(price,'')) = ''",
        ),
        "concat_rs_rs": _count(
            conn,
            "SELECT COUNT(*) FROM menu_items WHERE price LIKE '%Rs.%Rs.%'",
        ),
        "contains_from": _count(
            conn,
            "SELECT COUNT(*) FROM menu_items WHERE LOWER(COALESCE(price,'')) LIKE '%from%'",
        ),
        "original_29_items": _count(
            conn,
            """
            SELECT COUNT(*) FROM menu_items mi
            JOIN menu_categories mc ON mc.id = mi.category_id
            WHERE mc.restaurant_id BETWEEN 16 AND 44
            """,
        ),
        "id45plus_items": _count(
            conn,
            """
            SELECT COUNT(*) FROM menu_items mi
            JOIN menu_categories mc ON mc.id = mi.category_id
            WHERE mc.restaurant_id >= 45
            """,
        ),
    }
    print(f"\n[{label}]")
    for key, value in stats.items():
        print(f"  {key}: {value}")
    return stats


def is_clean(price: str | None) -> bool:
    return bool(price and CLEAN_RE.fullmatch(str(price).strip()))


def delete_empty_duplicates(conn: sqlite3.Connection) -> int:
    rows = conn.execute(
        """
        SELECT mi.id AS id,
               mc.restaurant_id AS restaurant_id,
               mc.category_name AS category_name,
               mi.name AS name,
               mi.price AS price
        FROM menu_items mi
        JOIN menu_categories mc ON mc.id = mi.category_id
        """
    ).fetchall()
    keyed: dict[tuple, list] = {}
    for row in rows:
        key = (row["restaurant_id"], row["category_name"], row["name"])
        keyed.setdefault(key, []).append(row)

    to_delete: list[int] = []
    for group in keyed.values():
        empties = [r for r in group if not str(r["price"] or "").strip()]
        filled = [r for r in group if str(r["price"] or "").strip()]
        if filled and empties:
            to_delete.extend(int(r["id"]) for r in empties)

    for item_id in to_delete:
        conn.execute("DELETE FROM menu_items WHERE id = ?", (item_id,))
    return len(to_delete)


def reparse_dirty_prices(conn: sqlite3.Connection) -> tuple[int, int]:
    rows = conn.execute(
        "SELECT id, price FROM menu_items"
    ).fetchall()
    updated = 0
    unparseable = 0
    for row in rows:
        raw = row["price"]
        if is_clean(raw):
            continue
        current, original = parse_price_parts(raw)
        if current is None:
            unparseable += 1
            continue
        conn.execute(
            """
            UPDATE menu_items
            SET price = ?, original_price = ?
            WHERE id = ?
            """,
            (current, original, row["id"]),
        )
        updated += 1
    return updated, unparseable


def delete_remaining_empty(conn: sqlite3.Connection) -> int:
    cur = conn.execute(
        "DELETE FROM menu_items WHERE TRIM(COALESCE(price,'')) = ''"
    )
    return int(cur.rowcount or 0)


def dedupe_same_name(conn: sqlite3.Connection) -> int:
    rows = conn.execute(
        """
        SELECT mi.id AS id,
               mc.restaurant_id AS restaurant_id,
               mc.category_name AS category_name,
               mi.name AS name,
               mi.original_price AS original_price
        FROM menu_items mi
        JOIN menu_categories mc ON mc.id = mi.category_id
        ORDER BY mi.id
        """
    ).fetchall()
    seen: set[tuple] = set()
    to_delete: list[int] = []
    for row in rows:
        key = (row["restaurant_id"], row["category_name"], row["name"])
        if key in seen:
            to_delete.append(int(row["id"]))
        else:
            seen.add(key)
    for item_id in to_delete:
        conn.execute("DELETE FROM menu_items WHERE id = ?", (item_id,))
    return len(to_delete)


def remaining_broken_samples(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    rows = conn.execute(
        f"""
        SELECT r.id AS restaurant_id, r.name AS restaurant, mi.id AS item_id,
               mi.name, mi.price
        FROM menu_items mi
        JOIN menu_categories mc ON mc.id = mi.category_id
        JOIN restaurants r ON r.id = mc.restaurant_id
        WHERE {BROKEN_WHERE}
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def fajita_check(conn: sqlite3.Connection) -> None:
    print("\n[Chicken Fajita Pizza after cleanup]")
    for row in conn.execute(
        """
        SELECT r.id AS rid, r.name AS rest, mi.id AS item_id, mi.name,
               mi.price, mi.original_price, mc.category_name
        FROM menu_items mi
        JOIN menu_categories mc ON mc.id = mi.category_id
        JOIN restaurants r ON r.id = mc.restaurant_id
        WHERE mi.name LIKE '%Chicken Fajita Pizza%'
          AND r.id IN (45, 69)
        ORDER BY r.id, mi.id
        """
    ):
        print(" ", dict(row))


def main() -> int:
    if not DB.exists():
        print(f"Database not found: {DB}", file=sys.stderr)
        return 1

    conn = get_connection(str(DB))
    init_db(conn)

    before = report(conn, "before")
    deleted_empty_dupes = delete_empty_duplicates(conn)
    updated, unparseable = reparse_dirty_prices(conn)
    deleted_empty_left = delete_remaining_empty(conn)
    deleted_dupes = dedupe_same_name(conn)
    conn.commit()
    after = report(conn, "after")

    print("\n[actions]")
    print(f"  deleted empty-price duplicates: {deleted_empty_dupes}")
    print(f"  reparsed dirty price strings: {updated}")
    print(f"  unparseable (then dropped if empty): {unparseable}")
    print(f"  deleted remaining empty-price rows: {deleted_empty_left}")
    print(f"  deleted extra same-name duplicates: {deleted_dupes}")
    print(
        f"  original 29 item count unchanged: "
        f"{before['original_29_items'] == after['original_29_items']} "
        f"({after['original_29_items']})"
    )
    print(f"  remaining broken-pattern rows: {after['broken']}")

    leftover = remaining_broken_samples(conn)
    if leftover:
        print("\n[leftover samples]")
        for row in leftover:
            print(" ", row)

    fajita_check(conn)
    conn.close()
    return 0 if after["broken"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
