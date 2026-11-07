"""Search mode, the crawler: for every Watchlist item without a link, search each site in
watchlist.json "sites"; on each site take the cheapest listing whose title matches the item,
and judge it with the same rules as a tracked page (your target, the store's own 'was' price,
the price history of that item on that site). You only hear about discounts.

Each (item, site) pair has its own id ("s-soundcore-r60i-nc@noon"), state and price history.
A site that can't be searched (blocked, captcha) raises one CAN'T SEARCH alert per site, not
one per item; an item that a site simply doesn't sell is 'not listed', which is not an error.
"""
from __future__ import annotations

import logging
import random

from . import alerts
from .core import process, sanity_check
from .models import NOT_LISTED, Item, Reading
from .notify import esc
from .prices import money, pct_off
from .search import pick, search_site

log = logging.getLogger("watcher.search")


def pair_id(item: Item, site) -> str:
    return f"{item.id}@{site.key}"


def wants(item: Item, site) -> bool:
    """The item's optional 'sites' column limits which sites it is searched on."""
    if not item.sites:
        return True
    return any(w and (w == site.key or w in site.key or w in site.host) for w in item.sites)


def target_for(rt, item: Item, currency: str) -> float | None:
    """Your target in the listing's currency; fx_rates converts, without a rate there is no target."""
    if item.target_price is None:
        return None
    if not currency or currency == item.currency:
        return item.target_price
    rates = {"EGP": 1.0, **(rt.s.fx_rates or {})}
    if item.currency not in rates or currency not in rates:
        return None
    return item.target_price * rates[item.currency] / rates[currency]


def make_pair(rt, item: Item, site, listing=None) -> Item:
    currency = (listing.currency if listing else "") or site.currency
    return Item(id=pair_id(item, site), name=item.name, url=listing.url if listing else site.url,
                store=site.key, target_price=target_for(rt, item, currency), currency=currency,
                priority=item.priority, alert_on_restock=item.alert_on_restock, notes=item.notes,
                only_seller=item.only_seller, row=item.row, parent_id=item.id,
                listing_title=listing.title if listing else "")


def known_pair(rt, item: Item, site, st) -> Item:
    """The listing last tracked for item@site (after a restart: its saved link and title)."""
    pair = rt.pairs.get(pair_id(item, site))
    if pair is not None and pair.listing_title:
        return pair
    pair = make_pair(rt, item, site)
    if st.last_url:
        pair.url, pair.listing_title = st.last_url, st.last_title
    return pair


def _interleave(jobs: list) -> list:
    """Alternate sites so no site gets a burst of searches, in a different order every time."""
    groups: dict[str, list] = {}
    for job in jobs:
        groups.setdefault(job[1].key, []).append(job)
    queues = list(groups.values())
    for queue in queues:
        random.shuffle(queue)
    random.shuffle(queues)
    ordered = []
    while any(queues):
        for queue in queues:
            if queue:
                ordered.append(queue.pop())
    return ordered


def crawl(rt, started: float, burst: bool, force: bool, first_run: bool, summary: list) -> list:
    sites = rt.active_sites()
    jobs = [(item, site) for item in rt.search_items() for site in sites
            if wants(item, site) and (force or rt.is_search_due(pair_id(item, site), item.priority, started, burst))]
    cache: dict = {}
    outcome: dict[str, str | None] = {}   # site key -> None if it worked this cycle, else the error
    notes: dict[str, list[str]] = {}       # item id -> one line per site, for the 🔎 message
    results = []
    for item, site in _interleave(jobs):
        if rt.stop_requested:
            break
        key = (site.key, " ".join(item.query.lower().split()))
        if key not in cache:  # two rows with the same name share one search
            cache[key] = search_site(rt.fetcher, site, item.query)
        result = cache[key]
        now = rt.now()
        pid = pair_id(item, site)
        st = rt.state.item(pid)
        rt.heartbeat["last_progress_ts"] = now

        if not result.ok:
            outcome.setdefault(site.key, result.error)
            st.last_check_at, st.last_error = now, result.error
            reading = Reading.failed(result.error, result.status)
            rt.history.add(now, pid, site.key, reading)
            summary.append(("fail", rt.pairs.get(pid) or make_pair(rt, item, site), reading))
            notes.setdefault(item.id, []).append(f"{site.key}: can't search ({result.error})")
            log.info("%-28s %-10s SEARCH FAILED: %s", item.name[:28], site.key, result.error)
            continue
        outcome[site.key] = None

        best, _ = pick(result.candidates, rt.matcher_for(item), item.min_price, item.max_price,
                       rt.s.accessory_price_ratio, prefer_url=st.last_url,
                       keep_within_percent=rt.s.min_drop_percent)
        if best is None:
            st.last_check_at, st.last_error = now, NOT_LISTED
            st.missing_count += 1
            rt.history.add(now, pid, site.key, Reading.failed(NOT_LISTED, result.status))
            if st.deal_open and st.missing_count >= rt.s.failure_alert_after:
                gone = known_pair(rt, item, site, st)  # its deal row says Ended instead of staying Active
                rt.deliver(gone, alerts.listing_gone(gone, st, rt.stats.get(pid).snapshot(now), rt.s, now))
            pair = rt.pairs.setdefault(pid, make_pair(rt, item, site))
            summary.append(("missing", pair, None))
            notes.setdefault(item.id, []).append(f"{site.key}: not listed")
            log.info("%-28s %-10s not listed (%d results)", item.name[:28], site.key, len(result.candidates))
            continue

        st.missing_count = 0
        st.last_url = best.url
        pair = make_pair(rt, item, site, best)
        rt.pairs[pid] = pair
        reading = Reading(price=best.price, list_price=best.list_price, currency=pair.currency,
                          in_stock=best.in_stock is not False and best.price is not None, seller=best.seller,
                          title=best.title, ok=True, source=f"search:{site.kind}", http_status=result.status)
        reading = sanity_check(rt, pair, reading)
        process(rt, pair, reading, first_run, summary)
        results.append((pair, reading))
        off = pct_off(best.list_price, best.price)
        notes.setdefault(item.id, []).append(
            f"{site.key}: {money(best.price)} {pair.currency}" + (f" (-{off:.0f}%)" if off and off >= 1 else ""))
    _site_health(rt, outcome)
    _announce(rt, notes, first_run)
    return results


def _site_health(rt, outcome: dict) -> None:
    """One ⚠️ per site after N failed cycles in a row, one ✅ when it works again."""
    health = rt.state.globals.setdefault("sites", {})
    for key, error in outcome.items():
        h = health.setdefault(key, {"fails": 0, "alerted": False, "error": None})
        if error is None:
            if h.get("alerted"):
                rt.notify(f"✅ {esc(key)}: search works again")
            h.update(fails=0, alerted=False, error=None)
            continue
        h["fails"] = int(h.get("fails") or 0) + 1
        h["error"] = error
        if not h.get("alerted") and h["fails"] >= rt.s.failure_alert_after:
            h["alerted"] = True
            rt.notify(f"⚠️ <b>CAN'T SEARCH {esc(key)}</b>\n{h['fails']} checks in a row failed: {esc(error)}\n"
                      "Blocked or captcha: try the impersonate backend for this site, or search it "
                      "from the laptop (README, section 10).")


def _announce(rt, notes: dict, first_run: bool) -> None:
    """One 🔎 message when an item added after the first run has been searched everywhere once."""
    announced = set(rt.state.globals.get("announced") or [])
    fresh = [item_id for item_id in notes if item_id not in announced]
    if not fresh:
        return
    if not first_run and rt.s.announce_new_items:
        names = {i.id: i.name for i in rt.search_items()}
        for item_id in fresh:
            rt.notify(f"🔎 Now searching for <b>{esc(names.get(item_id, item_id))}</b>\n"
                      + "\n".join(esc(line) for line in notes[item_id]))
    rt.state.globals["announced"] = sorted(announced | set(fresh))
