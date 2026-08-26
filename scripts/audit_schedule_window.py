"""Evidence for the daily-refresh run time and the atomic-swap decision.

Counts vendors whose name or cuisine marks them as breakfast/chai-only (open
in the morning, shut by dinner) versus dinner-oriented, because a single
scheduled run can only catch vendors that are open at that hour. Also reports
the SQLite journal mode, which decides whether a file swap is safe.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

DB = Path(__file__).resolve().parents[1] / "foodpanda-scraper" / "foodpanda.db"

# Morning trade in Karachi: nashta (halwa puri), chai/dhaba, paratha houses.
MORNING_TERMS = (
    "nashta", "halwa puri", "halwapuri", "chai", "tea", "quetta", "dhaba",
    "paratha", "sheermal", "naan", "breakfast", "cafe",
)
# Evening trade: bbq, karahi, burgers, pizza, biryani houses, desserts.
EVENING_TERMS = (
    "bbq", "b.b.q", "karahi", "burger", "pizza", "broast", "shawarma",
    "biryani", "ice cream", "dessert", "grill", "steak", "chinese",
)


def bucket(conn: sqlite3.Connection, terms: tuple[str, ...]) -> int:
    clause = " OR ".join(
        ["LOWER(name) LIKE ?" for _ in terms]
        + ["LOWER(COALESCE(cuisine,'')) LIKE ?" for _ in terms]
    )
    params = [f"%{t}%" for t in terms] * 2
    return conn.execute(
        f"SELECT COUNT(*) FROM restaurants WHERE {clause}", params
    ).fetchone()[0]


def main() -> int:
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row

    total = conn.execute("SELECT COUNT(*) FROM restaurants").fetchone()[0]
    morning = bucket(conn, MORNING_TERMS)
    evening = bucket(conn, EVENING_TERMS)
    print(f"restaurants={total}")
    print(f"  morning-trade signals (nashta/chai/paratha/quetta): {morning}")
    print(f"  evening-trade signals (bbq/burger/pizza/biryani):   {evening}")

    print("\n-- sample morning-trade names")
    rows = conn.execute(
        """
        SELECT name FROM restaurants
        WHERE LOWER(name) LIKE '%nashta%'
           OR LOWER(name) LIKE '%halwa%'
           OR LOWER(name) LIKE '%chai%'
           OR LOWER(name) LIKE '%paratha%'
        ORDER BY name LIMIT 15
        """
    ).fetchall()
    for row in rows:
        print(f"  {row['name']}")

    print("\n-- sqlite settings (atomic-swap decision)")
    for pragma in ("journal_mode", "page_size", "wal_autocheckpoint"):
        value = conn.execute(f"PRAGMA {pragma}").fetchone()[0]
        print(f"  {pragma} = {value}")
    size_mb = DB.stat().st_size / (1024 * 1024)
    print(f"  db size = {size_mb:.1f} MB")
    for suffix in ("-wal", "-shm"):
        side = Path(str(DB) + suffix)
        print(f"  {DB.name}{suffix} exists = {side.exists()}")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
