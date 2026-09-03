"""Read-only queries against the scraped foodpanda.db.

Every restaurant, dish, price and rating the agent states must come from
here. Milestone 1 treats the database as a static snapshot and opens it in
SQLite read-only mode so a bug in the agent cannot mutate scraped data.

Schema reminder (see foodpanda-scraper/db/schema.sql):

    restaurants -> menu_categories -> menu_items

menu_items has no restaurant_id, so every menu query joins through
menu_categories.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from db import geo, name_match

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = REPO_ROOT / "foodpanda-scraper" / "foodpanda.db"

# Prices are stored as TEXT ("964.75", occasionally with PKR/commas), so both
# SQL and Python need to coerce before comparing. Mirrors query_examples.py.
_PRICE_EXPR = (
    "CAST(REPLACE(REPLACE(COALESCE(mi.price, ''), 'PKR', ''), ',', '') AS REAL)"
)
_HAS_PRICE = "TRIM(COALESCE(mi.price, '')) != ''"

_RESTAURANT_COLUMNS = """
    r.id, r.name, r.cuisine, r.address, r.rating, r.review_count,
    r.delivery_time, r.url, r.image_url, r.delivery_areas
"""

MAX_LIKE_TERMS = 4

# Turn punctuation into spaces so LIKE '% desi %' cannot match "Desire".
_WORD_PUNCT = ("-", ",", ".", "/", "&", "(", ")", "[", "]", ":")


def _padded_sql(column: str) -> str:
    expr = f"LOWER(COALESCE({column}, ''))"
    expr = f"REPLACE({expr}, CHAR(39), ' ')"
    for mark in _WORD_PUNCT:
        expr = f"REPLACE({expr}, '{mark}', ' ')"
    return f"(' ' || {expr} || ' ')"


def word_match_sql(columns: list[str], terms: list[str]) -> tuple[str, list[str]]:
    """Whole-word match. 'desi' hits 'Desi Ghee', not 'Desire' or 'desiccated'.

    Terms are OR'd: any term in any column counts. That is the right default
    for restaurant search (a craving word can hit cuisine *or* name). Menu
    search of a multi-word dish uses word_match_all_sql instead.
    """
    clauses: list[str] = []
    params: list[str] = []
    for column in columns:
        padded = _padded_sql(column)
        for term in terms:
            clauses.append(f"{padded} LIKE ?")
            params.append(f"% {term} %")
    return "(" + " OR ".join(clauses) + ")", params


def word_match_all_sql(columns: list[str], terms: list[str]) -> tuple[str, list[str]]:
    """Each term must appear as a whole word in at least one of the columns.

    'cheese paratha' keeps Cheese Paratha and drops Plain Paratha / Cheese
    Omelette. Combined with ORDER BY price LIMIT, OR matching was filling the
    page with cheaper single-word hits and cutting the actual dish family.
    """
    if not terms:
        return "1=1", []
    parts: list[str] = []
    params: list[str] = []
    for term in terms:
        clause, term_params = word_match_sql(columns, [term])
        parts.append(clause)
        params.extend(term_params)
    return "(" + " AND ".join(parts) + ")", params


def location_match_sql(location: str) -> tuple[str, list[str]]:
    """Match an area against delivery area, address and name.

    `delivery_areas` comes first because it records the zone the vendor was
    seen delivering to. An address is only where the kitchen sits: Harmain
    Sharifain is addressed in Bahadurabad but delivers to Gulshan, and plenty
    of Jauhar addresses are spelled "Johar", so address text alone under-reports
    coverage badly.
    """
    terms = geo.area_search_terms(location) or [location.lower()]
    clauses: list[str] = []
    params: list[str] = []
    for column in ("r.delivery_areas", "r.address", "r.name"):
        padded = _padded_sql(column)
        for term in terms:
            clauses.append(f"{padded} LIKE ?")
            params.append(f"%{term}%")
    return "(" + " OR ".join(clauses) + ")", params



def db_path() -> Path:
    """Resolve the database path, allowing an env override."""
    return Path(os.getenv("FOODPANDA_DB_PATH") or DEFAULT_DB_PATH)


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    """Open the snapshot read-only. Raises FileNotFoundError if absent."""
    resolved = Path(path) if path else db_path()
    if not resolved.exists():
        raise FileNotFoundError(
            f"Scraped database not found: {resolved}. "
            "Run the scraper in foodpanda-scraper/ first."
        )
    conn = sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def session(path: str | Path | None = None) -> Iterator[sqlite3.Connection]:
    """Open and reliably close a read-only connection.

    sqlite3's own context manager handles transactions, not closing, so tools
    that query per call would otherwise leak handles.
    """
    conn = connect(path)
    try:
        yield conn
    finally:
        conn.close()


def parse_price(raw: Any) -> float | None:
    """Coerce a stored price string to a float, or None if unusable."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    cleaned = str(raw).replace("PKR", "").replace(",", "").strip()
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _terms(text: str | None) -> list[str]:
    """Split free text into a few useful LIKE terms.

    A craving like "something spicy, maybe biryani" becomes
    ["something", "spicy", "biryani"] so a match on any word can hit.
    """
    if not text:
        return []
    words = re.findall(r"[A-Za-z0-9&']{3,}", text.lower())
    stop = {
        "the", "and", "for", "with", "some", "something", "want", "would",
        "like", "food", "eat", "order", "near", "from", "any", "have", "get",
        "give", "please", "maybe", "really", "very", "feel", "feeling",
        "craving", "cravings", "dinner", "lunch", "people", "person",
    }
    seen: list[str] = []
    for word in words:
        if word in stop or word in seen:
            continue
        seen.append(word)
    return seen[:MAX_LIKE_TERMS] or words[:MAX_LIKE_TERMS]


def _row_in_location(row: dict[str, Any], location: str | None) -> bool:
    if not location:
        return True
    blob = geo.normalize_area_text(
        f"{row.get('delivery_areas') or ''} {row.get('address') or ''} "
        f"{row.get('name') or ''}"
    )
    return any(term in blob for term in geo.area_search_terms(location))


def resolve_restaurant_by_name(
    conn: sqlite3.Connection,
    name_query: str,
    location: str | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Best restaurant row for a spoken name, optionally scoped to delivery area.

    Returns (restaurant, error_message). error_message is set on ambiguity or
    no confident match. Scoring lives in db.name_match.
    """
    rows = [
        dict(row)
        for row in conn.execute(
            f"SELECT {_RESTAURANT_COLUMNS} FROM restaurants r"
        )
    ]
    if not rows:
        return None, "NO_MATCH: no restaurants in the snapshot."

    decision = name_match.resolve_restaurant_row(
        name_query,
        rows,
        name_match.CONVERSATIONAL,
        location=location,
        in_location=_row_in_location,
    )
    if decision.kind == "match" and decision.best_index is not None:
        return rows[decision.best_index], None
    if decision.kind == "ambiguous":
        alts = []
        for item in decision.eligible_ranked[:3]:
            alts.append(f"{item.name} (id={rows[item.index]['id']})")
        listed = ", ".join(alts) or decision.reason
        return None, (
            "AMBIGUOUS: more than one restaurant matches "
            f"{name_query!r}. Candidates: {listed}. "
            "Ask the user which branch or area they mean; do not say "
            "the restaurant was not found."
        )
    return None, (
        f"NO_MATCH: no confident restaurant match for {name_query!r}. "
        f"{decision.reason or ''}".strip()
    )


def search_restaurants(
    conn: sqlite3.Connection,
    craving: str | None = None,
    location: str | None = None,
    budget: float | None = None,
    limit: int = 25,
) -> list[dict[str, Any]]:
    """Find candidate restaurants; caller ranks them via db.ranking.

    Matching is deliberately layered, because a craving is often a dish name
    ("nihari") rather than a cuisine label:

    1. cuisine / restaurant name match
    2. menu item name or description match

    Each row carries match_source so the agent can explain why it surfaced.
    Filters are dropped rather than returning nothing: budget is a hint.
    Location is not optional — Foodpanda only lists vendors that deliver to
    the user's area, so an unmatched area returns [] instead of city-wide
    leftovers.
    """
    terms = _terms(craving)
    results: dict[int, dict[str, Any]] = {}

    def run(sql: str, params: list[Any], source: str) -> None:
        for row in conn.execute(sql, params):
            record = results.get(row["id"])
            if record is None:
                record = dict(row)
                record["match_source"] = source
                results[row["id"]] = record

    def where_extras(params: list[Any]) -> str:
        clauses = ""
        if location:
            like, like_params = location_match_sql(location)
            clauses += f" AND {like}"
            params += like_params
        if budget is not None:
            clauses += f"""
                AND EXISTS (
                    SELECT 1 FROM menu_categories mc
                    JOIN menu_items mi ON mi.category_id = mc.id
                    WHERE mc.restaurant_id = r.id
                      AND {_HAS_PRICE} AND {_PRICE_EXPR} <= ?
                )
            """
            params.append(budget)
        return clauses

    price_summary = f"""
        (SELECT MIN({_PRICE_EXPR}) FROM menu_categories mc
         JOIN menu_items mi ON mi.category_id = mc.id
         WHERE mc.restaurant_id = r.id AND {_HAS_PRICE}) AS min_item_price
    """

    if terms:
        like, like_params = word_match_sql(["r.cuisine", "r.name"], terms)
        params = list(like_params)
        sql = f"""
            SELECT {_RESTAURANT_COLUMNS}, {price_summary}
            FROM restaurants r
            WHERE {like} {where_extras(params)}
            LIMIT ?
        """
        run(sql, params + [limit], "cuisine_or_name")

        if len(results) < limit:
            item_like, item_params = word_match_sql(
                ["mi.name", "mi.description"], terms
            )
            params = list(item_params)
            sql = f"""
                SELECT {_RESTAURANT_COLUMNS}, {price_summary}
                FROM restaurants r
                WHERE EXISTS (
                    SELECT 1 FROM menu_categories mc
                    JOIN menu_items mi ON mi.category_id = mc.id
                    WHERE mc.restaurant_id = r.id AND {item_like}
                ) {where_extras(params)}
                LIMIT ?
            """
            run(sql, params + [limit], "menu_item")

    if not results:
        params = []
        sql = f"""
            SELECT {_RESTAURANT_COLUMNS}, {price_summary}
            FROM restaurants r
            WHERE 1=1 {where_extras(params)}
            LIMIT ?
        """
        run(sql, params + [limit], "unfiltered")

    # Do not drop the location filter. Showing Garden vendors to someone in
    # DHA would be the opposite of how Foodpanda's map works.
    return list(results.values())


def get_restaurant(
    conn: sqlite3.Connection,
    restaurant_id: int,
) -> dict[str, Any] | None:
    row = conn.execute(
        f"SELECT {_RESTAURANT_COLUMNS} FROM restaurants r WHERE r.id = ?",
        (restaurant_id,),
    ).fetchone()
    return dict(row) if row else None


def dataset_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """All (rating, review_count) rows, for dataset-level m and C."""
    return [
        dict(row)
        for row in conn.execute(
            "SELECT id, name, rating, review_count FROM restaurants"
        )
    ]


def search_menu(
    conn: sqlite3.Connection,
    restaurant_id: int,
    query: str | None = None,
    max_price: float | None = None,
    category: str | None = None,
    limit: int = 12,
) -> list[dict[str, Any]]:
    """Search one restaurant's menu. Filters are dropped before returning none.

    Multi-word queries require every term (AND). OR + cheapest-first LIMIT was
    returning Plain Paratha / Cheese Omelette for 'cheese paratha' and cutting
    the actual Cheese Paratha family off the page.
    """
    terms = _terms(query)

    def run(use_terms: bool, use_price: bool, use_category: bool) -> list[dict]:
        clauses = ["mc.restaurant_id = ?"]
        params: list[Any] = [restaurant_id]
        if use_terms and terms:
            like, like_params = word_match_all_sql(
                ["mi.name", "mi.description", "mc.category_name"], terms
            )
            clauses.append(like)
            params += like_params
        if use_category and category:
            clauses.append("mc.category_name LIKE ?")
            params.append(f"%{category}%")
        if use_price and max_price is not None:
            clauses.append(f"{_HAS_PRICE} AND {_PRICE_EXPR} <= ?")
            params.append(max_price)

        sql = f"""
            SELECT
                mi.id AS item_id, mi.name, mi.price, mi.description,
                mi.image_url, mc.category_name, {_PRICE_EXPR} AS price_value
            FROM menu_items mi
            JOIN menu_categories mc ON mc.id = mi.category_id
            WHERE {' AND '.join(clauses)}
            ORDER BY (price_value IS NULL), price_value ASC
            LIMIT ?
        """
        return [dict(row) for row in conn.execute(sql, params + [limit])]

    # Progressively relax: all filters, then drop price, then drop category.
    for attempt in ((True, True, True), (True, False, True), (True, False, False)):
        rows = run(*attempt)
        if rows:
            return rows
    return run(False, False, False)


def get_menu_item(
    conn: sqlite3.Connection,
    restaurant_id: int,
    item_id: int,
) -> dict[str, Any] | None:
    """Fetch one item, scoped to a restaurant so cross-vendor adds fail."""
    row = conn.execute(
        f"""
        SELECT
            mi.id AS item_id, mi.name, mi.price, mi.description,
            mi.image_url, mc.category_name, mc.restaurant_id,
            {_PRICE_EXPR} AS price_value
        FROM menu_items mi
        JOIN menu_categories mc ON mc.id = mi.category_id
        WHERE mi.id = ? AND mc.restaurant_id = ?
        """,
        (item_id, restaurant_id),
    ).fetchone()
    return dict(row) if row else None


def list_categories(conn: sqlite3.Connection, restaurant_id: int) -> list[str]:
    return [
        row["category_name"]
        for row in conn.execute(
            """
            SELECT DISTINCT category_name
            FROM menu_categories
            WHERE restaurant_id = ?
            ORDER BY category_name
            """,
            (restaurant_id,),
        )
    ]


def looks_like_deal(category_name: str) -> bool:
    lowered = (category_name or "").lower()
    return any(
        word in lowered for word in ("deal", "offer", "promo", "combo", "saver")
    )


def search_deals(
    conn: sqlite3.Connection,
    restaurant_id: int,
    query: str | None = None,
    budget: float | None = None,
    party_size: int | None = None,
    limit: int = 6,
) -> list[dict[str, Any]]:
    """Items from deal-named categories, optionally matched to craving/budget.

    The snapshot has category names like "Azaadi Deals" but no discount %.
    Matching is by name/description overlap and price fit, not invented off.
    """
    terms = _terms(query)
    deal_clause = """(
        LOWER(mc.category_name) LIKE '%deal%'
        OR LOWER(mc.category_name) LIKE '%offer%'
        OR LOWER(mc.category_name) LIKE '%promo%'
        OR LOWER(mc.category_name) LIKE '%combo%'
        OR LOWER(mc.category_name) LIKE '%saver%'
    )"""
    clauses = ["mc.restaurant_id = ?", deal_clause]
    params: list[Any] = [restaurant_id]
    if terms:
        like, like_params = word_match_all_sql(
            ["mi.name", "mi.description", "mc.category_name"], terms
        )
        clauses.append(like)
        params += like_params
    if budget is not None:
        clauses.append(f"{_HAS_PRICE} AND {_PRICE_EXPR} <= ?")
        params.append(budget)

    sql = f"""
        SELECT
            mi.id AS item_id, mi.name, mi.price, mi.description,
            mi.image_url, mc.category_name, {_PRICE_EXPR} AS price_value
        FROM menu_items mi
        JOIN menu_categories mc ON mc.id = mi.category_id
        WHERE {' AND '.join(clauses)}
        ORDER BY (price_value IS NULL), price_value ASC
        LIMIT ?
    """
    rows = [dict(row) for row in conn.execute(sql, params + [limit * 2])]
    if not rows and terms:
        # Craving didn't hit a deal item; still surface cheap deals at this vendor.
        return search_deals(
            conn,
            restaurant_id,
            query=None,
            budget=budget,
            party_size=party_size,
            limit=limit,
        )

    def score(row: dict[str, Any]) -> tuple:
        blob = f"{row.get('name') or ''} {row.get('category_name') or ''}".lower()
        group = any(
            word in blob
            for word in ("family", "share", "platter", "combo", "bucket", "party")
        )
        # Prefer group deals when feeding several people; otherwise cheaper items.
        group_rank = 0 if (party_size and party_size >= 3 and group) else 1
        price = row.get("price_value")
        price_rank = price if isinstance(price, (int, float)) else 10_000
        return (group_rank, price_rank)

    rows.sort(key=score)
    for row in rows:
        row["is_deal"] = True
    return rows[:limit]


def list_reviews(
    conn: sqlite3.Connection,
    restaurant_id: int,
    limit: int = 12,
) -> list[dict[str, Any]]:
    """Stored review text for one restaurant (manual sample or future sources)."""
    rows = conn.execute(
        """
        SELECT id, review_text, liked_dishes, owner_response, source
        FROM reviews
        WHERE restaurant_id = ?
        ORDER BY id
        LIMIT ?
        """,
        (restaurant_id, limit),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        record = dict(row)
        raw = record.get("liked_dishes")
        if raw:
            try:
                record["liked_dishes"] = json.loads(raw)
            except json.JSONDecodeError:
                record["liked_dishes"] = []
        else:
            record["liked_dishes"] = []
        out.append(record)
    return out


def restaurant_has_reviews(conn: sqlite3.Connection, restaurant_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM reviews WHERE restaurant_id = ? LIMIT 1",
        (restaurant_id,),
    ).fetchone()
    return row is not None
