"""Every price reading goes to a CSV: DATA_DIR/history/<device>-<YYYY-MM-DD>.csv (UTC date).

One small file per device per day keeps each upload to the HF dataset tiny, and lets the
Space and a home device write side by side without overwriting each other. All files are
read at startup to compute each item's 14-day median, lowest price and first reading.
"""
from __future__ import annotations

import csv
import logging
import statistics
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .models import Reading

log = logging.getLogger("watcher.history")

FIELDS = ["timestamp_utc", "item_id", "store", "price", "list_price", "in_stock", "seller", "ok", "error"]


def iso_utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text: str) -> float | None:
    try:
        return datetime.fromisoformat(str(text).strip().replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _num(value: float | None) -> str:
    return "" if value is None else f"{value:.2f}".rstrip("0").rstrip(".")


class History:
    def __init__(self, data_dir: Path, running_on: str):
        self.dir = Path(data_dir) / "history"
        self.running_on = running_on
        self.dirty: set[Path] = set()
        self._buffer: list[tuple[Path, dict]] = []

    def path_for(self, ts: float) -> Path:
        day = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")
        return self.dir / f"{self.running_on}-{day}.csv"

    def add(self, ts: float, item_id: str, store: str, reading: Reading) -> None:
        self._buffer.append((self.path_for(ts), {
            "timestamp_utc": iso_utc(ts),
            "item_id": item_id,
            "store": store,
            "price": _num(reading.price),
            "list_price": _num(reading.list_price),
            "in_stock": "1" if reading.in_stock else "0",
            "seller": (reading.seller or "").replace("\n", " "),
            "ok": "1" if reading.ok else "0",
            "error": (reading.error or "")[:200],
        }))

    def flush(self) -> None:
        """Write buffered rows (once per cycle: fast, no network)."""
        if not self._buffer:
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        by_file: dict[Path, list[dict]] = {}
        for path, row in self._buffer:
            by_file.setdefault(path, []).append(row)
        for path, rows in by_file.items():
            new = not path.exists()
            with path.open("a", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=FIELDS)
                if new:
                    writer.writeheader()
                writer.writerows(rows)
            self.dirty.add(path)
        self._buffer.clear()

    def changed_since(self, ts: float) -> set:
        """This device's history files written since `ts` (scheduled --once runs sync them later)."""
        if not self.dir.is_dir():
            return set()
        return {p for p in self.dir.glob(f"{self.running_on}-*.csv") if p.stat().st_mtime >= ts}

    def rows(self):
        """All rows from every device, oldest file first."""
        if not self.dir.is_dir():
            return
        for path in sorted(self.dir.glob("*.csv")):
            try:
                with path.open(newline="", encoding="utf-8") as fh:
                    yield from csv.DictReader(fh)
            except (OSError, csv.Error) as exc:
                log.warning("skipping unreadable history file %s: %s", path.name, exc)


@dataclass
class Snapshot:
    median: float | None
    lowest: float | None
    first_price: float | None
    history_days: float
    count: int


class ItemStats:
    def __init__(self, window_days: float):
        self.window = window_days * 86400
        self.recent: deque[tuple[float, float]] = deque()
        self.lowest: float | None = None
        self.first_price: float | None = None
        self.first_ts: float | None = None
        self.count = 0

    def seen(self, ts: float) -> None:
        if self.first_ts is None or ts < self.first_ts:
            self.first_ts = ts

    def add(self, ts: float, price: float) -> None:
        self.seen(ts)
        if self.first_price is None:
            self.first_price = price
        self.lowest = price if self.lowest is None else min(self.lowest, price)
        self.recent.append((ts, price))
        self.count += 1

    def snapshot(self, now: float) -> Snapshot:
        while self.recent and self.recent[0][0] < now - self.window:
            self.recent.popleft()
        median = statistics.median(p for _, p in self.recent) if self.recent else None
        days = (now - self.first_ts) / 86400 if self.first_ts else 0.0
        return Snapshot(median, self.lowest, self.first_price, days, len(self.recent))


class StatsBook:
    def __init__(self, window_days: float = 14):
        self.window_days = window_days
        self.items: dict[str, ItemStats] = {}

    def get(self, item_id: str) -> ItemStats:
        if item_id not in self.items:
            self.items[item_id] = ItemStats(self.window_days)
        return self.items[item_id]

    def load(self, history: History) -> int:
        rows = []
        for row in history.rows():
            if row.get("ok") != "1":
                continue
            ts = parse_iso(row.get("timestamp_utc", ""))
            if ts is None:
                continue
            price = None
            if row.get("in_stock") == "1" and row.get("price"):
                try:
                    price = float(row["price"])
                except ValueError:
                    price = None
            rows.append((ts, row.get("item_id", ""), price))
        rows.sort(key=lambda r: r[0])
        for ts, item_id, price in rows:
            stats = self.get(item_id)
            if price is not None:
                stats.add(ts, price)
            else:
                stats.seen(ts)
        return len(rows)
