"""Write remaining dry-run vendors, known 403 codes last, skip already in DB."""

from __future__ import annotations

from pathlib import Path

import discover_gulshan_jauhar as d
from scraperdb.database import get_connection

SRC = Path("gulshan_jauhar_dry_run.txt")
OUT = Path("gulshan_jauhar_dry_run_remaining.txt")
DEFER_CODES = {
    "oizv",
    "kz1i",
    "t6lb",
    "b4kx",
    "khqp",
    "qybr",
    "yki7",
    "n4vn",
    "fmd3",
    "x8hx",
    "m1v3",
    "t9pl",
}


def main() -> None:
    conn = get_connection("foodpanda.db")
    already = d.existing_vendor_codes(conn)
    conn.close()
    vendors = list(d.load_dry_run(SRC).values())
    remaining = [
        v
        for v in vendors
        if (v.get("code") or "").lower() not in already
    ]
    first = [v for v in remaining if (v.get("code") or "").lower() not in DEFER_CODES]
    last = [v for v in remaining if (v.get("code") or "").lower() in DEFER_CODES]
    first.sort(key=lambda v: v.get("name") or "")
    last.sort(key=lambda v: v.get("name") or "")
    ordered = first + last
    header = (
        f"Would insert {len(ordered)} remaining restaurants "
        f"({len(first)} fresh, {len(last)} previously 403/404 deferred)"
    )
    lines = [header, "reviews  pin  name  code  url"]
    for vendor in ordered:
        lines.append(
            f"{d._review_count(vendor):6}  "
            f"{vendor.get('discovered_from')}  "
            f"{vendor.get('name')}  "
            f"{vendor.get('code')}  "
            f"{vendor.get('url')}"
        )
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(header)
    print(f"Wrote {OUT}")
    print("Deferred last:")
    for vendor in last:
        print(f"  {vendor.get('code')}  {vendor.get('name')}")


if __name__ == "__main__":
    main()
