"""Where do Milestone 2 and Milestone 6 actually stand right now?

Read-only. Written to answer "is anything still running / did anything get
published" with data rather than recollection.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "foodpanda-scraper" / "foodpanda.db"


def m2() -> None:
    print("=== Milestone 2: live daily scraping ===")
    conn = sqlite3.connect(f"file:{SNAPSHOT}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    def scalar(sql: str) -> int:
        return int(conn.execute(sql).fetchone()["n"])

    print(f"  restaurants          {scalar('SELECT COUNT(*) n FROM restaurants')}")
    print(f"  menu_categories      {scalar('SELECT COUNT(*) n FROM menu_categories')}")
    print(f"  menu_items           {scalar('SELECT COUNT(*) n FROM menu_items')}")
    print(
        "  broken prices        "
        f"{scalar('SELECT COUNT(*) n FROM menu_items WHERE price IS NULL OR price < 0.01')}"
    )
    refreshed = scalar("SELECT COUNT(*) n FROM restaurants WHERE updated_at IS NOT NULL")
    print(f"  rows ever refreshed  {refreshed}  (0 = no scheduled run has published)")

    try:
        print(f"  scrape_runs recorded {scalar('SELECT COUNT(*) n FROM scrape_runs')}")
    except sqlite3.Error:
        print("  scrape_runs          table absent")

    # The M2 columns are added by scraperdb.init_db(), which only runs as part
    # of a refresh. Their absence proves no M2 code has ever touched this file.
    expected = {
        "restaurants": [
            "latitude",
            "longitude",
            "last_seen_at",
            "missing_streak",
            "availability",
        ],
        "scrape_runs": ["broken_prices", "blocked", "run_label"],
    }
    print("  M2 schema migration:")
    for table, columns in expected.items():
        try:
            have = {
                row["name"]
                for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
        except sqlite3.Error:
            print(f"    {table:<14} table absent")
            continue
        missing = [c for c in columns if c not in have]
        state = "applied" if not missing else f"NOT applied (missing {', '.join(missing)})"
        print(f"    {table:<14} {state}")
    conn.close()

    lock = ROOT / "scheduler" / "daily_scrape.lock"
    print(f"  scrape lock held     {lock.exists()}")
    leftovers = [
        p.name
        for p in SNAPSHOT.parent.iterdir()
        if p.name.startswith("foodpanda.db") and p.name != "foodpanda.db"
    ]
    print(f"  sidecar leftovers    {leftovers or 'none'}")


def m6() -> None:
    print("\n=== Milestone 6: API service layer ===")
    tenants = Path(os.getenv("TENANT_DB_PATH", ROOT / "data" / "tenants.db"))
    print(f"  tenant store         {tenants}")
    if not tenants.exists():
        print("    does not exist yet - created on first server start or CLI use")
    else:
        conn = sqlite3.connect(f"file:{tenants}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        for table in ("tenants", "api_keys", "usage_events", "usage_daily"):
            try:
                n = conn.execute(f"SELECT COUNT(*) n FROM {table}").fetchone()["n"]
                print(f"    {table:<16} {n}")
            except sqlite3.Error as exc:
                print(f"    {table:<16} n/a ({exc})")
        conn.close()

    for path in ("config/tiers.json", "auth/api_keys.py", "auth/rate_limit.py"):
        print(f"  {path:<20} {'present' if (ROOT / path).exists() else 'MISSING'}")

    sys.path.insert(0, str(ROOT))
    from auth.tiers import load_tiers

    print(f"  tiers configured     {', '.join(sorted(load_tiers()))}")


if __name__ == "__main__":
    m2()
    m6()
