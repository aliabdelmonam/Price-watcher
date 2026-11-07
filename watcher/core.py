"""check_all(): check every due item once, compare with what was seen before, send only
alerts that are new and real. Tracked links are read here; Watchlist rows without a link are
searched on every site by crawler.crawl(). The same function runs on the Space and the laptop.
"""
from __future__ import annotations

import logging
import random
from dataclasses import replace

from . import alerts
from .adapters import get_adapter
from .models import Item, Reading
from .notify import esc, short_title
from .prices import money, pct_off

log = logging.getLogger("watcher")


def interleave(items: list[Item], rng=random) -> list[Item]:
    """Shuffle within each store and alternate stores, so no store gets a burst or a fixed pattern."""
    groups: dict[str, list[Item]] = {}
    for item in items:
        groups.setdefault(item.store, []).append(item)
    queues = list(groups.values())
    for queue in queues:
        rng.shuffle(queue)
    rng.shuffle(queues)
    ordered = []
    while any(queues):
        for queue in queues:
            if queue:
                ordered.append(queue.pop())
    return ordered


def read_item(rt, item: Item) -> Reading:
    adapter = get_adapter(item.store)
    result = rt.fetcher.get(item.store, adapter.fetch_url(item.url))
    if result.error and not result.text:
        return Reading.failed(result.error, result.status)
    try:
        reading = adapter.parse(result.text, item.url, result.status)
    except Exception as exc:  # a parser bug must not stop the cycle
        log.exception("parser crashed for %s", item.id)
        reading = Reading.failed(f"parser crashed: {exc}", result.status)
    reading.http_status = result.status
    reading.currency = reading.currency or item.currency
    return reading


def sanity_check(rt, item: Item, reading: Reading) -> Reading:
    """Reject prices under 20% or over 300% of the item's median (accessory / installment prices)."""
    if not reading.ok or reading.price is None:
        return reading
    s = rt.s
    snap = rt.stats.get(item.id).snapshot(rt.now())
    reference = snap.median if snap.count >= 5 else None
    low = reference is not None and reading.price < reference * s.sanity_min_ratio
    high = reference is not None and reading.price > reference * s.sanity_max_ratio
    if reference is None and item.target_price and reading.price < item.target_price * s.sanity_min_ratio:
        reference, low = item.target_price, True
    if not (low or high):
        return reading
    return replace(reading, ok=False, suspicious=True,
                   error=f"suspicious price {money(reading.price)} vs usual {money(reference)}")


def effective(item: Item, reading: Reading) -> Reading:
    """With only_seller set, an offer from another seller counts as 'not available'."""
    if reading.ok and item.only_seller and reading.seller \
            and item.only_seller.lower() not in reading.seller.lower():
        return replace(reading, in_stock=False)
    return reading


def _failed(rt, item: Item, st, reading: Reading) -> None:
    st.fail_count += 1
    st.last_error = reading.error
    log.info("%-28s %-10s FAILED (%d): %s", item.name[:28], item.store, st.fail_count, reading.error)
    if st.fail_alerted:
        return
    if reading.suspicious:
        st.fail_alerted = True
        rt.notify(f"⚠️ <b>SUSPICIOUS PRICE: {esc(item.name)}</b>\nRead {money(reading.price)} "
                  f"{esc(reading.currency)}: {esc(reading.error)}.\nParsing error or a pricing glitch? "
                  f"Check it yourself:\n{esc(item.url)}", link=item.url)
    elif st.fail_count >= rt.s.failure_alert_after:
        st.fail_alerted = True
        rt.notify(f"⚠️ <b>CAN'T READ: {esc(item.name)}</b> ({esc(item.store)})\n"
                  f"{st.fail_count} failed reads in a row. Last error: {esc(reading.error)}\n{esc(item.url)}")


def _recovered(rt, item: Item, st, reading: Reading) -> None:
    if st.fail_alerted:
        if (st.last_error or "").startswith("suspicious"):
            rt.notify(f"✅ {esc(item.name)}: price looks normal again, {money(reading.price)} "
                      f"{esc(reading.currency)}")
        else:
            stock = "in stock" if reading.in_stock else "out of stock"
            rt.notify(f"✅ {esc(item.name)} is readable again: {money(reading.price)} "
                      f"{esc(reading.currency)}, {stock}")
    st.fail_alerted = False
    st.fail_count = 0
    st.last_error = None


def process(rt, item: Item, reading: Reading, first_run: bool, summary: list) -> None:
    now = rt.now()
    st = rt.state.item(item.id)
    st.last_check_at = now
    reading = effective(item, reading)
    rt.history.add(now, item.id, item.store, reading, url=item.url if item.parent_id else "")
    rt.heartbeat["last_progress_ts"] = now
    if not reading.ok:
        _failed(rt, item, st, reading)
        summary.append(("fail", item, reading))
        return
    if not item.named and reading.title:
        item.name = short_title(reading.title)
    _recovered(rt, item, st, reading)

    stats = rt.stats.get(item.id)
    snap = stats.snapshot(now)
    was_new = st.first_checked_at is None
    outcome = alerts.decide(item, reading, st, snap, rt.s, now)
    rt.deliver(item, outcome)
    if reading.in_stock and reading.price is not None:
        stats.add(now, reading.price)
    else:
        stats.seen(now)
    log.info("%-28s %-10s %10s %-12s %s", item.name[:28], item.store, money(reading.price),
             "in stock" if reading.in_stock else "OUT", outcome.alert.label if outcome.alert else "")
    if was_new and not first_run and outcome.alert is None and not item.parent_id:
        target = f"target {money(item.target_price)}" if item.target_price is not None else "no target set"
        stock = "in stock" if reading.in_stock else "out of stock"
        rt.notify(f"🆕 Now tracking <b>{esc(item.name)}</b> ({esc(item.store)}): "
                  f"{money(reading.price)} {esc(reading.currency)}, {stock}, {target}.")
    on_sale = outcome.alert is not None and outcome.alert.headline == "ALREADY_ON_SALE"
    summary.append(("sale" if on_sale else "ok", item, reading))


def first_run_summary(summary: list) -> str:
    on_sale = [(i, r) for kind, i, r in summary if kind == "sale"]
    normal = [i for kind, i, _ in summary if kind == "ok"]
    missing: dict[str, list[str]] = {}
    search_failures: dict[tuple, int] = {}
    page_failures = []
    for kind, item, reading in summary:
        if kind == "missing":
            missing.setdefault(item.name, []).append(item.store)
        elif kind == "fail" and item.parent_id:
            key = (item.store, reading.error or "error")
            search_failures[key] = search_failures.get(key, 0) + 1
        elif kind == "fail":
            page_failures.append((item, reading))
    lines = ["📋 <b>First check done</b>"]
    if on_sale:
        lines.append(f"On sale now ({len(on_sale)}):")
        for item, r in on_sale:
            bits = [f"{money(r.price)} {esc(r.currency)}"]
            if item.target_price is not None:
                bits.append(f"target {money(item.target_price)}" + (" ✅" if r.price <= item.target_price else ""))
            off = pct_off(r.list_price, r.price)
            if off and off >= 1:
                bits.append(f"-{off:.0f}% off the store's 'was' price")
            lines.append(f"• {esc(item.name)} · {esc(item.store)} · " + ", ".join(bits))
    else:
        lines.append("Nothing is at your target or visibly discounted right now.")
    lines.append(f"Not on sale: {len(normal)}")
    if missing:
        lines.append("Not listed: " + "; ".join(f"{esc(name)} at {esc(', '.join(stores))}"
                                                for name, stores in missing.items()))
    if page_failures:
        lines.append(f"Couldn't read {len(page_failures)} (alert after 3 failures in a row):")
        lines.extend(f"• {esc(i.name)} · {esc(i.store)}: {esc(r.error)}" for i, r in page_failures)
    if search_failures:
        lines.append("Couldn't search (alert after 3 failed checks in a row):")
        lines.extend(f"• {esc(store)}: {esc(error)} ({n} item{'s' if n > 1 else ''})"
                     for (store, error), n in search_failures.items())
    return "\n".join(lines)


def check_all(rt, force: bool = False) -> list[tuple[Item, Reading]]:
    """One cycle. Returns what was read (run_local --once prints it as a table)."""
    from .crawler import crawl  # imported here because crawler imports this module

    started = rt.now()
    rt.cycle_no += 1
    rt.alerts_this_cycle = 0
    rt.flush_retries()
    rt.flush_deals()
    rt.reload_watchlist()
    items = rt.active_items()
    if rt.paused and not force:
        rt.beat(mode="paused", checked=0, started=started)
        rt.periodic()
        return []
    burst = rt.in_burst(started)
    due = [item for item in items if force or rt.is_due(item, started, burst)]
    first_run = not rt.state.globals.get("first_summary_sent") and not rt.state.anything_checked()
    summary: list = []
    results = []
    for item in interleave(due):
        if rt.stop_requested:
            break
        reading = sanity_check(rt, item, read_item(rt, item))
        process(rt, item, reading, first_run, summary)
        results.append((item, reading))
    results += crawl(rt, started, burst, force, first_run, summary)
    if first_run and summary:
        rt.notify(first_run_summary(summary))
        rt.state.globals["first_summary_sent"] = True  # once, even if every read failed
    rt.history.flush()
    rt.state.save()
    rt.beat(mode="burst" if burst else "normal", checked=len(results), started=started)
    rt.periodic()
    return results
