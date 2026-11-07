"""Polite fetching: one session per store, a random 2-6 s gap between requests to the same
store, a 15 s timeout and at most one retry.

Backends per store (settings.fetch_backend in watchlist.json):
  "requests"    default, plain requests with normal browser headers
  "impersonate" curl_cffi with a real Chrome TLS fingerprint (pip install curl_cffi);
                try this before the home-device fallback if a store blocks you
  "browser"     Playwright, last resort for pages that need JavaScript (10-20x heavier)
  "browser_visible"  the same with a visible window (some stores refuse headless browsers)
A local file path or file:// URL is read from disk, to test an adapter on a saved page.

Browsers: one Playwright per thread, shared by every Fetcher in that thread (a second one
started in the same thread fails with "using Playwright Sync API inside the asyncio loop").
A browser that was closed by hand or crashed is started again on the next fetch.
"""
from __future__ import annotations

import importlib.util
import logging
import random
import re
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import requests

log = logging.getLogger("watcher.fetch")

# Best: put your own browser's User-Agent in .env (USER_AGENT=...), copied from chrome://version.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)

_ENCODINGS = "gzip, deflate, br" if importlib.util.find_spec("brotli") else "gzip, deflate"

# What a fully loaded page shows, per store family: product cards on a search page, the product
# block on a product page. Waiting for it lets JavaScript sites render before the page is read.
SEARCH_READY = {"amazon": 'div[data-component-type="s-search-result"]', "jumia": "article.prd",
                "noon": '[data-qa="plp-product-box"]'}
PRODUCT_READY = {"amazon": "#productTitle, #outOfStock", "jumia": "h1", "noon": "h1"}

_BROWSERS = threading.local()  # this thread's Playwright and its browsers


def browser_headers(user_agent: str) -> dict[str, str]:
    headers = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
        "Accept-Encoding": _ENCODINGS,
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
    }
    match = re.search(r"Chrome/(\d+)", user_agent)
    if match and "Edg/" not in user_agent:
        version = match.group(1)
        platform = "Windows" if "Windows" in user_agent else "macOS" if "Mac OS" in user_agent else "Linux"
        headers["sec-ch-ua"] = f'"Chromium";v="{version}", "Google Chrome";v="{version}", "Not?A_Brand";v="99"'
        headers["sec-ch-ua-mobile"] = "?0"
        headers["sec-ch-ua-platform"] = f'"{platform}"'
    return headers


@dataclass
class FetchResult:
    status: int | None
    text: str
    url: str
    error: str | None = None
    elapsed: float = 0.0


def _short(exc: Exception) -> str:
    text = str(exc) or exc.__class__.__name__
    return text if len(text) < 160 else text[:157] + "..."


def _network_error(exc: Exception) -> str:
    """A short reason instead of urllib3's very long exception text."""
    text = f"{exc.__class__.__name__}: {exc}".lower()
    if any(k in text for k in ("nameresolution", "getaddrinfo", "name or service not known",
                               "could not resolve", "nodename nor servname")):
        return "network error: can't resolve the store's address (no internet?)"
    if "timeout" in text or "timed out" in text:
        return "network error: timed out"
    if "ssl" in text:
        return "network error: SSL problem"
    if "proxy" in text:
        return "network error: proxy refused the connection"
    if any(k in text for k in ("refused", "reset", "remotedisconnected", "aborted")):
        return "network error: the store closed the connection"
    return f"network error: {exc.__class__.__name__}"


def _decode(resp) -> str:
    ctype = (resp.headers.get("content-type") or "").lower()
    if "charset" not in ctype:
        try:
            resp.encoding = "utf-8"  # the stores are UTF-8; requests would guess Latin-1
        except Exception:
            pass
    try:
        return resp.text
    except Exception:
        return resp.content.decode("utf-8", "replace")


def store_family(store: str) -> str:
    return next((name for name in ("amazon", "noon", "jumia") if str(store).startswith(name)), "")


def is_search_url(url: str) -> bool:
    """amazon /s?k=, jumia /catalog/?q=, noon /search/?q= (and the generic ?q= search pages)."""
    parts = urlsplit(url)
    path, query = parts.path.lower().rstrip("/"), "&" + parts.query.lower()
    return (path.endswith("/s") and "&k=" in query) or (
        ("/catalog" in path or "/search" in path) and ("&q=" in query or "&query=" in query))


def _quietly(action) -> None:
    try:
        action()
    except Exception:
        pass


def _browser_state():
    if not hasattr(_BROWSERS, "browsers"):
        _BROWSERS.playwright = None
        _BROWSERS.browsers = {}  # (headless, user agent) -> (browser, context)
    return _BROWSERS


def close_browsers() -> None:
    """Close this thread's browsers and its Playwright; the next browser fetch starts them again."""
    state = _browser_state()
    for browser, context in list(state.browsers.values()):
        _quietly(context.close)
        _quietly(browser.close)
    state.browsers.clear()
    if state.playwright is not None:
        _quietly(state.playwright.stop)
        state.playwright = None


def _connected(browser) -> bool:
    try:
        return bool(browser.is_connected())
    except Exception:
        return False


def local_path(url: str) -> Path | None:
    if url.startswith("file://"):
        return Path(urllib.request.url2pathname(urlsplit(url).path))
    if "://" not in url:
        path = Path(url).expanduser()
        if path.exists():
            return path
    return None


class Fetcher:
    def __init__(self, settings, user_agent: str = "", sleep=time.sleep):
        self.timeout = float(settings.request_timeout_seconds)
        low, high = (list(settings.store_pause_seconds) + [2, 6])[:2]
        self.pause = (float(low), float(max(low, high)))
        self.backends = {str(k).lower(): str(v).lower() for k, v in (settings.fetch_backend or {}).items()}
        self.user_agent = user_agent or DEFAULT_USER_AGENT
        self.sleep = sleep
        self._sessions: dict[tuple[str, str], object] = {}
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()
        self._browser_threads: set[int] = set()  # threads where this fetcher opened a browser

    # ------------------------------------------------------------------ public

    def get(self, store: str, url: str) -> FetchResult:
        path = local_path(url)
        if path is not None:
            try:
                return FetchResult(200, path.read_text(encoding="utf-8", errors="replace"), url)
            except OSError as exc:
                return FetchResult(None, "", url, f"cannot read file: {_short(exc)}")
        backend = self.backends.get(store, "requests")
        if backend in ("browser", "browser_visible"):
            self._pace(store)
            try:
                return self._browser_get(url, store, headless=backend != "browser_visible")
            finally:
                self._last[store] = time.monotonic()
        session = self._session(store, backend)
        result = FetchResult(None, "", url, "not fetched")
        for attempt in range(2):  # at most one retry
            self._pace(store)
            started = time.monotonic()
            try:
                resp = session.get(url, timeout=self.timeout, allow_redirects=True)
            except Exception as exc:
                self._last[store] = time.monotonic()
                result = FetchResult(None, "", url, _network_error(exc), time.monotonic() - started)
                continue
            self._last[store] = time.monotonic()
            result = FetchResult(resp.status_code, _decode(resp), str(resp.url), None, time.monotonic() - started)
            if resp.status_code in (500, 502, 504) and attempt == 0:
                continue  # 503 is a block signal at Amazon; retrying it fast does not help
            return result
        return result

    def resolve(self, url: str) -> str:
        """Follow a short link (amzn.eu/d/...) to the real product URL."""
        try:
            resp = self._session("_resolve", "requests").get(url, timeout=self.timeout, allow_redirects=True)
            return str(resp.url)
        except Exception as exc:
            log.warning("could not resolve %s: %s", url, _short(exc))
            return url

    def close(self) -> None:
        for session in self._sessions.values():
            try:
                session.close()
            except Exception:
                pass
        self._sessions.clear()
        if threading.get_ident() in self._browser_threads:  # Playwright objects belong to their thread
            self._browser_threads.discard(threading.get_ident())
            close_browsers()

    # ----------------------------------------------------------------- helpers

    def _pace(self, store: str) -> None:
        last = self._last.get(store)
        if last is None:
            return
        wait = last + random.uniform(*self.pause) - time.monotonic()
        if wait > 0:
            self.sleep(wait)

    def _session(self, store: str, backend: str):
        with self._lock:
            key = (store, backend)
            if key not in self._sessions:
                self._sessions[key] = self._new_session(backend)
            return self._sessions[key]

    def _new_session(self, backend: str):
        if backend == "impersonate":
            try:
                from curl_cffi import requests as curl_requests

                session = curl_requests.Session(impersonate="chrome")
                session.headers.update({"Accept-Language": "en-US,en;q=0.9,ar;q=0.8"})
                return session
            except Exception as exc:
                log.warning("impersonate backend unavailable (%s): pip install curl_cffi. Using requests.", exc)
        session = requests.Session()
        session.headers.update(browser_headers(self.user_agent))
        return session

    def _context(self, headless: bool):
        """This thread's browser context for this user agent; started (again) when needed."""
        from playwright.sync_api import sync_playwright

        state = _browser_state()
        key = (headless, self.user_agent)
        entry = state.browsers.get(key)
        if entry is not None:
            browser, context = entry
            if _connected(browser):
                return context
            state.browsers.pop(key, None)  # closed by hand or crashed: start a new one
            _quietly(context.close)
            _quietly(browser.close)
        if state.playwright is None:
            state.playwright = sync_playwright().start()
        self._browser_threads.add(threading.get_ident())
        browser = state.playwright.chromium.launch(headless=headless, args=["--no-sandbox"])
        context = browser.new_context(user_agent=self.user_agent, locale="en-US")
        state.browsers[key] = (browser, context)
        return context

    def _browser_get(self, url: str, store: str, *, headless: bool = True) -> FetchResult:
        started = time.monotonic()
        try:
            importlib.import_module("playwright.sync_api")
        except ImportError:
            return FetchResult(None, "", url, "playwright not installed: pip install playwright && "
                                              "playwright install chromium")
        page = None
        for attempt in range(2):
            try:
                page = self._context(headless).new_page()
                break
            except Exception as exc:
                if attempt == 1:
                    return FetchResult(None, "", url, f"browser start failed: {_short(exc)}",
                                       time.monotonic() - started)
                close_browsers()  # a broken browser or Playwright: start everything once more
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=int(self.timeout * 2000))
            status = resp.status if resp else None
            if status is None or status < 400:
                self._wait_until_ready(page, store, url)
            return FetchResult(status, page.content(), page.url, None, time.monotonic() - started)
        except Exception as exc:
            return FetchResult(None, "", url, f"browser error: {_short(exc)}", time.monotonic() - started)
        finally:
            _quietly(page.close)

    def _wait_until_ready(self, page, store: str, url: str) -> None:
        family = store_family(store)
        if is_search_url(url):
            selector, limit = SEARCH_READY.get(family), min(30_000, int(self.timeout * 1000))
        else:
            selector, limit = PRODUCT_READY.get(family), min(10_000, int(self.timeout * 1000))
        if not selector:
            return
        try:
            page.wait_for_selector(selector, timeout=limit)
        except Exception:
            pass  # keep what loaded: the page parser tells blocked / no results / layout changed
