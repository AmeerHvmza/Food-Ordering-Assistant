"""List the area spellings that actually occur in scraped addresses.

Aliases in db/geo.py should be driven by this, not by guesswork.
"""

from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db import queries  # noqa: E402

# Rough shapes of the areas whose spelling varies most.
PATTERNS = {
    "jauhar": r"[jg]\w*[uoa]\w*har",
    "gulshan": r"gulshan[\w-]*",
    "nazimabad": r"\w*nazimabad",
}


def main() -> None:
    with queries.session() as conn:
        addresses = [
            (row["address"] or "").lower()
            for row in conn.execute("SELECT address FROM restaurants")
        ]
    for label, pattern in PATTERNS.items():
        counts: Counter[str] = Counter()
        for address in addresses:
            for hit in re.findall(pattern, address):
                counts[hit] += 1
        print(f"\n{label}:")
        for spelling, n in counts.most_common(15):
            print(f"  {n:4}  {spelling}")


if __name__ == "__main__":
    main()
