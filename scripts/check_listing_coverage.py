"""Does the multi-pin listing index actually cover the stored dataset?

plans/SCRAPE_SCHEDULE_PLAN.md section 3 claims that crawling disco from a single
city-centre point would miss most of the 209 rows, because disco returns
vendors that deliver *to the queried point* and 145 rows were discovered from
Gulshan/Jauhar pins 10-15 km away. This measures it instead of asserting it:
crawl every pin, then report coverage for the Saddar pin alone versus all pins.

Read-only against the database. Rate-limited exactly like a real scrape, so it
takes several minutes.

    python scripts/check_listing_coverage.py
"""

from __future__ import annotations

import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "foodpanda-scraper"))

import config  # noqa: E402
from scraper import api_client, areas  # noqa: E402

DB = ROOT / "foodpanda-scraper" / "foodpanda.db"
PAGE = 48
MAX_OFFSET = 480
SOLO_PIN = "Saddar"  # what build_listing_index used to crawl, alone


def stored_codes() -> dict[str, sqlite3.Row]:
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, name, url, delivery_areas FROM restaurants ORDER BY id"
    ).fetchall()
    conn.close()
    out: dict[str, sqlite3.Row] = {}
    for row in rows:
        code = (api_client.extract_vendor_code(row["url"] or "") or "").lower()
        if code:
            out[code] = row
    return out


def crawl() -> dict[str, set[str]]:
    """pin label -> vendor codes returned by that pin."""
    by_pin: dict[str, set[str]] = {}
    for label, lat, lng in areas.refresh_pins():
        codes: set[str] = set()
        started = time.time()
        for sort in ("rating_desc", None):
            for offset in range(0, MAX_OFFSET, PAGE):
                page = api_client.fetch_vendors(
                    lat, lng, limit=PAGE, offset=offset, sort=sort
                )
                time.sleep(config.MIN_DELAY_SEC)
                if not page:
                    break
                for vendor in page:
                    code = (vendor.get("code") or "").lower()
                    if code:
                        codes.add(code)
                if len(page) < PAGE:
                    break
        by_pin[label] = codes
        print(
            f"  {label:<42} {len(codes):>4} vendors "
            f"({time.time() - started:.0f}s)",
            flush=True,
        )
    return by_pin


def main() -> int:
    stored = stored_codes()
    print(f"stored restaurants with a parseable vendor code: {len(stored)}\n")

    print("crawling pins (rate-limited, several minutes)...", flush=True)
    by_pin = crawl()

    everything: set[str] = set()
    for codes in by_pin.values():
        everything |= codes
    solo = by_pin.get(SOLO_PIN, set())

    covered_all = [c for c in stored if c in everything]
    covered_solo = [c for c in stored if c in solo]

    print(f"\ndistinct vendors seen across all pins: {len(everything)}")
    print(f"distinct vendors seen from {SOLO_PIN} alone: {len(solo)}\n")

    print(f"coverage of the stored dataset ({len(stored)} rows):")
    print(f"  all pins   : {len(covered_all):>4}  ({len(covered_all)/len(stored):.0%})")
    print(f"  {SOLO_PIN} only: {len(covered_solo):>4}  ({len(covered_solo)/len(stored):.0%})")
    print(f"  gained by multi-pin: {len(covered_all) - len(covered_solo)}")

    print("\nby recorded delivery area:")
    buckets: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    for code, row in stored.items():
        key = row["delivery_areas"] or "(none recorded)"
        buckets[key][0] += 1
        if code in everything:
            buckets[key][1] += 1
        if code in solo:
            buckets[key][2] += 1
    for key in sorted(buckets):
        total, all_pins, saddar = buckets[key]
        print(f"  {key:<42} total={total:>3} all_pins={all_pins:>3} {SOLO_PIN}={saddar:>3}")

    missing = [stored[c]["name"] for c in stored if c not in everything]
    print(f"\nnot in any pin's feed right now: {len(missing)}")
    for name in missing[:20]:
        print(f"  {name}")
    if len(missing) > 20:
        print(f"  ... and {len(missing) - 20} more")
    print(
        "\nVendors missing here are expected to be the ones closed at this hour; "
        "they keep yesterday's data and are re-probed via fd-api."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
