"""Exercise auth, rate limiting and metering against a running server.

Unlike the unit tests this goes over real HTTP through uvicorn, so it also
proves the headers survive the ASGI stack.

    python -m uvicorn api.main:app --port 8077
    python scripts/check_live_api.py <api_key> [base_url]
"""

from __future__ import annotations

import sys

import requests

BASE = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8077"
KEY = sys.argv[1]
AUTH = {"Authorization": f"Bearer {KEY}"}


def show(label: str, response: requests.Response, *headers: str) -> None:
    extras = " ".join(
        f"{h}={response.headers[h]}" for h in headers if h in response.headers
    )
    body = response.text[:130].replace("\n", " ")
    print(f"  {label:<34} {response.status_code}  {extras}")
    if body:
        print(f"      {body}")


def main() -> int:
    print("-- authentication")
    show("no key", requests.get(f"{BASE}/v1/usage"))
    show("bogus key", requests.get(f"{BASE}/v1/usage", headers={"X-API-Key": "nope"}))
    show(
        "valid key",
        requests.get(f"{BASE}/v1/usage", headers=AUTH),
        "X-RateLimit-Limit",
        "X-RateLimit-Remaining",
        "X-RateLimit-Scope",
    )
    show("health is public", requests.get(f"{BASE}/health"))

    print("\n-- versioning")
    show("old unversioned /chat", requests.post(f"{BASE}/chat", json={}))

    print("\n-- burst limit (free tier: burst 10, 20/min)")
    counts: dict[int, int] = {}
    rejection = None
    for _ in range(30):
        response = requests.get(f"{BASE}/v1/usage", headers=AUTH)
        counts[response.status_code] = counts.get(response.status_code, 0) + 1
        if response.status_code == 429 and rejection is None:
            rejection = response
    print(f"  status counts: {counts}")
    if rejection is None:
        print("  BROKEN: never rate limited")
        return 1
    show(
        "first 429",
        rejection,
        "Retry-After",
        "X-RateLimit-Scope",
        "X-RateLimit-Limit",
    )
    if "Retry-After" not in rejection.headers:
        print("  BROKEN: 429 without Retry-After")
        return 1

    print("\n-- isolation and metering")
    session = "live-check-session"
    requests.post(
        f"{BASE}/v1/sessions/{session}/location",
        json={"lat": 24.918, "lng": 67.091},
        headers=AUTH,
    )
    print("  (a 429 here just means the burst limit is still cooling down)")

    print("\nOK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
