"""Audit listing metadata completeness for new vs original restaurant rows."""

from __future__ import annotations

from scraperdb.database import get_connection

BASELINE = 84

STATS = """
SELECT
  COUNT(*) AS total,
  SUM(CASE WHEN address IS NULL OR address = '' THEN 1 ELSE 0 END) AS no_address,
  SUM(CASE WHEN rating IS NULL THEN 1 ELSE 0 END) AS no_rating,
  SUM(CASE WHEN cuisine IS NULL OR cuisine = '' THEN 1 ELSE 0 END) AS no_cuisine,
  SUM(CASE WHEN image_url IS NULL OR image_url = '' THEN 1 ELSE 0 END) AS no_image,
  SUM(CASE WHEN delivery_time IS NULL OR delivery_time = '' THEN 1 ELSE 0 END) AS no_eta,
  SUM(CASE WHEN review_count IS NULL THEN 1 ELSE 0 END) AS no_reviews
FROM restaurants
WHERE id {cmp} ?
"""


def main() -> None:
    conn = get_connection("foodpanda.db")
    for label, cmp_op in (("NEW (id>84)", ">"), ("ORIGINAL (id<=84)", "<=")):
        row = conn.execute(STATS.format(cmp=cmp_op), (BASELINE,)).fetchone()
        print(label, dict(row))
    print()
    for area in ("auhar", "ulshan"):
        by_addr = conn.execute(
            "SELECT COUNT(*) AS n FROM restaurants WHERE address LIKE ?",
            (f"%{area}%",),
        ).fetchone()["n"]
        by_name = conn.execute(
            "SELECT COUNT(*) AS n FROM restaurants WHERE address LIKE ? OR name LIKE ?",
            (f"%{area}%", f"%{area}%"),
        ).fetchone()["n"]
        print(f"{area}: address_only={by_addr} address_or_name={by_name}")
    print()
    orig_items = conn.execute(
        "SELECT COUNT(*) AS n FROM menu_items mi "
        "JOIN menu_categories mc ON mi.category_id = mc.id "
        "JOIN restaurants r ON mc.restaurant_id = r.id WHERE r.id <= ?",
        (BASELINE,),
    ).fetchone()["n"]
    areas = conn.execute(
        "SELECT COUNT(*) AS n FROM restaurants "
        "WHERE delivery_areas IS NOT NULL AND delivery_areas != ''"
    ).fetchone()["n"]
    legacy = conn.execute(
        "SELECT COUNT(*) AS n FROM restaurants WHERE id BETWEEN 16 AND 44"
    ).fetchone()["n"]
    print(f"original menu items (id<=84): {orig_items}")
    print(f"original ids 16-44 present:   {legacy}")
    print(f"rows with delivery_areas:     {areas}")
    print()
    print("Sample new rows:")
    for row in conn.execute(
        "SELECT id, name, rating, review_count, cuisine, address, delivery_time, "
        "image_url FROM restaurants WHERE id > ? LIMIT 5",
        (BASELINE,),
    ):
        print(f"  {dict(row)}")
    conn.close()


if __name__ == "__main__":
    main()
