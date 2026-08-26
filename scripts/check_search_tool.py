"""Exercise the agent's search_restaurants tool the way the chat calls it."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.tools import search_restaurants  # noqa: E402


def main() -> None:
    location = sys.argv[1] if len(sys.argv) > 1 else "Jauhar"
    craving = sys.argv[2] if len(sys.argv) > 2 else "chai"
    result = search_restaurants.invoke(
        {
            "name": "search_restaurants",
            "type": "tool_call",
            "id": "check",
            "tool_call_id": "check",
            "args": {
                "craving": craving,
                "location": location,
                "state": {"location": location},
            },
        }
    )
    message = result.update["messages"][0]
    print(message.content)
    showcase = result.update.get("showcase") or {}
    print(f"\ncards={len(showcase.get('items') or [])}")


if __name__ == "__main__":
    main()
