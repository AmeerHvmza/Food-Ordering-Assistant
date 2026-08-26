"""Benchmark /chat turns with the real model. Logs agent.timing lines.

Turns match the latency report:
  1. I'm in Saddar looking for pizza
  2. Add that pizza and show the cart

Run from repo root: python scripts/measure_chat_latency.py
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

load_dotenv()

TURNS = (
    "I'm in Saddar looking for pizza",
    "Add that pizza and show the cart",
)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(name)s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("groq").setLevel(logging.INFO)

    from agent.llm import NoProviderConfigured, describe
    from api import sessions
    from api.sessions import latest_reply

    try:
        print(f"model={describe()}")
    except NoProviderConfigured as exc:
        print(f"SKIP: {exc}")
        return 2

    session_id = f"latency-bench-{int(time.time())}"
    print(f"session={session_id}")
    for i, message in enumerate(TURNS, 1):
        print(f"\n=== TURN {i}: {message!r} ===")
        t0 = time.perf_counter()
        state = sessions.send_message(session_id, message)
        wall = (time.perf_counter() - t0) * 1000
        reply = latest_reply(state)
        print(f"turn_wall_ms={wall:.1f}")
        print(f"locked={state.get('restaurant_name')} cart={state.get('cart')}")
        print(f"reply_chars={len(reply)}")
        print(reply[:500])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
