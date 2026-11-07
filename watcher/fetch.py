"""Polite fetching: one session per store, a random 2-6 s gap between requests to the same
store, a 15 s timeout and at most one retry.

Backends per store (settings.fetch_backend in watchlist.json):
  "requests"    default, plain requests with normal browser headers
  "impersonate" curl_cffi with a real Chrome TLS fingerprint (pip install curl_cffi);
                try this before the home-device fallback if a store blocks you
  "browser"     Playwright, last resort for pages that need JavaScript (10-20x heavier)
A local file path or file:// URL is read from disk, to test an adapter on a saved page.
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
        self._tls = threading.local()

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
        for browser, _context in getattr(self._tls, "browsers", {}).values():
            try:
                browser.close()
            except Exception:
                pass
        self._tls.browsers = {}
        playwright = getattr(self._tls, "playwright", None)
        if playwright is not None:
            try:
                playwright.stop()
            except Exception:
                pass
            self._tls.playwright = None

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

    def _browser_get(self, url: str, store: str, *, headless: bool = True) -> FetchResult:
        started = time.monotonic()
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return FetchResult(None, "", url, "playwright not installed: pip install playwright && "
                                              "playwright install chromium")
        try:
            browsers = getattr(self._tls, "browsers", None)
            if browsers is None:
                browsers = self._tls.browsers = {}
            if headless not in browsers:  # Playwright objects stay on their own thread
                playwright = getattr(self._tls, "playwright", None)
                if playwright is None:
                    playwright = self._tls.playwright = sync_playwright().start()
                browser = playwright.chromium.launch(headless=headless, args=["--no-sandbox"])
                context = browser.new_context(user_agent=self.user_agent, locale="en-US")
                browsers[headless] = (browser, context)
            page = browsers[headless][1].new_page()
        except Exception as exc:
            return FetchResult(None, "", url, f"browser start failed: {_short(exc)}")
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=int(self.timeout * 2000))
            status = resp.status if resp else None
            if status is None or status < 400:
                selectors = {
                    "amazon_eg": 'div[data-component-type="s-search-result"]',
                    "amazon_com": 'div[data-component-type="s-search-result"]',
                    "jumia": "article.prd",
                    "noon": '[data-qa="plp-product-box"]',
                }
                selector = selectors.get(store)
                if selector:
                    try:
                        page.wait_for_selector(selector, timeout=min(30_000, int(self.timeout * 1000)))
                    except Exception:
                        pass  # keep the response body so the site parser can classify it
            return FetchResult(resp.status if resp else None, page.content(), page.url, None,
                               time.monotonic() - started)
        except Exception as exc:
            return FetchResult(None, "", url, f"browser error: {_short(exc)}", time.monotonic() - started)
        finally:
            page.close()
