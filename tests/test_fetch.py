"""The browser backend with a fake Playwright that behaves like the real one: a second live
Playwright in one thread fails with the error seen on Nov 7 for noon."""
import sys
import threading
import types

import pytest

from watcher.config import Settings
from watcher.fetch import Fetcher, close_browsers

ASYNC_ERROR = "It looks like you are using Playwright Sync API inside the asyncio loop."
NOON_SEARCH = "https://www.noon.com/egypt-en/search/?q=soundcore%20r60i%20nc"
JUMIA_SEARCH = "https://www.jumia.com.eg/catalog/?q=soundcore+r60i+nc"
JUMIA_PRODUCT = "https://www.jumia.com.eg/anker-soundcore-r60i-nc-134706571.html"


class FakeBrowser:
    def __init__(self, headless):
        self.headless, self.connected, self.waits = headless, True, []

    def is_connected(self):
        return self.connected

    def new_context(self, **_):
        return FakeContext(self)

    def close(self):
        self.connected = False


class FakeContext:
    def __init__(self, browser):
        self.browser = browser

    def new_page(self):
        if not self.browser.connected:
            raise RuntimeError("Target page, context or browser has been closed")
        return FakePage(self.browser)

    def close(self):
        pass


class FakePage:
    def __init__(self, browser):
        self.browser, self.url = browser, ""

    def goto(self, url, **_):
        self.url = url
        return types.SimpleNamespace(status=200)

    def wait_for_selector(self, selector, timeout):
        self.browser.waits.append((selector, timeout))

    def content(self):
        return "<html><body>ok</body></html>"

    def close(self):
        pass


class FakePlaywright:
    """sync_playwright(): counts live instances per thread, like the real one."""

    def __init__(self):
        self.live, self.starts, self.launched = {}, 0, []

    def __call__(self):
        return types.SimpleNamespace(start=self._start)

    def _start(self):
        tid = threading.get_ident()
        if self.live.get(tid):
            raise RuntimeError(ASYNC_ERROR)
        self.live[tid] = True
        self.starts += 1

        def launch(headless=True, **_):
            browser = FakeBrowser(headless)
            self.launched.append(browser)
            return browser

        def stop():
            self.live[tid] = False

        return types.SimpleNamespace(chromium=types.SimpleNamespace(launch=launch), stop=stop)


@pytest.fixture
def playwright(monkeypatch):
    fake = FakePlaywright()
    module = types.ModuleType("playwright.sync_api")
    module.sync_playwright = fake
    monkeypatch.setitem(sys.modules, "playwright.sync_api", module)
    if "playwright" not in sys.modules:
        monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    close_browsers()
    yield fake
    close_browsers()


def fetcher(**backends) -> Fetcher:
    settings = Settings.from_dict({"fetch_backend": backends, "store_pause_seconds": [0, 0]})
    return Fetcher(settings, sleep=lambda _seconds: None)


def test_two_fetchers_in_one_thread_share_one_playwright(playwright):
    first, second = fetcher(noon="browser"), fetcher(noon="browser")
    assert first.get("noon", NOON_SEARCH).error is None
    assert second.get("noon", NOON_SEARCH).error is None              # used to fail with ASYNC_ERROR
    assert playwright.starts == 1 and len(playwright.launched) == 1


def test_headless_and_visible_browsers_live_side_by_side(playwright):
    f = fetcher(jumia="browser", noon="browser_visible")
    assert f.get("jumia", JUMIA_SEARCH).error is None and f.get("noon", NOON_SEARCH).error is None
    assert playwright.starts == 1 and [b.headless for b in playwright.launched] == [True, False]


def test_a_browser_you_closed_is_started_again(playwright):
    f = fetcher(noon="browser_visible")
    assert f.get("noon", NOON_SEARCH).error is None
    playwright.launched[-1].connected = False                        # you closed its window
    assert f.get("noon", NOON_SEARCH).error is None
    assert len(playwright.launched) == 2 and playwright.starts == 1


def test_close_stops_playwright_and_the_next_fetch_starts_it_again(playwright):
    f = fetcher(noon="browser")
    f.get("noon", NOON_SEARCH)
    f.close()
    assert not any(playwright.live.values())
    assert fetcher(noon="browser").get("noon", NOON_SEARCH).error is None and playwright.starts == 2


def test_each_thread_has_its_own_playwright(playwright):
    errors = []

    def run():
        f = fetcher(noon="browser")
        errors.append(f.get("noon", NOON_SEARCH).error)
        f.close()

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [None, None] and playwright.starts == 2


def test_product_pages_do_not_wait_for_search_cards(playwright):
    f = fetcher(jumia="browser")
    f.get("jumia", JUMIA_SEARCH)
    f.get("jumia", JUMIA_PRODUCT)
    (search_wait, _), (product_wait, limit) = playwright.launched[0].waits
    assert search_wait == "article.prd" and product_wait == "h1" and limit <= 10_000
