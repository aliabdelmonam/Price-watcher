"""Noon (noon.com/egypt-en, egypt-ar): a Next.js site.

The product JSON (price, sale_price, stock, seller) is embedded in the page, usually in the
__NEXT_DATA__ script. Layers, first hit wins:
  1. __NEXT_DATA__ -> the product object -> its offers
  2. any inline "offers":[...] JSON (covers the newer App Router payload too)
  3. JSON-LD Product
  4. CSS on data-qa price elements
When several sellers offer the product, the cheapest buyable offer is used and its seller is
shown in the alert (an only_seller column in the Watchlist insists on one seller).
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from ..models import Reading
from ..prices import parse_price
from . import common

STORE = "noon"
DOMAINS = ("noon.com",)
CURRENCY = "EGP"

_SKU = re.compile(r"/([A-Z0-9]{8,})/p/?$", re.I)
_OUT_WORDS = ("out of stock", "sold out", "currently unavailable", "غير متوفر", "نفدت الكمية")
PRICE_NOW = ('[data-qa="div-price-now"]', '[data-qa="pdp-price-now"]', ".priceNow", '[class*="priceNow"]')
PRICE_WAS = ('[data-qa="div-price-was"]', '[data-qa="pdp-price-was"]', ".priceWas", '[class*="priceWas"]',
             '[class*="oldPrice"]')


def sku_of(url: str) -> str | None:
    match = _SKU.search(urlsplit(url).path)
    return match.group(1).upper() if match else None


def canonical_url(url: str) -> str:
    return common.clean_url(url, keep=())


def fetch_url(url: str) -> str:
    return canonical_url(url)


def product_id(url: str) -> str | None:
    sku = sku_of(url)
    if not sku:
        return None
    return f"noon-{sku.lower()}"


def name_from_url(url: str) -> str:
    return common.slug_name(url)


# --------------------------------------------------------------------- offers

def _is_offer(d: dict) -> bool:
    return isinstance(d.get("price"), (int, float, str)) and ("sale_price" in d or "offer_code" in d)


def _offers_of(product: dict) -> list[dict]:
    offers = [o for o in product.get("offers") or [] if isinstance(o, dict)]
    for variant in product.get("variants") or []:
        if isinstance(variant, dict):
            offers += [o for o in variant.get("offers") or [] if isinstance(o, dict)]
    return [o for o in offers if _is_offer(o)]


def _find_product(data, want_sku: str | None) -> dict | None:
    fallback = None
    for d in common.iter_dicts(data):
        if not (isinstance(d.get("offers"), list) or isinstance(d.get("variants"), list)):
            continue
        if not _offers_of(d):
            continue
        sku = str(d.get("sku") or d.get("sku_config") or "").upper()
        if want_sku and sku and (sku.startswith(want_sku) or want_sku.startswith(sku)):
            return d
        if fallback is None and (not want_sku or not sku) and ("product_title" in d or "brand" in d):
            fallback = d
    return fallback


def _pick_offer(offers: list[dict], want_offer: str, want_sku: str | None) -> dict | None:
    if not offers:
        return None
    if want_offer:
        for offer in offers:
            if str(offer.get("offer_code") or "") == want_offer:
                return offer
    if want_sku:
        with_sku = [o for o in offers if o.get("sku")]
        same = [o for o in with_sku if str(o["sku"]).upper().startswith(want_sku)]
        if with_sku and not same:
            return None  # these offers belong to other products (recommendations)
        offers = same or offers
    buyable = [o for o in offers if _offer_in_stock(o) is not False]
    return min(buyable or offers, key=_offer_price)


def _offer_in_stock(offer: dict) -> bool | None:
    if isinstance(offer.get("is_buyable"), bool):
        return offer["is_buyable"]
    stock = offer.get("stock")
    if isinstance(stock, (int, float)):
        return stock > 0
    if isinstance(stock, dict) and isinstance(stock.get("max_to_buy"), (int, float)):
        return stock["max_to_buy"] > 0
    return None


def _offer_price(offer: dict) -> float:
    return parse_price(offer.get("sale_price")) or parse_price(offer.get("price")) or float("inf")


def _found_from_offer(offer: dict, title: str, source: str) -> common.Found:
    sale = parse_price(offer.get("sale_price"))
    regular = parse_price(offer.get("price"))
    price = sale or regular
    stock = _offer_in_stock(offer)
    seller = offer.get("store_name") or offer.get("partner_name") or offer.get("seller_name")
    if isinstance(offer.get("store"), dict):
        seller = seller or offer["store"].get("name")
    return common.Found(
        price=price,
        list_price=regular if sale and regular and regular > sale else None,
        currency=str(offer.get("currency") or CURRENCY),
        in_stock=True if stock is None else stock,
        seller=str(seller)[:80] if seller else None,
        title=title,
        source=source,
    )


def _from_next_data(soup, want_offer, want_sku) -> common.Found | None:
    data = common.script_json(soup, "__NEXT_DATA__")
    if not data:
        return None
    product = _find_product(data, want_sku)
    if not product:
        return None
    offer = _pick_offer(_offers_of(product), want_offer, want_sku)
    if not offer:
        return None
    title = str(product.get("product_title") or product.get("name") or "")
    return _found_from_offer(offer, title, "next_data")


def _from_inline_json(html: str, want_offer, want_sku) -> common.Found | None:
    for blob, source in ((html, "inline_json"), (common.rsc_payload(html), "rsc_payload")):
        if not blob or '"offers"' not in blob:
            continue
        offers: list[dict] = []
        for value in common.json_values_after_key(blob, "offers"):
            if isinstance(value, list):
                offers += [o for o in value if isinstance(o, dict) and _is_offer(o)]
        offer = _pick_offer(offers, want_offer, want_sku)
        if offer:
            titles = [t for t in common.json_values_after_key(blob, "product_title", 3) if isinstance(t, str)]
            return _found_from_offer(offer, titles[0] if titles else "", source)
    return None


def _from_css(soup) -> common.Found | None:
    price = list_price = None
    for selector in PRICE_NOW:
        el = soup.select_one(selector)
        price = parse_price(common.text_of(el)) if el is not None else None
        if price:
            break
    if not price:
        return None
    for selector in PRICE_WAS:
        el = soup.select_one(selector)
        list_price = parse_price(common.text_of(el)) if el is not None else None
        if list_price:
            break
    body = common.text_of(soup.find("main") or soup.body).lower()
    in_stock = not any(word in body for word in _OUT_WORDS)
    return common.Found(price=price, list_price=list_price if list_price and list_price > price else None,
                        currency=CURRENCY, in_stock=in_stock, title=common.page_title(soup), source="css")


def parse(html: str, url: str, status: int | None = 200) -> Reading:
    blocked = common.detect_block(status, html)
    if blocked:
        return Reading.failed(blocked, status)
    problem = common.http_problem(status)
    if problem:
        return Reading.failed(problem, status)
    soup = common.make_soup(html)
    want_offer = common.query_param(url, "o")
    want_sku = sku_of(url)
    found = (
        _from_next_data(soup, want_offer, want_sku)
        or _from_inline_json(html, want_offer, want_sku)
        or common.product_from_jsonld(common.jsonld_nodes(soup), url)
        or _from_css(soup)
    )
    if found is None:
        return Reading.failed("price not found (page layout changed?)", status, common.page_title(soup))
    if not found.title:
        found.title = common.meta_content(soup, "og:title") or common.page_title(soup)
    return common.reading_from_found(found, CURRENCY, status)
