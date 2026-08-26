"""Tests for manual review import and get_reviews tool."""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "foodpanda-scraper"))

from agent.tools import get_reviews  # noqa: E402
from db import queries  # noqa: E402
from scraperdb.database import init_db  # noqa: E402
from scripts.import_manual_reviews import (  # noqa: E402
    ParsedSection,
    build_liked_json,
    import_sections,
    match_restaurant,
    parse_section_lines,
    split_sections,
)


SAMPLE_SECTION = """\
New Quetta Test Hotel:
top:Farheen
3 days ago
Chai was very tasty.
Helpful

Batool
Top Reviewer
1 month ago
The parathas were fresh.
Liked 1 dishes: Aloo Cheese Paratha Rs. 200
Helpful

Samreen
2 months ago
Tea was good.
Liked: Doodh Patti Chai Rs. 80, Lachha Paratha Rs. 90
Restaurant response: We apologize and will improve.
Helpful

Sobia
6 months ago
Aalo ka paratha fifty fifty.
Liked: Kashmeiri Tea Rs. 270, Aalo Paratha Rs. 220
"""


class ParserTests(unittest.TestCase):
    def test_split_and_parse_sample(self) -> None:
        sections = split_sections(SAMPLE_SECTION)
        self.assertEqual(len(sections), 1)
        self.assertEqual(sections[0].header, "New Quetta Test Hotel")
        self.assertEqual(len(sections[0].reviews), 4)
        self.assertIn("tasty", sections[0].reviews[0].review_text.lower())
        self.assertEqual(sections[0].reviews[1].liked_raw, ["Aloo Cheese Paratha"])
        self.assertEqual(
            sections[0].reviews[2].owner_response,
            "We apologize and will improve.",
        )
        # Missing Helpful before next header — last review kept via section end.
        self.assertEqual(len(sections[0].reviews[3].liked_raw), 2)

    def test_top_prefix_reviewer_discarded(self) -> None:
        section = parse_section_lines(
            "X",
            ["top:SecretName", "2 days ago", "Body text here", "Helpful"],
        )
        self.assertEqual(len(section.reviews), 1)
        self.assertEqual(section.reviews[0].review_text, "Body text here")
        self.assertNotIn("SecretName", section.reviews[0].review_text)


class ImportDbTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db_path = Path(self.tmp.name)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE restaurants (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                url TEXT UNIQUE NOT NULL
            );
            CREATE TABLE menu_categories (
                id INTEGER PRIMARY KEY,
                restaurant_id INTEGER NOT NULL,
                category_name TEXT NOT NULL
            );
            CREATE TABLE menu_items (
                id INTEGER PRIMARY KEY,
                category_id INTEGER NOT NULL,
                name TEXT NOT NULL
            );
            """
        )
        init_db(self.conn)
        self.conn.execute(
            "INSERT INTO restaurants (id, name, url) VALUES (1, 'New Quetta Test Hotel', 'http://x/1')"
        )
        self.conn.execute(
            "INSERT INTO menu_categories (id, restaurant_id, category_name) VALUES (10, 1, 'Paratha')"
        )
        self.conn.executemany(
            "INSERT INTO menu_items (id, category_id, name) VALUES (?, 10, ?)",
            [
                (100, "Aloo Cheese Paratha"),
                (101, "Doodh Patti Chai"),
                (102, "Lachha Paratha"),
                (103, "Kashmiri Tea"),
                (104, "Aloo Paratha"),
            ],
        )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()
        self.db_path.unlink(missing_ok=True)

    def test_restaurant_match_threshold(self) -> None:
        rows = list(self.conn.execute("SELECT id, name FROM restaurants"))
        hit, _ = match_restaurant("New Quetta Test Hotel", rows)
        self.assertIsNotNone(hit)
        miss, reason = match_restaurant("New Quetta Agha Hotel", rows)
        self.assertIsNone(miss)
        self.assertIn("below", reason)

    def test_dedupe_on_rerun(self) -> None:
        sections = split_sections(SAMPLE_SECTION)
        stats1 = import_sections(self.conn, sections)
        stats2 = import_sections(self.conn, sections)
        self.assertEqual(stats1["inserted"], 4)
        self.assertEqual(stats2["inserted"], 0)
        self.assertEqual(stats2["dupes"], 4)
        count = self.conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
        self.assertEqual(count, 4)

    def test_liked_dish_item_ids(self) -> None:
        menu = [
            {"id": 100, "name": "Aloo Cheese Paratha"},
            {"id": 101, "name": "Doodh Patti Chai"},
        ]
        liked = build_liked_json(["Aloo Cheese Paratha Rs. 200"], menu, 1)
        self.assertIsNotNone(liked)
        assert liked is not None
        self.assertEqual(liked[0]["item_id"], 100)


class GetReviewsToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db_path = Path(self.tmp.name)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        init_db(self.conn)
        self.conn.execute(
            "INSERT INTO restaurants (id, name, url, rating) VALUES (5, 'Ek Cup Chai', 'http://x/5', 4.5)"
        )
        liked = json.dumps([{"name": "Anda Paratha", "item_id": 55}])
        self.conn.execute(
            """
            INSERT INTO reviews (restaurant_id, review_text, liked_dishes, owner_response, source, imported_at)
            VALUES (5, 'Paratha was good.', ?, NULL, 'manual_sample', '2026-08-25T00:00:00+00:00')
            """,
            (liked,),
        )
        self.conn.commit()
        self.conn.close()

    def tearDown(self) -> None:
        self.db_path.unlink(missing_ok=True)

    def test_get_reviews_populated_and_empty(self) -> None:
        with queries.session(self.db_path) as conn:
            rows = queries.list_reviews(conn, 5)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["liked_dishes"][0]["item_id"], 55)
            self.assertFalse(queries.restaurant_has_reviews(conn, 999))

        with unittest.mock.patch.object(queries, "DEFAULT_DB_PATH", self.db_path):
            text = get_reviews.invoke({"state": {}, "restaurant_id": 5})
            self.assertIn("Paratha was good", text)
            self.assertIn("item_id=55", text)
            empty = get_reviews.invoke({"state": {}, "restaurant_id": 999})
            self.assertIn("No restaurant", empty)


if __name__ == "__main__":
    unittest.main()
