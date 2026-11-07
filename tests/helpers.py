"""Fakes to test the watcher without network, Telegram or Notion."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from watcher.config import Config, Settings
from watcher.core import check_all
from watcher.fetch import FetchResult
from watcher.runtime import Runtime

CAIRO = ZoneInfo("Africa/Cairo")
CAPTCHA = (200, '<html><form action="/errors/validateCaptcha"></form></html>')
FIVE_MIN = 300
SITES = ["https://www.noon.com/egypt-en/", "https://www.amazon.eg/", "https://www.jumia.com.eg/"]
R60 = "Soundcore R60i NC True Wireless Earbuds with Adaptive ANC, Hi-Res LDAC, 50H Playtime, Black"
NOON_Q, AMZ_Q, JUM_Q = "noon.com/egypt-en/search/?q=", "amazon.eg/-/en/s?k=", "jumia.com.eg/catalog/?q="


def cairo_ts(y, mo, d, h, mi=0) -> float:
    return datetime(y, mo, d, h, mi, tzinfo=CAIRO).timestamp()


class Clock:
    def __init__(self, t: float):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class FakeFetcher:
    def __init__(self, pages: dict):
        self.pages = pages
        self.calls: list[str] = []

    def get(self, store, url):
        self.calls.append(url)
        for key, page in self.pages.items():
            if key in url:
                status, html = page if isinstance(page, tuple) else (200, page)
                return FetchResult(status, html, url)
        return FetchResult(None, "", url, "network error: no fake page")

    def resolve(self, url):
        return url

    def close(self):
        pass


class FakeTelegram:
    enabled = True
    chat_id = "1"
    last_error = None

    def __init__(self):
        self.sent: list[str] = []

    def send(self, text, link=None):
        self.sent.append(text)
        return True

    def get_updates(self, offset, timeout=0):
        return []

    def texts(self, word):
        return [t for t in self.sent if word in t]


class FakeNotion:
    """Your Notion Watchlist (`rows`) and Deals database (`deals`: page id -> last record written)."""

    enabled = True
    last_error = None

    def __init__(self, rows):
        self.rows = rows if rows is not None else []
        self.deals: dict[str, dict] = {}
        self.writes = 0
        self.latest: dict[str, str] = {}
        self.latest_calls = 0
        self.fail = False
        self.site_keys: list[str] = []

    def _check(self):
        if self.fail:
            raise RuntimeError("Notion unavailable")

    def connect(self):
        self._check()

    def read_watchlist(self):
        self._check()
        return [dict(r, _row=n, _page=f"wl-{n}") for n, r in enumerate(self.rows, start=1)]

    def add_watchlist_item(self, data):
        self._check()
        self.rows.append({k: data.get(k, "") for k in ("name", "url", "target_price", "notes")})
        return f"wl-{len(self.rows)}"

    def find_deal(self, key):
        self._check()
        return next((pid for pid, rec in self.deals.items() if rec["key"] == key and not rec.get("_deleted")), None)

    def create_deal(self, rec):
        self._check()
        pid = f"deal-{len(self.deals) + 1}"
        self.deals[pid] = dict(rec)
        self.writes += 1
        return pid

    def update_deal(self, pid, rec):
        self._check()
        if pid not in self.deals or self.deals[pid].get("_deleted"):
            return False
        self.deals[pid] = dict(rec)
        self.writes += 1
        return True

    def set_latest(self, page_id, text):
        self._check()
        self.latest[page_id] = text
        self.latest_calls += 1

    def rows_for(self, key=None):
        return [r for r in self.deals.values() if not r.get("_deleted") and (key is None or r["key"] == key)]


def make_cfg(data_dir, items=None, sites=None, **settings) -> Config:
    return Config(
        telegram_token="t", telegram_chat_id="1", notion_token="", notion_page="",
        hf_token="", hf_dataset="", running_on="laptop", data_dir=Path(data_dir),
        watchlist_file=Path(data_dir) / "watchlist.json", watchlist_items=items or [], sites=sites or [],
        stores=set(), exclude_stores=set(), bot_commands=True, loop_enabled=True, user_agent="",
        settings=Settings.from_dict({"jitter_seconds": 0, **settings}),
    )


def row(item_id, url, target=None, **extra) -> dict:
    data = {"id": item_id, "name": item_id.upper(), "url": url,
            "target_price": "" if target is None else target, "priority": "normal", "enabled": True, "notes": ""}
    data.update(extra)
    return data


def amazon_url(asin: str) -> str:
    return f"https://www.amazon.eg/dp/{asin}"


def amazon_page(price=None, was=None, stock="In Stock", seller="Amazon.eg", title="Test Product") -> str:
    parts = [f'<span id="productTitle">{title}</span><div id="corePriceDisplay_desktop_feature_div">']
    if price is not None:
        parts.append(f'<span class="a-price priceToPay"><span class="a-offscreen">EGP{price:,.2f}</span></span>')
    if was is not None:
        parts.append('<span class="basisPrice"><span class="a-price a-text-price" data-a-strike="true">'
                     f'<span class="a-offscreen">EGP{was:,.2f}</span></span></span>')
    parts.append(f'</div><div id="availability"><span>{stock}</span></div>')
    parts.append('<div offer-display-feature-name="desktop-merchant-info">'
                 f'<span class="offer-display-feature-text-message">{seller}</span></div>')
    return "<html><body>" + "".join(parts) + "</body></html>"


def noon_results(*hits):
    data = {"props": {"pageProps": {"catalog": {"hits": [
        {"sku": sku, "name": name, "brand": "soundcore", "url": "soundcore-item", "price": price,
         "sale_price": sale, "is_buyable": True} for sku, name, price, sale in hits]}}}}
    return f'<html><body><script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script></body></html>'


def amazon_results(*cards):
    out = []
    for asin, title, price, was in cards:
        price_html = f'<span class="a-price"><span class="a-offscreen">EGP{price:,.2f}</span></span>' if price else ""
        was_html = (f'<span class="a-price a-text-price" data-a-strike="true"><span class="a-offscreen">'
                    f'EGP{was:,.2f}</span></span>') if was else ""
        out.append(f'<div data-component-type="s-search-result" data-asin="{asin}"><div data-cy="title-recipe">'
                   f'<a class="a-link-normal" href="/dp/{asin}"><h2 aria-label="{title}"><span>{title}</span></h2>'
                   f'</a></div><div data-cy="price-recipe">{price_html}{was_html}</div></div>')
    return "<html><body>" + "".join(out) + "</body></html>"


def jumia_results(*cards):
    out = []
    for name, price, old, href in cards:
        old_html = f'<div class="old">EGP {old:,.2f}</div>' if old else ""
        out.append(f'<article class="prd _fb col c-prd"><a class="core" href="{href}" data-gtm-brand="Anker">'
                   f'<div class="info"><h3 class="name">{name}</h3><div class="prc">EGP {price:,.2f}</div>'
                   f'<div class="s-prc-w">{old_html}</div></div></a></article>')
    return "<html><body><main>" + "".join(out) + "</main></body></html>"


class Harness:
    """A Runtime wired to fakes, with a clock you move by hand."""

    def __init__(self, tmp_path, rows, pages, start=None, sites=None, notion=None, boot=True, **settings):
        self.clock = Clock(start or cairo_ts(2026, 11, 6, 15))
        self.tg = FakeTelegram()
        self.notion = notion or FakeNotion(rows)
        self.fetcher = FakeFetcher(pages)
        self.data_dir = Path(tmp_path) / "data"
        self.settings = settings
        self.sites = sites
        self.rt = self.new_runtime() if boot else None

    def _runtime(self) -> Runtime:
        return Runtime(make_cfg(self.data_dir, sites=self.sites, **self.settings), fetcher=self.fetcher,
                       telegram=self.tg, notion=self.notion, clock=self.clock)

    def new_runtime(self) -> Runtime:
        rt = self._runtime()
        rt.start()
        self.tg.sent.clear()
        return rt

    def cycle(self, advance=200, force=False):
        self.clock.advance(advance)
        self.rt.force_reload = True
        return check_all(self.rt, force=force)

    def once_run(self, advance=FIVE_MIN):
        """What `python run_local.py --once` does, as if a scheduler started it."""
        self.clock.advance(advance)
        rt = self._runtime()
        rt.start(announce=False, quick=True)
        try:
            check_all(rt, force=True)
        finally:
            rt.shutdown(quick=True)
        self.rt = rt

    def calls(self, key) -> int:
        return sum(key in url for url in self.fetcher.calls)
