"""Burst Gemini until RPM 429 to confirm Groq fallback in agent.timing logs.

Does not run unless GOOGLE_API_KEY (or GEMINI_API_KEY) and GROQ_API_KEY are
set. Unset LLM_PROVIDER so routing is used. Stop after the first fallback or
after MAX_CALLS, whichever comes first.

  python scripts/demo_gemini_rpm_fallback.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

load_dotenv()

from langchain_core.messages import HumanMessage  # noqa: E402

from agent.llm import get_llm, use_gemini_routing  # noqa: E402
from agent.llm_router import BUSY_MESSAGE, is_system_busy, reset_gemini_cooldown  # noqa: E402

MAX_CALLS = 20


def main() -> int:
    if not use_gemini_routing():
        print(
            "Routing is off. Unset LLM_PROVIDER (or set it to gemini) and "
            "provide GOOGLE_API_KEY / GEMINI_API_KEY."
        )
        return 1
    if not os.getenv("GROQ_API_KEY"):
        print("GROQ_API_KEY is required so fallback can run.")
        return 1

    reset_gemini_cooldown()
    get_llm.cache_clear()
    model = get_llm()
    print(f"Calling {MAX_CALLS} tiny completions to trip Gemini RPM...")
    for i in range(1, MAX_CALLS + 1):
        result = model.invoke([HumanMessage(content="Reply with the single word ok.")])
        meta = getattr(result, "response_metadata", None) or {}
        served = meta.get("routed_provider")
        fallback = meta.get("fallback")
        print(f"  {i:02d} served={served} fallback={fallback} content={result.content!r:.80}")
        if served == "groq" or is_system_busy(result) or result.content == BUSY_MESSAGE:
            print("Fallback path hit. Watch agent.timing for llm_route / llm_route_429.")
            return 0
    print("No 429 in this burst (quota may be higher than 15 RPM).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
