"""Flask frontend for browsing scraped Foodpanda restaurants and menus."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from flask import Flask, abort, jsonify, render_template

import config

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB = BASE_DIR / config.DEFAULT_DB_PATH

app = Flask(__name__)


def get_db(db_path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(db_path or DEFAULT_DB)
    if not path.exists():
        abort(503, description=f"Database not found: {path}. Run main.py first.")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _restaurant_list(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT
            r.id,
            r.name,
            r.url,
            r.rating,
            r.cuisine,
            r.address,
            r.delivery_time,
            r.image_url,
            COUNT(mi.id) AS item_count
        FROM restaurants r
        LEFT JOIN menu_categories mc ON mc.restaurant_id = r.id
        LEFT JOIN menu_items mi ON mi.category_id = mc.id
        GROUP BY r.id
        ORDER BY (r.rating IS NULL), r.rating DESC, r.name ASC
        """
    ).fetchall()
    return [dict(row) for row in rows]


def _restaurant_detail(conn: sqlite3.Connection, restaurant_id: int) -> dict | None:
    row = conn.execute(
        """
        SELECT id, name, url, rating, cuisine, address, delivery_time, image_url, scraped_at
        FROM restaurants
        WHERE id = ?
        """,
        (restaurant_id,),
    ).fetchone()
    if row is None:
        return None

    restaurant = dict(row)
    categories = conn.execute(
        """
        SELECT id, category_name
        FROM menu_categories
        WHERE restaurant_id = ?
        ORDER BY id
        """,
        (restaurant_id,),
    ).fetchall()

    menu = []
    for cat in categories:
        items = conn.execute(
            """
            SELECT id, name, price, description, image_url
            FROM menu_items
            WHERE category_id = ?
            ORDER BY id
            """,
            (cat["id"],),
        ).fetchall()
        menu.append(
            {
                "id": cat["id"],
                "category_name": cat["category_name"],
                "menu_items": [dict(item) for item in items],
            }
        )

    restaurant["categories"] = menu
    restaurant["item_count"] = sum(len(c["menu_items"]) for c in menu)
    return restaurant


@app.route("/")
def index():
    conn = get_db()
    try:
        restaurants = _restaurant_list(conn)
    finally:
        conn.close()
    return render_template("index.html", restaurants=restaurants)


@app.route("/restaurant/<int:restaurant_id>")
def restaurant_page(restaurant_id: int):
    conn = get_db()
    try:
        restaurant = _restaurant_detail(conn, restaurant_id)
    finally:
        conn.close()
    if restaurant is None:
        abort(404)
    return render_template("restaurant.html", restaurant=restaurant)


@app.route("/api/restaurants")
def api_restaurants():
    conn = get_db()
    try:
        restaurants = _restaurant_list(conn)
    finally:
        conn.close()
    return jsonify(restaurants)


@app.route("/api/restaurant/<int:restaurant_id>")
def api_restaurant(restaurant_id: int):
    conn = get_db()
    try:
        restaurant = _restaurant_detail(conn, restaurant_id)
    finally:
        conn.close()
    if restaurant is None:
        abort(404)
    return jsonify(restaurant)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
