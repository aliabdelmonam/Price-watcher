"""Control the watcher from Telegram. Only messages from TELEGRAM_CHAT_ID are obeyed.

The loop long-polls getUpdates while it waits between cycles, so commands answer within a
second or two and no extra server is needed. Run commands on one device only
(BOT_COMMANDS=0 on the others), because Telegram hands each message to one poller.
"""
from __future__ import annotations

import logging
import re

from .adapters import default_item_id, get_adapter, is_short_link, store_for_url
from .crawler import wants
from .notify import esc
from .prices import money, parse_amount
from .watchlist import clean_id, search_id

log = logging.getLogger("watcher.commands")

HELP = """<b>Commands</b>
/status – last check, items OK/failing, mode
/list – items with their latest prices per site
/add &lt;name&gt; [@target] – search your sites for a product, e.g. /add soundcore r60i nc @1200
/add &lt;url&gt; &lt;target&gt; [name] – track one product page
/pause – stop checking · /resume – start again
/burst on [minutes] – check high-priority items every minute (default 120 min)
/burst off – back to normal
/check – run a check now"""

ADD_USAGE = ("Usage:\n/add soundcore r60i nc @1200 – search your sites (the target is optional)\n"
             "/add https://www.amazon.eg/dp/B0XXXX 6500 [name] – track one product page")

def ago(seconds: float | None) -> str:
    if seconds is None:
        return "never"
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds} s ago"
    if seconds < 5400:
        return f"{seconds // 60} min ago"
    return f"{seconds / 3600:.1f} h ago"


class BotCommands:
    def __init__(self, rt):
        self.rt = rt
        self.enabled = rt.tg.enabled and rt.cfg.bot_commands
        self._conflict_logged = False

    # ---------------------------------------------------------------- polling

    def poll(self, timeout: int) -> bool:
        """Wait up to `timeout` s for messages; handle them. Returns False if polling failed."""
        rt = self.rt
        offset = rt.state.globals.get("tg_offset")
        updates = rt.tg.get_updates(offset, timeout=timeout)
        if updates is None:
            if "conflict" in str(rt.tg.last_error).lower() and not self._conflict_logged:
                log.warning("another device is reading bot commands; set BOT_COMMANDS=0 on one of them")
                self._conflict_logged = True
            return False
        skip_older = rt.started_at - 600 if offset is None else 0
        for update in updates:
            rt.state.globals["tg_offset"] = int(update["update_id"]) + 1
            message = update.get("message") or {}
            if str((message.get("chat") or {}).get("id")) != rt.tg.chat_id:
                continue
            if message.get("date", 0) < skip_older:
                continue  # first start: ignore old backlog
            text = (message.get("text") or "").strip()
            if text.startswith("/"):
                try:
                    reply = self.handle(text)
                except Exception as exc:
                    log.exception("command failed")
                    reply = f"⚠️ command failed: {esc(exc)}"
                if reply:
                    rt.tg.send(reply)
        if updates:
            rt.state.save()
        return True

    # --------------------------------------------------------------- commands

    def handle(self, text: str) -> str:
        parts = text.split()
        command = parts[0].split("@")[0].lower()
        args = parts[1:]
        handler = {
            "/status": self.status, "/list": self.list_items, "/add": self.add, "/pause": self.pause,
            "/resume": self.resume, "/burst": self.burst, "/check": self.check_now,
            "/help": lambda _a: HELP, "/start": lambda _a: HELP,
        }.get(command)
        return handler(args) if handler else f"Unknown command {esc(command)}.\n\n{HELP}"

    def status(self, _args) -> str:
        rt = self.rt
        hb, healthy = rt.health()
        now = rt.now()
        mode = "⏸ paused" if rt.paused else ("⚡ burst" if rt.in_burst(now) else "normal")
        until = rt.manual_burst_until()
        if until > now:
            mode += f" (manual, until {rt.local_time(until, '%H:%M')})"
        lines = [
            f"{'🟢' if healthy else '🟠'} <b>{esc(rt.cfg.running_on)}</b> · {mode} · v{hb['version']}",
            f"Last cycle: {ago(hb['seconds_since_last_cycle'])}"
            + (f", took {hb['cycle_seconds']} s" if hb.get("cycle_seconds") is not None else ""),
            f"Items: {hb['items_total']} · OK {hb['items_ok']} · failing {hb['items_failing']}",
        ]
        lines += [f"⚠️ {esc(line)}" for line in hb.get("failing") or []]
        if rt.deals.pending:
            lines.append(f"📝 {len(rt.deals.pending)} deal row(s) waiting for Notion")
        if rt.sync.last_error:
            lines.append(f"☁️ dataset sync problem: {esc(rt.sync.last_error[:120])}")
        sites = ", ".join(site.key for site in rt.active_sites())
        if rt.search_items():
            lines.append(f"Searching {len(rt.search_items())} item(s) on: {esc(sites or 'no sites')}")
        lines.append(f"Watchlist: {esc(rt.watchlist_source)}")
        return "\n".join(lines)

    def list_items(self, _args) -> str:
        rt = self.rt
        page, search = rt.active_items(), rt.search_items()
        if not page and not search:
            return "No items. Add rows to your Notion Watchlist or use /add."
        lines = [f"📋 <b>{len(page) + len(search)} items</b> ({esc(rt.watchlist_source)})"]
        for item in search:
            target = money(item.target_price) if item.target_price is not None else "none"
            parts = [esc(rt.site_status(item, site)) for site in rt.active_sites() if wants(item, site)]
            lines.append(f"{len(lines)}. 🔎 {esc(item.name)} (target {target}): " + (" · ".join(parts) or "no sites"))
        for item in page:
            st = rt.state.items.get(item.id)
            if st is None or st.last_check_at is None:
                now_part = "not checked yet"
            elif st.fail_count:
                now_part = f"⚠️ failing: {esc(st.last_error)}"
            elif not st.last_in_stock:
                now_part = "out of stock"
            else:
                hit = item.target_price is not None and st.last_price is not None and st.last_price <= item.target_price
                now_part = f"{money(st.last_price)}" + (" ✅" if hit else "")
            target = money(item.target_price) if item.target_price is not None else "none"
            flag = {"high": "🔴", "low": "⚪"}.get(item.priority, "🟡")
            lines.append(f"{len(lines)}. {flag} {esc(item.name)} · {esc(item.store)}: {now_part} (target {target})")
        return "\n".join(lines)

    def add(self, args) -> str:
        if not args:
            return ADD_USAGE
        return self._add_link(args) if re.match(r"https?://", args[0]) else self._add_search(args)

    def _save_row(self, row: dict) -> str:
        rt = self.rt
        try:
            if not rt.notion.enabled:
                raise RuntimeError("Notion is not set up")
            rt.notion.add_watchlist_item(row)
            where = "your Notion Watchlist"
        except Exception as exc:
            rt.overrides.add(row)
            where = f"a local list (Notion unavailable: {esc(str(exc)[:80])})"
        rt.reload_watchlist(force=True)
        rt.request_check_now = True
        return where

    def _add_search(self, args) -> str:
        rt = self.rt
        words, target = list(args), None
        last = words[-1]
        if last.startswith("@"):
            target = parse_amount(last[1:])
            if target is None:
                return f"⚠️ {esc(last)} is not a price.\n{ADD_USAGE}"
            words = words[:-1]
        elif len(words) >= 2 and re.fullmatch(r"[\d٠-٩][\d٠-٩,.٬٫]*", last) and (parse_amount(last) or 0) >= 100:
            target, words = parse_amount(last), words[:-1]
        name = " ".join(words).strip()
        if len(name) < 3:
            return ADD_USAGE
        for item in rt.items_all or []:
            if item.is_search and (item.id == search_id(name) or item.name.casefold() == name.casefold()):
                return f"Already searching for <b>{esc(item.name)}</b> (target {money(item.target_price)})."
        row = {"id": search_id(name), "name": name, "url": "", "target_price": "" if target is None else target,
               "currency": "EGP", "priority": "normal", "alert_on_restock": "TRUE", "enabled": "TRUE",
               "notes": "added from Telegram"}
        where = self._save_row(row)
        sites = ", ".join(s.key for s in rt.active_sites()) or "no sites (check watchlist.json)"
        goal = f"target {money(target)}" if target is not None else "no target: store discounts and price drops only"
        return f"✅ Searching for <b>{esc(name)}</b> ({goal}) on {esc(sites)}. Added to {where}. Results shortly…"

    def _add_link(self, args) -> str:
        rt = self.rt
        usage = ADD_USAGE
        if len(args) < 2:
            return usage
        target = parse_amount(args[1])
        if target is None:
            return f"⚠️ {esc(args[1])} is not a number.\n{usage}"
        url = rt.resolve_short(args[0]) if is_short_link(args[0]) else args[0]
        store = store_for_url(url)
        adapter = get_adapter(store)
        url = adapter.canonical_url(url)
        name = " ".join(args[2:]).strip()  # empty: the page title is used
        item_id = clean_id(default_item_id(url))
        for item in rt.items_all or []:
            if item.url == url or item.id == item_id:
                return f"Already tracking that as <b>{esc(item.name)}</b> (target {money(item.target_price)})."
        row = {"id": item_id, "name": name, "url": url, "target_price": target,
               "currency": getattr(adapter, "CURRENCY", "") or "EGP", "priority": "normal",
               "alert_on_restock": "TRUE", "enabled": "TRUE", "notes": "added from Telegram"}
        where = self._save_row(row)
        return (f"✅ Added <b>{esc(name or 'the product')}</b> ({esc(store)}), target {money(target)}, "
                f"to {where}. Checking it now…")

    def pause(self, _args) -> str:
        self.rt.state.globals["paused"] = True
        self.rt.state.save()
        return "⏸ Paused. I'll still answer commands. /resume to continue."

    def resume(self, _args) -> str:
        self.rt.state.globals["paused"] = False
        self.rt.state.save()
        self.rt.request_check_now = True
        return "▶️ Resumed. Checking now."

    def burst(self, args) -> str:
        rt = self.rt
        if not args or args[0].lower() not in ("on", "off"):
            return "Usage: /burst on [minutes] or /burst off"
        if args[0].lower() == "off":
            rt.state.globals["burst_until"] = 0
            rt.state.save()
            still = " (a scheduled burst window is still active)" if rt.in_burst() else ""
            return "Burst off." + still
        minutes = parse_amount(args[1]) if len(args) > 1 else None
        minutes = minutes or rt.s.manual_burst_minutes
        until = rt.now() + minutes * 60
        rt.state.globals["burst_until"] = until
        rt.state.save()
        rt.request_check_now = True
        return (f"⚡ Burst on until {rt.local_time(until, '%H:%M')}: high-priority items every "
                f"{rt.s.burst_interval_seconds:g} s, the rest at the normal interval.")

    def check_now(self, _args) -> str:
        self.rt.request_check_now = True
        self.rt.force_next_cycle = True
        return "🔎 Checking now…"
