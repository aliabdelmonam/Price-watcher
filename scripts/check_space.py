#!/usr/bin/env python3
"""Ask your private Space for /health or /test, using HF_TOKEN and SPACE_URL from .env.

    python scripts/check_space.py              # /health: is the loop alive?
    python scripts/check_space.py test         # one product per store, fetched from the cloud
    python scripts/check_space.py test <url>   # one product link, fetched from the cloud
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from watcher.config import env  # noqa: E402  (importing it also loads .env)


def main() -> None:
    url, token = env("SPACE_URL"), env("HF_TOKEN")
    if not (url and token):
        sys.exit("Set SPACE_URL (https://<you>-price-watcher.hf.space) and HF_TOKEN in .env")
    what = (sys.argv[1] if len(sys.argv) > 1 else "health").strip("/")
    params = {"url": sys.argv[2]} if len(sys.argv) > 2 else None
    resp = requests.get(f"{url.rstrip('/')}/{what}", params=params,
                        headers={"Authorization": f"Bearer {token}"}, timeout=300)
    print(f"HTTP {resp.status_code}")
    try:
        data = resp.json()
    except ValueError:
        print(resp.text[:800])
        return
    if isinstance(data, list):
        for row in data:
            print(f"{row['store']:<14} {row['verdict']:<14} HTTP {row['http_status']}  "
                  f"price {row['price']}  {row['error'] or ''}")
        if not data:
            print("no items: fill the Watchlist tab first")
    else:
        print(json.dumps(data, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
