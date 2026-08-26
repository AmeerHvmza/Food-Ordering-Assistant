"""Name-match promotion and get_reviews by spoken restaurant name."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "foodpanda-scraper"))

from agent.tools import (  # noqa: E402
    MAX_RESTAURANT_RESULTS,
    _promote_name_matches,
    get_reviews,
    order_restaurant_hits,
)
from db import queries, ranking  # noqa: E402
from scraperdb.database import init_db  # noqa: E402


class NameMatchPromotionTests(unittest.TestCase):
    def test_pizza_day_night_surfaces_in_top_five(self) -> None:
        conn = sqlite3.connect(str(REPO_ROOT / "foodpanda-scraper" / "foodpanda.db"))
        conn.row_factory = sqlite3.Row
        try:
            craving = "pizza day night"
            location = "Gulistan-e-Jauhar"
            rows = queries.search_restaurants(
                conn, craving=craving, location=location, limit=25
            )
            m, c = ranking.compute_m_and_c(queries.dataset_rows(conn))
            ranked = order_restaurant_hits(
                ranking.rank_restaurants(rows, m=m, C=c),
                craving,
            )
            ranked = _promote_name_matches(ranked, craving)
            top = ranked[:MAX_RESTAURANT_RESULTS]
            ids = [r["id"] for r in top]
            self.assertIn(174, ids)
            self.assertEqual(top[0]["id"], 174)
        finally:
            conn.close()

    def test_resolve_by_spoken_name(self) -> None:
        conn = sqlite3.connect(str(REPO_ROOT / "foodpanda-scraper" / "foodpanda.db"))
        conn.row_factory = sqlite3.Row
        try:
            hit, err = queries.resolve_restaurant_by_name(
                conn,
                "Pizza Day Night",
                location="Gulistan-e-Jauhar",
            )
            self.assertIsNone(err)
            self.assertIsNotNone(hit)
            assert hit is not None
            self.assertEqual(hit["id"], 174)
        finally:
            conn.close()


class GetReviewsByNameTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db_path = Path(self.tmp.name)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        init_db(self.conn)
        self.conn.execute(
            "INSERT INTO restaurants (id, name, url, delivery_areas) "
            "VALUES (174, 'Pizza Day Night - Johar', 'http://x/174', 'Gulistan-e-Jauhar')"
        )
        self.conn.execute(
            """
            INSERT INTO reviews
                (restaurant_id, review_text, liked_dishes, owner_response, source, imported_at)
            VALUES (174, 'Great pizza for the price.', NULL, NULL, 'manual_sample', '2026-08-25')
            """
        )
        self.conn.commit()
        self.conn.close()

    def tearDown(self) -> None:
        self.db_path.unlink(missing_ok=True)

    def test_get_reviews_by_name(self) -> None:
        with unittest.mock.patch.object(queries, "DEFAULT_DB_PATH", self.db_path):
            state = {"location": "Gulistan-e-Jauhar"}
            text = get_reviews.invoke(
                {"state": state, "restaurant_name": "Pizza Day Night"}
            )
            self.assertIn("Great pizza for the price", text)
            self.assertIn("id=174", text)


if __name__ == "__main__":
    unittest.main()
