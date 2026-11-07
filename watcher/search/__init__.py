"""Search a site for an item. Noon, Amazon and Jumia have their own readers; any other site
goes through the generic one (Shopify / WooCommerce APIs, then the site's search page)."""
from __future__ import annotations

from . import amazon, generic, jumia, noon
from .common import Candidate, Matcher, SearchResult, pick, tokens

KINDS = {"amazon": amazon, "noon": noon, "jumia": jumia}


def search_site(fetcher, site, query: str) -> SearchResult:
    module = KINDS.get(site.kind)
    if module is None:
        return generic.search(fetcher, site, query)
    url = module.search_url(site, query)
    res = fetcher.get(site.key, url)
    if res.error and not res.text:
        return SearchResult.failed(res.error, res.status, url)
    try:
        return module.parse(res.text, site, res.status, url)
    except Exception as exc:  # a parser bug must not stop the cycle
        return SearchResult.failed(f"parser crashed: {exc}", res.status, url)


__all__ = ["Candidate", "Matcher", "SearchResult", "pick", "search_site", "tokens"]
