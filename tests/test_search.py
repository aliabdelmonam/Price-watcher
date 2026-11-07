"""Search mode: sites, title matching, each site's results reader, and the crawler loop."""
import csv
import json

import pytest

from helpers import (AMZ_Q, CAPTCHA, FIVE_MIN, JUM_Q, NOON_Q, R60, SITES, Harness, amazon_page,
                     amazon_results, amazon_url, jumia_results, noon_results, row)
from watcher.adapters import amazon_eg, default_item_id, store_for_url
from watcher.adapters.common import make_soup
from watcher.commands import BotCommands
from watcher.crawler import target_for
from watcher.fetch import FetchResult
from watcher.models import Item
from watcher.prices import detect_currency
from watcher.search import Candidate, Matcher, generic, pick
from watcher.search import amazon as amazon_search
from watcher.search import jumia as jumia_search
from watcher.search import noon as noon_search
from watcher.search.common import generic_cards, jsonld_candidates
from watcher.sites import apply_site_rows, load_sites

SITE = {s.key: s for s in load_sites(SITES + ["https://shop.example.com/"])[0]}


# ----------------------------------------------------------------- sites + matching

def test_sites_from_config():
    sites, problems = load_sites(["https://www.noon.com/egypt-ar/", "amazon.eg", "https://www.jumia.com.eg",
                                  "https://www.amazon.com/", {"url": "shop.example.com/en", "currency": "egp"},
                                  "https://www.noon.com/egypt-en/", 42])
    assert [s.key for s in sites] == ["noon", "amazon_eg", "jumia", "amazon_com", "shop.example.com"]
    assert sites[0].url == "https://www.noon.com/egypt-en/"   # English titles match English names
    assert sites[1].root == "https://www.amazon.eg" and sites[3].currency == "USD"
    assert sites[4].kind == "generic" and sites[4].url == "https://shop.example.com/en/"
    assert len(problems) == 2


@pytest.mark.parametrize("title, ok", [
    (R60, True),
    ("Anker Soundcore R60i-NC Earbuds, 52dB ANC", True),
    ("Soundcore R60i NC Earbuds with Charging Case", True),
    ("Soundcore R50i NC Earbuds", False),
    ("Soundcore R60i Wireless Earbuds", False),
    ("soundcore P31i/R60i NC Left and Right Replacement Earbuds", False),
    ("Silicone Case for Soundcore R60i NC", False),
    ("Ear Tips for Soundcore R60i NC, 4 pairs", False),
])
def test_matcher_finds_the_product_not_accessories(title, ok):
    assert Matcher("soundcore r60i NC earpods").check(title)[0] is ok


def test_matcher_options():
    assert Matcher("anker charger 65w").check("Anker 65W USB-C Charger, Nano")[0]
    pro = Matcher("iphone 16 pro", exclude="max")
    assert pro.check("Apple iPhone 16 Pro 256GB Black Titanium")[0]
    assert not pro.check("Apple iPhone 16 Pro Max 256GB")[0]
    assert not pro.check("Apple iPhone 16 128GB")[0]
    strict = Matcher("ps5", must_include="playstation 5 slim")
    assert strict.check("Sony PlayStation 5 Slim Console")[0] and not strict.check("Sony PlayStation 5 Console")[0]


def test_pick_takes_the_cheapest_real_match():
    cands = [Candidate("Soundcore R60i NC Black", "u1", 1394.95, 1570), Candidate("Soundcore R60i NC White", "u2", 1450),
             Candidate("Soundcore R60i NC Blue", "u3", 1420), Candidate("Soundcore R60i NC ear tips set", "u4", 150),
             Candidate("Soundcore R60i NC Pink", "u5", None), Candidate("Soundcore R50i NC", "u6", 900)]
    best, verdicts = pick(cands, Matcher("soundcore r60i nc"))
    assert best.url == "u1"
    why = {c.url: v for c, v in verdicts}
    assert why["u4"].startswith("far cheaper") and why["u5"] == "no price shown" and why["u6"].startswith("missing")
    assert pick(cands, Matcher("soundcore r60i nc"), min_price=1400)[0].url == "u3"


# ------------------------------------------------------------------ site readers

def test_amazon_results():
    html = amazon_results(("B0SPONSOR1", "Silicone Case for Soundcore R60i NC", 199, None),
                          ("B0R60INC01", R60, 1399, 1699), ("B0NOPRICE1", "Soundcore R60i NC Earbuds", None, None))
    result = amazon_search.parse(html, SITE["amazon_eg"], 200, "u")
    assert result.ok and len(result.candidates) == 3
    best, _ = pick(result.candidates, Matcher("soundcore r60i nc"))
    assert (best.url, best.price, best.list_price, best.currency) == ("https://www.amazon.eg/dp/B0R60INC01", 1399, 1699, "EGP")
    blocked = amazon_search.parse(CAPTCHA[1], SITE["amazon_eg"], 200, "u")
    assert not blocked.ok and blocked.blocked
    assert amazon_search.search_url(SITE["amazon_eg"], "soundcore r60i nc") == "https://www.amazon.eg/s?k=soundcore+r60i+nc"


def test_noon_results_from_page_data_and_app_router_payload():
    result = noon_search.parse(noon_results(("N70284977V", R60, 1570, 1394.95)), SITE["noon"], 200, "u")
    c = result.candidates[0]
    assert (c.price, c.list_price) == (1394.95, 1570)
    assert c.url == "https://www.noon.com/egypt-en/soundcore-item/N70284977V/p/"
    hit = {"sku": "N70284977V", "name": R60, "url": "x", "price": 1570, "sale_price": 1394.95}
    line = "7:" + json.dumps({"hits": [hit]}) + "\n"
    html = "<html><body><script>self.__next_f.push([1," + json.dumps(line) + "])</script></body></html>"
    assert noon_search.parse(html, SITE["noon"], 200, "u").candidates[0].price == 1394.95
    assert noon_search.search_url(SITE["noon"], "r60i nc") == "https://www.noon.com/egypt-en/search/?q=r60i%20nc"


def test_jumia_results():
    html = jumia_results((R60, 1450, 1999, "/anker-soundcore-r60i-nc-259677059.html?search=1"),
                         ("Soundcore R60i NC Replacement Case", 250, None, "/case-1.html"))
    best, _ = pick(jumia_search.parse(html, SITE["jumia"], 200, "u").candidates, Matcher("soundcore r60i nc"))
    assert best.url == "https://www.jumia.com.eg/anker-soundcore-r60i-nc-259677059.html" and best.list_price == 1999


WOO = ('<ul class="products"><li class="product"><a href="/product/r60i/" class="woocommerce-LoopProduct-link">'
       '<h2 class="woocommerce-loop-product__title">Soundcore R60i NC Earbuds</h2><span class="price"><del>'
       '<span class="amount">EGP 1,570.00</span></del> <ins><span class="amount">EGP 1,350.00</span></ins>'
       '</span></a></li></ul>')
MAGENTO = ('<ol><li class="item product product-item"><div class="product-item-info"><a class="product-item-link" '
           'href="/r60i.html">Soundcore R60i NC</a><div class="price-box"><span class="special-price"><span '
           'class="price">EGP 1,300.00</span></span><span class="old-price"><span class="price">EGP 1,500.00</span>'
           '</span></div></div></li></ol>')
ITEMLIST = ('<script type="application/ld+json">{"@context":"https://schema.org","@type":"ItemList",'
            '"itemListElement":[{"@type":"ListItem","position":1,"url":"https://shop.example.com/p/r60i",'
            '"item":{"@type":"Product","name":"Soundcore R60i NC","offers":{"@type":"Offer","price":"1299",'
            '"priceCurrency":"EGP","availability":"https://schema.org/InStock"}}}]}</script>')


@pytest.mark.parametrize("body, price, was", [(WOO, 1350, 1570), (MAGENTO, 1300, 1500), (ITEMLIST, 1299, None)])
def test_any_other_site_search_page(body, price, was):
    soup = make_soup(f"<html><body>{body}</body></html>")
    found = jsonld_candidates(soup, "https://shop.example.com/", "EGP") or \
        generic_cards(soup, "https://shop.example.com/", "EGP")
    assert len(found) == 1 and (found[0].price, found[0].list_price) == (price, was)
    assert found[0].url.startswith("https://shop.example.com/")


class PageFetcher:
    def __init__(self, pages):
        self.pages = pages

    def get(self, store, url):
        for key, text in self.pages.items():
            if key in url:
                return FetchResult(200, text, url)
        return FetchResult(404, "<html>Not found</html>", url)


def test_any_other_site_shopify_and_woocommerce_apis():
    site = SITE["shop.example.com"]
    shop = {"resources": {"results": {"products": [{"title": "Soundcore R60i NC", "url": "/products/r60i?_pos=1&_sid=x",
                                                    "price": "1299.00", "compare_at_price_max": "1599.00",
                                                    "available": True}]}}}
    result = generic.search(PageFetcher({"/search/suggest.json": json.dumps(shop)}), site, "soundcore r60i nc")
    assert result.candidates[0].list_price == 1599
    assert result.candidates[0].url == "https://shop.example.com/products/r60i"
    generic._working.clear()
    generic._dead.clear()
    woo = [{"name": "Soundcore R60i NC &#8211; Black", "permalink": "https://shop.example.com/p/r60i/",
            "is_in_stock": True, "prices": {"price": "129900", "regular_price": "159900", "currency_code": "EGP",
                                            "currency_minor_unit": 2}}]
    c = generic.search(PageFetcher({"/wp-json/wc/store/v1/products": json.dumps(woo)}), site, "x").candidates[0]
    assert (c.title, c.price, c.list_price) == ("Soundcore R60i NC – Black", 1299, 1599)
    generic._working.clear()


def test_other_amazon_countries_and_currencies(tmp_path):
    url = "https://www.amazon.com/Soundcore-R60i/dp/B0DTEST001/ref=x"
    assert store_for_url(url) == "amazon_com" and default_item_id(url) == "amzcom-b0dtest001"
    assert amazon_eg.fetch_url(url) == "https://www.amazon.com/dp/B0DTEST001?psc=1"
    page = ('<span id="productTitle">x</span><div id="corePrice_feature_div"><span class="a-price">'
            '<span class="a-offscreen">$25.99</span></span></div><div id="availability">In Stock</div>')
    assert (amazon_eg.parse(page, url, 200).currency, detect_currency("E£ 10"), detect_currency("12", "AED")) == \
        ("USD", "EGP", "AED")
    rt = Harness(tmp_path, [], {}, fx_rates={"USD": 50}).rt
    item = Item(id="s", name="x", url="", store="search", target_price=1500)
    assert (target_for(rt, item, "EGP"), target_for(rt, item, "USD"), target_for(rt, item, "AED")) == (1500, 30, None)


# ---------------------------------------------------------------- crawler, end to end

def test_crawler_reports_only_real_discounts(tmp_path):
    h = Harness(tmp_path, [row("", "", 1200, name="Soundcore R60i NC")],
                {NOON_Q: noon_results(("N70284977V", R60, 1570, 1394.95)),
                 AMZ_Q: amazon_results(("B0R50I0001", "Soundcore R50i NC Earbuds", 899, None)),
                 JUM_Q: CAPTCHA}, sites=SITES)
    h.cycle()
    [sale] = h.tg.texts("ALREADY ON SALE")                          # the store shows 11% off at noon
    assert "· noon" in sale and "Listing:" in sale and "-11.1%" in sale and "price drop" not in sale
    summary = h.tg.texts("First check done")[0]
    assert "Not listed: Soundcore R60i NC at amazon_eg" in summary and "jumia" in summary
    [rec] = h.notion.rows_for()
    assert (rec["site"], rec["why"], rec["status"]) == ("noon", ["ON SALE"], "Active")
    assert rec["listing"].startswith("Soundcore") and rec["link"].endswith("/N70284977V/p/")
    assert h.notion.latest["wl-1"] == "noon 1,394.95 EGP (-11%) · amazon_eg not listed · jumia can't search"
    h.tg.sent.clear()
    h.cycle(FIVE_MIN)
    h.cycle(FIVE_MIN)
    assert len(h.tg.texts("CAN'T SEARCH jumia")) == 1               # one alert per site, after 3 failures
    h.cycle(FIVE_MIN)
    assert len(h.tg.texts("CAN'T SEARCH")) == 1
    h.fetcher.pages[JUM_Q] = jumia_results((R60, 1150, None, "/soundcore-r60i-nc-1.html"))
    h.fetcher.pages[NOON_Q] = noon_results(("N70284977V", R60, 1570, 1180))
    h.cycle(FIVE_MIN)
    assert len(h.tg.texts("search works again")) == 1
    assert any("EVEN LOWER" in t and "· noon" in t for t in h.tg.sent)
    assert any("ALREADY ON SALE" in t and "· jumia" in t for t in h.tg.sent)
    assert len(h.notion.rows_for()) == 2                            # one row per item per site
    assert h.tg.texts("Now searching") == []                        # first-run items are not re-announced


def test_no_discount_means_no_message(tmp_path):
    h = Harness(tmp_path, [row("x", amazon_url("B0TEST0001"), 5000), row("", "", 1200, name="Soundcore R60i NC")],
                {"B0TEST0001": amazon_page(5999), NOON_Q: noon_results(("N70284977V", R60, 1500, 1480))},
                sites=SITES[:1], store_discount_percent=10)
    h.cycle()
    h.tg.sent.clear()
    for _ in range(3):
        h.cycle(FIVE_MIN)
    assert h.tg.sent == [] and h.notion.rows_for() == []             # 1.3% off < your 10%, above target: silence


def test_any_store_discount_counts_by_default(tmp_path):
    h = Harness(tmp_path, [row("", "", 1200, name="Soundcore R60i NC")],
                {NOON_Q: noon_results(("N70284977V", R60, 1500, 1480))}, sites=SITES[:1])
    h.cycle()
    for _ in range(3):
        h.cycle(FIVE_MIN)
    assert len(h.tg.texts("ALREADY ON SALE")) == 1 and len(h.notion.rows_for()) == 1   # once, never repeated


def test_searches_run_every_search_interval(tmp_path):
    h = Harness(tmp_path, [row("", "", 1200, name="Soundcore R60i NC")],
                {NOON_Q: noon_results(("N70284977V", R60, 1570, 1394.95))}, sites=SITES[:1],
                search_interval_seconds=600)
    for _ in range(4):
        h.cycle(advance=200)
    assert h.calls(NOON_Q) == 2                                     # at 0 s and at 600 s


def test_add_search_item_from_telegram(tmp_path):
    h = Harness(tmp_path, [row("x", amazon_url("B0TEST0001"), 5000)],
                {"B0TEST0001": amazon_page(5999),
                 NOON_Q: noon_results(("N70284977V", R60, 1570, 1500), ("N2", "Silicone Case for Soundcore R60i NC", 199, 149))},
                sites=SITES[:1])
    h.cycle()
    bot = BotCommands(h.rt)
    reply = bot.handle("/add soundcore r60i nc @1200")
    assert "Searching for" in reply and h.notion.rows[-1]["url"] == "" and h.notion.rows[-1]["target_price"] == 1200
    assert "Already searching" in bot.handle("/add Soundcore R60i NC 1300")
    h.cycle(FIVE_MIN)
    [note] = h.tg.texts("Now searching")
    assert "noon: 1,500 EGP" in note                                # the earbuds, not the 149 case
    assert "🔎 soundcore r60i nc (target 1,200): noon 1,500" in bot.handle("/list")


def test_notion_site_rows_only_switch_configured_sites():
    configured = load_sites(SITES)[0]
    rows = [{"key": "Noon Egypt", "url": "https://www.noon.com/egypt-en/", "enabled": False, "currency": ""},
            {"key": "amazon_com", "url": "https://www.amazon.com/", "enabled": True, "currency": ""},
            {"key": "jumia", "url": "", "enabled": True, "currency": "egp"}]
    sites = apply_site_rows(configured, rows)
    assert [(s.key, s.enabled, s.currency) for s in sites] == [
        ("noon", False, "EGP"), ("amazon_eg", True, "EGP"), ("jumia", True, "EGP")]
    assert apply_site_rows(configured, []) == configured                # no rows: nothing changes


def test_near_equal_listings_do_not_swap_every_check(tmp_path):
    a, b = "/soundcore-r60i-nc-black-1.html", "/soundcore-r60i-nc-white-2.html"
    h = Harness(tmp_path, [row("", "", None, name="Soundcore R60i NC")],
                {JUM_Q: jumia_results((R60, 1360, None, a), (R60, 1355, None, b))}, sites=SITES[2:])
    h.cycle()
    h.tg.sent.clear()
    pid = "s-soundcore-r60i-nc@jumia"
    assert h.rt.pairs[pid].url.endswith(b)
    h.fetcher.pages[JUM_Q] = jumia_results((R60, 1350, None, a), (R60, 1355, None, b))   # a is 0.4% cheaper
    h.cycle(FIVE_MIN)
    assert h.rt.pairs[pid].url.endswith(b) and h.tg.sent == []         # not worth switching listings
    h.fetcher.pages[JUM_Q] = jumia_results((R60, 1290, None, a), (R60, 1355, None, b))   # a is 4.8% cheaper
    h.cycle(FIVE_MIN)
    assert h.rt.pairs[pid].url.endswith(a)
    [drop] = h.tg.texts("PRICE DROP")
    assert "Previous price: 1,355" in drop


def test_a_deal_whose_listing_disappears_ends_after_three_checks(tmp_path):
    listing = (R60, 1355, 1800, "/anker-soundcore-r60i-nc-134706571.html")
    h = Harness(tmp_path, [row("", "", None, name="Soundcore R60i NC")], {JUM_Q: jumia_results(listing)},
                sites=SITES[2:])
    h.cycle()
    assert h.notion.rows_for()[0]["status"] == "Active"                 # 25% off: already on sale
    h.fetcher.pages[JUM_Q] = jumia_results(("Soundcore R50i NC", 900, None, "/r50i-1.html"))
    for _ in range(2):
        h.cycle(FIVE_MIN)
    assert h.notion.rows_for()[0]["status"] == "Active"                 # one or two misses can be a glitch
    h.cycle(FIVE_MIN)
    [rec] = h.notion.rows_for()
    assert (rec["status"], rec["price"]) == ("Ended", 1355) and rec["link"].endswith("-134706571.html")
    h.tg.sent.clear()
    h.fetcher.pages[JUM_Q] = jumia_results(listing)
    h.cycle(FIVE_MIN)
    assert h.tg.sent == [] and h.notion.rows_for()[0]["status"] == "Active"   # back within a day: quietly
    [path] = (h.data_dir / "history").glob("*.csv")
    rows = list(csv.DictReader(path.open(newline="", encoding="utf-8")))
    assert rows[-1]["url"].endswith("-134706571.html") and rows[-2]["error"] == "not listed"
