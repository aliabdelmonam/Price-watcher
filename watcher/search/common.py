"""Shared parts of site search: result types, title matching, choosing the listing to track,
and readers for search pages of sites without a dedicated adapter."""
from __future__ import annotations

import json
import re
import statistics
from dataclasses import dataclass, field
from urllib.parse import urljoin

from ..adapters import common as page
from ..prices import detect_currency, normalize_digits, parse_price


@dataclass
class Candidate:
    """One product listing on a search results page."""

    title: str
    url: str
    price: float | None = None
    list_price: float | None = None   # the store's 'was' price, if shown
    currency: str = ""
    in_stock: bool | None = None      # None: the results page doesn't say
    seller: str | None = None


@dataclass
class SearchResult:
    ok: bool
    candidates: list = field(default_factory=list)
    error: str | None = None
    status: int | None = None
    url: str = ""
    blocked: bool = False

    @classmethod
    def failed(cls, error: str, status: int | None = None, url: str = "", blocked: bool = False) -> "SearchResult":
        return cls(ok=False, error=error, status=status, url=url, blocked=blocked)


# --------------------------------------------------------------------- matching

STOPWORDS = {"the", "a", "an", "and", "with", "for", "of", "by", "in", "on", "to", "و", "مع", "من", "في"}
GENERIC_WORDS = {
    # stores name the product type differently, so these words never decide a match
    "earbuds", "earbud", "earpods", "earphones", "earphone", "headphones", "headphone", "headset", "buds",
    "wireless", "bluetooth", "true", "tws", "ear", "new", "original", "genuine", "official", "version",
    "black", "white", "blue", "pink", "red", "green", "gold", "silver", "gray", "grey", "purple", "beige",
    "سماعة", "سماعات", "لاسلكية", "بلوتوث", "اسود", "أسود", "ابيض", "أبيض", "ازرق", "أزرق",
}
DEFAULT_EXCLUDE = {
    # anywhere in a title: not the product itself
    "replacement", "refurbished", "renewed", "used", "dummy", "replica", "fake",
    "بديل", "بديلة", "مستعمل", "مجدد", "تقليد",
}
ACCESSORY_WORDS = {
    # only when they come before the product's model or brand: "Case for Soundcore R60i NC"
    "case", "cases", "cover", "covers", "skin", "skins", "sticker", "stickers", "protector", "strap", "straps",
    "pouch", "sleeve", "holder", "stand", "mount", "dock", "cable", "charger", "adapter", "tips", "eartips",
    "hooks", "earhooks", "film", "glass", "lens", "bag", "keychain",
    "جراب", "كفر", "حافظة", "واقي", "شاحن", "كابل", "لاصقة", "ستاند",
}
FOR_WORDS = {"for", "fits", "compatible"}


def tokens(text) -> list[str]:
    return re.findall(r"\w+", normalize_digits(text or "").lower().replace("_", " "))


class Matcher:
    """Decides whether a listing title is the item you want (any color, seller or bundle).

    Words with a digit (model numbers like r60i, 1000xm5, 256gb) must all appear. The other
    words must appear too (match_min_ratio), except product-type and color words, which stores
    name differently. Accessories and refurbished items are ruled out."""

    def __init__(self, query: str, must_include: str = "", exclude: str = "", extra_exclude=(),
                 min_ratio: float = 1.0):
        words = [t for t in tokens(query) if t not in STOPWORDS]
        self.query_words = set(words)
        if str(must_include or "").strip():
            self.required = [t for t in tokens(must_include) if t not in STOPWORDS]
            self.optional: list[str] = []
        else:
            self.required = [t for t in words if any(ch.isdigit() for ch in t)]
            self.optional = [t for t in words if t not in self.required and t not in GENERIC_WORDS]
            if not self.required and not self.optional:
                self.optional = words
        self.min_ratio = float(min_ratio)
        excluded = set(DEFAULT_EXCLUDE) | set(tokens(exclude)) | {t for w in extra_exclude or () for t in tokens(w)}
        self.exclude = {w for w in excluded if w not in self.query_words}
        self.anchors = self.required or self.optional

    @staticmethod
    def _has(word: str, present: set, squashed: str) -> bool:
        return word in present or (len(word) >= 4 and word in squashed)

    def check(self, title: str) -> tuple[bool, str]:
        toks = tokens(title)
        present, squashed = set(toks), "".join(toks)
        missing = [w for w in self.required if not self._has(w, present, squashed)]
        if missing:
            return False, "missing " + " ".join(missing)
        if self.optional:
            absent = [w for w in self.optional if not self._has(w, present, squashed)]
            if (len(self.optional) - len(absent)) / len(self.optional) < self.min_ratio - 1e-9:
                return False, "missing " + " ".join(absent)
        bad = sorted(w for w in self.exclude if w in present)
        if bad:
            return False, f"excluded word: {bad[0]}"
        if self._accessory(toks):
            return False, "looks like an accessory"
        return True, "match"

    def _accessory(self, toks: list[str]) -> bool:
        first = next((i for i, t in enumerate(toks)
                      if any(t == a or (len(a) >= 4 and a in t) for a in self.anchors)), len(toks))
        before = toks[:first]
        if any(t in ACCESSORY_WORDS and t not in self.query_words for t in before):
            return True
        return first < len(toks) and any(t in FOR_WORDS for t in before[-6:])


def pick(candidates: list, matcher: Matcher, min_price=None, max_price=None, accessory_ratio: float = 0.35):
    """The listing to track on one site: the cheapest in-stock listing whose title matches.

    Returns (best or None, [(candidate, verdict), ...] in page order)."""
    verdict: dict[int, str] = {}
    matched = []
    for c in candidates:
        ok, why = matcher.check(c.title)
        if not ok:
            verdict[id(c)] = why
        elif c.price is None and c.in_stock is not False:
            verdict[id(c)] = "no price shown"
        elif c.price is not None and min_price and c.price < min_price:
            verdict[id(c)] = "under min_price"
        elif c.price is not None and max_price and c.price > max_price:
            verdict[id(c)] = "over max_price"
        else:
            matched.append(c)
    prices = [c.price for c in matched if c.price is not None]
    if len(prices) >= 3 and accessory_ratio:
        floor = statistics.median(prices) * accessory_ratio
        for cheap in [c for c in matched if c.price is not None and c.price < floor]:
            matched.remove(cheap)
            verdict[id(cheap)] = "far cheaper than the other matches (accessory?)"
    buyable = [c for c in matched if c.in_stock is not False and c.price is not None]
    best = min(buyable, key=lambda c: c.price) if buyable else (matched[0] if matched else None)
    for c in matched:
        verdict[id(c)] = "picked" if c is best else "match"
    return best, [(c, verdict[id(c)]) for c in candidates]


# ------------------------------------------------------------- results pages

NO_RESULTS = ("no results for", "did not match any products", "no results found", "no products found",
              "no products were found", "we couldn't find", "we could not find", "0 results",
              "لا توجد نتائج", "لم يتم العثور", "لا يوجد نتائج")


def no_results_page(html: str) -> bool:
    low = (html or "")[:400000].lower()
    return any(phrase in low for phrase in NO_RESULTS)


def rsc_objects(payload: str):
    """JSON objects in a Next.js App Router payload ('7:{...}' lines)."""
    for line in (payload or "").splitlines():
        _, sep, rest = line.partition(":")
        rest = rest.strip()
        if sep and rest[:1] in ("{", "["):
            try:
                yield json.loads(rest)
            except ValueError:
                continue


def jsonld_candidates(soup, base_url: str, currency: str) -> list[Candidate]:
    """Products described in JSON-LD, e.g. an ItemList on a search page."""
    out, seen = [], set()
    for node in page.jsonld_nodes(soup):
        item = node.get("item") if isinstance(node.get("item"), dict) else node
        found = page.product_from_jsonld([item], "")
        offers = item.get("offers") if isinstance(item.get("offers"), dict) else {}
        link = item.get("url") or node.get("url") or offers.get("url") or ""
        if found is None or not link or (found.price is None and found.in_stock is not False):
            continue
        url = urljoin(base_url, str(link))
        if url not in seen:
            seen.add(url)
            out.append(Candidate(found.title or str(item.get("name") or ""), url, found.price, found.list_price,
                                 found.currency or currency, found.in_stock))
    return out


CARD_SELECTORS = (
    "li.product", "li.product-item", "div.product-item", "div.product-card", "div.product-grid-item",
    "div.grid-product", "article.product-miniature", "div.product-miniature", "div.card-product",
    "div.product-box", "article.product", "div.product", "[data-product-id]",
)
WAS_SELECTOR = (
    "del, s, strike, .old-price, .price-old, .was-price, .compare-price, .price--compare, "
    "[class*='compare-at'], [class*='old-price'], [class*='was-price'], [class*='price-before'], "
    "[class*='before-price']"
)
TITLE_SELECTORS = ("h2", "h3", "h4", ".product-title", ".product-name", ".product-item-link",
                   ".woocommerce-loop-product__title", ".card__heading", "[class*='title']", "[class*='name']")
LINK_SELECTORS = ("h2 a[href]", "h3 a[href]", "h4 a[href]", "a.product-item-link[href]",
                  "a.woocommerce-LoopProduct-link[href]", "a[class*='title'][href]", "a[class*='name'][href]",
                  "a[href]")
OUT_OF_STOCK = ("out of stock", "sold out", "نفدت", "غير متوفر")


def _card_link(card, base_url: str):
    for selector in LINK_SELECTORS:
        a = card.select_one(selector)
        href = (a.get("href") or "").strip() if a is not None else ""
        if href and not href.startswith(("#", "javascript:", "mailto:")):
            return a, urljoin(base_url, href)
    return None, ""


def _card_title(card, link) -> str:
    for selector in TITLE_SELECTORS:
        text = page.text_of(card.select_one(selector))
        if text and re.search(r"[^\W\d_]", text):
            return text
    text = page.text_of(link) if link is not None else ""
    if not text and link is not None:
        text = str(link.get("title") or "")
    if not text:
        img = card.find("img")
        text = str(img.get("alt") or "") if img is not None else ""
    return text.strip()


def _card_prices(card, currency: str):
    was_el = card.select_one(WAS_SELECTOR)
    was = parse_price(page.text_of(was_el)) if was_el is not None else None
    scope = card.select_one("[class*='price']")
    if scope is None or (was_el is not None and (scope is was_el or was_el in scope.parents)):
        scope = card
    parts = []
    for text in scope.find_all(string=True):
        if was_el is not None and was_el in text.parents:
            continue
        text = text.strip()
        if text and re.search(r"\d", normalize_digits(text)):
            parts.append(text)
    joined = " ".join(parts)
    return parse_price(joined), was, detect_currency(joined, currency)


def generic_cards(soup, base_url: str, currency: str) -> list[Candidate]:
    """Product cards on an ordinary search page (WooCommerce, Shopify, Magento and most themes)."""
    cards: list = []
    for selector in CARD_SELECTORS:
        for el in soup.select(selector):
            if not any(el is c or c in el.parents or el in c.parents for c in cards):
                cards.append(el)
    out = []
    for card in cards[:80]:
        link, url = _card_link(card, base_url)
        title = _card_title(card, link) if url else ""
        price, was, cur = _card_prices(card, currency)
        if not title or price is None:
            continue
        low = page.text_of(card).lower()
        out.append(Candidate(title, url, price, was if was and was > price else None, cur,
                             not any(w in low for w in OUT_OF_STOCK)))
    return out
