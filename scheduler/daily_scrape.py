"""Daily refresh of every restaurant already in foodpanda.db.

Scrapes into a sidecar copy, verifies it, then swaps it onto the live file, so
the agent never reads a half-updated database. Does not discover new areas or
change restaurant IDs.

    python scheduler/daily_scrape.py --run-now      # one refresh now
    python scheduler/daily_scrape.py --daemon       # 21:00 Asia/Karachi
    python scheduler/daily_scrape.py --status       # last runs at a glance

Design decisions and their evidence live in plans/SCRAPE_SCHEDULE_PLAN.md.
"""

from __future__ import annotations

import argparse
import logging
import os
import random
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRAPER_ROOT = REPO_ROOT / "foodpanda-scraper"
sys.path.insert(0, str(SCRAPER_ROOT))

import config  # noqa: E402
from scraper import api_client, areas  # noqa: E402
from scraper.menu import scrape_menu  # noqa: E402
from scraperdb.database import (  # noqa: E402
    count_broken_prices,
    count_restaurants,
    count_restaurants_without_menu,
    finish_scrape_run,
    get_connection,
    init_db,
    list_restaurants_for_refresh,
    mark_vendor_missing,
    mark_vendor_seen,
    recent_scrape_runs,
    replace_restaurant_menu,
    start_scrape_run,
    update_listing_only,
)

logger = logging.getLogger("daily_scrape")

DEFAULT_DB = SCRAPER_ROOT / "foodpanda.db"
LOCK_PATH = REPO_ROOT / "scheduler" / "daily_scrape.lock"

DISCO_PAGE_SIZE = 48
DISCO_MAX_OFFSET = 480

# Consecutive fd-api 403s that end the menu phase. Same guard the discovery
# scripts use: PerimeterX blocks get longer the harder you push them.
MAX_CONSECUTIVE_403 = 5

# Runs with no evidence a vendor exists before it is flagged 'unlisted'.
# One run per day, so this is six days. Flag only — nothing is ever deleted.
MISSING_STREAK_THRESHOLD = 6

# A lock older than this belongs to a dead run whatever the pid says.
MAX_RUN_AGE_SEC = 2 * 60 * 60

SCHEDULE_HOUR = 21
SCHEDULE_MINUTE = 0


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------


def _karachi_tz():
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo("Asia/Karachi")
    except Exception:
        return timezone(timedelta(hours=5))


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rate_limit() -> None:
    """Unchanged from every manual scrape: 1.5-3.0s between requests."""
    time.sleep(random.uniform(config.MIN_DELAY_SEC, config.MAX_DELAY_SEC))


def setup_logging() -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    formatter = logging.Formatter(config.LOG_FORMAT)
    has_console = any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
        for h in root.handlers
    )
    if not has_console:
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(formatter)
        root.addHandler(console)
    if not any(isinstance(h, logging.FileHandler) for h in root.handlers):
        file_handler = logging.FileHandler(
            SCRAPER_ROOT / config.LOG_FILE, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)


# --------------------------------------------------------------------------
# locking
# --------------------------------------------------------------------------


def _pid_alive(pid: int) -> bool:
    """Is this pid a running process?

    os.kill(pid, 0) is only a liveness probe on POSIX. On Windows
    signal.CTRL_C_EVENT == 0, so that call delivers a console Ctrl+C to the
    process group and returns successfully even for a pid that no longer
    exists — which would make every stale lock look live forever.
    """
    if sys.platform == "win32":
        import ctypes

        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(
            process_query_limited_information, False, pid
        )
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == still_active
            return True
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def acquire_lock() -> bool:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    if LOCK_PATH.exists():
        age = time.time() - LOCK_PATH.stat().st_mtime
        try:
            old_pid = int(LOCK_PATH.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            old_pid = None
        if age > MAX_RUN_AGE_SEC:
            logger.warning(
                "Lock is %.0f min old (pid %s); treating as stale",
                age / 60,
                old_pid,
            )
        elif old_pid and _pid_alive(old_pid):
            logger.error("Another daily scrape is running (pid %s)", old_pid)
            return False
        LOCK_PATH.unlink(missing_ok=True)
    LOCK_PATH.write_text(str(os.getpid()), encoding="utf-8")
    return True


def release_lock() -> None:
    try:
        if (
            LOCK_PATH.exists()
            and LOCK_PATH.read_text(encoding="utf-8").strip() == str(os.getpid())
        ):
            LOCK_PATH.unlink()
    except OSError:
        pass


# --------------------------------------------------------------------------
# sidecar / swap
# --------------------------------------------------------------------------


def checkpoint_and_copy(live: Path, dest: Path) -> None:
    conn = sqlite3.connect(str(live))
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    if dest.exists():
        dest.unlink()
    shutil.copy2(live, dest)


def replace_with_retry(src: Path, dst: Path, attempts: int = 10) -> bool:
    """os.replace, retried.

    On Windows the replace fails if any process has the target open. The agent
    holds read-only connections for the length of one tool call, so a collision
    is brief but perfectly possible, and losing a 20 minute scrape to it would
    be silly.
    """
    for attempt in range(1, attempts + 1):
        try:
            os.replace(src, dst)
            return True
        except PermissionError as exc:
            logger.warning(
                "Swap attempt %s/%s blocked (%s); retrying", attempt, attempts, exc
            )
            time.sleep(0.5)
    return False


# --------------------------------------------------------------------------
# listing index
# --------------------------------------------------------------------------


def build_listing_index(
    pins: list[tuple[str, float, float]] | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, set[str]]]:
    """Crawl every known pin once and index vendors by code.

    Returns (code -> listing row, code -> canonical areas that returned it).

    Crawling all pins rather than one city-centre point is not optional: disco
    returns vendors that deliver *to the queried point*, and 145 of the 209
    rows were discovered from Gulshan/Jauhar pins ~10-15 km from the default
    coordinates. A single-point index would miss most of the dataset and then
    fetch its menus at the wrong lat/lng, which returns empty menus.
    """
    pins = pins if pins is not None else areas.refresh_pins()
    index: dict[str, dict[str, Any]] = {}
    seen_in: dict[str, set[str]] = {}

    for label, lat, lng in pins:
        found = 0
        for sort in ("rating_desc", None):
            for offset in range(0, DISCO_MAX_OFFSET, DISCO_PAGE_SIZE):
                page = api_client.fetch_vendors(
                    lat, lng, limit=DISCO_PAGE_SIZE, offset=offset, sort=sort
                )
                _rate_limit()
                if not page:
                    break
                for vendor in page:
                    code = (vendor.get("code") or "").lower()
                    if not code:
                        continue
                    index.setdefault(code, vendor)
                    seen_in.setdefault(code, set()).add(
                        areas.canonical_area(label)
                    )
                    found += 1
                if len(page) < DISCO_PAGE_SIZE:
                    break
        logger.info("Pin %-42s -> %s vendor rows", label, found)

    logger.info(
        "Listing index: %s distinct vendors across %s pins", len(index), len(pins)
    )
    return index, seen_in


# --------------------------------------------------------------------------
# per-restaurant refresh
# --------------------------------------------------------------------------


def _coords_for(row: Any, listing: dict[str, Any] | None) -> tuple[float, float]:
    """Best known coordinates for this vendor.

    Menus come back empty when the request lat/lng is outside the vendor's
    delivery area, so the vendor's own point beats the city centre.
    """
    if listing:
        lat, lng = listing.get("latitude"), listing.get("longitude")
        if lat is not None and lng is not None:
            return float(lat), float(lng)
    lat, lng = row["latitude"], row["longitude"]
    if lat is not None and lng is not None:
        return float(lat), float(lng)
    recorded = (row["delivery_areas"] or "").split(",")[0].strip()
    for label, plat, plng in areas.refresh_pins():
        if recorded and areas.canonical_area(label) == recorded:
            return plat, plng
    return config.DEFAULT_LAT, config.DEFAULT_LNG


def scrape_menu_with_retry(
    listing: dict[str, Any], lat: float, lng: float
) -> tuple[list[dict[str, Any]], bool]:
    """Two attempts. Returns (categories, hit_403).

    page=None keeps the Playwright DOM fallback disabled on purpose: the DOM
    price node concatenates strikethrough and current price, which is the bug
    that produced 'from Rs. 300Rs. 600' rows (NOTES section 8). A menu comes
    from the API or not at all.
    """
    hit_403 = False
    for attempt in (1, 2):
        try:
            categories = scrape_menu(listing, page=None, lat=lat, lng=lng)
        except Exception as exc:
            logger.warning(
                "Menu attempt %s failed for %s: %s", attempt, listing.get("name"), exc
            )
            categories = []
        if api_client.LAST_MENU_STATUS == 403:
            hit_403 = True
            return [], hit_403
        if categories and sum(len(c.get("items") or []) for c in categories) > 0:
            return categories, hit_403
        if attempt == 1:
            _rate_limit()
    return [], hit_403


def _stub_from_row(row: Any, code: str) -> dict[str, Any]:
    """Listing dict built from what we already stored.

    Only used to give scrape_menu a name/code/url. Every field here is
    COALESCEd on write, so passing yesterday's values back can never erase
    anything.
    """
    return {
        "name": row["name"],
        "url": row["url"],
        "code": code,
        "rating": row["rating"],
        "review_number": row["review_count"],
        "cuisine": row["cuisine"],
        "address": row["address"],
        "delivery_time": row["delivery_time"],
        "image_url": row["image_url"],
        "latitude": row["latitude"],
        "longitude": row["longitude"],
    }


class RunStats:
    def __init__(self) -> None:
        self.ok = 0
        self.failed = 0
        self.skipped = 0
        self.listing_only = 0
        self.missing = 0
        self.flagged: list[str] = []
        self.blocked = 0
        self.consecutive_403 = 0
        self.menu_phase_aborted = False

    @property
    def note(self) -> str:
        return (
            f"ok={self.ok} listing_only={self.listing_only} "
            f"failed={self.failed} missing={self.missing} "
            f"blocked_403={self.blocked}"
        )


def refresh_one(
    conn: sqlite3.Connection,
    row: Any,
    index: dict[str, dict[str, Any]],
    seen_in: dict[str, set[str]],
    stats: RunStats,
    stamp: str,
) -> None:
    code = (api_client.extract_vendor_code(row["url"] or "") or "").lower()
    name = row["name"]
    listing = index.get(code)
    observed = sorted(seen_in.get(code, set()))
    conclusive_absence = False

    if listing is None and code:
        # Not in any pin's feed. It may simply be closed right now, so ask the
        # per-vendor endpoint before treating absence as evidence of anything.
        lat, lng = _coords_for(row, None)
        listing = api_client.fetch_vendor_meta(code, lat=lat, lng=lng)
        status = api_client.LAST_MENU_STATUS
        _rate_limit()
        if listing is None:
            # A 403 says nothing about the restaurant, only about our access.
            conclusive_absence = status is not None and status != 403
            if status == 403:
                stats.blocked += 1

    if listing is not None:
        mark_vendor_seen(conn, int(row["id"]), stamp, observed)
    elif conclusive_absence:
        streak = mark_vendor_missing(
            conn, int(row["id"]), MISSING_STREAK_THRESHOLD
        )
        stats.missing += 1
        if streak >= MISSING_STREAK_THRESHOLD:
            stats.flagged.append(f"{name} (id={row['id']}, {streak} runs)")
            logger.warning(
                "id=%s %s absent for %s consecutive runs -> availability=unlisted "
                "(not deleted)",
                row["id"],
                name,
                streak,
            )

    merged = _stub_from_row(row, code)
    if listing is not None:
        for key, value in listing.items():
            if value is not None and value != "":
                merged[key] = value
    merged.setdefault("url", row["url"])
    merged["code"] = code or merged.get("code")

    if stats.menu_phase_aborted:
        if listing is not None:
            update_listing_only(conn, int(row["id"]), merged, stamp)
            stats.listing_only += 1
        else:
            stats.skipped += 1
        return

    lat, lng = _coords_for(row, listing)
    categories, hit_403 = scrape_menu_with_retry(merged, lat, lng)

    if hit_403:
        stats.blocked += 1
        stats.consecutive_403 += 1
        if stats.consecutive_403 >= MAX_CONSECUTIVE_403:
            stats.menu_phase_aborted = True
            logger.error(
                "fd-api returned 403 %s times in a row (PerimeterX). Ending the "
                "menu phase; listing updates already gathered are kept and "
                "yesterday's menus stay in place.",
                stats.consecutive_403,
            )
    else:
        stats.consecutive_403 = 0

    if categories:
        replace_restaurant_menu(conn, int(row["id"]), merged, categories, stamp)
        stats.ok += 1
        return

    stats.failed += 1
    if listing is not None:
        update_listing_only(conn, int(row["id"]), merged, stamp)
        stats.listing_only += 1
        logger.error(
            "Menu failed for id=%s %s — kept yesterday's menu, refreshed listing",
            row["id"],
            name,
        )
    else:
        stats.skipped += 1
        logger.error(
            "No listing and no menu for id=%s %s — left yesterday untouched",
            row["id"],
            name,
        )


# --------------------------------------------------------------------------
# verification
# --------------------------------------------------------------------------


def verify_sidecar(conn: sqlite3.Connection, baseline: dict[str, int]) -> list[str]:
    """Checks that must pass before anything is published.

    A regression here is an alert, not a silent pass: the swap is abandoned and
    the live database keeps yesterday's data.
    """
    problems: list[str] = []

    broken = count_broken_prices(conn)
    logger.info("Post-run price diagnostic: %s broken rows", broken)
    if broken:
        problems.append(
            f"{broken} menu rows have NULL/empty/concatenated/'from' prices "
            "(expected 0)"
        )

    total = count_restaurants(conn)
    if total < baseline["restaurants"]:
        problems.append(
            f"restaurant count fell {baseline['restaurants']} -> {total}"
        )

    empty = count_restaurants_without_menu(conn)
    if empty > baseline["empty_menus"]:
        problems.append(
            f"restaurants with no menu rose {baseline['empty_menus']} -> {empty}"
        )

    return problems


# --------------------------------------------------------------------------
# the run
# --------------------------------------------------------------------------


def refresh_database(
    db_path: Path,
    *,
    run_label: str = "manual",
    limit: int | None = None,
    only_id: int | None = None,
    swap: bool = True,
    verbose: bool = False,
) -> int:
    if not db_path.exists():
        logger.error("Database not found: %s", db_path)
        return 1

    sidecar = Path(str(db_path) + ".scraping")
    rejected = Path(str(db_path) + ".rejected")
    backup = Path(str(db_path) + ".bak")
    log_path = str(SCRAPER_ROOT / config.LOG_FILE)

    if sidecar.exists():
        logger.warning("Removing leftover sidecar %s", sidecar)
        sidecar.unlink()

    checkpoint_and_copy(db_path, sidecar)
    conn = get_connection(str(sidecar))
    init_db(conn)
    run_id = start_scrape_run(conn, _now(), log_path, run_label)

    baseline = {
        "restaurants": count_restaurants(conn),
        "empty_menus": count_restaurants_without_menu(conn),
        "broken_prices": count_broken_prices(conn),
    }
    logger.info(
        "Baseline: %s restaurants, %s without menus, %s broken prices",
        baseline["restaurants"],
        baseline["empty_menus"],
        baseline["broken_prices"],
    )

    rows = list_restaurants_for_refresh(conn)
    if only_id is not None:
        rows = [r for r in rows if int(r["id"]) == only_id]
    if limit is not None:
        rows = rows[:limit]

    stats = RunStats()
    status = "ok"

    try:
        index, seen_in = build_listing_index()
        for position, row in enumerate(rows, start=1):
            logger.info(
                "[%s/%s] Refresh %s id=%s", position, len(rows), row["name"], row["id"]
            )
            refresh_one(conn, row, index, seen_in, stats, _now())
            conn.commit()
            _rate_limit()

        problems = verify_sidecar(conn, baseline)

        if stats.menu_phase_aborted:
            status = "blocked"
        elif stats.failed and not stats.ok:
            status = "failed"
        elif stats.failed:
            status = "partial"

        if problems:
            status = "price_regression"
            for problem in problems:
                logger.error("REGRESSION: %s", problem)

        finish_scrape_run(
            conn,
            run_id,
            finished_at=_now(),
            status=status,
            ok=stats.ok,
            failed=stats.failed,
            skipped=stats.skipped,
            listing_only=stats.listing_only,
            note=stats.note,
            broken_prices=count_broken_prices(conn),
            blocked=stats.blocked,
        )
        conn.close()
        conn = None

        if problems:
            if rejected.exists():
                rejected.unlink()
            os.replace(sidecar, rejected)
            logger.error(
                "NOT PUBLISHING. Live database untouched; rejected copy kept at %s",
                rejected,
            )
            return 1

        if not swap:
            logger.info("--no-swap: sidecar discarded, live database untouched")
            return 0

        if backup.exists():
            backup.unlink()
        shutil.copy2(db_path, backup)
        if not replace_with_retry(sidecar, db_path):
            logger.error(
                "Could not swap sidecar onto %s (file stayed locked). Work is "
                "preserved at %s — retry the swap or re-run.",
                db_path,
                sidecar,
            )
            return 1

        logger.info("Published %s (%s)", db_path, stats.note)
        if verbose:
            _print_summary(status, stats, db_path)
        return 0

    except Exception:
        logger.exception("Daily scrape aborted; live database left unchanged")
        try:
            if conn is not None:
                finish_scrape_run(
                    conn,
                    run_id,
                    finished_at=_now(),
                    status="aborted",
                    ok=stats.ok,
                    failed=stats.failed,
                    skipped=stats.skipped,
                    listing_only=stats.listing_only,
                    note="aborted before swap",
                    blocked=stats.blocked,
                )
                conn.close()
        except Exception:
            pass
        return 1
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        if sidecar.exists():
            try:
                sidecar.unlink()
            except OSError:
                pass


def _print_summary(status: str, stats: RunStats, db_path: Path) -> None:
    print(f"status      : {status}")
    print(f"refreshed   : {stats.ok}")
    print(f"listing only: {stats.listing_only}")
    print(f"failed      : {stats.failed}")
    print(f"absent      : {stats.missing}")
    print(f"403s        : {stats.blocked}")
    if stats.flagged:
        print("flagged unlisted (not deleted):")
        for entry in stats.flagged:
            print(f"  - {entry}")
    print(f"database    : {db_path}")
    print("history     : python scheduler/daily_scrape.py --status")


def run_job(db_path: Path, **kwargs: Any) -> int:
    if not acquire_lock():
        return 1
    try:
        return refresh_database(db_path, **kwargs)
    finally:
        release_lock()


def show_status(db_path: Path, limit: int = 5) -> int:
    if not db_path.exists():
        print(f"Database not found: {db_path}")
        return 1
    conn = get_connection(str(db_path))
    init_db(conn)
    runs = recent_scrape_runs(conn, limit)
    if not runs:
        print("No scheduled run has completed yet.")
    for run in runs:
        print(
            f"#{run['id']} [{run['run_label'] or 'manual'}] "
            f"{run['started_at']} -> {run['finished_at'] or 'unfinished'} "
            f"{run['status']}"
        )
        print(
            f"    ok={run['restaurants_ok']} listing_only={run['listing_only']} "
            f"failed={run['restaurants_failed']} "
            f"skipped={run['restaurants_skipped']} "
            f"broken_prices={run['broken_prices']} blocked={run['blocked']}"
        )
        if run["note"]:
            print(f"    {run['note']}")

    stale = conn.execute(
        "SELECT COUNT(*) AS n FROM restaurants WHERE availability = 'unlisted'"
    ).fetchone()["n"]
    never = conn.execute(
        "SELECT COUNT(*) AS n FROM restaurants WHERE updated_at IS NULL"
    ).fetchone()["n"]
    print(f"\nflagged unlisted : {stale}")
    print(f"never refreshed  : {never}")
    print(f"broken prices now: {count_broken_prices(conn)}")
    print(f"log              : {SCRAPER_ROOT / config.LOG_FILE}")
    conn.close()
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Daily Foodpanda snapshot refresh")
    parser.add_argument("--run-now", action="store_true", help="Run one refresh now")
    parser.add_argument(
        "--daemon",
        action="store_true",
        help=f"APScheduler at {SCHEDULE_HOUR:02d}:{SCHEDULE_MINUTE:02d} Asia/Karachi",
    )
    parser.add_argument("--status", action="store_true", help="Show recent runs")
    parser.add_argument(
        "--db-path",
        default=str(DEFAULT_DB),
        help="Live SQLite path (default: foodpanda-scraper/foodpanda.db)",
    )
    parser.add_argument(
        "--run-label",
        default=None,
        help="Label recorded on the run (dinner/morning/manual)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Refresh only the first N restaurants (testing)",
    )
    parser.add_argument(
        "--only-id", type=int, default=None, help="Refresh a single restaurant id"
    )
    parser.add_argument(
        "--no-swap",
        action="store_true",
        help="Do everything except publish (testing)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    args = parse_args(argv)
    db_path = Path(args.db_path)

    if args.status:
        return show_status(db_path)

    if args.daemon:
        from apscheduler.schedulers.blocking import BlockingScheduler
        from apscheduler.triggers.cron import CronTrigger

        tz = _karachi_tz()
        scheduler = BlockingScheduler(timezone=tz)
        scheduler.add_job(
            lambda: run_job(db_path, run_label=args.run_label or "dinner"),
            CronTrigger(hour=SCHEDULE_HOUR, minute=SCHEDULE_MINUTE, timezone=tz),
            id="daily_scrape_dinner",
            replace_existing=True,
            misfire_grace_time=3600,
            coalesce=True,
        )
        # Second window for the nashta/chai cohort, deferred by decision. It is
        # a full pass like the dinner one; uncomment to enable.
        # scheduler.add_job(
        #     lambda: run_job(db_path, run_label="morning"),
        #     CronTrigger(hour=9, minute=30, timezone=tz),
        #     id="daily_scrape_morning",
        #     replace_existing=True,
        #     misfire_grace_time=3600,
        #     coalesce=True,
        # )
        logger.info(
            "Daemon scheduled for %02d:%02d Asia/Karachi; db=%s",
            SCHEDULE_HOUR,
            SCHEDULE_MINUTE,
            db_path,
        )
        scheduler.start()
        return 0

    if args.run_now:
        return run_job(
            db_path,
            run_label=args.run_label or "manual",
            limit=args.limit,
            only_id=args.only_id,
            swap=not args.no_swap,
            verbose=True,
        )

    print("Pass --run-now, --daemon or --status. See README.md.")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
