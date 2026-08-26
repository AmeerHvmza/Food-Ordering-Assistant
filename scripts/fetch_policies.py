"""Extract Foodpanda Pakistan policy text into policies.md.

The agent quotes platform policy (delivery fees, cancellation, vouchers) to
users, so the text must come from the real pages rather than from model memory.

Two input paths:

1. `--online` hits the live pages directly. This is the preferred path but
   www.foodpanda.pk sits behind PerimeterX, which answers plain HTTP clients
   with HTTP 403 (and sometimes just stalls the connection). See
   foodpanda-scraper/NOTES.md section 3.
2. Default: extract from dated page captures in `scripts/captures/`. Refresh a
   capture by opening the URL in a real browser and saving the rendered text.

Either way the extraction itself is deterministic, so policies.md is always
traceable to real page text and never to model recall.

# POLICIES GO STALE. foodpanda revises its Terms without notice and the
# published date is printed in policies.md. Re-run this periodically (monthly
# is reasonable), refresh the captures, and diff the output before shipping.
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from pathlib import Path

import requests
from bs4 import BeautifulSoup

REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_PATH = REPO_ROOT / "policies.md"
CAPTURE_DIR = Path(__file__).resolve().parent / "captures"
TOS_CAPTURE = CAPTURE_DIR / "tos_capture.txt"
FAQ_CAPTURE = CAPTURE_DIR / "faq_capture.txt"

TOS_URL = "https://www.foodpanda.pk/contents/terms-and-conditions.htm"
FAQ_URL = "https://www.foodpanda.pk/contents/faq.htm"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
TIMEOUT_SEC = 20

# Top-level Terms sections the assistant actually needs. The full document is
# ~60KB of corporate boilerplate; loading all of it into every prompt wastes
# tokens without helping a user decide what to eat.
WANTED_SECTIONS = {
    6: "Orders (confirmation, minimum order value, allergens, cancellation)",
    7: "Prices and Payments (delivery fees, taxes, credit)",
    8: "Delivery, Pick-Up and Vendor Delivery",
    9: "Vouchers, Discounts and Promotions",
}

HEADING_RE = re.compile(r"^(\d{1,2})\s+[A-Za-z]")
PUBLISHED_RE = re.compile(r"Published:\s*\[?([^\]\n]+)\]?")


class BlockedError(RuntimeError):
    """Raised when bot protection answers instead of the real page."""


def html_to_text(html: str) -> str:
    """Flatten HTML to stripped, newline-separated text."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def fetch_text(url: str) -> str:
    """Download a page and flatten it to text, or raise BlockedError."""
    try:
        resp = requests.get(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
            timeout=TIMEOUT_SEC,
        )
    except requests.RequestException as exc:
        raise BlockedError(f"request failed: {exc}") from exc

    if resp.status_code != 200:
        raise BlockedError(f"HTTP {resp.status_code} (PerimeterX blocks plain clients)")
    lowered = resp.text.lower()
    if "captcha" in lowered or "access to this page has been denied" in lowered:
        raise BlockedError("bot-protection interstitial returned instead of the page")
    return html_to_text(resp.text)


def read_capture(path: Path) -> str:
    """Load a saved page capture, flattening it if it is raw HTML."""
    if not path.exists():
        raise FileNotFoundError(
            f"Capture not found: {path}\n"
            "Open the URL in a real browser, save the rendered text, and place "
            "it here (see the module docstring)."
        )
    raw = path.read_text(encoding="utf-8", errors="replace")
    if "<html" in raw[:2000].lower():
        return html_to_text(raw)
    lines = [line.strip() for line in raw.splitlines()]
    return "\n".join(line for line in lines if line)


def find_headings(lines: list[str]) -> list[tuple[int, int]]:
    """Return (section_number, line_index) for top-level numbered headings.

    Sub-clauses like "6.1 When you place an Order" do not match, because the
    pattern requires whitespace directly after the digits.
    """
    found: list[tuple[int, int]] = []
    for idx, line in enumerate(lines):
        match = HEADING_RE.match(line)
        if match:
            found.append((int(match.group(1)), idx))
    return found


def extract_sections(text: str) -> tuple[dict[int, str], list[int]]:
    """Slice the wanted top-level sections out of the Terms text."""
    lines = text.splitlines()
    headings = find_headings(lines)
    sections: dict[int, str] = {}

    for position, (number, start) in enumerate(headings):
        if number not in WANTED_SECTIONS or number in sections:
            continue
        end = len(lines)
        for next_number, next_start in headings[position + 1:]:
            if next_number > number:
                end = next_start
                break
        body = "\n\n".join(lines[start:end])
        sections[number] = body.strip()

    missing = sorted(n for n in WANTED_SECTIONS if n not in sections)
    return sections, missing


def published_date(text: str) -> str:
    match = PUBLISHED_RE.search(text)
    return match.group(1).strip() if match else "unknown"


def build_markdown(
    tos_text: str,
    faq_text: str,
    source_mode: str,
) -> tuple[str, list[int]]:
    sections, missing = extract_sections(tos_text)
    fetched_on = dt.date.today().isoformat()
    published = published_date(tos_text)

    parts: list[str] = [
        "# Foodpanda Pakistan — platform policy extract",
        "",
        "> Generated by `scripts/fetch_policies.py`. Do not hand-edit; re-run the",
        "> script instead so the text stays traceable to the real pages.",
        "",
        f"- **Extracted:** {fetched_on}",
        f"- **Input:** {source_mode}",
        f"- **Terms published date (as stated on the page):** {published}",
        f"- **Terms source:** {TOS_URL}",
        f"- **FAQ source:** {FAQ_URL}",
        "",
        "**Staleness warning:** foodpanda revises these Terms without notice.",
        "Anything below is only accurate as of the fetch date above. Re-run the",
        "fetch script periodically and re-check before relying on a specific",
        "clause. This extract covers the operational sections only, not the full",
        "Terms of Use.",
        "",
        "---",
        "",
    ]

    if missing:
        parts += [
            "> **Extraction warning:** expected sections "
            + ", ".join(str(n) for n in missing)
            + " were not found. The page structure may have changed; verify"
            " `scripts/fetch_policies.py` against the live Terms.",
            "",
        ]

    for number in sorted(sections):
        parts += [
            f"## Terms section {number} — {WANTED_SECTIONS[number]}",
            "",
            sections[number],
            "",
        ]

    parts += [
        "## FAQ page (verbatim)",
        "",
        faq_text.strip(),
        "",
    ]
    return "\n".join(parts), missing


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        default=str(OUTPUT_PATH),
        help="Path to write the extract (default: policies.md at repo root)",
    )
    parser.add_argument(
        "--online",
        action="store_true",
        help="Try the live URLs first (usually blocked by PerimeterX)",
    )
    parser.add_argument("--tos-capture", default=str(TOS_CAPTURE))
    parser.add_argument("--faq-capture", default=str(FAQ_CAPTURE))
    args = parser.parse_args()

    source_mode = ""
    tos_text = faq_text = ""

    if args.online:
        try:
            tos_text = fetch_text(TOS_URL)
            faq_text = fetch_text(FAQ_URL)
            source_mode = "live fetch of the URLs below"
        except BlockedError as exc:
            print(f"Live fetch blocked ({exc}); falling back to captures.", file=sys.stderr)

    if not tos_text:
        try:
            tos_text = read_capture(Path(args.tos_capture))
            faq_text = read_capture(Path(args.faq_capture))
        except FileNotFoundError as exc:
            # Never clobber a good policies.md with a failed run.
            print(f"{exc}", file=sys.stderr)
            print("policies.md left unchanged.", file=sys.stderr)
            return 1
        source_mode = (
            f"browser page captures ({Path(args.tos_capture).name}, "
            f"{Path(args.faq_capture).name}) — direct HTTP is blocked by PerimeterX"
        )

    markdown, missing = build_markdown(tos_text, faq_text, source_mode)
    out_path = Path(args.output)
    out_path.write_text(markdown, encoding="utf-8")

    print(f"Wrote {out_path} ({len(markdown):,} chars) from {source_mode}")
    if missing:
        print(f"WARNING: missing Terms sections: {missing}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
