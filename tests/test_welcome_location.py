"""Browser location endpoint and welcome copy, no LLM spend."""

from __future__ import annotations

import os
import tempfile
import unittest
import uuid
from pathlib import Path

# Milestone 6 made these routes authenticated, so the test needs its own tenant
# store. Set before anything opens the database.
os.environ.setdefault(
    "TENANT_DB_PATH",
    str(Path(tempfile.gettempdir()) / f"tenants_welcome_{uuid.uuid4().hex}.db"),
)

from fastapi.testclient import TestClient  # noqa: E402

from api import sessions  # noqa: E402
from api.main import app  # noqa: E402
from auth import api_keys, rate_limit, store  # noqa: E402


class LocationWelcomeTests(unittest.TestCase):
    def setUp(self) -> None:
        store.init_db()
        rate_limit.reset_for_tests()
        self.client = TestClient(app)
        sessions._pending.clear()
        tenant = api_keys.create_tenant(f"welcome-{uuid.uuid4().hex[:8]}", "unlimited")
        self.headers = {"Authorization": f"Bearer {api_keys.create_key(tenant)}"}

    def test_granted_welcome_skips_area_question(self) -> None:
        sid = "geo-granted"
        loc = self.client.post(
            f"/v1/sessions/{sid}/location",
            json={"lat": 24.8820, "lng": 67.0270},
            headers=self.headers,
        )
        self.assertEqual(loc.status_code, 200)
        self.assertEqual(loc.json()["location"], "Garden")
        welcome = self.client.post(
            f"/v1/sessions/{sid}/welcome", headers=self.headers
        )
        self.assertEqual(welcome.status_code, 200)
        reply = welcome.json()["reply"]
        self.assertIn("Garden", reply)
        self.assertNotIn("Which area", reply)
        self.assertEqual(welcome.json()["state"]["location"], "Garden")

    def test_denied_welcome_asks_for_area(self) -> None:
        sid = "geo-denied"
        welcome = self.client.post(
            f"/v1/sessions/{sid}/welcome", headers=self.headers
        )
        self.assertEqual(welcome.status_code, 200)
        reply = welcome.json()["reply"]
        self.assertIn("in the mood", reply)
        self.assertIn("which area", reply.lower())
        self.assertNotIn("Saddar, Garden", reply)
        self.assertIsNone(welcome.json()["state"].get("location"))

    def test_far_away_pin_does_not_set_location(self) -> None:
        sid = "geo-lahore"
        loc = self.client.post(
            f"/v1/sessions/{sid}/location",
            json={"lat": 31.5204, "lng": 74.3587},
            headers=self.headers,
        )
        self.assertIsNone(loc.json()["location"])
        welcome = self.client.post(
            f"/v1/sessions/{sid}/welcome", headers=self.headers
        )
        self.assertIn("in the mood", welcome.json()["reply"])


if __name__ == "__main__":
    unittest.main()
