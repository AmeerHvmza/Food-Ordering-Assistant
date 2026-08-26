"""Daily refresh must never lose yesterday's data.

Every test runs against a temp database with the network faked out. The rules
being pinned down here are the ones where a plausible-looking implementation
silently destroys data:

  - a partial listing response must not blank the columns it omits
  - delivery_areas only ever widens
  - a restaurant that is merely closed must not drift toward 'gone'
  - a PerimeterX 403 is evidence about our access, never about a restaurant
  - a price regression must not be published
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "foodpanda-scraper"))

from scheduler import daily_scrape  # noqa: E402
from scraperdb.database import (  # noqa: E402
    get_connection,
    init_db,
    insert_restaurant_with_menu,
    merge_delivery_areas,
)

PIN = [("Gulshan-e-Iqbal", 24.9180, 67.0910)]


def _menu(price: str = "250") -> list[dict]:
    return [
        {
            "category_name": "Mains",
            "items": [
                {
                    "name": "Chicken Karahi",
                    "price": price,
                    "description": None,
                    "image_url": None,
                }
            ],
        }
    ]


class DailyScrapeCase(unittest.TestCase):
    """Temp DB with one fully-populated restaurant, network stubbed out."""

    def setUp(self) -> None:
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self.db = Path(tmp.name)

        conn = get_connection(str(self.db))
        init_db(conn)
        self.rid = insert_restaurant_with_menu(
            conn,
            {
                "name": "Cafe Akbar",
                "url": "https://foodpanda.pk/restaurant/aaaa/cafe-akbar",
                "rating": 4.5,
                "cuisine": "Pakistani, BBQ",
                "address": "Block 5, Gulshan-e-Iqbal",
                "delivery_time": "40 min",
                "image_url": "https://img/akbar.jpg",
                "review_number": 4000,
            },
            _menu(),
        )
        conn.execute(
            "UPDATE restaurants SET delivery_areas = 'Gulshan-e-Iqbal' WHERE id = ?",
            (self.rid,),
        )
        conn.commit()
        conn.close()

        # Real sleeps would make this suite take minutes.
        patcher = mock.patch.object(daily_scrape, "_rate_limit", lambda: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._cleanup_files)

    def _cleanup_files(self) -> None:
        for suffix in ("", ".scraping", ".rejected", ".bak"):
            Path(str(self.db) + suffix).unlink(missing_ok=True)

    def row(self) -> sqlite3.Row:
        conn = get_connection(str(self.db))
        try:
            return conn.execute(
                "SELECT * FROM restaurants WHERE id = ?", (self.rid,)
            ).fetchone()
        finally:
            conn.close()

    def item_names(self) -> list[str]:
        conn = get_connection(str(self.db))
        try:
            return [
                r["name"]
                for r in conn.execute(
                    """
                    SELECT mi.name FROM menu_items mi
                    JOIN menu_categories mc ON mc.id = mi.category_id
                    WHERE mc.restaurant_id = ? ORDER BY mi.name
                    """,
                    (self.rid,),
                )
            ]
        finally:
            conn.close()

    def run_refresh(
        self,
        *,
        vendors=None,
        meta=None,
        menu=None,
        menu_status: int | None = 200,
        expect: int = 0,
    ) -> int:
        """Run one refresh with the API faked. Returns the exit code."""

        def fake_fetch_vendors(lat, lng, limit=48, offset=0, sort=None):
            if offset or vendors is None:
                return []
            return list(vendors)

        def fake_fetch_vendor_meta(code, lat=None, lng=None):
            daily_scrape.api_client.LAST_MENU_STATUS = menu_status
            return meta

        def fake_scrape_menu(restaurant, page=None, lat=None, lng=None):
            daily_scrape.api_client.LAST_MENU_STATUS = menu_status
            return list(menu) if menu else []

        with mock.patch.object(
            daily_scrape.api_client, "fetch_vendors", fake_fetch_vendors
        ), mock.patch.object(
            daily_scrape.api_client, "fetch_vendor_meta", fake_fetch_vendor_meta
        ), mock.patch.object(
            daily_scrape, "scrape_menu", fake_scrape_menu
        ), mock.patch.object(
            daily_scrape.areas, "refresh_pins", lambda: PIN
        ):
            code = daily_scrape.refresh_database(self.db, run_label="test")
        self.assertEqual(code, expect)
        return code


class ListingPreservationTests(DailyScrapeCase):
    def test_partial_listing_does_not_blank_other_columns(self) -> None:
        """The bug this guards: a stub carrying only rating/reviews used to be
        written literally, nulling address, cuisine and image."""
        before = self.row()
        self.run_refresh(
            vendors=[
                {
                    "code": "aaaa",
                    "name": "Cafe Akbar",
                    "rating": 4.7,
                    "review_number": 4200,
                    # no cuisine / address / delivery_time / image_url today
                }
            ],
            menu=_menu("300"),
        )
        after = self.row()
        self.assertEqual(after["rating"], 4.7)
        self.assertEqual(after["review_count"], 4200)
        self.assertEqual(after["cuisine"], before["cuisine"])
        self.assertEqual(after["address"], before["address"])
        self.assertEqual(after["delivery_time"], before["delivery_time"])
        self.assertEqual(after["image_url"], before["image_url"])
        self.assertIsNotNone(after["updated_at"])

    def test_menu_is_replaced_on_success(self) -> None:
        self.run_refresh(
            vendors=[{"code": "aaaa", "name": "Cafe Akbar", "rating": 4.6}],
            menu=[
                {
                    "category_name": "Mains",
                    "items": [
                        {"name": "Mutton Karahi", "price": "1200"},
                        {"name": "Roti", "price": "30"},
                    ],
                }
            ],
        )
        self.assertEqual(self.item_names(), ["Mutton Karahi", "Roti"])

    def test_menu_failure_keeps_yesterdays_menu(self) -> None:
        self.run_refresh(
            vendors=[{"code": "aaaa", "name": "Cafe Akbar", "rating": 4.6}],
            menu=[],
            expect=0,
        )
        self.assertEqual(self.item_names(), ["Chicken Karahi"])
        self.assertEqual(self.row()["rating"], 4.6)


class DeliveryAreaTests(DailyScrapeCase):
    def test_areas_widen_never_narrow(self) -> None:
        self.assertEqual(
            merge_delivery_areas("Gulshan-e-Iqbal", ["Gulistan-e-Jauhar"]),
            "Gulistan-e-Jauhar, Gulshan-e-Iqbal",
        )
        self.assertEqual(merge_delivery_areas("Gulshan-e-Iqbal", []), "Gulshan-e-Iqbal")
        self.assertEqual(merge_delivery_areas(None, ["Saddar"]), "Saddar")

    def test_refresh_records_the_pin_that_saw_the_vendor(self) -> None:
        self.run_refresh(
            vendors=[{"code": "aaaa", "name": "Cafe Akbar", "rating": 4.6}],
            menu=_menu(),
        )
        self.assertEqual(self.row()["delivery_areas"], "Gulshan-e-Iqbal")

    def test_absence_does_not_clear_recorded_areas(self) -> None:
        self.run_refresh(vendors=[], meta=None, menu=[], menu_status=404)
        self.assertEqual(self.row()["delivery_areas"], "Gulshan-e-Iqbal")


class AbsenceTests(DailyScrapeCase):
    def test_seen_in_feed_resets_streak(self) -> None:
        conn = get_connection(str(self.db))
        conn.execute(
            "UPDATE restaurants SET missing_streak = 3 WHERE id = ?", (self.rid,)
        )
        conn.commit()
        conn.close()

        self.run_refresh(
            vendors=[{"code": "aaaa", "name": "Cafe Akbar", "rating": 4.6}],
            menu=_menu(),
        )
        after = self.row()
        self.assertEqual(after["missing_streak"], 0)
        self.assertEqual(after["availability"], "listed")
        self.assertIsNotNone(after["last_seen_at"])

    def test_conclusive_absence_increments_streak(self) -> None:
        self.run_refresh(vendors=[], meta=None, menu=[], menu_status=404)
        self.assertEqual(self.row()["missing_streak"], 1)

    def test_closed_but_reachable_vendor_never_drifts_toward_gone(self) -> None:
        """The load-bearing guard for the single 21:00 window.

        74 of 209 rows are morning-only businesses that are shut at 21:00 and
        will be missing from every dinner feed. fd-api still answers for them,
        and that answer is proof they exist.
        """
        for _ in range(MISSING_THRESHOLD_PROBE):
            self.run_refresh(
                vendors=[],
                meta={"code": "aaaa", "name": "Cafe Akbar", "rating": 4.5},
                menu=_menu(),
            )
        after = self.row()
        self.assertEqual(after["missing_streak"], 0)
        self.assertEqual(after["availability"], "listed")

    def test_403_absence_is_inconclusive(self) -> None:
        """A block says nothing about the restaurant."""
        for _ in range(MISSING_THRESHOLD_PROBE):
            self.run_refresh(vendors=[], meta=None, menu=[], menu_status=403)
        after = self.row()
        self.assertEqual(after["missing_streak"], 0)
        self.assertEqual(after["availability"], "listed")

    def test_flagged_unlisted_at_threshold_but_never_deleted(self) -> None:
        for _ in range(daily_scrape.MISSING_STREAK_THRESHOLD):
            self.run_refresh(vendors=[], meta=None, menu=[], menu_status=404)
        after = self.row()
        self.assertGreaterEqual(
            after["missing_streak"], daily_scrape.MISSING_STREAK_THRESHOLD
        )
        self.assertEqual(after["availability"], "unlisted")
        # Still present, still complete.
        self.assertEqual(after["name"], "Cafe Akbar")
        self.assertEqual(after["address"], "Block 5, Gulshan-e-Iqbal")
        self.assertEqual(self.item_names(), ["Chicken Karahi"])


class RegressionGateTests(DailyScrapeCase):
    def test_broken_prices_are_not_published(self) -> None:
        before = self.item_names()
        self.run_refresh(
            vendors=[{"code": "aaaa", "name": "Cafe Akbar", "rating": 4.6}],
            menu=[
                {
                    "category_name": "Deals",
                    "items": [{"name": "Deal", "price": "from Rs. 300Rs. 600"}],
                }
            ],
            expect=1,
        )
        # Live database untouched, evidence preserved for inspection.
        self.assertEqual(self.item_names(), before)
        self.assertEqual(self.row()["rating"], 4.5)
        self.assertTrue(Path(str(self.db) + ".rejected").exists())

    def test_empty_price_is_also_caught(self) -> None:
        self.run_refresh(
            vendors=[{"code": "aaaa", "name": "Cafe Akbar", "rating": 4.6}],
            menu=[
                {
                    "category_name": "Deals",
                    "items": [{"name": "Deal", "price": "   "}],
                }
            ],
            expect=1,
        )
        self.assertEqual(self.item_names(), ["Chicken Karahi"])


class BlockAbortTests(DailyScrapeCase):
    """A PerimeterX block must stop the run, not corrupt it."""

    def setUp(self) -> None:
        super().setUp()
        conn = get_connection(str(self.db))
        self.extra_ids = []
        for n in range(2, 10):
            rid = insert_restaurant_with_menu(
                conn,
                {
                    "name": f"Vendor {n}",
                    "url": f"https://foodpanda.pk/restaurant/bb{n:02d}/vendor-{n}",
                    "rating": 4.0,
                    "cuisine": "Fast Food",
                    "address": f"Block {n}",
                    "delivery_time": "35 min",
                    "image_url": f"https://img/{n}.jpg",
                    "review_number": 100 * n,
                },
                _menu(f"{100 + n}"),
            )
            self.extra_ids.append(rid)
        conn.commit()
        conn.close()

    def test_403_streak_aborts_menu_phase_and_preserves_everything(self) -> None:
        listing = [
            {"code": "aaaa", "name": "Cafe Akbar", "rating": 4.9, "review_number": 5555}
        ] + [
            {"code": f"bb{n:02d}", "name": f"Vendor {n}", "rating": 4.1}
            for n in range(2, 10)
        ]
        # Every vendor is in the feed, but every menu request is blocked.
        self.run_refresh(vendors=listing, menu=[], menu_status=403, expect=0)

        conn = get_connection(str(self.db))
        try:
            run = conn.execute(
                "SELECT status, blocked FROM scrape_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
            self.assertEqual(run["status"], "blocked")
            self.assertGreaterEqual(
                run["blocked"], daily_scrape.MAX_CONSECUTIVE_403
            )
            # Listing data still refreshed, menus untouched, nothing flagged.
            first = conn.execute(
                "SELECT rating, review_count, missing_streak, availability "
                "FROM restaurants WHERE id = ?",
                (self.rid,),
            ).fetchone()
            self.assertEqual(first["rating"], 4.9)
            self.assertEqual(first["review_count"], 5555)
            self.assertEqual(first["missing_streak"], 0)
            self.assertEqual(first["availability"], "listed")
            items = conn.execute("SELECT COUNT(*) AS n FROM menu_items").fetchone()
            self.assertEqual(items["n"], 9)
        finally:
            conn.close()
        self.assertEqual(self.item_names(), ["Chicken Karahi"])


class LockTests(unittest.TestCase):
    def test_dead_pid_does_not_hold_the_lock_forever(self) -> None:
        """os.kill(pid, 0) reports dead pids as alive on Windows, which would
        wedge the scheduler permanently after one crash."""
        self.assertFalse(daily_scrape._pid_alive(999_999))

    def test_current_process_is_alive(self) -> None:
        import os

        self.assertTrue(daily_scrape._pid_alive(os.getpid()))


# How many runs to simulate when proving a streak must NOT grow.
MISSING_THRESHOLD_PROBE = daily_scrape.MISSING_STREAK_THRESHOLD + 2


if __name__ == "__main__":
    unittest.main()
