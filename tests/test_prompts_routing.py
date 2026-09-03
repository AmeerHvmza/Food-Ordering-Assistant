"""Routing prompt is much smaller than the full system prompt."""

from __future__ import annotations

import unittest

from agent.prompts import build_routing_prompt, build_system_prompt


class PromptSizeTests(unittest.TestCase):
    def test_routing_drops_policies(self) -> None:
        state = {"location": "Saddar", "craving": "pizza", "messages": []}
        full = build_system_prompt(state)
        routing = build_routing_prompt(state)
        self.assertGreater(len(full), 10000)
        self.assertLess(len(routing), 2500)
        self.assertIn("FOODPANDA PLATFORM POLICY", full)
        self.assertNotIn("FOODPANDA PLATFORM POLICY", routing)
        self.assertIn("do not call view_cart", routing.lower())
        self.assertIn("Roman Urdu", full)
        self.assertIn("TONE", full)
        self.assertIn("TONE & LANGUAGE", routing)
        self.assertIn("unlock_restaurant", full)
        self.assertIn("unlock_restaurant", routing)
        self.assertIn("3000→800", full)
        self.assertIn("Johar/Jauhar", full)
        self.assertIn("remember_preferences never unlocks", full)
        self.assertIn("remember_preferences does not unlock", routing)


if __name__ == "__main__":
    unittest.main()
