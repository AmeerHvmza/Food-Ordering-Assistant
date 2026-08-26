"""Live tone/language pass against the real agent.

Sends a fixed query set (Urdu script, Hindi, Roman Urdu, mixed, English,
tool-heavy browse, mixed proper-nouns) and prints raw replies plus
script/tone/accuracy flags. Conversation quality still needs a human read.

Run from repo root: python scripts/test_tone_language.py
"""

from __future__ import annotations

import logging
import re
import sys
import time
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

load_dotenv()

ARABIC_RE = re.compile(r"[\u0600-\u06FF]")
DEVANAGARI_RE = re.compile(r"[\u0900-\u097F]")
CORPORATE = (
    "i'd be happy to assist",
    "i would be happy to assist",
    "how may i help",
    "please let me know how i can",
    "thank you for reaching out",
    "it would be my pleasure",
    "certainly, i can assist",
)
PRICE_RE = re.compile(r"(?:rs\.?|pkr)\s*[\d,]+", re.I)

CASES = (
    {
        "id": "urdu_script",
        "expect_lang": "roman_urdu",
        "location": "Saddar",
        "query": "مجھے بریانی چاہیے، سستی سی",
        "note": "Pure Urdu script -> Roman Urdu, no Arabic letters in the reply.",
    },
    {
        "id": "hindi_devanagari",
        "expect_lang": "roman_urdu",
        "location": "Saddar",
        "query": "मुझे सस्ती बिरयानी चाहिए",
        "note": "Devanagari Hindi -> Roman Urdu, no Devanagari in the reply.",
    },
    {
        "id": "roman_urdu",
        "expect_lang": "roman_urdu",
        "location": "Saddar",
        "query": "kya deal hai is restaurant pe?",
        "note": "Roman Urdu stays Latin; mixed English is ok.",
    },
    {
        "id": "mixed",
        "expect_lang": "mixed",
        "location": "Saddar",
        "query": "yaar I need something spicy, koi deal ho to bata",
        "note": "Mixed input -> mixed Roman Urdu + English, still Latin script.",
    },
    {
        "id": "english",
        "expect_lang": "english",
        "location": "Saddar",
        "query": "I want cheap biryani for two",
        "note": "English in -> English out.",
    },
    {
        "id": "tool_heavy",
        "expect_lang": "english",
        "location": "Saddar",
        "query": (
            "Show me lots of restaurants that deliver here, I want to browse "
            "options — pizza, burgers, biryani, whatever is popular."
        ),
        "note": "Many tool results; tone must stay short/casual, not a dump.",
    },
    {
        "id": "urdu_proper_nouns",
        "expect_lang": "roman_urdu",
        "location": "Saddar",
        "query": "مجھے Pizza Hut سے 500 روپے کی pizza چاہیے",
        "note": "Keep Pizza Hut / 500 as-is; do not Urdu-script the names.",
    },
)


def has_arabic(text: str) -> bool:
    return bool(ARABIC_RE.search(text))


def has_devanagari(text: str) -> bool:
    return bool(DEVANAGARI_RE.search(text))


def looks_corporate(text: str) -> bool:
    lowered = text.lower()
    return any(phrase in lowered for phrase in CORPORATE)


def bullet_lines(text: str) -> int:
    return sum(
        1
        for line in text.splitlines()
        if line.strip().startswith(("- ", "* ", "•"))
        or re.match(r"^\d+\.\s", line.strip())
    )


def latin_ratio(text: str) -> float:
    letters = [ch for ch in text if unicodedata.category(ch).startswith("L")]
    if not letters:
        return 1.0
    latin = sum(1 for ch in letters if "LATIN" in unicodedata.name(ch, ""))
    return latin / len(letters)


def extract_reply(state: dict) -> str:
    from api.sessions import latest_reply

    return latest_reply(state)


def showcase_names(state: dict) -> list[str]:
    items = (state.get("showcase") or {}).get("items") or []
    names = []
    for item in items:
        name = item.get("name") or item.get("title")
        if name:
            names.append(str(name))
    return names


def evaluate(case: dict, reply: str, state: dict) -> dict[str, object]:
    expect = case["expect_lang"]
    arabic = has_arabic(reply)
    devanagari = has_devanagari(reply)
    latin = latin_ratio(reply)
    bullets = bullet_lines(reply)
    corporate = looks_corporate(reply)
    names = showcase_names(state)
    prices_in_reply = PRICE_RE.findall(reply)

    script_ok = not arabic and not devanagari
    if expect == "english":
        lang_ok = script_ok and latin >= 0.9
        # English replies can still use a stray "yaar"; that's mixed. Flag it.
        roman_markers = (
            "yaar",
            "bhai",
            "chalo",
            "waise",
            "hai",
            "hain",
            "kya",
            "mein",
            "kaafi",
            "toh",
            "phir",
            "agar",
            "liye",
        )
        mixed_leak = sum(1 for m in roman_markers if re.search(rf"\b{m}\b", reply, re.I))
        lang_ok = script_ok and latin >= 0.9 and mixed_leak == 0
        lang_note = f"latin_ratio={latin:.2f} roman_markers={mixed_leak}"
    else:
        lang_ok = script_ok
        lang_note = f"latin_ratio={latin:.2f}"

    proper_noun_ok = True
    if case["id"] == "urdu_proper_nouns":
        # Must keep the Latin brand name if it mentions the restaurant.
        mentioned = "pizza hut" in reply.lower() or "pizzahut" in reply.lower()
        if mentioned:
            proper_noun_ok = not has_arabic("Pizza Hut")  # tautology
        # Fail if Pizza Hut was rewritten in Arabic script near the brand.
        proper_noun_ok = not ARABIC_RE.search(reply) and "pizza" in reply.lower()

    accuracy_notes = []
    if names:
        leaked = [n for n in names if n.lower() in reply.lower()]
        accuracy_notes.append(f"showcase={len(names)} named_in_reply={len(leaked)}")
    if prices_in_reply:
        accuracy_notes.append(f"prices={prices_in_reply}")

    return {
        "script_ok": script_ok,
        "lang_ok": lang_ok,
        "lang_note": lang_note,
        "arabic": arabic,
        "devanagari": devanagari,
        "corporate": corporate,
        "bullets": bullets,
        "chars": len(reply),
        "proper_noun_ok": proper_noun_ok,
        "names": names[:8],
        "accuracy_notes": accuracy_notes,
        "location": state.get("location"),
        "restaurant": state.get("restaurant_name"),
    }


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    _configure_stdio()
    logging.basicConfig(level=logging.WARNING, format="%(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    from agent.llm import NoProviderConfigured, describe
    from api import sessions

    try:
        print(f"model={describe()}")
    except NoProviderConfigured as exc:
        print(f"SKIP: {exc}")
        return 2

    stamp = int(time.time())
    hard_fails = 0
    for i, case in enumerate(CASES, 1):
        session_id = f"tone-lang-{stamp}-{case['id']}"
        print(f"\n{'=' * 72}")
        print(f"CASE {i}/{len(CASES)} {case['id']}")
        print(f"expect={case['expect_lang']}  {case['note']}")
        print(f"query={case['query']}")
        sessions.set_location(session_id, case["location"])
        t0 = time.perf_counter()
        state = sessions.send_message(session_id, case["query"])
        wall_ms = (time.perf_counter() - t0) * 1000
        reply = extract_reply(state)
        flags = evaluate(case, reply, state)
        print(f"wall_ms={wall_ms:.0f} chars={flags['chars']} location={flags['location']}")
        print("--- reply ---")
        print(reply)
        print("--- flags ---")
        print(
            f"script_ok={flags['script_ok']} lang_ok={flags['lang_ok']} "
            f"{flags['lang_note']} arabic={flags['arabic']} "
            f"devanagari={flags['devanagari']}"
        )
        print(
            f"corporate={flags['corporate']} bullets={flags['bullets']} "
            f"proper_noun_ok={flags['proper_noun_ok']}"
        )
        if flags["names"]:
            print(f"showcase_names={flags['names']}")
        if flags["accuracy_notes"]:
            print(f"accuracy={flags['accuracy_notes']}")
        if flags["restaurant"]:
            print(f"locked={flags['restaurant']}")
        if not flags["lang_ok"] or not flags["script_ok"] or flags["corporate"]:
            hard_fails += 1
            print("HARD_FLAG: language/script/corporate")
        if not flags["proper_noun_ok"]:
            hard_fails += 1
            print("HARD_FLAG: proper noun")

    print(f"\n{'=' * 72}")
    print(f"hard_flags={hard_fails}")
    return 1 if hard_fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
