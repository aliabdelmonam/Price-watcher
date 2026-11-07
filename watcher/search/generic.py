"""Any other site: its Shopify or WooCommerce search API if it has one, else its search page.

The method that worked is remembered per site. A site where nothing works is retried after
30 minutes instead of on every check, so a bad entry in "sites" doesn't hammer anyone.
"""
from __future__ import annotations

import html as html_lib
import json
import time
from urllib.parse import quote, quote_plus, urljoin

from ..adapters import common as page
from ..prices import parse_price
from .common import Candidate, SearchResult, generic_cards, jsonld_candidates, no_results_page

SEARCH_PAGES = ("search?q={q}", "?s={q}&post_type=product", "catalogsearch/result/?q={q}", "search?query={q}")
RETRY_DEAD_AFTER = 1800
_working: dict[str, str] = {}
_dead: dict[str, tuple[float, SearchResult]] = {}


def _get(fetcher, site, url):
    res = fetcher.get(site.key, url)
    if res.error and not res.text:
        return None, SearchResult.failed(res.error, res.status, url)
    blocked = page.detect_block(res.status, res.text)
    if blocked:
        return None, SearchResult.failed(blocked, res.status, url, blocked=True)
    return res, None


def _json(text):
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def shopify(fetcher, site, query: str) -> SearchResult:
    url = f"{site.root}/search/suggest.json?q={quote(query)}&resources[type]=product&resources[limit]=10"
    res, failure = _get(fetcher, site, url)
    if failure:
        return failure
    try:
        products = _json(res.text)["resources"]["results"]["products"]
    except (TypeError, KeyError):
        return SearchResult.failed("not a Shopify store", res.status, url)
    out = []
    for p in products if isinstance(products, list) else []:
        if not isinstance(p, dict):
            continue
        price = parse_price(p.get("price")) or parse_price(p.get("price_min"))
        was = parse_price(p.get("compare_at_price_max")) or parse_price(p.get("compare_at_price_min"))
        available = p.get("available")
        out.append(Candidate(html_lib.unescape(str(p.get("title") or "")),
                             page.clean_url(urljoin(site.root + "/", str(p.get("url") or ""))), price,
                             was if was and price and was > price else None, site.currency,
                             available if isinstance(available, bool) else None))
    return SearchResult(ok=True, candidates=out, status=res.status, url=url)


def _minor(prices: dict, key: str, unit: int) -> float | None:
    try:
        value = float(prices.get(key)) / unit
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def woocommerce(fetcher, site, query: str) -> SearchResult:
    url = f"{site.root}/wp-json/wc/store/v1/products?search={quote(query)}&per_page=20"
    res, failure = _get(fetcher, site, url)
    if failure:
        return failure
    data = _json(res.text)
    if not isinstance(data, list) or any(not isinstance(p, dict) or "prices" not in p for p in data):
        return SearchResult.failed("not a WooCommerce store", res.status, url)
    out = []
    for p in data:
        prices = p.get("prices") or {}
        unit = 10 ** int(prices.get("currency_minor_unit") or 0)
        price, regular = _minor(prices, "price", unit), _minor(prices, "regular_price", unit)
        stock = p.get("is_in_stock")
        out.append(Candidate(html_lib.unescape(str(p.get("name") or "")), str(p.get("permalink") or ""), price,
                             regular if regular and price and regular > price else None,
                             str(prices.get("currency_code") or site.currency).upper(),
                             stock if isinstance(stock, bool) else None))
    return SearchResult(ok=True, candidates=out, status=res.status, url=url)


def search_page(fetcher, site, query: str) -> SearchResult:
    first_failure = None
    for pattern in SEARCH_PAGES:
        url = site.url + pattern.format(q=quote_plus(query))
        res, failure = _get(fetcher, site, url)
        if failure:
            if failure.blocked:
                return failure
            first_failure = first_failure or failure
            continue
        if res.status and res.status >= 400:
            first_failure = first_failure or SearchResult.failed(f"HTTP {res.status}", res.status, url)
            continue
        soup = page.make_soup(res.text)
        found = jsonld_candidates(soup, url, site.currency) or generic_cards(soup, url, site.currency)
        if found:
            return SearchResult(ok=True, candidates=found, status=res.status, url=url)
        if no_results_page(res.text):
            return SearchResult(ok=True, status=res.status, url=url)
    return first_failure or SearchResult.failed("no products found on the site's search pages", url=site.url)


METHODS = {"shopify": shopify, "woocommerce": woocommerce, "search_page": search_page}


def search(fetcher, site, query: str, clock=time.monotonic) -> SearchResult:
    known = _working.get(site.key)
    if known in METHODS:
        result = METHODS[known](fetcher, site, query)
        if result.ok or result.blocked:
            return result
    dead = _dead.get(site.key)
    if dead and clock() - dead[0] < RETRY_DEAD_AFTER:
        return dead[1]
    first_ok = first_failure = None
    for name, method in METHODS.items():
        if name == known:
            continue
        result = method(fetcher, site, query)
        if result.blocked:
            return result
        if result.ok and (result.candidates or name != "search_page"):
            _working[site.key] = name  # the platform answered: use it from now on
            _dead.pop(site.key, None)
            return result
        if result.ok:
            first_ok = first_ok or result
        else:
            first_failure = first_failure or result
    if first_ok:
        return first_ok
    _dead[site.key] = (clock(), first_failure)
    return first_failure
