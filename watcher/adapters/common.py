"""Helpers shared by the store adapters."""
from __future__ import annotations

import importlib.util
import json
import re
from dataclasses import dataclass
from typing import Any, Iterator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from ..models import Reading
from ..prices import parse_price

_PARSER = "lxml" if importlib.util.find_spec("lxml") else "html.parser"  # html.parser: slower, still works

TRACKING_PARAMS = {
    "ref", "ref_", "tag", "th", "psc", "smid", "spm", "gclid", "fbclid", "srsltid", "_encoding",
    "content-id", "linkcode", "linkid", "camp", "creative", "creativeasin", "ascsubtag", "shareid",
    "share_id", "qid", "sr", "keywords", "crid", "sprefix", "dib", "dib_tag", "social_share",
    "starsleft", "skiptwisterog", "_branch_match_id", "_pos", "_sid", "_ss", "_psq", "mc_cid", "mc_eid", "igshid",
}
_TRACKING_PREFIXES = ("utm_", "pd_rd", "pf_rd")


def make_soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html or "", _PARSER)


def text_of(el) -> str:
    if el is None:
        return ""
    return " ".join(el.get_text(" ", strip=True).split())


def host_of(url: str) -> str:
    host = (urlsplit(url.strip()).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def host_matches(url: str, domains) -> bool:
    host = host_of(url)
    return any(host == d or host.endswith("." + d) for d in domains)


def clean_url(url: str, keep: tuple[str, ...] | None = None) -> str:
    """Drop tracking parameters (or keep only `keep`) and the #fragment."""
    parts = urlsplit(url.strip())
    query = parse_qsl(parts.query, keep_blank_values=False)
    if keep is not None:
        query = [(k, v) for k, v in query if k in keep]
    else:
        query = [
            (k, v) for k, v in query
            if k.lower() not in TRACKING_PARAMS and not k.lower().startswith(_TRACKING_PREFIXES)
        ]
    return urlunsplit((parts.scheme or "https", parts.netloc.lower(), parts.path or "/", urlencode(query), ""))


def query_param(url: str, name: str) -> str:
    for key, value in parse_qsl(urlsplit(url).query):
        if key == name:
            return value
    return ""


def slug_name(url: str) -> str:
    """A readable fallback name from the URL path (the page title replaces it after the first read)."""
    path = [p for p in urlsplit(url).path.split("/") if p and p not in ("dp", "p", "-", "en", "ar")]
    best = max(path, key=len, default="")
    best = re.sub(r"\.html?$", "", best)
    best = re.sub(r"-\d{5,}$", "", best)
    words = re.sub(r"[-_+]+", " ", best).strip()
    return words[:60].strip() or host_of(url)


# ---------------------------------------------------------------- JSON helpers

_TRAILING_COMMA = re.compile(r",\s*([\]}])")


def loads_lenient(raw: str | None) -> Any:
    raw = (raw or "").strip()
    if not raw:
        return None
    cleaned = re.sub(r"^\s*(<!--|//\s*<!\[CDATA\[)", "", raw)
    cleaned = re.sub(r"(-->|//\s*\]\]>)\s*$", "", cleaned)
    cleaned = _TRAILING_COMMA.sub(r"\1", cleaned)
    for candidate in (raw, cleaned):
        try:
            return json.loads(candidate, strict=False)
        except (ValueError, TypeError):
            continue
    return None


def iter_dicts(obj: Any, _depth: int = 0) -> Iterator[dict]:
    """Every dict inside nested JSON, parents before children."""
    if _depth > 80:
        return
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from iter_dicts(value, _depth + 1)
    elif isinstance(obj, list):
        for value in obj:
            yield from iter_dicts(value, _depth + 1)


def json_values_after_key(text: str, key: str, limit: int = 30) -> list:
    """Decode the JSON value that follows every `"key":` in a blob of HTML or JS."""
    out = []
    decoder = json.JSONDecoder(strict=False)
    for match in re.finditer(r'"%s"\s*:\s*' % re.escape(key), text):
        try:
            value, _ = decoder.raw_decode(text, match.end())
        except ValueError:
            continue
        out.append(value)
        if len(out) >= limit:
            break
    return out


_NEXT_F = re.compile(r'self\.__next_f\.push\(\[\d+\s*,\s*"((?:[^"\\]|\\.)*)"\]\)', re.S)


def rsc_payload(html: str) -> str:
    """Unescaped Next.js App Router payload (self.__next_f.push chunks), if the page has one."""
    parts = []
    for match in _NEXT_F.finditer(html):
        try:
            parts.append(json.loads('"' + match.group(1) + '"'))
        except ValueError:
            continue
    return "".join(parts)


def script_json(soup: BeautifulSoup, element_id: str) -> Any:
    tag = soup.find("script", id=element_id)
    return loads_lenient(tag.string or tag.get_text()) if tag else None


# ------------------------------------------------------- structured product data

@dataclass
class Found:
    price: float | None = None
    list_price: float | None = None
    currency: str = ""
    in_stock: bool | None = None
    seller: str | None = None
    title: str = ""
    source: str = ""


_IN = {"instock", "limitedavailability", "onlineonly", "preorder", "presale", "backorder", "available", "true"}
_OUT = {"outofstock", "soldout", "discontinued", "instoreonly", "oos", "unavailable", "false", "notavailable"}
PRODUCT_TYPES = {"product", "productgroup", "individualproduct"}


def availability_in_stock(value) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, dict):
        value = value.get("@id") or value.get("name")
    key = re.sub(r"[\s_-]", "", str(value).rsplit("/", 1)[-1].lower())
    if key in _IN:
        return True
    if key in _OUT:
        return False
    return None


def jsonld_nodes(soup: BeautifulSoup) -> list[dict]:
    nodes: list[dict] = []

    def walk(data):
        if isinstance(data, list):
            for entry in data:
                walk(entry)
        elif isinstance(data, dict):
            nodes.append(data)
            for key in ("@graph", "mainEntity", "itemListElement"):
                if isinstance(data.get(key), (dict, list)):
                    walk(data[key])

    for tag in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        walk(loads_lenient(tag.string or tag.get_text()))
    return nodes


def _types(node: dict) -> set[str]:
    value = node.get("@type")
    values = value if isinstance(value, list) else [value]
    return {str(v).rsplit("/", 1)[-1].lower() for v in values if v}


def _offers(node: dict) -> list[dict]:
    offers = node.get("offers")
    if isinstance(offers, dict):
        inner = offers.get("offers")
        if isinstance(inner, list) and inner:
            return [o for o in inner if isinstance(o, dict)]
        return [offers]
    if isinstance(offers, list):
        return [o for o in offers if isinstance(o, dict)]
    return []


def _name(value) -> str | None:
    if isinstance(value, dict):
        value = value.get("name")
    value = " ".join(str(value or "").split())
    return value[:80] or None


def _found(product: dict, offer: dict | None, parent: dict | None) -> Found:
    found = Found(title=str(product.get("name") or (parent or {}).get("name") or "").strip(), source="jsonld")
    if not offer:
        return found
    found.price = parse_price(offer.get("price"))
    if found.price is None:
        found.price = parse_price(offer.get("lowPrice"))
    found.currency = str(offer.get("priceCurrency") or "")
    specs = offer.get("priceSpecification")
    for spec in specs if isinstance(specs, list) else [specs] if isinstance(specs, dict) else []:
        value = parse_price(spec.get("price"))
        if value is None:
            continue
        kind = str(spec.get("priceType") or "").lower()
        if any(word in kind for word in ("listprice", "strikethrough", "msrp", "srp")):
            found.list_price = found.list_price or value
        elif found.price is None:
            found.price = value
        found.currency = found.currency or str(spec.get("priceCurrency") or "")
    found.in_stock = availability_in_stock(offer.get("availability"))
    found.seller = _name(offer.get("seller"))
    if found.list_price and found.price and found.list_price <= found.price:
        found.list_price = None
    return found


def product_from_jsonld(nodes: list[dict], url: str = "") -> Found | None:
    """Pick the Product (or variant) that matches the URL and return its offer."""
    variant = query_param(url, "variant") or query_param(url, "sku")
    rows: list[tuple[dict, dict | None, dict | None]] = []
    for node in nodes:
        if not _types(node) & PRODUCT_TYPES:
            continue
        for child in node.get("hasVariant") or []:
            if isinstance(child, dict):
                rows.extend((child, offer, node) for offer in (_offers(child) or [None]))
        rows.extend((node, offer, None) for offer in (_offers(node) or [None]))
    if not rows:
        return None
    if variant:
        for product, offer, parent in rows:
            haystack = " ".join(str(x) for x in (
                product.get("sku"), product.get("url"), (offer or {}).get("sku"), (offer or {}).get("url")))
            if variant in haystack:
                return _found(product, offer, parent)
    for product, offer, parent in rows:
        found = _found(product, offer, parent)
        if found.price is not None or found.in_stock is False:
            return found
    return _found(*rows[0])


def product_from_microdata(soup: BeautifulSoup) -> Found | None:
    scope = soup.find(attrs={"itemtype": re.compile(r"schema\.org/Product", re.I)})
    if scope is None:
        return None
    price_el = scope.find(attrs={"itemprop": "price"})
    if price_el is None:
        return None
    currency_el = scope.find(attrs={"itemprop": "priceCurrency"})
    stock_el = scope.find(attrs={"itemprop": "availability"})
    name_el = scope.find(attrs={"itemprop": "name"})
    return Found(
        price=parse_price(price_el.get("content") or text_of(price_el)),
        currency=(currency_el.get("content") or text_of(currency_el)) if currency_el else "",
        in_stock=availability_in_stock(stock_el.get("href") or stock_el.get("content") or text_of(stock_el))
        if stock_el else None,
        title=(name_el.get("content") or text_of(name_el)) if name_el else "",
        source="microdata",
    )


def meta_content(soup: BeautifulSoup, *names: str) -> str:
    for name in names:
        for attr in ("property", "name", "itemprop"):
            tag = soup.find("meta", attrs={attr: name})
            if tag and tag.get("content"):
                return tag["content"].strip()
    return ""


def product_from_meta(soup: BeautifulSoup) -> Found | None:
    price = parse_price(meta_content(soup, "product:price:amount", "og:price:amount"))
    sale = parse_price(meta_content(soup, "product:sale_price:amount"))
    if price is None and sale is None:
        return None
    availability = meta_content(soup, "product:availability", "og:availability")
    return Found(
        price=sale or price,
        list_price=price if sale and price and price > sale else None,
        currency=meta_content(soup, "product:price:currency", "og:price:currency"),
        in_stock=availability_in_stock(availability) if availability else None,
        title=meta_content(soup, "og:title"),
        source="meta",
    )


def page_title(soup: BeautifulSoup) -> str:
    h1 = text_of(soup.find("h1"))
    return h1 or meta_content(soup, "og:title") or text_of(soup.find("title"))


# ------------------------------------------------------------- blocks and results

def detect_block(status: int | None, html: str) -> str | None:
    """Recognise captcha / bot-wall pages so they count as failures, not as 'no price'."""
    head = (html or "")[:30000].lower()
    code = f" (HTTP {status})" if status else ""
    if "/errors/validatecaptcha" in head or "px-captcha" in head:
        return f"blocked: captcha{code}"
    if "<title>just a moment" in head or "cf-browser-verification" in head or "cf_chl_opt" in head:
        return f"blocked: Cloudflare challenge{code}"
    if "<title>access denied</title>" in head:
        return f"blocked: access denied{code}"
    if status in (401, 403, 429, 503):
        if "captcha" in head:
            return f"blocked: captcha{code}"
        return f"blocked{code}"
    return None


def http_problem(status: int | None) -> str | None:
    if status is None or 200 <= status < 300:
        return None
    if status == 404:
        return "page not found (404): check the URL"
    if status == 410:
        return "product removed (410)"
    return f"HTTP {status}"


def reading_from_found(found: Found, default_currency: str, status: int | None) -> Reading:
    in_stock = found.in_stock if found.in_stock is not None else found.price is not None
    if found.price is None and in_stock:
        return Reading.failed("price not found (page layout changed?)", status, found.title)
    return Reading(
        price=found.price,
        list_price=found.list_price,
        currency=found.currency or default_currency,
        in_stock=bool(in_stock),
        seller=found.seller,
        title=found.title,
        ok=True,
        source=found.source,
        http_status=status,
    )
