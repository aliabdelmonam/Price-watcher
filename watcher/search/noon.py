"""Noon search results (noon.com/egypt-en/search/?q=...): the product list embedded in the page."""
from __future__ import annotations

from urllib.parse import quote, urljoin

from ..adapters import common as page
from ..prices import detect_currency, parse_price
from .common import Candidate, SearchResult, generic_cards, no_results_page, rsc_objects

PRICE_KEYS = ("sale_price", "price", "offer_price")


def search_url(site, query: str) -> str:
    return f"{site.url}search/?q={quote(query)}"


def _is_hit(d: dict) -> bool:
    sku, name = d.get("sku"), d.get("name") or d.get("product_title")
    if not (isinstance(sku, str) and sku.strip() and isinstance(name, str) and name.strip()):
        return False
    return any(parse_price(d.get(k)) for k in PRICE_KEYS if not isinstance(d.get(k), (dict, list)))


def _link(site, hit: dict) -> str:
    slug = str(hit.get("url") or "").strip()
    if slug.startswith("http"):
        return slug
    if slug.startswith("/") and "/p" in slug:
        return urljoin(site.root + "/", slug)
    return f"{site.url}{slug.strip('/') or 'product'}/{hit['sku']}/p/"


def _candidate(site, hit: dict) -> Candidate:
    sale = parse_price(hit.get("sale_price"))
    regular = parse_price(hit.get("price")) or parse_price(hit.get("offer_price"))
    price = sale or regular
    name = " ".join(str(hit.get("name") or hit.get("product_title")).split())
    brand = str(hit.get("brand") or "").strip()
    stock = hit.get("is_buyable")
    return Candidate(title=name if not brand or brand.lower() in name.lower() else f"{brand} {name}",
                     url=_link(site, hit), price=price,
                     list_price=regular if sale and regular and regular > sale else None,
                     currency=site.currency,
                     in_stock=stock if isinstance(stock, bool) else (True if price else None))


def parse(html: str, site, status: int | None = 200, url: str = "") -> SearchResult:
    blocked = page.detect_block(status, html)
    if blocked:
        return SearchResult.failed(blocked, status, url, blocked=True)
    problem = page.http_problem(status)
    if problem:
        return SearchResult.failed(problem, status, url)
    soup = page.make_soup(html)
    # Current Noon product cards are rendered client-side; use the same public card fields
    # exposed in the user's working Playwright reference when embedded JSON is unavailable.
    cards = soup.select('[data-qa="plp-product-box"]')
    card_hits: dict[str, Candidate] = {}
    for card in cards:
        name = page.text_of(card.select_one('[data-qa="plp-product-box-name"]'))
        price_text = page.text_of(card.select_one('[data-qa="plp-product-box-price"] strong'))
        if not price_text:
            price_text = page.text_of(card.select_one('[data-qa="plp-product-box-price"]'))
        price = parse_price(price_text)
        link_el = card.select_one("a[href]")
        href = (link_el.get("href") or "").strip() if link_el else ""
        if not name or price is None or not href:
            continue
        old = parse_price(page.text_of(card.select_one('[class*="_oldPrice_"]')))
        link = page.clean_url(urljoin(site.root + "/", href), keep=())
        card_hits.setdefault(link, Candidate(name, link, price,
                                             old if old and old > price else None,
                                             detect_currency(price_text, site.currency), True))
    if card_hits:
        return SearchResult(ok=True, candidates=list(card_hits.values()), status=status, url=url)
    payload = page.rsc_payload(html)
    hits: dict[str, dict] = {}
    for blob in (page.script_json(soup, "__NEXT_DATA__"), *rsc_objects(payload)):
        for d in page.iter_dicts(blob):
            if _is_hit(d):
                hits.setdefault(d["sku"], d)
    if not hits:
        for text in (html, payload):
            for value in page.json_values_after_key(text or "", "hits"):
                for d in value if isinstance(value, list) else []:
                    if isinstance(d, dict) and _is_hit(d):
                        hits.setdefault(d["sku"], d)
    out = [_candidate(site, hit) for hit in hits.values()]
    if not out:
        out = [c for c in generic_cards(soup, url or site.url, site.currency) if "/p/" in c.url]
    if out:
        return SearchResult(ok=True, candidates=out, status=status, url=url)
    if no_results_page(html):
        return SearchResult(ok=True, status=status, url=url)
    return SearchResult.failed("no products on the results page (blocked or layout changed?)", status, url)
