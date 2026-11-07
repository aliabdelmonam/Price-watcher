"""Amazon (amazon.eg, also amazon.com/.ae/.sa/...): no full JSON-LD, so prices come from the buy-box markup.

Price: the visible .a-price .a-offscreen inside the core price block, then whole+fraction
spans, then the hidden numeric inputs Amazon keeps for the variation picker.
Stock: the #availability block (English and Arabic wording).
Seller: the "Sold by" block, to catch cheap third-party sellers.
The page is requested in English (/-/en/) so wording is predictable; Arabic also parses.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from ..models import Reading
from ..prices import detect_currency, parse_price
from . import common

STORE = "amazon_eg"
DOMAINS = ("amazon.eg", "amazon.com", "amazon.ae", "amazon.sa", "amazon.co.uk", "amazon.de")
CURRENCY = "EGP"
CURRENCIES = {"amazon.eg": "EGP", "amazon.com": "USD", "amazon.ae": "AED", "amazon.sa": "SAR",
              "amazon.co.uk": "GBP", "amazon.de": "EUR"}
ENGLISH_PREFIX = ("amazon.eg", "amazon.ae", "amazon.sa")  # Arabic-first stores: ask for the English page

_ASIN = re.compile(r"/(?:dp|gp/product|gp/aw/d|exec/obidos/asin|o/asin)/([A-Z0-9]{10})(?=[/?#]|$)", re.I)

PRICE_SELECTORS = (
    "#corePriceDisplay_desktop_feature_div .priceToPay .a-offscreen",
    "#corePriceDisplay_desktop_feature_div .apexPriceToPay .a-offscreen",
    "#corePrice_feature_div .priceToPay .a-offscreen",
    "#corePrice_feature_div .a-price:not(.a-text-price) .a-offscreen",
    "#apex_desktop .apexPriceToPay .a-offscreen",
    "#apex_desktop .priceToPay .a-offscreen",
    "#corePrice_desktop .apexPriceToPay .a-offscreen",
    "#priceblock_dealprice",
    "#priceblock_ourprice",
    "#priceblock_saleprice",
    "#price_inside_buybox",
    "#newBuyBoxPrice",
)
WHOLE_FRACTION_SELECTORS = (
    "#corePriceDisplay_desktop_feature_div .priceToPay",
    "#corePrice_feature_div .a-price:not(.a-text-price)",
    "#apex_desktop .priceToPay",
)
LIST_PRICE_SELECTORS = (
    "#corePriceDisplay_desktop_feature_div .basisPrice .a-offscreen",
    "#corePriceDisplay_desktop_feature_div .a-price.a-text-price[data-a-strike='true'] .a-offscreen",
    "#corePrice_feature_div .a-price.a-text-price[data-a-strike='true'] .a-offscreen",
    "#corePrice_desktop .a-price.a-text-price[data-a-strike='true'] .a-offscreen",
    "#apex_desktop .basisPrice .a-offscreen",
    "#corePrice_desktop .a-text-price .a-offscreen",
    "#listPrice",
    "#priceblock_listprice",
    ".priceBlockStrikePriceString",
)
SELLER_SELECTORS = (
    "#sellerProfileTriggerId",
    "div[offer-display-feature-name='desktop-merchant-info'] .offer-display-feature-text-message",
    "#merchantInfoFeature_feature_div .offer-display-feature-text-message",
    "#tabular-buybox .tabular-buybox-text[tabular-attribute-name='Sold by'] span",
    "#tabular-buybox .tabular-buybox-text[tabular-attribute-name='Sold by']",
)
OUT_OF_STOCK = (
    "currently unavailable", "temporarily out of stock", "out of stock", "unavailable",
    "no featured offers", "available from these sellers", "see all buying options",
    "غير متوفر", "غير متاح", "نفدت", "نفذت",
)
IN_STOCK = (
    "in stock", "left in stock", "only ", "usually ships", "ships within", "dispatched",
    "pre-order", "preorder", "available", "متوفر", "متبقي", "متاح", "يُشحن", "يشحن",
)


def asin_of(url: str) -> str | None:
    match = _ASIN.search(urlsplit(url).path)
    return match.group(1).upper() if match else None


def _origin(url: str) -> tuple[str, str]:
    netloc = (urlsplit(url).netloc or "www.amazon.eg").lower()
    host = netloc[4:] if netloc.startswith("www.") else netloc
    return "www." + host, host


def currency_for(url: str) -> str:
    _, host = _origin(url)
    return next((c for d, c in CURRENCIES.items() if host == d or host.endswith("." + d)), CURRENCY)


def canonical_url(url: str) -> str:
    asin = asin_of(url)
    return f"https://{_origin(url)[0]}/dp/{asin}" if asin else common.clean_url(url)


def fetch_url(url: str) -> str:
    asin = asin_of(url)
    if not asin:
        return common.clean_url(url)
    netloc, host = _origin(url)
    prefix = "/-/en" if host.endswith(ENGLISH_PREFIX) else ""
    return f"https://{netloc}{prefix}/dp/{asin}?psc=1"


def product_id(url: str) -> str | None:
    asin = asin_of(url)
    if not asin:
        return None
    country = _origin(url)[1].split("amazon.", 1)[-1].replace(".", "")
    return f"amz-{asin.lower()}" if country == "eg" else f"amz{country}-{asin.lower()}"


def name_from_url(url: str) -> str:
    return common.slug_name(url)


def classify_stock(text: str) -> bool | None:
    lowered = text.lower()
    if any(word in lowered for word in OUT_OF_STOCK):
        return False
    if any(word in lowered for word in IN_STOCK):
        return True
    return None


def block_reason(html: str) -> str | None:
    if "/errors/validateCaptcha" in html or "Type the characters you see in this image" in html \
            or "Enter the characters you see below" in html:
        return "blocked: captcha"
    if "api-services-support@amazon.com" in html:
        return "blocked: automated-access page"
    if "Sorry! Something went wrong" in html and "productTitle" not in html:
        return "Amazon error page (try later)"
    return None


def _price(soup) -> tuple[float | None, str, str]:
    for selector in PRICE_SELECTORS:
        for el in soup.select(selector):
            price = parse_price(common.text_of(el))
            if price:
                return price, "css", common.text_of(el)
    for selector in WHOLE_FRACTION_SELECTORS:
        el = soup.select_one(selector)
        if el is None:
            continue
        whole = common.text_of(el.select_one(".a-price-whole")).rstrip(" .,٫")
        fraction = common.text_of(el.select_one(".a-price-fraction"))
        price = parse_price(f"{whole}.{fraction}" if whole and fraction.isdigit() else whole)
        if price:
            return price, "css-whole", whole
    el = soup.select_one("#twister-plus-price-data-price")
    if el is not None and parse_price(el.get("value")):
        return parse_price(el.get("value")), "twister-input", ""
    for div in soup.select(".twister-plus-buying-options-price-data"):
        data = common.loads_lenient(div.get_text())
        groups = data.values() if isinstance(data, dict) else [data]
        for group in groups:
            for entry in group if isinstance(group, list) else [group]:
                if isinstance(entry, dict) and entry.get("buyingOptionType", "NEW") == "NEW":
                    price = parse_price(entry.get("priceAmount"))
                    if price:
                        return price, "twister-json", ""
    el = soup.select_one("#attach-base-product-price")
    if el is not None and parse_price(el.get("value")):
        return parse_price(el.get("value")), "attach-input", ""
    return None, "", ""


def _list_price(soup) -> float | None:
    for selector in LIST_PRICE_SELECTORS:
        el = soup.select_one(selector)
        price = parse_price(common.text_of(el)) if el is not None else None
        if price:
            return price
    return None


def _stock(soup) -> bool | None:
    if soup.select_one("#outOfStock") is not None:
        return False
    for selector in ("#availability", "#availabilityInsideBuyBox_feature_div", "#availability_feature_div"):
        verdict = classify_stock(common.text_of(soup.select_one(selector)))
        if verdict is not None:
            return verdict
    if soup.select_one("#buybox-see-all-buying-choices") is not None and soup.select_one("#add-to-cart-button") is None:
        return False
    if soup.select_one("#add-to-cart-button") is not None or soup.select_one("#buy-now-button") is not None:
        return True
    return None


def _seller(soup) -> str | None:
    for selector in SELLER_SELECTORS:
        value = common.text_of(soup.select_one(selector))
        if value:
            return value[:80]
    info = common.text_of(soup.select_one("#merchant-info"))
    if info:
        match = re.search(r"sold by\s+(.+?)(?:\s+and\s+|\.\s|\.$|$)", info, re.I)
        if match:
            return match.group(1).strip()[:80]
        if "amazon" in info.lower():
            return "Amazon.eg"
    return None


def parse(html: str, url: str, status: int | None = 200) -> Reading:
    blocked = block_reason(html or "") or common.detect_block(status, html)
    if blocked:
        return Reading.failed(blocked, status)
    problem = common.http_problem(status)
    if problem:
        return Reading.failed(problem, status)
    soup = common.make_soup(html)
    title = common.text_of(soup.select_one("#productTitle"))
    price, source, price_text = _price(soup)
    currency = detect_currency(price_text, currency_for(url))
    stock = _stock(soup)
    seller = _seller(soup)
    if not title and price is None:
        return Reading.failed("no product data in page (blocked or layout changed)", status)
    if price is None:
        if stock is False:
            return Reading(currency=currency, in_stock=False, seller=seller, title=title,
                           ok=True, source="availability", http_status=status)
        return Reading.failed("price not found (page layout changed?)", status, title)
    list_price = _list_price(soup)
    if list_price is not None and list_price <= price:
        list_price = None
    return Reading(
        price=price,
        list_price=list_price,
        currency=currency,
        in_stock=stock if stock is not None else True,
        seller=seller,
        title=title,
        ok=True,
        source=source,
        http_status=status,
    )

