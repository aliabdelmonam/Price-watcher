"""Everything one running watcher holds: components, watchlist, sites, state, heartbeat."""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import date, datetime, timedelta
from datetime import time as dtime
from zoneinfo import ZoneInfo

from . import __version__
from .alerts import format_alert, migrate_state
from .config import Config, read_watchlist_file
from .crawler import pair_id, wants
from .fetch import Fetcher
from .history import History, StatsBook, iso_utc
from .models import NOT_LISTED, Item
from .notify import Telegram, esc, short_title
from .notion import NotionClient, PendingDeals, explain
from .prices import money, pct_off
from .search import Matcher
from .sites import apply_site_rows, load_sites
from .state import StateStore
from .sync import DatasetSync
from .watchlist import Overrides, build_items, load_cache, merge_items, save_cache, source_of

log = logging.getLogger("watcher")

_WINDOW = re.compile(r"(?:(\d{4}-\d{2}-\d{2})\s+)?(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})")


def window_active(spec: str, now_local: datetime) -> bool | None:
    """'23:50-01:30' daily (may cross midnight) or '2026-11-27 09:00-11:00' on one date. None if invalid."""
    match = _WINDOW.fullmatch(str(spec).strip())
    if not match:
        return None
    day, h1, m1, h2, m2 = match.groups()
    try:
        start_t, end_t = dtime(int(h1), int(m1)), dtime(int(h2), int(m2))
        days = [date.fromisoformat(day)] if day else [now_local.date(), now_local.date() - timedelta(days=1)]
    except ValueError:
        return None
    for d in days:
        start = datetime.combine(d, start_t, tzinfo=now_local.tzinfo)
        end = datetime.combine(d, end_t, tzinfo=now_local.tzinfo)
        if end <= start:
            end += timedelta(days=1)
        if start <= now_local < end:
            return True
    return False


class Runtime:
    def __init__(self, cfg: Config, *, fetcher=None, telegram=None, notion=None, sync=None, clock=time.time):
        self.cfg = cfg
        self.s = cfg.settings
        self.tz = ZoneInfo(self.s.timezone)
        self.clock = clock
        self.fetcher = fetcher or Fetcher(self.s, cfg.user_agent)
        self.tg = telegram or Telegram(cfg.telegram_token, cfg.telegram_chat_id)
        self.notion = notion or NotionClient(cfg.notion_token, cfg.notion_page, timezone_name=self.s.timezone)
        self.sync = sync or DatasetSync(cfg.hf_token, cfg.hf_dataset, cfg.data_dir, cfg.running_on)
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        self.history = History(cfg.data_dir, cfg.running_on)
        self.state = StateStore(cfg.data_dir, cfg.running_on)
        self.stats = StatsBook(self.s.median_days)
        self.deals = PendingDeals(cfg.data_dir)
        self.overrides = Overrides(cfg.data_dir)
        self.sites, self._site_problems = load_sites(cfg.sites)
        self.notion.site_keys = [site.key for site in self.sites]
        self.items_all: list[Item] | None = None
        self.pairs: dict[str, Item] = {}   # "item@site" -> the listing found there (search mode)
        self._matchers: dict = {}
        self.watchlist_source = ""
        self.force_reload = False
        self._last_watchlist_load = 0.0
        self._last_rows_json = ""
        self._cache_dirty = False
        self._file_rows: list = list(cfg.watchlist_items)  # watchlist.json "items", re-read when it changes
        self._file_stamp: int | None = None
        self._resolved: dict[str, str] = {}
        self.retry: list[tuple[str, str | None, int]] = []
        self.started_at = self.clock()
        self._last_sync = self.started_at
        self.alerts_this_cycle = 0
        self.cycle_no = 0
        self.restore_note = ""
        self.request_check_now = False  # set by /check, /add, /resume, /burst
        self.force_next_cycle = False   # /check: check every item, ignoring the schedule
        self.stop_requested = False
        self.heartbeat = {
            "running_on": cfg.running_on, "version": __version__, "started_at": iso_utc(self.started_at),
            "last_cycle_at": None, "last_cycle_ts": None, "last_progress_ts": self.started_at,
            "cycle_seconds": None, "items_total": 0, "items_ok": 0, "items_failing": 0, "failing": [],
            "mode": "starting", "burst_mode": False, "paused": False, "sites_failing": 0,
            "checked_last_cycle": 0, "pending_notion_rows": 0, "watchlist": "",
        }

    # ------------------------------------------------------------------ basics

    def now(self) -> float:
        return self.clock()

    def local_time(self, ts: float | None = None, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
        return datetime.fromtimestamp(self.now() if ts is None else ts, self.tz).strftime(fmt)

    @property
    def paused(self) -> bool:
        return bool(self.state.globals.get("paused"))

    def manual_burst_until(self) -> float:
        return float(self.state.globals.get("burst_until") or 0)

    def in_burst(self, now: float | None = None) -> bool:
        now = self.now() if now is None else now
        if self.manual_burst_until() > now:
            return True
        local = datetime.fromtimestamp(now, self.tz)
        for spec in self.s.burst_windows or []:
            active = window_active(spec, local)
            if active is None:
                self.warn_once(f"window:{spec}", f"⚠️ burst window {spec!r} not understood "
                                                 "(use 23:50-01:30 or 2026-11-27 09:00-11:00)")
            elif active:
                return True
        return False

    def current_interval(self, now: float | None = None) -> float:
        return self.s.burst_interval_seconds if self.in_burst(now) else self.s.interval_seconds

    def is_due(self, item: Item, now: float, burst: bool) -> bool:
        st = self.state.items.get(item.id)
        if st is None or st.last_check_at is None or item.priority == "high":
            return True
        target = self.s.interval_seconds * (self.s.low_priority_every if item.priority == "low" else 1)
        slack = (self.s.burst_interval_seconds if burst else self.s.interval_seconds) / 2
        return now - st.last_check_at >= target - slack

    def is_search_due(self, pair: str, priority: str, now: float, burst: bool) -> bool:
        st = self.state.items.get(pair)
        if st is None or st.last_check_at is None:
            return True
        if burst and priority == "high":
            interval = self.s.burst_interval_seconds
        else:
            interval = self.s.search_interval_seconds * (self.s.low_priority_every if priority == "low" else 1)
        slack = min(interval, self.current_interval(now)) / 2
        return now - st.last_check_at >= interval - slack

    # --------------------------------------------------------------- lifecycle

    def start(self, announce: bool = True, quick: bool = False) -> None:
        """quick: a scheduled --once run. Uses the local data and syncs at most every 30 minutes."""
        if quick and self.state.path.exists():
            self.restore_note = "local data"
        else:
            self.restore_note = self.sync.restore()
        self.state.load()
        if migrate_state(self.state):
            log.info("saved deal state moved to the current deal rules")
        loaded = self.stats.load(self.history)
        log.info("history: %d readings loaded; %s", loaded, self.restore_note)
        for problem in self._site_problems:
            self.warn_once(f"site:{problem}", f"⚠️ watchlist.json sites: {esc(problem)}")
        self.reload_watchlist(force=True)
        if announce:
            self.notify(self.startup_message())

    def startup_message(self) -> str:
        page, search, sites = self.active_items(), self.search_items(), self.active_sites()
        per_store: dict[str, int] = {}
        for item in page:
            per_store[item.store] = per_store.get(item.store, 0) + 1
        windows = ", ".join(self.s.burst_windows or []) or "none"
        lines = [f"🟢 <b>watcher started</b> on {esc(self.cfg.running_on)} (v{__version__})"]
        if search:
            lines.append(f"Searching {len(search)} item(s) on {len(sites)} site(s): "
                         + esc(", ".join(s.key for s in sites) or "none (check watchlist.json sites)"))
        if page:
            lines.append(f"Tracking {len(page)} product link(s): "
                         + " · ".join(f"{k} {v}" for k, v in sorted(per_store.items())))
        if page or search:
            names = [f"{item.name} ({source_of(item)})" for item in search + page]
            more = f" and {len(names) - 12} more" if len(names) > 12 else ""
            lines.append("Items: " + esc(" · ".join(names[:12])) + more)
        else:
            lines.append('No items yet: add rows to your Notion Watchlist, "items" to watchlist.json, or use /add.')
        lines.append(f"Every {self.s.search_interval_seconds / 60:g} min · burst {self.s.burst_interval_seconds:g} s "
                     f"in {esc(windows)}")
        lines.append(f"Watchlist: {esc(self.watchlist_source)} · History: {esc(self.restore_note)}")
        if self.cfg.stores or self.cfg.exclude_stores:
            lines.append(f"Stores here: {esc(','.join(sorted(self.cfg.stores)) or 'all')}"
                         + (f", except {esc(','.join(sorted(self.cfg.exclude_stores)))}" if self.cfg.exclude_stores else ""))
        if self.paused:
            lines.append("⏸ Paused. Send /resume to start checking.")
        return "\n".join(lines)

    def shutdown(self, quick: bool = False) -> None:
        try:
            self.history.flush()
            self.flush_deals()
            self.state.save()
            last = float(self.state.globals.get("last_sync_ts") or 0)
            if not quick or self.now() - last >= self.s.history_sync_minutes * 60:
                if quick:
                    self.history.dirty |= self.history.changed_since(last)
                self.sync_now("shutdown")
        except Exception as exc:
            log.warning("shutdown sync failed: %s", exc)
        self.fetcher.close()

    # --------------------------------------------------------------- watchlist

    def resolve_short(self, url: str) -> str:
        if url not in self._resolved:
            self._resolved[url] = self.fetcher.resolve(url)
        return self._resolved[url]

    def reload_watchlist(self, force: bool = False) -> None:
        now = self.now()
        due = now - self._last_watchlist_load >= self.s.watchlist_refresh_minutes * 60
        if not (force or self.force_reload or due or self.items_all is None):
            return
        self.force_reload = False
        self._last_watchlist_load = now
        notion_rows, notion_label = [], "Notion Watchlist"
        if self.notion.enabled:
            try:
                notion_rows = self.notion.read_watchlist()
                self.clear_warning("notion-read")
                rows_json = json.dumps(notion_rows, sort_keys=True, default=str)
                if rows_json != self._last_rows_json:
                    save_cache(self.cfg.data_dir, notion_rows)
                    self._last_rows_json = rows_json
                    self._cache_dirty = True
            except Exception as exc:
                notion_rows = load_cache(self.cfg.data_dir) or []
                notion_label = "saved copy of your Notion Watchlist"
                self.warn_once("notion-read", f"⚠️ Can't read your Notion Watchlist ({esc(str(exc)[:150])}). "
                                              f"{esc(explain(exc))} Using the last saved copy.")
            else:
                self._reload_sites()
        # Every source is watched: the Notion Watchlist, watchlist.json "items" and /add. An item
        # in several places is watched once, with the first source's settings.
        problems: list[str] = []
        groups = [
            (notion_label, build_items(notion_rows, "Watchlist", problems, self.resolve_short)),
            ("watchlist.json", build_items(self._read_file_rows(), "watchlist.json", problems, self.resolve_short)),
            ("/add", build_items(self.overrides.rows(), "/add", problems, self.resolve_short)),
        ]
        items = merge_items(*(group for _, group in groups))
        kept = {id(item) for item in items}
        counts = [(label, sum(id(item) in kept for item in group)) for label, group in groups]
        source = " + ".join(f"{label} ({n})" for label, n in counts if n) or "no items"
        for item in items:
            st = self.state.items.get(item.id)
            if not item.named and st is not None and st.last_title:
                item.name = short_title(st.last_title)
        previous = set(self.state.globals.get("row_problems") or [])
        for fixed in previous - set(problems):
            self.clear_warning(fixed)  # a row that breaks again later is reported again
        self.state.globals["row_problems"] = sorted(set(problems))
        for problem in problems:
            self.warn_once(problem, f"⚠️ {esc(problem)}")
        self.items_all = items
        self.watchlist_source = source

    def _read_file_rows(self) -> list:
        """watchlist.json "items", read again whenever the file changes: no restart needed."""
        path = self.cfg.watchlist_file
        try:
            stamp = path.stat().st_mtime_ns
        except OSError:
            return self._file_rows
        if stamp != self._file_stamp:
            self._file_stamp = stamp
            try:
                data = read_watchlist_file(path)
                rows = data.get("items") if isinstance(data, dict) else None
                self._file_rows = list(rows) if isinstance(rows, list) else []
                self.clear_warning("watchlist.json")
            except (OSError, ValueError) as exc:
                self.warn_once("watchlist.json", f"⚠️ {esc(str(exc))}. Still watching the items read before.")
        return self._file_rows

    def _reload_sites(self) -> None:
        """watchlist.json "sites" decides which sites exist; the Notion Sites rows only switch them
        on or off, so a stale row (e.g. Amazon US) is never searched and a row that doesn't match
        exactly never drops a configured site. A Sites problem never stops the Watchlist."""
        if not hasattr(self.notion, "read_sites"):
            return
        try:
            rows = self.notion.read_sites()
        except Exception as exc:
            log.warning("can't read the Notion Sites database (%s); searching the watchlist.json sites", exc)
            return
        configured, self._site_problems = load_sites(self.cfg.sites)
        self.sites = apply_site_rows(configured, rows)
        self.notion.site_keys = [site.key for site in self.sites]

    def active_items(self) -> list[Item]:
        """Rows with a link, for the stores this device checks."""
        return [i for i in (self.items_all or []) if i.url and self.cfg.handles_store(i.store)]

    def search_items(self) -> list[Item]:
        """Rows without a link: searched on every site."""
        return [i for i in (self.items_all or []) if i.is_search]

    def active_sites(self) -> list:
        return [s for s in self.sites if s.enabled and self.cfg.handles_store(s.key)]

    def matcher_for(self, item: Item) -> Matcher:
        key = (item.query, item.must_include, item.exclude)
        if key not in self._matchers:
            self._matchers[key] = Matcher(item.query, item.must_include, item.exclude,
                                          extra_exclude=self.s.exclude_words, min_ratio=self.s.match_min_ratio)
        return self._matchers[key]

    # ---------------------------------------------------------------- messages

    def notify(self, text: str, link: str | None = None) -> bool:
        ok = self.tg.send(text, link=link)
        if not ok and self.tg.enabled:
            self.retry.append((text, link, 1))
        return ok

    def flush_retries(self) -> None:
        queued, self.retry = self.retry, []
        for text, link, attempts in queued:
            if not self.tg.send(text, link=link) and attempts < 3:
                self.retry.append((text, link, attempts + 1))

    def warn_once(self, key: str, text: str) -> None:
        """Tell you about a problem once, even across restarts; again only after it was fixed."""
        warned = self.state.globals.setdefault("warned", {})
        if key in warned:
            return
        warned[key] = self.now()
        log.warning("%s", text)
        self.notify(text)

    def clear_warning(self, key: str) -> None:
        (self.state.globals.get("warned") or {}).pop(key, None)

    def alert_crash(self, exc: Exception) -> None:
        if self.now() - float(self.state.globals.get("last_crash_alert") or 0) < 1800:
            return
        self.state.globals["last_crash_alert"] = self.now()
        self.notify(f"💥 cycle crashed: {esc(type(exc).__name__)}: {esc(str(exc)[:300])}\n"
                    "The loop keeps running; check the logs.")

    # ---------------------------------------------------------- status texts

    def site_status(self, item: Item, site) -> str:
        """'noon 1,394.95 EGP (-11%)', 'amazon_eg not listed', ... for one site of a search item."""
        st = self.state.items.get(pair_id(item, site))
        if st is None or st.last_check_at is None:
            return f"{site.key} not searched yet"
        if st.last_error == NOT_LISTED:
            return f"{site.key} not listed"
        if st.last_error:
            return f"{site.key} can't search"
        pair = self.pairs.get(pair_id(item, site))
        return f"{site.key} " + self._price_text(st, pair.target_price if pair else item.target_price,
                                                 pair.currency if pair else site.currency)

    def link_status(self, item: Item) -> str:
        st = self.state.items.get(item.id)
        if st is None or st.last_check_at is None:
            return "not checked yet"
        if st.fail_count:
            return "can't read the page"
        return self._price_text(st, item.target_price, item.currency)

    @staticmethod
    def _price_text(st, target, currency: str) -> str:
        if not st.last_in_stock or st.last_price is None:
            return "out of stock"
        text = f"{money(st.last_price)} {currency}".rstrip()
        off = pct_off(st.last_list_price, st.last_price)
        if off and off >= 1:
            text += f" (-{off:.0f}%)"
        if target is not None and st.last_price <= target:
            text += " ✅"
        return text

    def latest_text(self, item: Item) -> str:
        if item.is_search:
            parts = [self.site_status(item, site) for site in self.active_sites() if wants(item, site)]
            return " · ".join(parts) or "no sites"
        return f"{item.store}: {self.link_status(item)}"

    def siblings_lines(self, item: Item, mine: float | None) -> list[str]:
        """Same product name at other stores or sites -> tell which one is cheapest."""
        key = item.name.strip().casefold()
        pool = [i for i in self.items_all or [] if i.url] + list(self.pairs.values())
        others = [i for i in pool if i.id != item.id and i.name.strip().casefold() == key]
        if not others:
            return []
        parts, cheapest = [], mine is not None
        for other in others:
            st = self.state.items.get(other.id)
            if st is None or st.last_check_at is None:
                parts.append(f"{other.store} not checked yet")
            elif st.last_error == NOT_LISTED:
                parts.append(f"{other.store} not listed")
            elif st.last_error or st.last_ok_at is None:
                parts.append(f"{other.store} can't read")
            elif st.last_in_stock and st.last_price:
                cheaper = mine is not None and st.last_price < mine
                cheapest = cheapest and not cheaper
                parts.append(f"{other.store} {money(st.last_price)}" + (" (cheaper)" if cheaper else ""))
            else:
                parts.append(f"{other.store} out of stock")
        head = f"✅ Cheapest of {len(others) + 1} places. " if cheapest else ""
        return [head + "Others: " + " · ".join(esc(p) for p in parts)]

    # ------------------------------------------------------- Telegram + Notion

    def deliver(self, item: Item, outcome) -> None:
        """Telegram first (seconds matter), then Notion. A Notion problem never delays Telegram."""
        if outcome.alert is not None:
            outcome.alert.extra_lines = self.siblings_lines(item, outcome.alert.verdict.price)
            self.notify(format_alert(outcome.alert), link=item.url)
            self.alerts_this_cycle += 1
        if outcome.record is not None and self.notion.enabled:
            self.deals.add(outcome.record)
            self.flush_deals()

    def flush_deals(self) -> None:
        """Write waiting Deals rows. Each item@site has one row: found, then updated, never duplicated."""
        if not self.notion.enabled or not self.deals.pending:
            return
        for key in list(self.deals.pending):
            try:
                self._write_deal(self.deals.pending[key])
            except Exception as exc:
                log.warning("Notion write failed, %d deal row(s) waiting: %s", len(self.deals.pending), exc)
                self.warn_once("notion-write", f"⚠️ Can't write to Notion ({esc(str(exc)[:150])}). "
                                               f"{esc(explain(exc))} Telegram alerts keep coming; the Deals "
                                               "rows are written when it works again.")
                break
            self.deals.done(key)
        else:
            self.clear_warning("notion-write")
        self.deals.save()

    def _write_deal(self, rec: dict) -> None:
        st = self.state.item(rec["key"])
        if st.deal_dismissed is not None and st.deal_dismissed == rec.get("found"):
            return  # you deleted this deal's row in Notion: it stays deleted until the next deal
        page = st.deal_page_id or self.notion.find_deal(rec["key"])
        if page:
            if self.notion.update_deal(page, rec):
                st.deal_page_id, st.deal_page_found = page, rec.get("found")
                return
            st.deal_page_id = ""
            if st.deal_page_found == rec.get("found"):
                st.deal_dismissed = rec.get("found")
                return
        st.deal_page_id = self.notion.create_deal(rec)
        st.deal_page_found = rec.get("found")

    def update_latest(self) -> None:
        """Each Notion Watchlist row's 'Latest prices', written only when it changed."""
        if not self.notion.enabled:
            return
        written = self.state.globals.setdefault("latest", {})
        current = {i.source_page for i in self.items_all or [] if i.source_page}
        for page in [p for p in written if p not in current]:
            del written[page]
        for item in self.items_all or []:
            if not item.source_page:
                continue
            text = self.latest_text(item)
            if written.get(item.source_page) == text:
                continue
            try:
                self.notion.set_latest(item.source_page, text)
            except Exception as exc:
                log.warning("could not update Latest prices of %s: %s", item.name, exc)
                if getattr(exc, "status", 0) not in (400, 404):
                    return  # Notion unreachable: next cycle
            written[item.source_page] = text

    # ------------------------------------------------------------ housekeeping

    def beat(self, *, mode: str, checked: int, started: float) -> None:
        items = self.active_items()
        failing = [i for i in items if (self.state.items.get(i.id) and self.state.items[i.id].fail_count)]
        health = self.state.globals.get("sites") or {}
        bad_sites = [f"{key} search: {h.get('error')}" for key, h in health.items() if h.get("fails")]
        total = len(items) + len(self.search_items())
        now = self.now()
        self.heartbeat.update({
            "last_cycle_at": iso_utc(now), "last_cycle_ts": now, "last_progress_ts": now,
            "cycle_seconds": round(now - started, 1), "items_total": total,
            "items_ok": total - len(failing), "items_failing": len(failing), "sites_failing": len(bad_sites),
            "failing": ([f"{i.name} ({i.store}): {self.state.items[i.id].last_error}" for i in failing]
                        + bad_sites)[:10],
            "mode": mode, "burst_mode": mode == "burst", "paused": self.paused, "checked_last_cycle": checked,
            "pending_notion_rows": len(self.deals.pending), "watchlist": self.watchlist_source,
        })

    def health(self) -> tuple[dict, bool]:
        now = self.now()
        hb = dict(self.heartbeat)
        last = hb.get("last_cycle_ts")
        hb["seconds_since_last_cycle"] = round(now - last, 1) if last else None
        limit = 3 * self.s.interval_seconds + self.s.jitter_seconds
        if last is None:
            healthy = now - self.started_at < max(900, limit)
        else:
            healthy = now - float(hb.get("last_progress_ts") or last) <= limit
        hb["healthy"] = healthy
        return hb, healthy

    def periodic(self) -> None:
        self.flush_deals()
        self.update_latest()
        if self.now() - self._last_sync >= self.s.history_sync_minutes * 60:
            self.sync_now("periodic")
        elif self.alerts_this_cycle and self.sync.enabled:
            self.sync.push([self.state.path], f"state after alerts ({self.cfg.running_on})")
        self.state.save()

    def sync_now(self, why: str) -> None:
        self._last_sync = self.now()
        if not self.sync.enabled:
            return
        self.state.globals["last_sync_ts"] = self.now()
        self.state.save()
        paths = set(self.history.dirty) | {self.state.path}
        if self.overrides.dirty:
            paths.add(self.overrides.path)
        if self._cache_dirty:
            paths.add(self.cfg.data_dir / "watchlist_cache.json")
        if self.sync.push(paths, f"{why} sync from {self.cfg.running_on} at {self.local_time()}"):
            self.history.dirty.clear()
            self.overrides.dirty = False
            self._cache_dirty = False
