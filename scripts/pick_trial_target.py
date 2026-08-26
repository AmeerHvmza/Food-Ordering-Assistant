"""Pick a Gulshan/Jauhar restaurant to use as a daily-refresh trial target."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

db = Path(sys.argv[1] if len(sys.argv) > 1 else "trial_copy.db")
conn = sqlite3.connect(str(db))
conn.row_factory = sqlite3.Row
rows = conn.execute(
    """
    SELECT r.id, r.name, r.rating, r.review_count, r.cuisine, r.address,
           r.delivery_areas, r.updated_at,
           (SELECT COUNT(*) FROM menu_items mi
            JOIN menu_categories mc ON mc.id = mi.category_id
            WHERE mc.restaurant_id = r.id) AS items
    FROM restaurants r
    WHERE r.delivery_areas LIKE '%Gulshan%'
      AND r.rating IS NOT NULL
    ORDER BY r.review_count DESC
    LIMIT 5
    """
).fetchall()
for row in rows:
    print(dict(row))
conn.close()
