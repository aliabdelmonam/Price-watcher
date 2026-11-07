"""Generic adapter for any store: JSON-LD Product, then microdata, then price meta tags.

Most Shopify / WooCommerce / brand stores expose one of these, so a new store usually
works without writing code. Run `python run_local.py --url <product url>` to check.
"""
from __future__ import annotations

from ..models import Reading
from . import common

STORE = "generic"
DOMAINS: tuple[str, ...] = ()
CURRENCY = ""  # unknown; the item's currency column is used


def canonical_url(url: str) -> str:
    return common.clean_url(url)


def fetch_url(url: str) -> str:
    return canonical_url(url)


def product_id(url: str) -> str | None:
    return None


def name_from_url(url: str) -> str:
    return common.slug_name(url)


def parse(html: str, url: str, status: int | None = 200) -> Reading:
    blocked = common.detect_block(status, html)
    if blocked:
        return Reading.failed(blocked, status)
    problem = common.http_problem(status)
    if problem:
        return Reading.failed(problem, status)
    soup = common.make_soup(html)
    candidates = (
        common.product_from_jsonld(common.jsonld_nodes(soup), url),
        common.product_from_microdata(soup),
        common.product_from_meta(soup),
    )
    found = next((f for f in candidates if f and (f.price is not None or f.in_stock is False)), None)
    if found is None:
        return Reading.failed(
            "no structured price data on this page (needs a store-specific adapter)",
            status,
            common.page_title(soup),
        )
    if not found.title:
        found.title = common.page_title(soup)
    return common.reading_from_found(found, CURRENCY, status)
