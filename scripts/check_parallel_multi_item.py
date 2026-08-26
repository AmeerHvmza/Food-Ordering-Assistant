"""Live end-to-end check for the multi-item request that crashed.

Runs the real graph (real LLM, real DB) through the conversation from the bug
report and reports, per tool round, how many tool calls the model emitted. A
round with 2+ calls is parallel tool calling actually happening; if every round
has exactly one call the crash is "fixed" only because the feature went away.

Usage: python scripts/check_parallel_multi_item.py
"""

from __future__ import annotations

import logging
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

load_dotenv()

from agent.state import public_showcase, serialize_state  # noqa: E402
from api import sessions  # noqa: E402

TURNS = [
    "i'm in Gulistan-e-Jauhar, looking for chai and parathas",
    "i would like to order 2 parathas and 3 cup chai do it for me from quetta mashallah",
]

_rounds: list[list[str]] = []


class _ToolRoundCapture(logging.Handler):
    """agent.graph logs 'tool_round names=[...]' once per tools node step."""

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if not message.startswith("tool_round names="):
            return
        names = message.split("tool_round names=", 1)[1].split(" duration_ms=")[0]
        _rounds.append(names)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(name)s %(message)s")
    logging.getLogger("agent.timing").addHandler(_ToolRoundCapture())

    session_id = f"parallel-check-{uuid.uuid4().hex[:8]}"
    state: dict = {}

    for turn, message in enumerate(TURNS, start=1):
        print(f"\n=== turn {turn}: {message}")
        before = len(_rounds)
        state = sessions.send_message(session_id, message)
        snapshot = serialize_state(state)
        reply = next(
            (
                m["content"]
                for m in reversed(snapshot["messages"])
                if m["role"] == "assistant" and m["content"].strip()
            ),
            "",
        )
        print(f"--- assistant: {reply[:600]}")
        for rnd in _rounds[before:]:
            print(f"--- tool round: {rnd}")

    snapshot = serialize_state(state)
    print("\n=== final cart")
    for line in snapshot["cart"]:
        print(f"  {line['name']} x{line['qty']} @ {line.get('price')}")
    print(f"  subtotal={snapshot['cart_subtotal']} totals={snapshot['cart_totals']}")

    showcase = public_showcase(state.get("showcase"))
    print("\n=== final showcase")
    if showcase:
        print(f"  kind={showcase['kind']} title={showcase['title']}")
        for item in showcase["items"]:
            print(f"  card: {item.get('kind')} {item.get('name')}")
    else:
        print("  none")

    parallel = [r for r in _rounds if r.count("'") >= 4]
    print("\n=== verdict")
    print(f"  tool rounds: {len(_rounds)}")
    print(f"  rounds with 2+ calls (parallel): {len(parallel)}")
    for rnd in parallel:
        print(f"    {rnd}")
    if not parallel:
        print("  WARNING: no parallel round observed; feature may be disabled")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
