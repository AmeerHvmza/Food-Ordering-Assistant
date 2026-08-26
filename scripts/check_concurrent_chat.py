"""Fire a truly parallel burst at POST /v1/chat.

    python scripts/check_concurrent_chat.py <api_key> [base_url] [n]

Expect only 200 and 429. A 502 means a provider failure escaped the graph.
"""

from __future__ import annotations

import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

KEY = sys.argv[1]
BASE = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8000"
N = int(sys.argv[3]) if len(sys.argv) > 3 else 21
AUTH = {"Authorization": f"Bearer {KEY}"}


def hit(i: int) -> tuple[int, str]:
    response = requests.post(
        f"{BASE}/v1/chat",
        headers=AUTH,
        json={"session_id": f"concurrent-{i}", "message": "hi"},
        timeout=60,
    )
    detail = ""
    if response.status_code >= 400:
        try:
            detail = str(response.json().get("detail", ""))[:120]
        except Exception:
            detail = response.text[:120]
    return response.status_code, detail


def main() -> int:
    with ThreadPoolExecutor(max_workers=N) as pool:
        rows = [
            fut.result()
            for fut in as_completed(pool.submit(hit, i) for i in range(N))
        ]
    counts = Counter(code for code, _ in rows)
    print(f"n={N} counts={dict(counts)}")
    for code, detail in rows:
        if code not in {200, 429}:
            print(f"  UNEXPECTED {code} {detail}")
    bad = [code for code, _ in rows if code not in {200, 429}]
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
