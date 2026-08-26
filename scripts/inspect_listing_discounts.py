"""Step 1 for Milestone 3: dump real disco discounts/discounts_info shapes.

Does not write to the database. Hits a few listing pins, classifies the
fields, and prints examples. Rate-limited like other one-shot probes.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
SCRAPER = ROOT / "foodpanda-scraper"
sys.path.insert(0, str(SCRAPER))

import config  # noqa: E402
from scraper.areas import refresh_pins  # noqa: E402
from scraper.api_client import _disco_headers  # noqa: E402

DB = ROOT / "foodpanda-scraper" / "foodpanda.db"
PINS = [
    ("Saddar", 24.8607, 67.0011),
    ("Gulshan NIPA", 24.9180, 67.0910),
    ("Jauhar Chowk", 24.9170, 67.1340),
    ("Bahadurabad", 24.8825, 67.0680),
]


def _empty(value: Any) -> bool:
    if value is None:
        return True
    if value == "" or value == [] or value == {}:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    return False


def _shape(value: Any) -> str:
    if value is None:
        return "None"
    if isinstance(value, list):
        if not value:
            return "list[]"
        inner = Counter(_shape(item) for item in value[:8])
        return f"list[{len(value)}] of {dict(inner)}"
    if isinstance(value, dict):
        return "dict{" + ", ".join(sorted(value.keys())) + "}"
    return type(value).__name__


def fetch_raw(lat: float, lng: float, limit: int = 48, offset: int = 0) -> list[dict]:
    params = {
        "latitude": str(lat),
        "longitude": str(lng),
        "language_id": "1",
        "include": "characteristics",
        "dynamic_pricing": "0",
        "configuration": "Variant3",
        "country": "pk",
        "vertical": "restaurants",
        "limit": str(limit),
        "offset": str(offset),
        "customer_type": "regular",
        "sort": "rating_desc",
    }
    resp = requests.get(
        config.DISCO_VENDORS_URL,
        headers=_disco_headers(),
        params=params,
        timeout=20,
    )
    resp.raise_for_status()
    items = (resp.json().get("data") or {}).get("items") or []
    return [item for item in items if isinstance(item, dict)]


def restaurant_codes() -> dict[str, str]:
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT id, name, url FROM restaurants").fetchall()
    cols = [r[1] for r in conn.execute("PRAGMA table_info(restaurants)")]
    conn.close()
    codes: dict[str, str] = {}
    for row in rows:
        url = row["url"] or ""
        parts = url.rstrip("/").split("/")
        try:
            idx = parts.index("restaurant")
            codes[parts[idx + 1]] = f"{row['id']} {row['name']}"
        except (ValueError, IndexError):
            continue
    return codes, cols


def main() -> int:
    codes, cols = restaurant_codes()
    print(f"db restaurants={len(codes)} columns={cols}")
    print(f"discount-ish columns: {[c for c in cols if 'discount' in c.lower() or 'deal' in c.lower()] or 'NONE'}")
    print()

    seen: dict[str, dict] = {}
    for label, lat, lng in PINS:
        print(f"-- fetching {label} {lat},{lng}")
        items = fetch_raw(lat, lng)
        print(f"   {len(items)} vendors")
        for item in items:
            code = item.get("code")
            if code and code not in seen:
                seen[code] = item
        time.sleep(2.0)

    print(f"\nunique vendors this crawl: {len(seen)}")
    in_snapshot = [c for c in seen if c in codes]
    print(f"overlap with snapshot 209: {len(in_snapshot)}")

    disc_shapes: Counter[str] = Counter()
    info_shapes: Counter[str] = Counter()
    nonempty_disc = 0
    nonempty_info = 0
    examples: list[dict] = []

    for code, item in seen.items():
        d = item.get("discounts") if "discounts" in item else "<MISSING KEY>"
        i = item.get("discounts_info") if "discounts_info" in item else "<MISSING KEY>"
        disc_shapes[_shape(d) if d != "<MISSING KEY>" else "MISSING KEY"] += 1
        info_shapes[_shape(i) if i != "<MISSING KEY>" else "MISSING KEY"] += 1
        if d != "<MISSING KEY>" and not _empty(d):
            nonempty_disc += 1
        if i != "<MISSING KEY>" and not _empty(i):
            nonempty_info += 1
        if (not _empty(d) and d != "<MISSING KEY>") or (
            not _empty(i) and i != "<MISSING KEY>"
        ):
            if len(examples) < 12:
                examples.append(
                    {
                        "in_snapshot": code in codes,
                        "ours": codes.get(code),
                        "code": code,
                        "name": item.get("name"),
                        "discounts": d,
                        "discounts_info": i,
                        "is_promoted": item.get("is_promoted"),
                        "tags": item.get("tags"),
                        "loyalty_programs": item.get("loyalty_programs"),
                    }
                )

    print("\ndiscounts shapes:", dict(disc_shapes))
    print("discounts_info shapes:", dict(info_shapes))
    print(f"non-empty discounts: {nonempty_disc}/{len(seen)}")
    print(f"non-empty discounts_info: {nonempty_info}/{len(seen)}")

    ours_nonempty = 0
    for code, item in seen.items():
        if code not in codes:
            continue
        d, i = item.get("discounts"), item.get("discounts_info")
        if not _empty(d) or not _empty(i):
            ours_nonempty += 1
    print(f"snapshot vendors with a non-empty deal field this crawl: {ours_nonempty}/{len(in_snapshot)}")

    # One raw item's top-level keys, so we can see what else is unused.
    sample = next(iter(seen.values()))
    print("\nraw vendor keys:", sorted(sample.keys()))

    print("\n=== examples (non-empty discounts or discounts_info) ===")
    print(json.dumps(examples, indent=2, default=str)[:12000])

    # Also dump every distinct discounts_info value if they are strings/short.
    info_values: Counter[str] = Counter()
    for item in seen.values():
        i = item.get("discounts_info")
        if _empty(i):
            info_values["<empty>"] += 1
        else:
            info_values[repr(i)[:200]] += 1
    print("\n=== discounts_info value frequencies (top 20) ===")
    for value, n in info_values.most_common(20):
        print(f"  {n:4d}  {value}")

    disc_values: Counter[str] = Counter()
    for item in seen.values():
        d = item.get("discounts")
        if _empty(d):
            disc_values["<empty>"] += 1
        else:
            disc_values[repr(d)[:240]] += 1
    print("\n=== discounts value frequencies (top 20) ===")
    for value, n in disc_values.most_common(20):
        print(f"  {n:4d}  {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
