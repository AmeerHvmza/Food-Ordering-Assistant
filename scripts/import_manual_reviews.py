"""Import manually copied Foodpanda review text into foodpanda.db.

Parses data/manual_reviews_raw.txt, fuzzy-matches restaurant headers to
restaurants.name, matches Liked dishes to menu_items, and inserts rows into
reviews. Reviewer names are parsed and discarded — never stored or logged.

    python scripts/import_manual_reviews.py
    python scripts/import_manual_reviews.py --dry-run
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRAPER = ROOT / "foodpanda-scraper"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SCRAPER))

from db import name_match  # noqa: E402
from scraperdb.database import get_connection, init_db  # noqa: E402

logger = logging.getLogger("import_manual_reviews")

DEFAULT_RAW = ROOT / "data" / "manual_reviews_raw.txt"
DEFAULT_DB = SCRAPER / "foodpanda.db"

DISH_RATIO_MIN = 0.86
DISH_GAP_MIN = 0.08

HEADER_RE = re.compile(r"^[A-Za-z].*:$")
TIME_RE = re.compile(
    r"(?i)^(yesterday|today|\d+\s+(day|days|week|weeks|month|months|year|years)\s+ago)$"
)
HELPFUL_RE = re.compile(r"(?i)^helpful(\s+\d+)?$")
LIKED_RE = re.compile(r"(?i)^liked(?:\s+\d+\s+dishes?)?:\s*(.+)$")
TOP_REVIEWER_RE = re.compile(r"(?i)^top reviewer$")
RS_PRICE_RE = re.compile(r"\s+Rs\.?\s*\d+(?:\.\d+)?\s*$", re.IGNORECASE)

DISH_ALIASES = (
    (re.compile(r"\baalo\b", re.I), "aloo"),
    (re.compile(r"\blaccha\b", re.I), "lachha"),
    (re.compile(r"\blacha\b", re.I), "lachha"),
    (re.compile(r"\bchoclate\b", re.I), "chocolate"),
    (re.compile(r"\bomellete\b", re.I), "omelette"),
    (re.compile(r"\bkashmeiri\b", re.I), "kashmiri"),
    (re.compile(r"\bsuleimani\b", re.I), "sulemani"),
    (re.compile(r"\bsulenani\b", re.I), "sulemani"),
    (re.compile(r"\bchanay\b", re.I), "chanay"),
    (re.compile(r"\bchanay\b", re.I), "chana"),
)


@dataclass
class ParsedReview:
    review_text: str
    liked_raw: list[str] = field(default_factory=list)
    owner_response: str | None = None


@dataclass
class ParsedSection:
    header: str
    reviews: list[ParsedReview] = field(default_factory=list)


def apply_dish_aliases(text: str) -> str:
    out = text
    for pattern, repl in DISH_ALIASES:
        out = pattern.sub(repl, out)
    return out


def is_header_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped.endswith(":"):
        return False
    if stripped.lower().startswith("restaurant response"):
        return False
    return bool(HEADER_RE.match(stripped))


def is_time_line(line: str) -> bool:
    return bool(TIME_RE.match(line.strip()))


def is_helpful_line(line: str) -> bool:
    return bool(HELPFUL_RE.match(line.strip()))


def is_top_reviewer_line(line: str) -> bool:
    return bool(TOP_REVIEWER_RE.match(line.strip()))


def strip_dish_price(label: str) -> str:
    return RS_PRICE_RE.sub("", label.strip()).strip(" ,")


def parse_liked_dishes(line: str) -> list[str]:
    match = LIKED_RE.match(line.strip())
    if not match:
        return []
    chunk = match.group(1).strip()
    parts = [strip_dish_price(p) for p in chunk.split(",")]
    return [p for p in parts if p]


def skip_reviewer_line(line: str) -> bool:
    """Consume and discard a reviewer identity line (never log it)."""
    stripped = line.strip()
    if not stripped:
        return False
    if stripped.lower().startswith("top:"):
        return True
    # Single-token reviewer names; multi-line bodies handled separately.
    if " " not in stripped and len(stripped) <= 24:
        return True
    return False


def parse_section_lines(header: str, lines: list[str]) -> ParsedSection:
    section = ParsedSection(header=header)
    i = 0
    n = len(lines)

    while i < n:
        while i < n and not lines[i].strip():
            i += 1
        if i >= n:
            break

        if skip_reviewer_line(lines[i]):
            i += 1
        else:
            # Malformed block without reviewer line — still try to parse body.
            pass

        review = ParsedReview(review_text="")
        body_parts: list[str] = []
        owner_parts: list[str] | None = None

        while i < n:
            raw = lines[i]
            stripped = raw.strip()
            if not stripped:
                i += 1
                continue

            if is_helpful_line(stripped):
                i += 1
                break

            if is_top_reviewer_line(stripped):
                i += 1
                continue

            if is_time_line(stripped):
                i += 1
                continue

            liked = parse_liked_dishes(stripped)
            if liked:
                review.liked_raw.extend(liked)
                i += 1
                continue

            if stripped.lower().startswith("restaurant response:"):
                rest = stripped.split(":", 1)[1].strip()
                owner_parts = [rest] if rest else []
                i += 1
                continue

            if owner_parts is not None:
                if is_helpful_line(stripped):
                    break
                owner_parts.append(stripped)
                i += 1
                continue

            body_parts.append(stripped)
            i += 1

        text = " ".join(body_parts).strip()
        if not text:
            logger.warning("EMPTY_REVIEW header=%s (skipped)", header)
            continue
        review.review_text = text
        if owner_parts:
            review.owner_response = " ".join(owner_parts).strip() or None
        section.reviews.append(review)

    return section


def split_sections(text: str) -> list[ParsedSection]:
    lines = text.splitlines()
    sections: list[ParsedSection] = []
    current_header: str | None = None
    current_lines: list[str] = []

    for line in lines:
        if is_header_line(line):
            if current_header is not None:
                sections.append(parse_section_lines(current_header, current_lines))
            current_header = line.strip()[:-1]
            current_lines = []
        elif current_header is not None:
            current_lines.append(line)

    if current_header is not None:
        sections.append(parse_section_lines(current_header, current_lines))
    return sections


def match_restaurant(
    header: str,
    restaurants: list[sqlite3.Row],
) -> tuple[sqlite3.Row | None, str]:
    if not restaurants:
        return None, "no restaurants in database"
    names = [row["name"] for row in restaurants]
    decision = name_match.decide_name_match(header, names, name_match.IMPORT)
    if decision.kind != "match" or decision.best_index is None:
        return None, decision.reason or "unmatched"
    best = restaurants[decision.best_index]
    second = decision.second_score
    gap = (decision.best_score or 0) - (second or 0)
    return (
        best,
        f"matched id={best['id']} combo={decision.best_score:.3f} gap={gap:.3f}",
    )


def load_menu_items(conn: sqlite3.Connection, restaurant_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT mi.id, mi.name
        FROM menu_items mi
        JOIN menu_categories mc ON mc.id = mi.category_id
        WHERE mc.restaurant_id = ?
        ORDER BY mi.id
        """,
        (restaurant_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def match_dish(
    liked_name: str,
    menu_items: list[dict[str, Any]],
    restaurant_id: int,
) -> tuple[int | None, str | None]:
    target = name_match.normalize_name(apply_dish_aliases(strip_dish_price(liked_name)))
    if not target:
        return None, None

    exact_hits: list[int] = []
    for item in menu_items:
        item_norm = name_match.normalize_name(apply_dish_aliases(item["name"]))
        if item_norm == target:
            exact_hits.append(int(item["id"]))
    if exact_hits:
        if len(exact_hits) > 1:
            logger.info(
                "DISH_DUPLICATE restaurant_id=%s liked=%r using lowest id=%s",
                restaurant_id,
                liked_name,
                min(exact_hits),
            )
        return min(exact_hits), None

    scored: list[tuple[float, int, str]] = []
    for item in menu_items:
        item_norm = name_match.normalize_name(apply_dish_aliases(item["name"]))
        ratio = SequenceMatcher(None, target, item_norm).ratio()
        scored.append((ratio, int(item["id"]), item["name"]))
    scored.sort(reverse=True)
    if not scored:
        logger.warning(
            "DISH_UNMATCHED restaurant_id=%s liked=%r (no menu items)",
            restaurant_id,
            liked_name,
        )
        return None, None

    best_ratio, best_id, best_name = scored[0]
    second_ratio = scored[1][0] if len(scored) > 1 else 0.0
    gap = best_ratio - second_ratio
    if best_ratio >= DISH_RATIO_MIN and gap >= DISH_GAP_MIN:
        return best_id, best_name

    logger.warning(
        "DISH_UNMATCHED restaurant_id=%s liked=%r best=%r ratio=%.3f gap=%.3f",
        restaurant_id,
        liked_name,
        scored[0][2],
        best_ratio,
        gap,
    )
    return None, best_name


def build_liked_json(
    liked_raw: list[str],
    menu_items: list[dict[str, Any]],
    restaurant_id: int,
) -> list[dict[str, Any]] | None:
    if not liked_raw:
        return None
    out: list[dict[str, Any]] = []
    for name in liked_raw:
        display = strip_dish_price(name)
        item_id, _ = match_dish(name, menu_items, restaurant_id)
        out.append({"name": display, "item_id": item_id})
    return out or None


def ensure_reviews_table(conn: sqlite3.Connection) -> None:
    init_db(conn)


def import_sections(
    conn: sqlite3.Connection,
    sections: list[ParsedSection],
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    restaurants = list(conn.execute("SELECT id, name FROM restaurants"))
    imported_at = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()

    stats: dict[str, Any] = {
        "matched_headers": [],
        "unmatched_headers": [],
        "inserted": 0,
        "dupes": 0,
        "empty_skipped": 0,
        "liked_total": 0,
        "liked_matched": 0,
        "examples": [],
    }

    for section in sections:
        row, detail = match_restaurant(section.header, restaurants)
        if row is None:
            logger.error("UNMATCHED header=%r %s", section.header, detail)
            stats["unmatched_headers"].append({"header": section.header, "reason": detail})
            continue

        restaurant_id = int(row["id"])
        logger.info("MATCH header=%r -> %s", section.header, detail)
        stats["matched_headers"].append(
            {"header": section.header, "restaurant_id": restaurant_id, "name": row["name"]}
        )
        menu_items = load_menu_items(conn, restaurant_id)

        for review in section.reviews:
            liked = build_liked_json(review.liked_raw, menu_items, restaurant_id)
            if liked:
                for dish in liked:
                    stats["liked_total"] += 1
                    if dish.get("item_id") is not None:
                        stats["liked_matched"] += 1

            liked_json = json.dumps(liked, ensure_ascii=False) if liked else None
            if dry_run:
                if len(stats["examples"]) < 3:
                    stats["examples"].append(
                        {
                            "restaurant_id": restaurant_id,
                            "name": row["name"],
                            "review_text": review.review_text,
                            "liked_dishes": liked,
                            "owner_response": review.owner_response,
                        }
                    )
                stats["inserted"] += 1
                continue

            cur = conn.execute(
                """
                INSERT OR IGNORE INTO reviews
                    (restaurant_id, review_text, liked_dishes, owner_response,
                     source, imported_at)
                VALUES (?, ?, ?, ?, 'manual_sample', ?)
                """,
                (
                    restaurant_id,
                    review.review_text,
                    liked_json,
                    review.owner_response,
                    imported_at,
                ),
            )
            if cur.rowcount:
                stats["inserted"] += 1
                if len(stats["examples"]) < 3:
                    stats["examples"].append(
                        {
                            "restaurant_id": restaurant_id,
                            "name": row["name"],
                            "review_text": review.review_text,
                            "liked_dishes": liked,
                            "owner_response": review.owner_response,
                        }
                    )
            else:
                stats["dupes"] += 1

    if not dry_run:
        conn.commit()
    return stats


def print_report(stats: dict[str, Any]) -> None:
    print("\n=== Manual reviews import report ===")
    print(f"Matched headers: {len(stats['matched_headers'])}")
    print(f"Unmatched headers: {len(stats['unmatched_headers'])}")
    if stats["unmatched_headers"]:
        print("Unmatched:")
        for item in stats["unmatched_headers"]:
            print(f"  - {item['header']!r}: {item['reason']}")
    print(f"Reviews inserted: {stats['inserted']}")
    print(f"Duplicates skipped: {stats['dupes']}")
    liked_total = stats["liked_total"]
    liked_matched = stats["liked_matched"]
    if liked_total:
        pct = 100.0 * liked_matched / liked_total
        print(
            f"Liked dishes matched to menu item_id: {liked_matched}/{liked_total} ({pct:.1f}%)"
        )
    else:
        print("Liked dishes matched: n/a (none in source)")
    print("\nExample review objects:")
    for ex in stats["examples"]:
        print(json.dumps(ex, ensure_ascii=False, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", type=Path, default=DEFAULT_RAW)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    if not args.raw.exists():
        logger.error("Raw file not found: %s", args.raw)
        return 1
    if not args.db.exists():
        logger.error("Database not found: %s", args.db)
        return 1

    text = args.raw.read_text(encoding="utf-8")
    sections = split_sections(text)
    logger.info("Parsed %s restaurant sections from %s", len(sections), args.raw)

    conn = get_connection(str(args.db))
    try:
        ensure_reviews_table(conn)
        stats = import_sections(conn, sections, dry_run=args.dry_run)
    finally:
        conn.close()

    print_report(stats)
    return 1 if stats["unmatched_headers"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
