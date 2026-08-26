"""One-shot fd-api probe. Exit 0 if menu JSON returns, 1 if blocked."""

from __future__ import annotations

import sys

from scraper import api_client

CODE = sys.argv[1] if len(sys.argv) > 1 else "s8dm"
LAT = 24.9180
LNG = 67.0910


def main() -> int:
    cats = api_client.fetch_menu(CODE, lat=LAT, lng=LNG)
    n = sum(len(c.get("items") or []) for c in cats) if cats else 0
    print(f"code={CODE} status={api_client.LAST_MENU_STATUS} categories={len(cats or [])} items={n}")
    return 0 if cats else 1


if __name__ == "__main__":
    raise SystemExit(main())
