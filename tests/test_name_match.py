"""Shared restaurant-name matcher: CONVERSATIONAL, IMPORT, SEARCH_PROMOTE."""

from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from db import name_match, queries  # noqa: E402
from db.name_match import CONVERSATIONAL, IMPORT, SEARCH_PROMOTE  # noqa: E402


def _live_rows() -> list[dict]:
    conn = sqlite3.connect(str(REPO_ROOT / "foodpanda-scraper" / "foodpanda.db"))
    conn.row_factory = sqlite3.Row
    try:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT id, name, address, delivery_areas FROM restaurants"
            )
        ]
    finally:
        conn.close()


class ScoringTests(unittest.TestCase):
    def test_normalize_folds_johar_and_punctuation(self) -> None:
        self.assertEqual(
            name_match.normalize_name("Dunkin' - Johar"),
            "dunkin jauhar",
        )

    def test_jacksons_single_token_is_no_match(self) -> None:
        names = [r["name"] for r in _live_rows()]
        decision = name_match.decide_name_match("Jackson's", names, CONVERSATIONAL)
        self.assertEqual(decision.kind, "no_match")
        self.assertIn("1 token", decision.reason or "")


class ConversationalLiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rows = _live_rows()
        self.names = [r["name"] for r in self.rows]
        self.by_id = {r["id"]: r for r in self.rows}

    def _resolve(self, query: str, location: str | None = None):
        conn = sqlite3.connect(str(REPO_ROOT / "foodpanda-scraper" / "foodpanda.db"))
        conn.row_factory = sqlite3.Row
        try:
            return queries.resolve_restaurant_by_name(conn, query, location=location)
        finally:
            conn.close()

    def test_exact_full_name(self) -> None:
        hit, err = self._resolve("Pizza Day Night - Johar", "Gulistan-e-Jauhar")
        self.assertIsNone(err)
        self.assertEqual(hit["id"], 174)

    def test_casual_name(self) -> None:
        hit, err = self._resolve("pizza day night", "Gulistan-e-Jauhar")
        self.assertIsNone(err)
        self.assertEqual(hit["id"], 174)

    def test_reordered_words(self) -> None:
        hit, err = self._resolve("day night pizza", "Gulistan-e-Jauhar")
        self.assertIsNone(err)
        self.assertEqual(hit["id"], 174)

    def test_missing_word(self) -> None:
        hit, err = self._resolve("pizza night", "Gulistan-e-Jauhar")
        self.assertIsNone(err)
        self.assertEqual(hit["id"], 174)

    def test_generic_pizza_does_not_resolve(self) -> None:
        hit, err = self._resolve("pizza", "Gulistan-e-Jauhar")
        self.assertIsNone(hit)
        self.assertIsNotNone(err)
        self.assertTrue((err or "").startswith("NO_MATCH"))

    def test_branches_without_location_are_ambiguous(self) -> None:
        hit, err = self._resolve("pizza day night", location=None)
        self.assertIsNone(hit)
        self.assertIsNotNone(err)
        self.assertTrue((err or "").startswith("AMBIGUOUS"))
        self.assertIn("Johar", err or "")
        self.assertIn("FB Area", err or "")

    def test_location_picks_jauhar_branch(self) -> None:
        hit, err = self._resolve("pizza day night", "Gulistan-e-Jauhar")
        self.assertIsNone(err)
        self.assertEqual(hit["id"], 174)
        self.assertNotEqual(hit["id"], 229)

    def test_quetta_agha_does_not_cross_to_ajwa_awami(self) -> None:
        hit, err = self._resolve("new quetta agha", "Gulistan-e-Jauhar")
        self.assertIsNone(err)
        self.assertEqual(hit["id"], 164)
        self.assertNotIn(hit["id"], {165, 166, 167})

    def test_quetta_ajwa(self) -> None:
        hit, err = self._resolve("quetta ajwa", "Gulistan-e-Jauhar")
        self.assertIsNone(err)
        self.assertEqual(hit["id"], 166)

    def test_search_promote_lifts_pizza_day_night(self) -> None:
        names = [
            "CAESAR'S PIZZA - Gulistan e Johar",
            "Dunkin' - Johar",
            "Pizza Day Night - Johar",
            "The Big Pizza - Johar",
        ]
        decision = name_match.decide_name_match(
            "pizza day night", names, SEARCH_PROMOTE
        )
        self.assertEqual(decision.kind, "match")
        self.assertEqual(names[decision.best_index], "Pizza Day Night - Johar")


class ImportProfileTests(unittest.TestCase):
    def test_exact_header(self) -> None:
        names = [r["name"] for r in _live_rows()]
        decision = name_match.decide_name_match(
            "New Quetta Agha Hotel", names, IMPORT
        )
        self.assertEqual(decision.kind, "match")
        self.assertEqual(names[decision.best_index], "New Quetta Agha Hotel")

    def test_truncated_header_refuses_ajwa(self) -> None:
        names = [r["name"] for r in _live_rows()]
        decision = name_match.decide_name_match("New Quetta Agha", names, IMPORT)
        self.assertNotEqual(decision.kind, "match")


class PromptAmbiguousGuidanceTests(unittest.TestCase):
    def test_prompts_require_clarifying_question(self) -> None:
        from agent.prompts import ROLE_PROMPT, ROUTING_PROMPT

        blob = (ROLE_PROMPT + ROUTING_PROMPT).lower()
        self.assertIn("ambiguous", blob)
        self.assertIn("do not say", blob)
        self.assertTrue("couldn't find" in blob or "not found" in blob)


if __name__ == "__main__":
    unittest.main()
