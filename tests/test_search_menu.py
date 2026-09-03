"""search_menu must keep a multi-word dish family, not cheaper single-word hits.

Live bug: 'cheese paratha' at New Quetta Ajwa Hotel returned Plain Paratha /
Cheese Omelette and none of the 8 Cheese Paratha items. Cause: word_match_sql
ORs terms, then ORDER BY price LIMIT 10 fills the page with cheaper
one-word matches. Not related to the restaurant name-match refactor.
"""

from __future__ import annotations

import sqlite3
import unittest

from db.queries import search_menu, session, word_match_all_sql, word_match_sql


CHEESE_PARATHA_FAMILY = {
    "Chicken Cheese Paratha",
    "Cheese Paratha",
    "Aloo Cheese Paratha",
    "Kabab Cheese Paratha",
    "Pizza Cheese Paratha",
    "Spicy Aalo Cheese Paratha",
    "Kabab Cheese Paratha Roll",
    "Anda Cheese Paratha Roll",
}


def _restaurant_id(conn: sqlite3.Connection, name_like: str) -> int:
    row = conn.execute(
        "SELECT id FROM restaurants WHERE name LIKE ? LIMIT 1",
        (name_like,),
    ).fetchone()
    if row is None:
        raise unittest.SkipTest(f"snapshot has no restaurant matching {name_like!r}")
    return int(row["id"])


def _names_containing(conn: sqlite3.Connection, restaurant_id: int, needle: str) -> set[str]:
    rows = conn.execute(
        """
        SELECT mi.name
        FROM menu_items mi
        JOIN menu_categories mc ON mc.id = mi.category_id
        WHERE mc.restaurant_id = ? AND LOWER(mi.name) LIKE ?
        """,
        (restaurant_id, f"%{needle.lower()}%"),
    ).fetchall()
    return {row["name"] for row in rows}


class WordMatchAllTests(unittest.TestCase):
    def test_or_keeps_single_word_hits_and_drops_them(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE t (name TEXT)")
        conn.executemany(
            "INSERT INTO t VALUES (?)",
            [("Plain Paratha",), ("Cheese Omelette",), ("Cheese Paratha",)],
        )
        or_clause, or_params = word_match_sql(["name"], ["cheese", "paratha"])
        or_names = {
            row[0]
            for row in conn.execute(f"SELECT name FROM t WHERE {or_clause}", or_params)
        }
        and_clause, and_params = word_match_all_sql(["name"], ["cheese", "paratha"])
        and_names = {
            row[0]
            for row in conn.execute(
                f"SELECT name FROM t WHERE {and_clause}", and_params
            )
        }
        self.assertEqual(
            or_names, {"Plain Paratha", "Cheese Omelette", "Cheese Paratha"}
        )
        self.assertEqual(and_names, {"Cheese Paratha"})
        conn.close()


class CheeseParathaMenuTests(unittest.TestCase):
    def test_ajwa_cheese_paratha_returns_the_whole_family(self) -> None:
        with session() as conn:
            rid = _restaurant_id(conn, "%New Quetta Ajwa Hotel%")
            in_db = _names_containing(conn, rid, "cheese paratha")
            self.assertTrue(
                CHEESE_PARATHA_FAMILY.issubset(in_db),
                f"snapshot missing expected items: {CHEESE_PARATHA_FAMILY - in_db}",
            )
            # Same cap the chat tool uses (agent.tools.MAX_MENU_RESULTS).
            rows = search_menu(conn, restaurant_id=rid, query="cheese paratha", limit=10)
        names = {row["name"] for row in rows}
        missing = CHEESE_PARATHA_FAMILY - names
        self.assertFalse(
            missing,
            f"search_menu dropped exact Cheese Paratha items: {missing}; got {names}",
        )
        cheap_decoys = {"Paratha", "Sulemani Paratha", "Cheese Omelette"}
        self.assertFalse(
            names & cheap_decoys,
            f"single-word decoys leaked into the result: {names & cheap_decoys}",
        )

    def test_ghousia_chicken_cheese_returns_name_family(self) -> None:
        with session() as conn:
            rid = _restaurant_id(conn, "%Ghousia Fast Food%")
            family = {
                name
                for name in _names_containing(conn, rid, "chicken")
                if "cheese" in name.lower()
            }
            self.assertGreaterEqual(len(family), 3)
            rows = search_menu(
                conn, restaurant_id=rid, query="chicken cheese", limit=10
            )
        names = {row["name"] for row in rows}
        missing = family - names
        # Family is 9 items and limit is 10, so all must survive.
        self.assertFalse(
            missing,
            f"search_menu dropped chicken cheese items: {missing}; got {names}",
        )
        for name in names:
            lowered = name.lower()
            self.assertIn("chicken", lowered)
            self.assertIn("cheese", lowered)


if __name__ == "__main__":
    unittest.main()
