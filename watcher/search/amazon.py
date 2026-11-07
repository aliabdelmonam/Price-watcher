"""Amazon search results (amazon.eg, amazon.com, ...): the s-search-result product cards."""
from __future__ import annotations

from urllib.parse import quote_plus

from ..adapters import amazon_eg as product_page
from ..adapters import common as page
from ..prices import detect_currency, parse_price
from .common import Candidate, SearchResult, no_results_page

ENGLISH_PREFIX = ("amazon.eg", "amazon.ae", "amazon.sa")


def search_url(site, query: str) -> str:
    # Amazon Egypt's working Playwright flow uses /s?k=...; keep its exact route.
    path = "/s" if site.host == "amazon.eg" else ("/-/en/s" if site.host.endswith(ENGLISH_PREFIX) else "/s")
    return f"{site.root}{path}?k={quote_plus(query)}"


def _title(card) -> str:
    parts: list[str] = []
    for h2 in card.select("h2"):  # newer cards show the brand and the title in separate headings
        text = (h2.get("aria-label") or page.text_of(h2)).strip()
        if text and text not in parts:
            parts.append(text)
    return " ".join(parts)


def parse(html: str, site, status: int | None = 200, url: str = "") -> SearchResult:
    blocked = product_page.block_reason(html or "") or page.detect_block(status, html)
    if blocked:
        return SearchResult.failed(blocked, status, url, blocked=True)
    problem = page.http_problem(status)
    if problem:
        return SearchResult.failed(problem, status, url)
    soup = page.make_soup(html)
    cards = soup.select('div[data-component-type="s-search-result"]') or soup.select("div.s-result-item[data-asin]")
    out = []
    for card in cards:
        asin = (card.get("data-asin") or "").strip()
        title = _title(card)
        if not asin or not title:
            continue
        price_text = page.text_of(card.select_one(".a-price:not(.a-text-price) .a-offscreen"))
        price = parse_price(price_text)
        was_el = (card.select_one('.a-price[data-a-strike="true"] .a-offscreen')
                  or card.select_one(".a-price.a-text-price .a-offscreen"))
        was = parse_price(page.text_of(was_el)) if was_el is not None else None
        out.append(Candidate(title=title, url=f"{site.root}/dp/{asin}", price=price,
                             list_price=was if was and price and was > price else None,
                             currency=detect_currency(price_text, site.currency),
                             in_stock=True if price else None))
    if out:
        return SearchResult(ok=True, candidates=out, status=status, url=url)
    if no_results_page(html):
        return SearchResult(ok=True, status=status, url=url)
    return SearchResult.failed("no products on the results page (blocked or layout changed?)", status, url)
