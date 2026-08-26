"""Trim Gulshan NIPA pin to top 23 by review_count. Dry-run file only."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import discover_gulshan_jauhar as d

NIPA = "Gulshan-e-Iqbal NIPA / Block 5"
KEEP_NIPA = 23
SRC = Path("gulshan_jauhar_dry_run.txt")
BACKUP = Path("gulshan_jauhar_dry_run_247.txt")


def main() -> None:
    vendors = list(d.load_dry_run(SRC).values())
    if not BACKUP.exists():
        BACKUP.write_text(SRC.read_text(encoding="utf-8"), encoding="utf-8")

    nipa = [v for v in vendors if v.get("discovered_from") == NIPA]
    other = [v for v in vendors if v.get("discovered_from") != NIPA]
    nipa.sort(
        key=lambda v: (-int(v.get("review_number") or 0), v.get("name") or "")
    )
    kept_nipa = nipa[:KEEP_NIPA]
    trimmed = other + kept_nipa
    trimmed.sort(key=lambda v: v.get("name") or "")

    header = (
        f"Would insert {len(trimmed)} restaurants "
        f"(NIPA pin trimmed to top {KEEP_NIPA} by review_count; "
        f"dropped {len(nipa) - KEEP_NIPA} NIPA candidates)"
    )
    lines = [header, "reviews  pin  name  code  url"]
    for vendor in trimmed:
        lines.append(
            f"{d._review_count(vendor):6}  "
            f"{vendor.get('discovered_from')}  "
            f"{vendor.get('name')}  "
            f"{vendor.get('code')}  "
            f"{vendor.get('url')}"
        )
    SRC.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(header)
    print(f"NIPA before={len(nipa)} kept={len(kept_nipa)} dropped={len(nipa) - KEEP_NIPA}")
    print("Kept NIPA (review_count desc):")
    for vendor in kept_nipa:
        print(f"  {d._review_count(vendor):6}  {vendor.get('name')}  {vendor.get('code')}")
    print("Pin breakdown (would-insert):")
    for pin, n in Counter(v.get("discovered_from") for v in trimmed).most_common():
        print(f"  {n:4}  {pin}")
    print(f"Wrote {SRC}")


if __name__ == "__main__":
    main()
