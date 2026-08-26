"""Price extraction for deal/discount Foodpanda menu items."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "foodpanda-scraper"))

from scraper.api_client import normalize_menu
from scraper.prices import extract_item_prices, parse_price_parts


class ParsePricePartsTests(unittest.TestCase):
    def test_concatenated_from_display_string(self) -> None:
        current, original = parse_price_parts("from Rs. 399Rs. 549")
        self.assertEqual(current, "399")
        self.assertEqual(original, "549")

    def test_fajita_example(self) -> None:
        current, original = parse_price_parts("from Rs. 300Rs. 600")
        self.assertEqual(current, "300")
        self.assertEqual(original, "600")

    def test_from_single_amount(self) -> None:
        current, original = parse_price_parts("from Rs. 528")
        self.assertEqual(current, "528")
        self.assertIsNone(original)

    def test_clean_original_29_style(self) -> None:
        current, original = parse_price_parts("964.75")
        self.assertEqual(current, "964.75")
        self.assertIsNone(original)

    def test_integer_from_api(self) -> None:
        current, original = parse_price_parts(300)
        self.assertEqual(current, "300")
        self.assertIsNone(original)

    def test_empty(self) -> None:
        self.assertEqual(parse_price_parts(""), (None, None))
        self.assertEqual(parse_price_parts(None), (None, None))


class ExtractItemPricesTests(unittest.TestCase):
    def test_structured_variation_beats_display_string(self) -> None:
        product = {
            "display_price": "from Rs. 300Rs. 600",
            "product_variations": [
                {"price": 300, "price_before_discount": 600, "name": "6 Inches"},
                {"price": 500, "price_before_discount": 800, "name": "9 Inches"},
            ],
        }
        current, original = extract_item_prices(product)
        self.assertEqual(current, "300")
        self.assertEqual(original, "600")

    def test_empty_display_and_no_variation_price_skipped(self) -> None:
        product = {
            "display_price": "",
            "product_variations": [],
        }
        self.assertEqual(extract_item_prices(product), (None, None))


class NormalizeMenuTests(unittest.TestCase):
    def test_skips_empty_stub_and_dedupes_by_name(self) -> None:
        payload = {
            "menus": [
                {
                    "menu_categories": [
                        {
                            "name": "Pizza",
                            "products": [
                                {
                                    "name": "Chicken Fajita Pizza",
                                    "display_price": "",
                                    "product_variations": [],
                                },
                                {
                                    "name": "Chicken Fajita Pizza",
                                    "display_price": "from Rs. 300Rs. 600",
                                    "product_variations": [
                                        {
                                            "price": 300,
                                            "price_before_discount": 600,
                                        }
                                    ],
                                },
                            ],
                        }
                    ]
                }
            ]
        }
        categories = normalize_menu(payload)
        self.assertEqual(len(categories), 1)
        items = categories[0]["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["name"], "Chicken Fajita Pizza")
        self.assertEqual(items[0]["price"], "300")
        self.assertEqual(items[0]["original_price"], "600")

    def test_falls_back_to_first_rs_in_display_price(self) -> None:
        payload = {
            "menus": [
                {
                    "menu_categories": [
                        {
                            "name": "Deals",
                            "products": [
                                {
                                    "name": "Azaadi Deal Small Pizza",
                                    "display_price": "from Rs. 399Rs. 549",
                                    "product_variations": [{}],
                                }
                            ],
                        }
                    ]
                }
            ]
        }
        items = normalize_menu(payload)[0]["items"]
        self.assertEqual(items[0]["price"], "399")
        self.assertEqual(items[0]["original_price"], "549")


if __name__ == "__main__":
    unittest.main()
