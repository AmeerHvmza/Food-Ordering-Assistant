"""Restaurant search should not rank bakeries first for a spicy craving."""

from __future__ import annotations

import sqlite3
import unittest

from agent.tools import order_restaurant_hits
from db.queries import word_match_sql


class WordMatchTests(unittest.TestCase):
    def test_desi_does_not_match_desire(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE t (name TEXT)")
        conn.execute("INSERT INTO t VALUES ('Chocolate Heaven & Desire Pastry')")
        conn.execute("INSERT INTO t VALUES ('Experience true desire with strawberry')")
        conn.execute("INSERT INTO t VALUES ('Khopra Pak is a desiccated coconut sweet')")
        conn.execute("INSERT INTO t VALUES ('Desi Ghee (400gms)')")
        conn.execute("INSERT INTO t VALUES ('Chicken Biryani with spicy marinade')")
        clause, params = word_match_sql(["name"], ["desi"])
        names = [
            row[0]
            for row in conn.execute(f"SELECT name FROM t WHERE {clause}", params)
        ]
        self.assertEqual(names, ["Desi Ghee (400gms)"])
        conn.close()

    def test_spicy_matches_whole_word(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE t (name TEXT)")
        conn.execute("INSERT INTO t VALUES ('Plain Spicy - Regular Pack')")
        conn.execute("INSERT INTO t VALUES ('Strawberry cup cake')")
        clause, params = word_match_sql(["name"], ["spicy"])
        names = [
            row[0]
            for row in conn.execute(f"SELECT name FROM t WHERE {clause}", params)
        ]
        self.assertEqual(names, ["Plain Spicy - Regular Pack"])
        conn.close()


class OrderHitsTests(unittest.TestCase):
    def test_dessert_only_drops_below_savoury_for_spicy(self) -> None:
        rows = [
            {
                "name": "Rehmat-e-Shereen - Garden",
                "cuisine": "Cakes & Bakery, Desserts",
                "match_source": "menu_item",
                "weighted_rating": 4.86,
            },
            {
                "name": "Delizia - Garden",
                "cuisine": "Cakes & Bakery, Desserts",
                "match_source": "menu_item",
                "weighted_rating": 4.85,
            },
            {
                "name": "United King - Garden",
                "cuisine": "Cakes & Bakery, Wraps & Rolls, Samosa, Pakistani",
                "match_source": "menu_item",
                "weighted_rating": 4.83,
            },
            {
                "name": "Al Syed Biryani And Pakwan Center",
                "cuisine": "Pakistani, Biryani",
                "match_source": "menu_item",
                "weighted_rating": 4.82,
            },
        ]
        ordered = order_restaurant_hits(rows, "desi spicy")
        names = [r["name"] for r in ordered]
        self.assertEqual(names[0], "United King - Garden")
        self.assertEqual(names[1], "Al Syed Biryani And Pakwan Center")
        self.assertTrue(names[2].startswith("Rehmat") or names[2].startswith("Delizia"))

    def test_dessert_stays_first_for_cake_craving(self) -> None:
        rows = [
            {
                "name": "Rehmat-e-Shereen - Garden",
                "cuisine": "Cakes & Bakery, Desserts",
                "match_source": "menu_item",
                "weighted_rating": 4.86,
            },
            {
                "name": "Al Syed Biryani And Pakwan Center",
                "cuisine": "Pakistani, Biryani",
                "match_source": "menu_item",
                "weighted_rating": 4.82,
            },
        ]
        ordered = order_restaurant_hits(rows, "cake")
        self.assertEqual(ordered[0]["name"], "Rehmat-e-Shereen - Garden")


if __name__ == "__main__":
    unittest.main()
