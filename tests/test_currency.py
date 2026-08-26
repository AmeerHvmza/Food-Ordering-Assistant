"""Currency symbol post-processing."""

from __future__ import annotations

import unittest

from agent.currency import sanitize_currency


class SanitizeCurrencyTests(unittest.TestCase):
    def test_replaces_indian_rupee_symbol(self) -> None:
        self.assertEqual(
            sanitize_currency("That's ₹399 for the deal."),
            "That's Rs.399 for the deal.",
        )

    def test_leaves_pkr_and_rs_alone(self) -> None:
        self.assertEqual(sanitize_currency("PKR 399 / Rs. 399"), "PKR 399 / Rs. 399")

    def test_empty(self) -> None:
        self.assertEqual(sanitize_currency(""), "")


if __name__ == "__main__":
    unittest.main()
