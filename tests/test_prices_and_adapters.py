import json
from pathlib import Path

import pytest

from watcher.adapters import amazon_eg, default_item_id, get_adapter, jsonld, jumia, noon, store_for_url
from watcher.prices import parse_amount, parse_bool, parse_price

FIXTURES = Path(__file__).parent / "fixtures"
EXPECTED = json.loads((FIXTURES / "expected.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("text, value", [
    ("EGP5,999.00", 5999), ("٥٬٩٩٩٫٠٠ جنيه", 5999), ("5.999,00 EGP", 5999), ("1,249.50", 1249.5),
    ("EGP 99.00", 99), ("5.999", 5999), ("0.5", 0.5), ("١٢٬٥٠٠", 12500), (6500, 6500),
    (" ", None), ("Free", None), (None, None), (0, None),
])
def test_parse_price(text, value):
    assert parse_price(text) == value


def test_parse_amount_is_strict():
    assert parse_amount("6,500") == 6500
    assert parse_amount("EGP 6500") == 6500
    assert parse_amount("٦٥٠٠ جنيه") == 6500
    assert parse_amount(6500.0) == 6500
    for bad in ("abc", "about 6k", "6k", "", None):
        assert parse_amount(bad) is None


def test_parse_bool():
    assert parse_bool("TRUE", False) and parse_bool(True, False) and not parse_bool("no", True)
    assert parse_bool("", True) is True


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_adapter_reads_fixture(name):
    exp = EXPECTED[name]
    html = (FIXTURES / name).read_text(encoding="utf-8")
    reading = get_adapter(exp["store"]).parse(html, exp["url"], 200)
    assert reading.ok == exp["ok"], reading.error
    if exp["ok"]:
        for key in ("price", "list_price", "in_stock", "seller"):
            if key in exp:
                assert getattr(reading, key) == exp[key], f"{key}: got {getattr(reading, key)!r}"


def test_blocked_or_missing_pages_are_failures_not_price_changes():
    reading = amazon_eg.parse("<html>Service Unavailable</html>", "https://www.amazon.eg/dp/B09XS7JWHH", 503)
    assert not reading.ok and "blocked" in reading.error
    assert not jumia.parse("<html></html>", "https://www.jumia.com.eg/x-123456.html", 404).ok
    assert not jsonld.parse("<html><h1>No data</h1></html>", "https://shop.example.com/p/1", 200).ok


def test_noon_ignores_recommendation_offers():
    blob = ('<script id="__NEXT_DATA__" type="application/json">{"props":{"recs":[{"sku":"N9999999A",'
            '"product_title":"Cable","offers":[{"sku":"N9999999A","price":99,"sale_price":49}]}]}}</script>')
    assert not noon.parse(blob, "https://www.noon.com/egypt-en/x/N53346840A/p/", 200).ok


def test_urls_are_cleaned_and_identified():
    a = "https://www.amazon.eg/-/en/Sony-WH-1000XM5/dp/B09XS7JWHH/ref=sr_1_3?crid=X&keywords=sony&th=1"
    assert store_for_url(a) == "amazon_eg"
    assert amazon_eg.canonical_url(a) == "https://www.amazon.eg/dp/B09XS7JWHH"
    assert amazon_eg.fetch_url(a) == "https://www.amazon.eg/-/en/dp/B09XS7JWHH?psc=1"
    assert default_item_id(a) == "amz-b09xs7jwhh"
    assert amazon_eg.name_from_url(a) == "Sony WH 1000XM5"
    assert store_for_url("https://amazon.eg/dp/B09XS7JWHH") == "amazon_eg"
    n = "https://www.noon.com/egypt-en/sony-wh/N53346840A/p/?o=abc123&utm_source=x"
    assert store_for_url(n) == "noon"
    assert noon.canonical_url(n) == "https://www.noon.com/egypt-en/sony-wh/N53346840A/p/"
    assert default_item_id(n) == "noon-n53346840a"
    j = "https://www.jumia.com.eg/xiaomi-redmi-note-13-123456789.html?shopAll=1#reviews"
    assert jumia.canonical_url(j) == "https://www.jumia.com.eg/xiaomi-redmi-note-13-123456789.html"
    assert default_item_id(j) == "jumia-123456789"
    g = "https://shop.example.com/products/am90?variant=222&utm_source=ig&fbclid=1"
    assert store_for_url(g) == "shop.example.com"
    assert jsonld.canonical_url(g) == "https://shop.example.com/products/am90?variant=222"
    assert default_item_id(g).startswith("shop-")
