"""Jumia Egypt (jumia.com.eg).

Sources, combined field by field:
  - window.__STORE__ page data (rawPrice / rawOldPrice / isBuyable / seller), when present
  - the visible price block right after the <h1> (the big price span and the struck-out one)
  - JSON-LD Product (price, availability)
The visible price is preferred over JSON-LD because flash-sale prices show there first.
"""
from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

from ..models import Reading
from ..prices import parse_price
from . import common

STORE = "jumia"
DOMAINS = ("jumia.com.eg",)
CURRENCY = "EGP"

_ID = re.compile(r"-(\d{5,})\.html$")
_STORE_JSON = re.compile(r"window\.__(?:STORE|INITIAL_STATE)__\s*=\s*", re.S)
_OUT_WORDS = ("out of stock", "sold out", "نفدت الكمية", "غير متوفر", "نفذت الكمية")
_IN_WORDS = ("in stock", "few units left", "units left", "متوفر", "متبقي")


def canonical_url(url: str) -> str:
    return common.clean_url(url, keep=())


def fetch_url(url: str) -> str:
    return canonical_url(url)


def product_id(url: str) -> str | None:
    match = _ID.search(urlsplit(url).path)
    return f"jumia-{match.group(1)}" if match else None


def name_from_url(url: str) -> str:
    return common.slug_name(url)


def _from_store(html: str) -> common.Found | None:
    match = _STORE_JSON.search(html)
    if not match:
        return None
    try:
        data, _ = json.JSONDecoder(strict=False).raw_decode(html, match.end())
    except ValueError:
        return None
    product = data.get("product") if isinstance(data, dict) and isinstance(data.get("product"), dict) else None
    if product is None or not isinstance(product.get("prices"), dict):
        product = next((d for d in common.iter_dicts(data)
                        if isinstance(d.get("prices"), dict) and "rawPrice" in d["prices"]
                        and ("simples" in d or "isBuyable" in d)), None)
    if product is None:
        return None
    prices = product.get("prices") or {}
    price = parse_price(prices.get("rawPrice") or prices.get("price"))
    old = parse_price(prices.get("rawOldPrice") or prices.get("oldPrice"))
    seller = product.get("seller") or product.get("sellerEntity") or {}
    in_stock = product.get("isBuyable")
    if not isinstance(in_stock, bool):
        simples = [s for s in product.get("simples") or [] if isinstance(s, dict)]
        flags = [s.get("isBuyable") for s in simples if isinstance(s.get("isBuyable"), bool)]
        in_stock = any(flags) if flags else None
    return common.Found(
        price=price,
        list_price=old if old and price and old > price else None,
        currency=CURRENCY,
        in_stock=in_stock,
        seller=(seller.get("name") if isinstance(seller, dict) else str(seller or "")) or None,
        title=str(product.get("name") or ""),
        source="page_store",
    )


def _from_css(soup) -> common.Found:
    found = common.Found(currency=CURRENCY, source="css")
    h1 = soup.find("h1")
    found.title = common.text_of(h1)
    start = h1 or soup
    price_el = start.find_next("span", class_="-fs24") if h1 else soup.select_one("span.-fs24")
    if price_el is not None:
        found.price = parse_price(common.text_of(price_el))
        box = price_el.parent.parent if price_el.parent is not None and price_el.parent.parent is not None else None
        old_el = box.find("span", class_="-lthr") if box is not None else None
        old = parse_price(common.text_of(old_el)) if old_el is not None else None
        if old and found.price and old > found.price:
            found.list_price = old
    for heading in soup.find_all(["h2", "h3", "p"]):
        label = common.text_of(heading).lower()
        if label in ("seller information", "معلومات البائع"):
            nxt = heading.find_next("p")
            if nxt is not None and common.text_of(nxt):
                found.seller = common.text_of(nxt)[:80]
            break
    for el in soup.select("main p, main span, main button, p, button"):
        label = common.text_of(el).lower()
        if not label or len(label) > 40:
            continue
        if any(word == label or label.startswith(word) for word in _OUT_WORDS):
            found.in_stock = False
            break
        if any(word == label or label.startswith(word) for word in _IN_WORDS):
            found.in_stock = True
            break
    return found


def parse(html: str, url: str, status: int | None = 200) -> Reading:
    blocked = common.detect_block(status, html)
    if blocked:
        return Reading.failed(blocked, status)
    problem = common.http_problem(status)
    if problem:
        return Reading.failed(problem, status)
    soup = common.make_soup(html)
    store = _from_store(html)
    css = _from_css(soup)
    ld = common.product_from_jsonld(common.jsonld_nodes(soup), url)
    layers = [x for x in (store, css, ld) if x is not None]
    pick = lambda attr: next((getattr(x, attr) for x in layers if getattr(x, attr) not in (None, "")), None)  # noqa: E731
    price_layer = next((x for x in layers if x.price is not None), None)
    found = common.Found(
        price=price_layer.price if price_layer else None,
        list_price=pick("list_price"),
        currency=CURRENCY,
        in_stock=next((x.in_stock for x in (ld, store, css) if x is not None and x.in_stock is not None), None),
        seller=pick("seller"),
        title=pick("title") or common.page_title(soup),
        source=price_layer.source if price_layer else "",
    )
    if found.list_price and found.price and found.list_price <= found.price:
        found.list_price = None
    if found.price is None and found.in_stock is not False:
        return Reading.failed("price not found (page layout changed?)", status, found.title)
    return common.reading_from_found(found, CURRENCY, status)
