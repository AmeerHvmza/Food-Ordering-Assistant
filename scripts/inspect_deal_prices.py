"""Inspect deal-price rows and dump raw fd-api fields for one broken item."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "foodpanda-scraper"))

from scraper import api_client  # noqa: E402

DB = ROOT / "foodpanda-scraper" / "foodpanda.db"


def main() -> None:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    def n(sql: str) -> int:
        return int(conn.execute(sql).fetchone()[0])

    print("empty price", n("SELECT COUNT(*) FROM menu_items WHERE TRIM(COALESCE(price,'')) = ''"))
    print(
        "Rs.Rs. or from",
        n(
            "SELECT COUNT(*) FROM menu_items WHERE price LIKE '%Rs.%Rs.%' "
            "OR LOWER(COALESCE(price,'')) LIKE '%from%'"
        ),
    )
    print(
        "id>=45 empty",
        n(
            "SELECT COUNT(*) FROM menu_items mi "
            "JOIN menu_categories mc ON mc.id = mi.category_id "
            "WHERE mc.restaurant_id >= 45 AND TRIM(COALESCE(mi.price,'')) = ''"
        ),
    )
    print(
        "id>=45 RsRs/from",
        n(
            "SELECT COUNT(*) FROM menu_items mi "
            "JOIN menu_categories mc ON mc.id = mi.category_id "
            "WHERE mc.restaurant_id >= 45 AND ("
            "mi.price LIKE '%Rs.%Rs.%' OR LOWER(COALESCE(mi.price,'')) LIKE '%from%')"
        ),
    )
    print(
        "id 16-44 empty",
        n(
            "SELECT COUNT(*) FROM menu_items mi "
            "JOIN menu_categories mc ON mc.id = mi.category_id "
            "WHERE mc.restaurant_id BETWEEN 16 AND 44 "
            "AND TRIM(COALESCE(mi.price,'')) = ''"
        ),
    )

    print("\n=== samples ===")
    for row in conn.execute(
        """
        SELECT r.id AS rid, r.name AS rest, mi.id AS item_id, mi.name,
               mi.price, mc.category_name
        FROM menu_items mi
        JOIN menu_categories mc ON mc.id = mi.category_id
        JOIN restaurants r ON r.id = mc.restaurant_id
        WHERE r.id IN (45, 69)
          AND (mi.name LIKE '%Fajita%' OR mi.price LIKE '%Rs.%Rs.%'
               OR TRIM(COALESCE(mi.price,'')) = '')
        ORDER BY r.id, mi.name, mi.id
        LIMIT 40
        """
    ):
        print(dict(row))

    rest = conn.execute(
        "SELECT id, name, url FROM restaurants WHERE id = 69"
    ).fetchone()
    print("\nrestaurant 69", dict(rest))
    code = api_client.extract_vendor_code(rest["url"])
    print("code", code)

    cats = None
    for lat, lng in ((24.9170, 67.1340), (24.8607, 67.0011), (24.8736, 67.0554)):
        cats = api_client.fetch_menu(code, lat=lat, lng=lng)
        n_items = sum(len(c.get("items") or []) for c in (cats or []))
        print("fetch_menu", lat, lng, "cats", None if cats is None else len(cats), "items", n_items)
        if n_items:
            break

    # Dump RAW product JSON for Chicken Fajita from the vendor payload.
    from scraper.api_client import _fd_api_headers, LAST_MENU_STATUS
    import requests
    import config

    url = config.FD_API_VENDOR_URL.format(vendor_code=code)
    params = {
        "latitude": "24.9170",
        "longitude": "67.1340",
        "language_id": "1",
        "include": "menus",
    }
    resp = requests.get(url, headers=_fd_api_headers(), params=params, timeout=20)
    print("raw HTTP", resp.status_code, "LAST", LAST_MENU_STATUS)
    if resp.status_code != 200:
        print(resp.text[:300])
        return
    payload = resp.json().get("data") or {}
    hits = []
    for menu in payload.get("menus") or []:
        for cat in menu.get("menu_categories") or []:
            for product in cat.get("products") or []:
                name = product.get("name") or ""
                if "fajita" in name.lower() or "chicken" in name.lower() and "pizza" in name.lower():
                    hits.append((cat.get("name"), product))
    print("matching products", len(hits))
    for cat_name, product in hits[:8]:
        variations = product.get("product_variations") or []
        slim = {
            "category": cat_name,
            "name": product.get("name"),
            "display_price": product.get("display_price"),
            "price": product.get("price"),
            "original_price": product.get("original_price"),
            "discounted_price": product.get("discounted_price"),
            "keys": sorted(product.keys()),
            "variation0_keys": sorted(variations[0].keys()) if variations else [],
            "variation0": variations[0] if variations else None,
        }
        print(json.dumps(slim, default=str, indent=2)[:2500])
        print("---")

    # Count how many products have empty variation price vs display_price
    empty_var = 0
    used_display = 0
    concatish = 0
    products = 0
    for menu in payload.get("menus") or []:
        for cat in menu.get("menu_categories") or []:
            for product in cat.get("products") or []:
                products += 1
                variations = product.get("product_variations") or []
                vp = variations[0].get("price") if variations and isinstance(variations[0], dict) else None
                dp = product.get("display_price")
                if vp in (None, "", 0):
                    empty_var += 1
                if isinstance(dp, str) and ("Rs." in dp or "from" in dp.lower()):
                    concatish += 1
                    used_display += 1
    print("products", products, "empty/zero var price", empty_var, "display looks concatenated", concatish)


if __name__ == "__main__":
    main()
