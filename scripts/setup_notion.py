#!/usr/bin/env python3
"""Connect the watcher to Notion (README section 3).

    python scripts/setup_notion.py              check the token and page, create Watchlist + Deals
    python scripts/setup_notion.py --example    also add "Soundcore R60i NC" to the Watchlist
    python scripts/setup_notion.py --test-deal  also write one test row to Deals (delete it afterwards)
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from watcher.config import load_config  # noqa: E402
from watcher.notion import DEALS, SITES, WATCHLIST, NotionClient, NotionError, explain  # noqa: E402
from watcher.sites import load_sites, parse_site  # noqa: E402
from watcher.watchlist import build_items  # noqa: E402


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--example", action="store_true", help="add the Soundcore R60i NC example to the Watchlist")
    parser.add_argument("--test-deal", action="store_true", help="write one test row to Deals")
    args = parser.parse_args()
    cfg = load_config()
    if not cfg.notion_token:
        sys.exit("Put NOTION_TOKEN=ntn_... in .env first (README section 3).")
    client = NotionClient(cfg.notion_token, cfg.notion_page, timezone_name=cfg.settings.timezone)
    if not client.page_id:
        sys.exit("Put NOTION_PAGE in .env: the link of the Notion page you connected (••• → Copy link).")
    client.site_keys = [site.key for site in load_sites(cfg.sites)[0]]
    try:
        client.connect()
        sites = client.read_sites()
        if not sites:
            for entry in cfg.sites:
                site, problem = parse_site(entry)
                if site and not problem:
                    client.add_site({"name": site.key, "key": site.key, "url": site.url,
                                     "enabled": site.enabled, "currency": site.currency})
            sites = client.read_sites()
        if args.example:
            if not any(r.get("name", "").casefold() == "soundcore r60i nc" for r in client.read_watchlist()):
                client.add_watchlist_item({"name": "Soundcore R60i NC", "notes": "example"})
        rows = client.read_watchlist()
        if args.test_deal:
            now = time.time()
            client.create_deal({"key": "test@setup", "item": "TEST ROW (delete me)", "site": "noon", "status": "Active",
                                "price": 999, "was": 1299, "store_discount": 23.1, "why": ["ON SALE"],
                                "in_stock": True, "currency": "EGP", "link": "https://www.noon.com/egypt-en/",
                                "found": now, "updated": now})
    except NotionError as exc:
        sys.exit(f"Notion said: {exc}\n{explain(exc)}")
    print(f"✅ Connected. Databases in your page: {SITES} ({client.db[SITES]}), "
          f"{WATCHLIST} ({client.db[WATCHLIST]}), {DEALS} ({client.db[DEALS]})")
    print(f"Sites: {len(sites)} enabled/disabled row(s)")
    problems: list[str] = []
    items = build_items(rows, "Watchlist", problems)
    print(f"Watchlist: {len(rows)} row(s), {len(items)} item(s) to watch")
    for item in items:
        mode = "searched on every site" if item.is_search else item.url
        print(f"  {item.name:<34} target {item.target_price}   {mode}")
    for problem in problems:
        print(f"  ⚠️ {problem}")
    if args.test_deal:
        print("A test row was written to Deals: look at it on your phone, then delete it.")


if __name__ == "__main__":
    main()
