#!/usr/bin/env python3
"""Run the watcher on your laptop. Same code as the Space.

  python run_local.py                      run forever (Ctrl+C stops cleanly and syncs)
  python run_local.py --once               one real cycle (alerts, Notion, history), print a table;
                                           safe to schedule every 5 minutes: nothing is ever repeated
  python run_local.py --dry-run            read every item and print it; no alerts, nothing saved
  python run_local.py --search "soundcore r60i nc"   search every site and show what would be picked
  python run_local.py --search "..." --site noon --exclude case --min-price 500
  python run_local.py --test               one product per store: worked / captcha / blocked
  python run_local.py --url <link>         read one product page and show what was found
  python run_local.py --url page.html --store amazon_eg    parse a page you saved (Ctrl+S)
  python run_local.py --save-fixture <link>  save the page into tests/fixtures for adapter tests
  python run_local.py --stores amazon_eg   only these stores (comma-separated)
  python run_local.py --exclude-stores noon
"""
from __future__ import annotations

import argparse
import json
import re
import signal
import sys
import threading

from watcher.adapters import get_adapter, store_for_url
from watcher.config import ROOT, load_config
from watcher.core import check_all
from watcher.fetch import Fetcher
from watcher.crawler import wants
from watcher.loop import Watcher, matcher_for, quick_items, read_url, setup_logging, smoke_test
from watcher.models import Item
from watcher.notify import short_title
from watcher.prices import money
from watcher.search import pick, search_site
from watcher.sites import load_sites


def table(rows: list[tuple]) -> None:
    header = ("item", "store", "price", "was", "stock", "seller", "result")
    data = [header] + [tuple(str(c) for c in r) for r in rows]
    widths = [min(max(len(r[i]) for r in data), 34) for i in range(len(header))]
    for n, row in enumerate(data):
        print("  ".join(cell[:widths[i]].ljust(widths[i]) for i, cell in enumerate(row)))
        if n == 0:
            print("  ".join("-" * w for w in widths))


def reading_row(name: str, store: str, r) -> tuple:
    stock = ("in stock" if r.in_stock else "OUT") if r.ok else ""
    result = f"ok ({r.source})" if r.ok else f"FAIL: {r.error}"
    return (name, store, money(r.price), money(r.list_price) if r.list_price else "", stock, r.seller or "", result)


def cmd_url(cfg, url: str, store: str | None) -> None:
    store = store or store_for_url(url)
    reading, seconds = read_url(Fetcher(cfg.settings, cfg.user_agent), url, store)
    print(f"store: {store}   fetched in {seconds:.1f}s   HTTP {reading.http_status}")
    print(json.dumps(reading.__dict__, ensure_ascii=False, indent=2))


def cmd_search(cfg, args) -> None:
    item = Item(id="cli", name=args.search, url="", store="search", must_include=args.must or "",
                exclude=args.exclude or "", min_price=args.min_price, max_price=args.max_price)
    sites, problems = load_sites(cfg.sites)
    for problem in problems:
        print(f"watchlist.json sites: {problem}")
    fetcher = Fetcher(cfg.settings, cfg.user_agent)
    matcher = matcher_for(cfg, item)
    for site in sites:
        if args.site and args.site.lower() not in site.key and args.site.lower() not in site.host:
            continue
        result = search_site(fetcher, site, item.query)
        print(f"\n== {site.key}   {result.url}")
        if not result.ok:
            print(f"   FAILED: {result.error}")
            continue
        best, verdicts = pick(result.candidates, matcher, item.min_price, item.max_price,
                              cfg.settings.accessory_price_ratio)
        matches = sum(v in ("match", "picked") for _, v in verdicts)
        print(f"   {len(result.candidates)} products on the page, {matches} match")
        for c, why in verdicts[:20]:
            mark = "->" if why == "picked" else (" +" if why == "match" else " -")
            was = f" (was {money(c.list_price)})" if c.list_price else ""
            print(f"   {mark} {money(c.price):>10} {c.currency:<3}{was:<16} {short_title(c.title, 64):<64}  [{why}]")
        print(f"   picked: {best.url}" if best else "   picked: nothing (not listed here)")


def cmd_save_fixture(cfg, url: str) -> None:
    store = store_for_url(url)
    adapter = get_adapter(store)
    result = Fetcher(cfg.settings, cfg.user_agent).get(store, adapter.fetch_url(url))
    if not result.text:
        sys.exit(f"could not fetch: {result.error}")
    reading = adapter.parse(result.text, url, result.status)
    pid = adapter.product_id(url) or re.sub(r"[^a-z0-9]+", "-", url.lower())[-40:]
    folder = ROOT / "tests" / "fixtures"
    path = folder / f"{store}_{pid}.html"
    path.write_text(result.text, encoding="utf-8")
    expected_path = folder / "expected.json"
    expected = json.loads(expected_path.read_text(encoding="utf-8")) if expected_path.exists() else {}
    expected[path.name] = {"store": store, "url": url, "ok": reading.ok, "price": reading.price,
                           "list_price": reading.list_price, "in_stock": reading.in_stock}
    expected_path.write_text(json.dumps(expected, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"saved {path.relative_to(ROOT)} (HTTP {result.status})")
    print(json.dumps(expected[path.name], indent=2, ensure_ascii=False))
    print("Compare with the real page; if anything is wrong, fix tests/fixtures/expected.json by hand.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--once", action="store_true", help="one real cycle, then exit")
    parser.add_argument("--dry-run", action="store_true", help="read and print only; no alerts, nothing saved")
    parser.add_argument("--test", action="store_true", help="one product per store: worked / blocked")
    parser.add_argument("--url", help="read one product link (or a saved .html file) and print it")
    parser.add_argument("--store", help="adapter to use with --url for a saved file (amazon_eg, noon, jumia)")
    parser.add_argument("--save-fixture", metavar="URL", help="save a product page as a test fixture")
    parser.add_argument("--search", metavar="NAME", help="search every site for NAME and show the matches")
    parser.add_argument("--site", help="with --search: only this site (noon, amazon_eg, ...)")
    parser.add_argument("--must", help="with --search: words a listing title must contain")
    parser.add_argument("--exclude", help="with --search: words that rule a listing out")
    parser.add_argument("--min-price", type=float, help="with --search: ignore cheaper listings")
    parser.add_argument("--max-price", type=float, help="with --search: ignore dearer listings")
    parser.add_argument("--stores", help="only check these stores (comma-separated)")
    parser.add_argument("--exclude-stores", help="skip these stores (comma-separated)")
    args = parser.parse_args()

    setup_logging()
    cfg = load_config(stores=args.stores, exclude_stores=args.exclude_stores)

    if args.search:
        return cmd_search(cfg, args)
    if args.url:
        return cmd_url(cfg, args.url, args.store)
    if args.save_fixture:
        return cmd_save_fixture(cfg, args.save_fixture)
    if args.test:
        report = smoke_test(cfg)
        for row in report:
            print(f"{row['store']:<12} {row['verdict']:<16} HTTP {row['http_status']}  price {row['price']}  "
                  f"{row['error'] or ''}")
        if not report:
            print("No items in the watchlist yet.")
        return
    if args.dry_run:
        fetcher = Fetcher(cfg.settings, cfg.user_agent)
        rows = []
        sites = load_sites(cfg.sites)[0]
        for item in quick_items(cfg):
            if item.url:
                reading, _ = read_url(fetcher, item.url, item.store)
                rows.append(reading_row(item.name, item.store, reading))
                continue
            for site in sites:
                if not (site.enabled and wants(item, site) and cfg.handles_store(site.key)):
                    continue
                result = search_site(fetcher, site, item.query)
                best = pick(result.candidates, matcher_for(cfg, item), item.min_price, item.max_price)[0] \
                    if result.ok else None
                if best is None:
                    note = "not listed" if result.ok else f"FAIL: {result.error}"
                    rows.append((item.name, site.key, "", "", "", "", note))
                else:
                    stock = "in stock" if best.in_stock is not False else "OUT"
                    rows.append((item.name, site.key, money(best.price),
                                 money(best.list_price) if best.list_price else "", stock, "",
                                 f"match: {short_title(best.title, 40)}"))
        table(rows)
        return

    watcher = Watcher(cfg)
    if args.once:
        watcher.rt.start(announce=False, quick=True)
        try:
            results = check_all(watcher.rt, force=True)
        finally:
            watcher.rt.shutdown(quick=True)
        table([reading_row(item.name, item.store, r) for item, r in results])
        return

    def stop(*_):
        print("\nstopping (saving and syncing)…")
        watcher.stop_event.set()
        watcher.rt.stop_requested = True

    signal.signal(signal.SIGINT, stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop)
    thread = threading.Thread(target=watcher.run, name="watcher-loop")
    thread.start()
    while thread.is_alive():
        thread.join(0.5)


if __name__ == "__main__":
    main()
