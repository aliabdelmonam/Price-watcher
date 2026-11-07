"""What the watcher remembers between cycles and restarts.

Saved as DATA_DIR/state/<device>.json. At startup every device's file is read and, per item,
the most recently checked entry wins, so moving from the laptop to the Space (or splitting
stores across devices) never re-sends alerts that were already sent.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

log = logging.getLogger("watcher.state")


@dataclass
class ItemState:
    first_checked_at: float | None = None
    last_check_at: float | None = None
    last_ok_at: float | None = None
    last_price: float | None = None
    # the regular price: the last price that wasn't a drop. A PRICE DROP is measured from it and
    # it is kept until the price recovers. Never the store's crossed-out 'was' price.
    reference_price: float | None = None
    last_list_price: float | None = None
    last_in_stock: bool | None = None
    last_seller: str | None = None
    last_title: str = ""
    last_url: str = ""                      # search mode: the listing picked last time (kept unless beaten)
    missing_count: int = 0                  # search mode: checks in a row the listing was not on the site
    fail_count: int = 0
    fail_alerted: bool = False
    last_error: str | None = None
    # the deal episode (see alerts.py)
    deal_open: bool = False
    deal_started_at: float | None = None    # when the current (or last) deal was first reported
    deal_closed_at: float | None = None
    deal_alert_price: float | None = None   # the price in the last Telegram message about it
    deal_prev_price: float | None = None    # the regular price before this deal ('Previous price' in Notion)
    deal_reasons: list = field(default_factory=list)
    deal_log_sig: str = ""                  # what was last written to Notion: identical writes are skipped
    deal_page_id: str = ""                  # its row in the Notion Deals database
    deal_page_found: float | None = None    # the deal that row belongs to
    deal_dismissed: float | None = None     # you deleted the row for this deal in Notion

    @classmethod
    def from_dict(cls, data: dict) -> "ItemState":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (data or {}).items() if k in names})


def write_json_atomic(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as exc:
        log.warning("could not read %s: %s", path, exc)
        return default


class StateStore:
    def __init__(self, data_dir: Path, running_on: str):
        self.dir = Path(data_dir) / "state"
        self.path = self.dir / f"{running_on}.json"
        self.items: dict[str, ItemState] = {}
        self.globals: dict = {}

    def load(self) -> None:
        merged: dict[str, ItemState] = {}
        if self.dir.is_dir():
            for path in sorted(self.dir.glob("*.json")):
                data = read_json(path, {})
                for item_id, raw in (data.get("items") or {}).items():
                    entry = ItemState.from_dict(raw)
                    current = merged.get(item_id)
                    if current is None or (entry.last_check_at or 0) > (current.last_check_at or 0):
                        merged[item_id] = entry
                if path == self.path:
                    self.globals = dict(data.get("globals") or {})
        self.items = merged

    def save(self) -> None:
        write_json_atomic(self.path, {
            "version": 1,
            "globals": self.globals,
            "items": {item_id: asdict(st) for item_id, st in self.items.items()},
        })

    def item(self, item_id: str) -> ItemState:
        if item_id not in self.items:
            self.items[item_id] = ItemState()
        return self.items[item_id]

    def anything_checked(self) -> bool:
        return any(st.first_checked_at for st in self.items.values())
