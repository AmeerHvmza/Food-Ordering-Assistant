"""Bayesian (IMDB-style) restaurant ranking from rating + review_count.

weighted_rating = (v / (v + m)) * R + (m / (v + m)) * C

R = restaurant average rating
v = restaurant review count
m = dataset median review count (computed at query time)
C = dataset mean rating (computed at query time)

Low-volume ratings are pulled toward C so a 5.0 with few reviews does not
outrank a 4.9 with thousands. m and C are recomputed from the current
dataset so a later re-scrape does not need a code change.

# Needs periodic re-fetch of review_count (see foodpanda-scraper NOTES.md).
"""

from __future__ import annotations

import sqlite3
import statistics
from typing import Any

# Hedge in agent language when v is below this fraction of m.
LOW_CONFIDENCE_FRACTION = 0.25


def weighted_rating(R: float, v: float, m: float, C: float) -> float:
    """Return the IMDB-style weighted rating.

    If m + v is 0 (empty dataset edge case), return C (or R if C is unused).
    """
    if v < 0:
        raise ValueError(f"review count v must be >= 0, got {v}")
    if m < 0:
        raise ValueError(f"threshold m must be >= 0, got {m}")
    denom = v + m
    if denom == 0:
        return C
    return (v / denom) * R + (m / denom) * C


def rating_confidence(v: float, m: float) -> str:
    """'low' when v < m/4, otherwise 'ok'."""
    if m <= 0:
        return "ok" if v > 0 else "low"
    return "low" if v < m * LOW_CONFIDENCE_FRACTION else "ok"


def compute_m_and_c(
    rows: list[dict[str, Any]] | sqlite3.Connection,
    *,
    review_col: str = "review_count",
    rating_col: str = "rating",
) -> tuple[float, float]:
    """Return (m, C) = (median review_count, mean rating) over the dataset.

    Rows with NULL review_count or rating are skipped for that statistic.
    Raises ValueError if there is not enough data to compute both.
    """
    if isinstance(rows, sqlite3.Connection):
        fetched = rows.execute(
            f"SELECT {rating_col} AS rating, {review_col} AS review_count "
            "FROM restaurants"
        ).fetchall()
        records = [dict(r) for r in fetched]
    else:
        records = rows

    counts = [
        float(r[review_col])
        for r in records
        if r.get(review_col) is not None
    ]
    ratings = [
        float(r[rating_col])
        for r in records
        if r.get(rating_col) is not None
    ]
    if not counts:
        raise ValueError("cannot compute m: no review_count values")
    if not ratings:
        raise ValueError("cannot compute C: no rating values")
    return float(statistics.median(counts)), float(statistics.mean(ratings))


def rank_restaurants(
    rows: list[dict[str, Any]],
    *,
    m: float | None = None,
    C: float | None = None,
) -> list[dict[str, Any]]:
    """Return copies of rows sorted by weighted_rating descending.

    Each copy gains weighted_rating and rating_confidence. Rows missing
    rating or review_count are sorted last (weighted_rating None).
    """
    if m is None or C is None:
        computed_m, computed_c = compute_m_and_c(rows)
        m = computed_m if m is None else m
        C = computed_c if C is None else C

    decorated: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        R = row.get("rating")
        v = row.get("review_count")
        if R is None or v is None:
            item["weighted_rating"] = None
            item["rating_confidence"] = "low"
            item["_sort"] = (0, 0.0)
        else:
            wr = weighted_rating(float(R), float(v), m, C)
            item["weighted_rating"] = wr
            item["rating_confidence"] = rating_confidence(float(v), m)
            item["_sort"] = (1, wr)
        decorated.append(item)

    decorated.sort(key=lambda r: r["_sort"], reverse=True)
    for item in decorated:
        item.pop("_sort", None)
    return decorated


def _self_check() -> None:
    """Sanity checks that do not need the live database."""
    # Low-volume 5.0 must rank below high-volume 4.9 at median m.
    m, C = 3208.0, 4.807
    al_maedat = weighted_rating(5.0, 63, m, C)
    mcdonalds = weighted_rating(4.9, 19935, m, C)
    foods_inn = weighted_rating(4.9, 39949, m, C)
    assert al_maedat < mcdonalds, (al_maedat, mcdonalds)
    assert al_maedat < foods_inn, (al_maedat, foods_inn)
    assert rating_confidence(63, m) == "low"
    assert rating_confidence(19935, m) == "ok"
    # Empty-dataset edge: m=0, v=0 -> C
    assert weighted_rating(5.0, 0, 0, C) == C


if __name__ == "__main__":
    import argparse
    from pathlib import Path

    _self_check()
    print("self-check passed")

    parser = argparse.ArgumentParser(description="Rank restaurants in a foodpanda.db")
    parser.add_argument(
        "--db-path",
        default=str(
            Path(__file__).resolve().parents[1]
            / "foodpanda-scraper"
            / "foodpanda.db"
        ),
    )
    args = parser.parse_args()
    db_path = Path(args.db_path)
    if not db_path.exists():
        raise SystemExit(f"Database not found: {db_path}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT id, name, rating, review_count FROM restaurants"
            )
        ]
        m, C = compute_m_and_c(conn)
        ranked = rank_restaurants(rows, m=m, C=C)
    finally:
        conn.close()

    print(f"m (median review_count) = {m:.0f}")
    print(f"C (mean rating)         = {C:.4f}")
    print()
    print(f"{'#':<4} {'name':<42} {'R':<6} {'v':<8} {'wr':<8} conf")
    for i, row in enumerate(ranked, 1):
        wr = row["weighted_rating"]
        wr_s = f"{wr:.4f}" if wr is not None else "n/a"
        print(
            f"{i:<4} {row['name'][:41]:<42} {row['rating']!s:<6} "
            f"{row['review_count']!s:<8} {wr_s:<8} {row['rating_confidence']}"
        )
