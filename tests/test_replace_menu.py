"""replace_restaurant_menu keeps restaurant.id and is all-or-nothing."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "foodpanda-scraper"))

from scraperdb.database import get_connection, init_db, insert_restaurant_with_menu, replace_restaurant_menu


class ReplaceMenuTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.conn = get_connection(self.tmp.name)
        init_db(self.conn)
        self.rid = insert_restaurant_with_menu(
            self.conn,
            {
                "name": "Old Place",
                "url": "https://foodpanda.pk/restaurant/aaaa/old-place",
                "rating": 4.5,
                "cuisine": "Pakistani",
                "address": "Saddar",
                "delivery_time": "45.0 min",
                "image_url": None,
                "review_number": 4000,
            },
            [
                {
                    "category_name": "Mains",
                    "items": [{"name": "Biryani", "price": "500", "description": None, "image_url": None}],
                }
            ],
        )

    def tearDown(self) -> None:
        self.conn.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def test_keeps_id_and_replaces_items(self) -> None:
        replace_restaurant_menu(
            self.conn,
            self.rid,
            {
                "name": "Old Place",
                "rating": 4.6,
                "cuisine": "Pakistani",
                "address": "Saddar",
                "delivery_time": "40.0 min",
                "image_url": None,
                "review_number": 4100,
            },
            [
                {
                    "category_name": "Mains",
                    "items": [
                        {"name": "Biryani", "price": "550", "description": None, "image_url": None},
                        {"name": "Karahi", "price": "900", "description": None, "image_url": None},
                    ],
                }
            ],
            "2026-08-18T00:00:00Z",
        )
        row = self.conn.execute(
            "SELECT id, rating, review_count, updated_at FROM restaurants WHERE id = ?",
            (self.rid,),
        ).fetchone()
        self.assertEqual(row["id"], self.rid)
        self.assertEqual(row["review_count"], 4100)
        self.assertEqual(row["updated_at"], "2026-08-18T00:00:00Z")
        names = [
            r[0]
            for r in self.conn.execute(
                """
                SELECT mi.name FROM menu_items mi
                JOIN menu_categories mc ON mc.id = mi.category_id
                WHERE mc.restaurant_id = ?
                ORDER BY mi.name
                """,
                (self.rid,),
            )
        ]
        self.assertEqual(names, ["Biryani", "Karahi"])


if __name__ == "__main__":
    unittest.main()
