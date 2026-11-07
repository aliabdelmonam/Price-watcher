"""When something is a deal, and when you hear about it: once per deal, never repeated.

A reading is a DEAL when the item is in stock and at least one of these holds:
  TARGET HIT   the price is at or below your target price
  ON SALE      the store's own 'was' price is at least store_discount_percent higher
  PRICE DROP   the price is at least drop_percent_alert below what you usually see there
               (the 14-day median; the first price seen until min_history_days of history)
With require_real_discount, ON SALE and PRICE DROP also need a YES verdict from your history.

Every item on every site has a deal episode. It opens when the item becomes a deal and
closes when it stops being one (the price went back up, or it sold out).
  Telegram  one message when a deal opens; another only if the price then falls a further
            deeper_drop_percent below the price in the last message. A deal that closes and
            comes back within repeat_after_hours at the same or a higher price reopens quietly.
  Notion    one Deals row per item per site, created at its first deal and then updated in
            place (price, status Active / Ended / Sold out). Nothing is written when nothing
            changed, so checking every 5 minutes never repeats a row or a message.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from .history import Snapshot
from .models import Item, Reading
from .notify import esc, short_title
from .prices import money, pct_off
from .state import ItemState

LABELS = {
    "ALREADY_ON_SALE": ("🏷", "ALREADY ON SALE"),
    "TARGET_HIT": ("🔥", "TARGET HIT"),
    "STORE_SALE": ("🏷", "ON SALE"),
    "BIG_DROP": ("📉", "PRICE DROP"),
    "EVEN_LOWER": ("⬇️", "EVEN LOWER"),
    "DEAL_ENDED": ("↩️", "DEAL ENDED"),
}
REASONS = {"TARGET_HIT": "TARGET HIT", "STORE_SALE": "ON SALE", "BIG_DROP": "PRICE DROP"}
SIGNATURE = ("status", "price", "was", "in_stock", "why", "seller", "listing", "link", "target", "found")


@dataclass
class Verdict:
    price: float | None           # the price when in stock, else None
    reasons: list                 # TARGET_HIT / STORE_SALE / BIG_DROP that hold right now
    at_target: bool
    store_discount: float | None  # % below the store's own 'was' price
    vs_usual: float | None        # % below the 14-day median (or the first price seen)
    baseline_kind: str            # "median" | "first" | ""
    real: str                     # YES | NO | TOO EARLY


@dataclass
class Alert:
    """One Telegram message."""

    item: Item
    reading: Reading
    snap: Snapshot
    verdict: Verdict
    headline: str
    previous_price: float | None = None  # EVEN_LOWER: the price in the last message
    extra_lines: list = field(default_factory=list)

    @property
    def label(self) -> str:
        return LABELS[self.headline][1]


@dataclass
class Outcome:
    alert: Alert | None = None   # a Telegram message to send
    record: dict | None = None   # the Notion Deals row to create or update


def assess(item: Item, r: Reading, snap: Snapshot, s) -> Verdict:
    price = r.price if (r.in_stock and r.price is not None) else None
    at_target = price is not None and item.target_price is not None and price <= item.target_price
    store_discount = pct_off(r.list_price, r.price) if r.list_price and r.price else None
    enough = snap.history_days >= s.min_history_days and snap.median is not None
    baseline = snap.median if enough else snap.first_price
    vs_usual = pct_off(baseline, price) if baseline and price is not None else None
    if enough:
        vs_median = pct_off(snap.median, price)
        real = "YES" if vs_median is not None and vs_median >= s.real_discount_percent else "NO"
    else:
        real = "TOO EARLY"
    reasons = []
    if at_target:
        reasons.append("TARGET_HIT")
    if price is not None and (store_discount or 0) >= s.store_discount_percent:
        reasons.append("STORE_SALE")
    if vs_usual is not None and vs_usual >= s.drop_percent_alert:
        reasons.append("BIG_DROP")
    if s.require_real_discount and real != "YES":
        reasons = [x for x in reasons if x == "TARGET_HIT"]
    return Verdict(price, reasons, at_target, store_discount, vs_usual,
                   "median" if enough else ("first" if baseline else ""), real)


def decide(item: Item, r: Reading, st: ItemState, snap: Snapshot, s, now: float) -> Outcome:
    """Judge one OK reading, move the deal episode on, update `st`. Returns what to send/write."""
    v = assess(item, r, snap, s)
    first = st.first_checked_at is None
    out = Outcome()
    deeper = 1 - s.deeper_drop_percent / 100
    status = None
    if v.reasons:
        status = "Active"
        if st.deal_open:
            if st.deal_alert_price and v.price <= st.deal_alert_price * deeper:
                out.alert = Alert(item, r, snap, v, "EVEN_LOWER", previous_price=st.deal_alert_price)
                st.deal_alert_price = v.price
        else:
            back_soon = (st.deal_closed_at is not None and st.deal_alert_price is not None
                         and now - st.deal_closed_at < s.repeat_after_hours * 3600
                         and v.price > st.deal_alert_price * deeper)
            st.deal_open, st.deal_closed_at = True, None
            if not back_soon:  # a new deal (a deal back soon at the same price reopens quietly)
                out.alert = Alert(item, r, snap, v, "ALREADY_ON_SALE" if first else v.reasons[0])
                st.deal_alert_price, st.deal_started_at = v.price, now
        st.deal_reasons = list(v.reasons)
    elif st.deal_open:
        st.deal_open, st.deal_closed_at = False, now
        status = "Sold out" if v.price is None else "Ended"
        if s.notify_deal_ended:
            out.alert = Alert(item, r, snap, v, "DEAL_ENDED")
    if status is not None:
        record = deal_record(item, r, snap, v, st, status, now)
        signature = json.dumps([record.get(k) for k in SIGNATURE], default=str)
        if signature != st.deal_log_sig:  # write to Notion only when something changed
            st.deal_log_sig = signature
            out.record = record
    if first:
        st.first_checked_at = now
    st.last_ok_at = now
    st.last_price = r.price
    st.last_list_price = r.list_price
    st.last_in_stock = r.in_stock
    st.last_seller = r.seller
    st.last_title = r.title or st.last_title
    return out


def deal_record(item: Item, r: Reading, snap: Snapshot, v: Verdict, st: ItemState, status: str, now: float) -> dict:
    seen = [x for x in (snap.lowest, v.price) if x is not None]
    return {
        "key": item.id, "item": item.name, "site": item.store, "status": status,
        "price": r.price if r.price is not None else st.last_price, "was": r.list_price,
        "currency": r.currency or item.currency,
        "store_discount": round(v.store_discount, 1) if v.store_discount and v.store_discount >= 1 else None,
        "vs_usual": round(v.vs_usual, 1) if v.vs_usual and v.vs_usual > 0 else None,
        "target": item.target_price, "usual": round(snap.median, 2) if snap.median else None,
        "lowest": min(seen) if seen else None, "real": v.real,
        "why": [REASONS[x] for x in st.deal_reasons], "in_stock": bool(r.in_stock),
        "seller": r.seller or "", "listing": short_title(item.listing_title or r.title, 300),
        "link": item.url, "found": st.deal_started_at, "updated": now,
    }


def format_alert(a: Alert) -> str:
    item, r, snap, v = a.item, a.reading, a.snap, a.verdict
    cur = r.currency or item.currency or ""
    emoji, label = LABELS[a.headline]
    where = f" · {esc(item.store)}" if item.parent_id else ""
    lines = [f"{emoji} <b>{label}: {esc(item.name)}</b>{where}"]
    if a.headline == "DEAL_ENDED":
        lines.append(f"Now {money(v.price)} {cur}" if v.price is not None else "Now sold out")
        lines.append(esc(item.url))
        return "\n".join(lines)
    price_line = f"<b>{money(v.price)} {cur}</b>"
    if v.store_discount and v.store_discount >= 1:
        price_line += f" (was {money(r.list_price)}, -{v.store_discount:.0f}%)"
    lines.append(price_line)
    if a.headline == "EVEN_LOWER" and a.previous_price:
        lines.append(f"Down from {money(a.previous_price)} in the last alert")
    if item.target_price is not None:
        lines.append(f"Your target: {money(item.target_price)} " + ("✅" if v.at_target else "(not reached)"))
    if v.reasons:
        lines.append("Why: " + ", ".join(REASONS[x].lower() for x in v.reasons))
    if v.baseline_kind == "median" and snap.median:
        if v.real == "YES":
            verdict = f"real discount ✅ (-{v.vs_usual:.0f}%)"
        elif v.vs_usual and v.vs_usual > 0:
            verdict = f"only -{v.vs_usual:.0f}% vs usual ⚠️"
        else:
            verdict = "not below the usual price ⚠️"
        lines.append(f"Usual price (14 days): {money(snap.median)} → {verdict}")
        if snap.lowest is not None and v.price is not None and v.price < snap.lowest:
            lines.append(f"Lowest price seen so far (previous low {money(snap.lowest)})")
    elif v.baseline_kind == "first" and v.vs_usual and v.vs_usual > 0:
        lines.append(f"-{v.vs_usual:.0f}% vs the first price seen ({money(snap.first_price)}), "
                     "not enough history yet")
    else:
        lines.append("Not enough history yet to judge the discount")
    if item.listing_title and item.listing_title.strip().casefold() != item.name.strip().casefold():
        lines.append(f"Listing: {esc(short_title(item.listing_title, 110))}")
    stock = "In stock" if r.in_stock else "Out of stock"
    if r.seller:
        stock += f" (seller: {esc(r.seller)})"
    lines.append(f"Stock: {stock}")
    lines.extend(a.extra_lines)
    lines.append(esc(item.url))
    return "\n".join(lines)
