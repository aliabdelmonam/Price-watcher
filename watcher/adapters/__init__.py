"""Pick the adapter for a product URL by its domain.

Each adapter module provides:
    STORE, DOMAINS, CURRENCY
    canonical_url(url)  -> clean URL used as the item's link
    fetch_url(url)      -> URL actually requested (e.g. Amazon's English page)
    product_id(url)     -> stable id such as 'amz-b09xs7jwhh', or None
    name_from_url(url)  -> readable fallback name
    parse(html, url, status) -> Reading
"""
from __future__ import annotations

import hashlib
import re
from types import ModuleType

from . import amazon_eg, jsonld, jumia, noon
from .common import host_matches, host_of

AMAZON_STORES = {"amazon.eg": "amazon_eg", "amazon.com": "amazon_com", "amazon.ae": "amazon_ae",
                 "amazon.sa": "amazon_sa", "amazon.co.uk": "amazon_uk", "amazon.de": "amazon_de"}
STORE_ADAPTERS: dict[str, ModuleType] = {"amazon_eg": amazon_eg, "noon": noon, "jumia": jumia}
SHORT_LINK_HOSTS = ("amzn.eu", "amzn.to", "a.co", "amzn.asia", "bit.ly", "tinyurl.com")


def store_for_url(url: str) -> str:
    host = host_of(url)
    for domain, key in AMAZON_STORES.items():
        if host == domain or host.endswith("." + domain):
            return key
    for module in (noon, jumia):
        if host_matches(url, module.DOMAINS):
            return module.STORE
    return host or "unknown"


def get_adapter(store: str) -> ModuleType:
    if store.startswith("amazon_"):
        return amazon_eg
    return STORE_ADAPTERS.get(store, jsonld)


def default_currency(url: str) -> str:
    adapter = get_adapter(store_for_url(url))
    return adapter.currency_for(url) if hasattr(adapter, "currency_for") else (adapter.CURRENCY or "EGP")


def is_short_link(url: str) -> bool:
    return host_matches(url, SHORT_LINK_HOSTS)


def default_item_id(url: str) -> str:
    store = store_for_url(url)
    pid = get_adapter(store).product_id(url)
    if pid:
        return pid
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]
    prefix = re.sub(r"[^a-z0-9]+", "-", store.split(".")[0].lower()).strip("-") or "item"
    return f"{prefix}-{digest}"
