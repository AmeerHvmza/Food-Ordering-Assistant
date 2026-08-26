"""Area search must survive address spelling variants and delivery zones.

A Jauhar chai search once returned two restaurants because the only signal was
`address LIKE '%Jauhar%'`: the newly scraped rows had no address at all, and
the ones that did spell it "Johar".
"""

from __future__ import annotations

import sqlite3
import unittest

from db.geo import area_search_terms
from db.queries import location_match_sql


def _fixture_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE restaurants "
        "(id INTEGER, name TEXT, address TEXT, delivery_areas TEXT)"
    )
    conn.executemany(
        "INSERT INTO restaurants VALUES (?, ?, ?, ?)",
        [
            (1, "The MAFIA 360 - JAUHAR CHAPTER", "Gulistan e jauhar block 13", None),
            (2, "Pizza Day Night - Johar", "gulistan e Johar block 13", None),
            (3, "Quetta Mama Hotel", "Block 20, Gulistan-e-Johar", None),
            (7, "100 Shawarma", "iqra complex block 17 gulistan-e-jouhar", None),
            (8, "Roadside Dhaba", "Abdul Hafeez Jalandhari Rd, Block 20", None),
            (4, "Ek Cup Chai", None, "Gulistan-e-Jauhar"),
            (5, "Harmain Sharifain Tea Cafe", "Bahadurabad, Karachi", "Gulshan-e-Iqbal"),
            (6, "Red Apple - Tariq Road", "Allama Iqbal Road, Tariq Road", None),
        ],
    )
    return conn


def _matching_ids(conn: sqlite3.Connection, location: str) -> list[int]:
    clause, params = location_match_sql(location)
    return [
        row[0]
        for row in conn.execute(
            f"SELECT id FROM restaurants r WHERE {clause} ORDER BY id", params
        )
    ]


class AreaTermTests(unittest.TestCase):
    def test_jauhar_and_johar_are_the_same_area(self) -> None:
        self.assertEqual(area_search_terms("Jauhar"), area_search_terms("Johar"))
        self.assertEqual(
            area_search_terms("Gulistan-e-Jauhar"), area_search_terms("jauhar")
        )

    def test_unknown_area_falls_back_to_itself(self) -> None:
        self.assertEqual(area_search_terms("Korangi"), ["korangi"])


class LocationMatchTests(unittest.TestCase):
    def test_jauhar_matches_every_spelling_in_the_snapshot(self) -> None:
        conn = _fixture_conn()
        self.assertEqual(_matching_ids(conn, "Jauhar"), [1, 2, 3, 4, 7])
        conn.close()

    def test_jalandhari_road_is_not_mistaken_for_jauhar(self) -> None:
        conn = _fixture_conn()
        self.assertNotIn(8, _matching_ids(conn, "Jauhar"))
        conn.close()

    def test_row_with_no_address_matches_on_delivery_area(self) -> None:
        conn = _fixture_conn()
        self.assertIn(4, _matching_ids(conn, "Gulistan-e-Jauhar"))
        conn.close()

    def test_delivery_area_beats_a_misleading_address(self) -> None:
        # Addressed in Bahadurabad, but it delivers to Gulshan.
        conn = _fixture_conn()
        self.assertIn(5, _matching_ids(conn, "Gulshan"))
        conn.close()

    def test_other_areas_are_not_dragged_in(self) -> None:
        conn = _fixture_conn()
        self.assertEqual(_matching_ids(conn, "Tariq Road"), [6])
        conn.close()


if __name__ == "__main__":
    unittest.main()
