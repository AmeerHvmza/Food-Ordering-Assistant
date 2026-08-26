"""Nearest Karachi area snap for browser geolocation."""

from __future__ import annotations

import unittest

from db.geo import nearest_area


class NearestAreaTests(unittest.TestCase):
    def test_garden_pin(self) -> None:
        match = nearest_area(24.8820, 67.0270)
        self.assertIsNotNone(match)
        self.assertEqual(match[0], "Garden")
        self.assertLess(match[1], 0.2)

    def test_saddar_pin(self) -> None:
        match = nearest_area(24.8607, 67.0011)
        self.assertIsNotNone(match)
        self.assertEqual(match[0], "Saddar")

    def test_lahore_is_out_of_coverage(self) -> None:
        self.assertIsNone(nearest_area(31.5204, 74.3587))


if __name__ == "__main__":
    unittest.main()
