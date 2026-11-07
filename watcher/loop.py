"""The thin loop around check_all(), shared by app.py (Space) and run_local.py (laptop)."""
from __future__ import annotations

import logging
import random
import sys
import threading
import time

from .adapters import get_adapter, store_for_url
from .commands import BotCommands
from .config import Config, load_config
from .core import check_all
from .fetch import Fetcher
from .models import Reading
from .notify import esc
from .runtime import Runtime
from .search import Matcher, pick, search_site
from .notion import NotionClient
from .sites import load_sites
from .watchlist import build_items, load_cache

log = logging.getLogger("watcher")


def setup_logging() -> None:
    for stream in (sys.stdout, sys.stderr):  # emoji in logs must never crash a Windows console
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("urllib3", "httpx", "httpcore", "huggingface_hub", "filelock"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def run_loop(rt: Runtime, stop: threading.Event, commands: BotCommands | None = None) -> None:
    commands = commands or BotCommands(rt)
    while not stop.is_set():
        start = time.time()
        force, rt.force_next_cycle = rt.force_next_cycle, False
        try:
            check_all(rt, force=force)
        except Exception as exc:  # never let one error kill the loop
            log.exception("cycle crashed")
            rt.alert_crash(exc)
        if stop.is_set():
            break
        burst = rt.in_burst()
        interval = rt.current_interval()
        jitter = min(rt.s.jitter_seconds, interval * 0.25) if burst else rt.s.jitter_seconds
        wait = max(5.0, interval - (time.time() - start)) + random.uniform(0, jitter)
        idle(rt, commands, wait, stop, burst)


def idle(rt: Runtime, commands: BotCommands, wait: float, stop: threading.Event, was_burst: bool) -> None:
    """Sleep until the next cycle while answering bot commands; wake early for /check or a burst window."""
    deadline = time.time() + wait
    while not stop.is_set():
        if rt.request_check_now:
            rt.request_check_now = False
            return
        if not was_burst and rt.in_burst():
            return
        remaining = deadline - time.time()
        if remaining <= 0:
            return
        if commands.enabled:
            if not commands.poll(timeout=int(max(1, min(remaining, 10)))):
                stop.wait(min(remaining, 5))
        else:
            stop.wait(min(remaining, 5))


class Watcher:
    def __init__(self, cfg: Config | None = None, runtime: Runtime | None = None):
        self.cfg = cfg or load_config()
        self.rt = runtime or Runtime(self.cfg)
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.error: str | None = None

    def start_background(self) -> None:
        if not self.cfg.loop_enabled:
            log.info("LOOP_ENABLED=0: serving / /health /test only, no checking loop")
            self.rt.heartbeat["mode"] = "test-only"
            return
        self.thread = threading.Thread(target=self.run, name="watcher-loop", daemon=True)
        self.thread.start()

    def run(self) -> None:
        try:
            self.rt.start()
            run_loop(self.rt, self.stop_event)
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            log.exception("watcher stopped")
            try:
                self.rt.notify(f"🔴 watcher loop stopped: {esc(self.error)}")
            except Exception:
                pass
        finally:
            self.rt.shutdown()

    def stop(self, timeout: float = 25) -> None:
        self.stop_event.set()
        self.rt.stop_requested = True
        if self.thread is not None:
            self.thread.join(timeout)

    def health(self) -> tuple[dict, bool]:
        if not self.cfg.loop_enabled:
            return {"mode": "test-only", "healthy": True, "running_on": self.cfg.running_on}, True
        data, ok = self.rt.health()
        if self.thread is not None and not self.thread.is_alive():
            ok = False
            data.update(healthy=False, error=self.error or "loop thread stopped")
        return data, ok


# ------------------------------------------------------------------ smoke test

def verdict(reading: Reading) -> str:
    error = (reading.error or "").lower()
    if reading.ok:
        return "worked" if reading.price is not None else "worked (out of stock, no price shown)"
    if "captcha" in error:
        return "captcha"
    if "blocked" in error or "access denied" in error or reading.http_status in (401, 403, 429, 503):
        return "blocked"
    if "price not found" in error or "no product data" in error or "no structured" in error:
        return "missing price"
    return "error"


def quick_items(cfg: Config):
    """The watchlist without starting the whole runtime (for --test, --dry-run, /test)."""
    rows, source = None, "watchlist.json"
    notion = NotionClient(cfg.notion_token, cfg.notion_page, timezone_name=cfg.settings.timezone)
    if notion.enabled:
        notion.site_keys = [site.key for site in load_sites(cfg.sites)[0]]
        try:
            rows, source = notion.read_watchlist(), "Watchlist"
        except Exception as exc:
            log.warning("Notion unreachable (%s); using the saved copy", exc)
            rows, source = load_cache(cfg.data_dir), "saved copy"
    problems: list[str] = []
    items = build_items(rows if rows is not None else cfg.watchlist_items, source, problems)
    for problem in problems:
        log.warning("%s", problem)
    return [i for i in items if not i.url or cfg.handles_store(i.store)]


def read_url(fetcher: Fetcher, url: str, store: str | None = None) -> tuple[Reading, float]:
    store = store or store_for_url(url)
    adapter = get_adapter(store)
    started = time.monotonic()
    result = fetcher.get(store, adapter.fetch_url(url) if "://" in url else url)
    if result.error and not result.text:
        return Reading.failed(result.error, result.status), time.monotonic() - started
    try:
        reading = adapter.parse(result.text, url, result.status)
    except Exception as exc:
        reading = Reading.failed(f"parser crashed: {exc}", result.status)
    reading.http_status = result.status
    return reading, time.monotonic() - started


def search_verdict(result) -> str:
    if result.ok:
        return f"worked ({len(result.candidates)} products)" if result.candidates else "worked (no results)"
    error = (result.error or "").lower()
    if "captcha" in error:
        return "captcha"
    if result.blocked or "blocked" in error or result.status in (401, 403, 429, 503):
        return "blocked"
    return "no products found" if "no products" in error else "error"


def matcher_for(cfg: Config, item) -> Matcher:
    return Matcher(item.query, item.must_include, item.exclude, extra_exclude=cfg.settings.exclude_words,
                   min_ratio=cfg.settings.match_min_ratio)


def smoke_test(cfg: Config, url: str | None = None, items=None, sites=None) -> list[dict]:
    """Fetch one product page per store, and search each site once:
    worked / captcha / blocked / missing price / no products found."""
    fetcher = Fetcher(cfg.settings, cfg.user_agent)
    items = items if items is not None else ([] if url else quick_items(cfg))
    if url:
        targets = [(store_for_url(url), url, "")]
    else:
        seen, targets = set(), []
        for item in items:
            if item.url and item.store not in seen:
                seen.add(item.store)
                targets.append((item.store, item.url, item.name))
    report = []
    for store, link, name in targets:
        reading, seconds = read_url(fetcher, link, store)
        report.append({
            "store": store, "item": name, "url": link, "http_status": reading.http_status,
            "verdict": verdict(reading), "price": reading.price, "was": reading.list_price,
            "in_stock": reading.in_stock if reading.ok else None, "seller": reading.seller,
            "title": (reading.title or "")[:80], "error": reading.error, "source": reading.source,
            "seconds": round(seconds, 1),
        })
    if not url:
        probe = next((i for i in items if i.is_search), None)
        query = probe.query if probe else "wireless earbuds"
        for site in sites if sites is not None else load_sites(cfg.sites)[0]:
            if not site.enabled or not cfg.handles_store(site.key):
                continue
            started = time.monotonic()
            result = search_site(fetcher, site, query)
            best = pick(result.candidates, matcher_for(cfg, probe), probe.min_price, probe.max_price)[0] \
                if probe and result.ok else None
            report.append({
                "store": site.key, "item": f"search: {query}", "url": result.url, "http_status": result.status,
                "verdict": search_verdict(result), "price": best.price if best else None,
                "was": best.list_price if best else None, "in_stock": None, "seller": None,
                "title": best.title[:80] if best else "", "error": result.error, "source": f"search:{site.kind}",
                "seconds": round(time.monotonic() - started, 1),
            })
    fetcher.close()
    return report
