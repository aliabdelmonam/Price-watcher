"""Jumia search results (jumia.com.eg/catalog/?q=...): the article.prd product cards."""
from __future__ import annotations

from urllib.parse import quote_plus, urljoin

from ..adapters import common as page
from ..prices import detect_currency, parse_price
from .common import Candidate, SearchResult, generic_cards, no_results_page

OUT_WORDS = ("out of stock", "sold out", "نفدت", "غير متوفر")


def search_url(site, query: str) -> str:
    return f"{site.root}/catalog/?q={quote_plus(query)}"


def parse(html: str, site, status: int | None = 200, url: str = "") -> SearchResult:
    blocked = page.detect_block(status, html)
    if blocked:
        return SearchResult.failed(blocked, status, url, blocked=True)
    problem = page.http_problem(status)
    if problem:
        return SearchResult.failed(problem, status, url)
    soup = page.make_soup(html)
    out = []
    for card in soup.select("article.prd"):
        a = card.select_one("a.core") or card.select_one("a[href]")
        if a is None or not a.get("href"):
            continue
        title = page.text_of(card.select_one(".name")) or str(
            a.get("data-gtm-name") or a.get("data-ga4-item_name") or a.get("data-name") or "")
        brand = str(a.get("data-gtm-brand") or a.get("data-ga4-item_brand") or "").strip()
        if brand and brand.lower() not in title.lower():
            title = f"{brand} {title}"
        price_text = page.text_of(card.select_one(".prc"))
        price = parse_price(price_text)
        old = parse_price(page.text_of(card.select_one(".old")))
        low = page.text_of(card).lower()
        out.append(Candidate(title=title.strip(), url=page.clean_url(urljoin(site.root + "/", a["href"]), keep=()),
                             price=price, list_price=old if old and price and old > price else None,
                             currency=detect_currency(price_text, site.currency),
                             in_stock=False if any(w in low for w in OUT_WORDS) else (True if price else None)))
    if not out:
        out = generic_cards(soup, url or site.url, site.currency)
    if out:
        return SearchResult(ok=True, candidates=out, status=status, url=url)
    if no_results_page(html):
        return SearchResult(ok=True, status=status, url=url)
    return SearchResult.failed("no products on the results page (blocked or layout changed?)", status, url)
