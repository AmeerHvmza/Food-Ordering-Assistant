"""Ad-hoc check: does an area + craving search actually find restaurants?"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db import geo, queries  # noqa: E402


def main() -> None:
    location = sys.argv[1] if len(sys.argv) > 1 else "Jauhar"
    craving = sys.argv[2] if len(sys.argv) > 2 else "chai"
    print(f"location={location!r} craving={craving!r}")
    print(f"area_search_terms={geo.area_search_terms(location)}")
    like, params = queries.location_match_sql(location)
    print(f"params={params}")

    with queries.session() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM restaurants r WHERE {like}", params
        ).fetchone()["n"]
        print(f"\nrestaurants matching area: {total}")

        rows = queries.search_restaurants(
            conn, craving=craving, location=location, limit=200
        )
    print(f"search_restaurants hits for '{craving}': {len(rows)}")
    for row in rows[:12]:
        print(
            f"  id={row['id']:>3} | {(row['name'] or '')[:38]:<38} | "
            f"{row.get('match_source'):<14} | "
            f"areas={row.get('delivery_areas') or '-'} | "
            f"addr={(row.get('address') or 'NULL')[:40]}"
        )


if __name__ == "__main__":
    main()
