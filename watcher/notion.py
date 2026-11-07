"""Notion: your Watchlist (you edit it, on your phone too) and the Deals log.

Setup is one token and one page. The first time, the watcher creates two databases inside
that page, Watchlist and Deals, and later adds any column that is missing. Deals has one row
per item per site: created at its first deal, then updated in place, never duplicated.
Talks to the Notion REST API directly, version 2022-06-28, which Notion keeps supporting for
databases with a single data source (the kind created here: don't add a second source).
"""
from __future__ import annotations

import copy
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import requests

from .state import read_json, write_json_atomic

log = logging.getLogger("watcher.notion")

API = "https://api.notion.com/v1"
VERSION = "2022-06-28"
SITES, WATCHLIST, DEALS = "Sites", "Watchlist", "Deals"
NUMBER = {"number": {"format": "number_with_commas"}}
PERCENT = {"number": {"format": "percent"}}


def _choices(*pairs) -> dict:
    return {"options": [{"name": name, "color": color} for name, color in pairs]}


SCHEMAS = {
    SITES: {
        "Name": {"title": {}},
        "Key": {"rich_text": {}},
        "URL": {"url": {}},
        "Enabled": {"checkbox": {}},
        "Currency": {"rich_text": {}},
    },
    WATCHLIST: {
        "Name": {"title": {}},
        "Target price": NUMBER,
        "Sites": {"multi_select": {"options": []}},
        "Priority": {"select": _choices(("high", "red"), ("normal", "yellow"), ("low", "gray"))},
        "Paused": {"checkbox": {}},
        "Exclude": {"rich_text": {}},
        "Must include": {"rich_text": {}},
        "Min price": NUMBER,
        "Max price": NUMBER,
        "Link": {"url": {}},
        "Notes": {"rich_text": {}},
        "Latest prices": {"rich_text": {}},
    },
    DEALS: {
        "Item": {"title": {}},
        "Site": {"select": {"options": []}},
        "Status": {"select": _choices(("Active", "green"), ("Ended", "gray"), ("Sold out", "red"))},
        "Price": NUMBER,
        "Previous price": NUMBER,
        "Was": NUMBER,
        "Store discount": PERCENT,
        "Below usual": PERCENT,
        "Target": NUMBER,
        "Usual price": NUMBER,
        "Lowest seen": NUMBER,
        "Real discount": {"select": _choices(("YES", "green"), ("NO", "red"), ("TOO EARLY", "gray"))},
        "Why": {"multi_select": _choices(("TARGET HIT", "orange"), ("ON SALE", "blue"), ("PRICE DROP", "purple"))},
        "In stock": {"checkbox": {}},
        "Currency": {"rich_text": {}},
        "Seller": {"rich_text": {}},
        "Listing": {"rich_text": {}},
        "Link": {"url": {}},
        "Found": {"date": {}},
        "Updated": {"date": {}},
        "Key": {"rich_text": {}},
    },
}


def notion_id(value) -> str | None:
    """A page id from a Notion link or a raw id: '...Price-Watcher-1a2b...6d?pvs=4' -> '1a2b3c4d-...'."""
    text = str(value or "").strip()
    if "://" in text:
        text = urlsplit(text).path.rstrip("/").split("/")[-1]
    match = re.search(r"([0-9a-fA-F]{32})$", text.split("?")[0].replace("-", ""))
    if not match:
        return None
    h = match.group(1).lower()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def value_of(prop):
    """Plain Python value of a Notion property as returned by the API."""
    if not isinstance(prop, dict):
        return None
    kind = prop.get("type")
    value = prop.get(kind)
    if kind in ("title", "rich_text"):
        return "".join(str(part.get("plain_text") or "") for part in value or [])
    if kind == "select":
        return value.get("name") if isinstance(value, dict) else None
    if kind == "multi_select":
        return [str(option.get("name") or "") for option in value or []]
    if kind == "date":
        return value.get("start") if isinstance(value, dict) else None
    if kind == "formula":
        return value.get(value.get("type")) if isinstance(value, dict) else None
    return value  # number, checkbox, url, email, phone_number


def _rich(text) -> list:
    text = str(text or "")
    return [{"type": "text", "text": {"content": text[:2000]}}] if text else []


class NotionError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"{status} {code}: {message}" if status else f"{code}: {message}")
        self.status, self.code = status, code


def explain(exc) -> str:
    """What to do about a Notion error, in one sentence."""
    status, text = getattr(exc, "status", 0), str(exc).lower()
    if status == 401:
        return "The token is wrong or was revoked: copy the connection's secret into NOTION_TOKEN again."
    if status == 404 or "object_not_found" in text:
        return ("The page isn't connected: open it in Notion → ••• (top right) → Connections → "
                "add your Price Watcher connection.")
    if status == 403:
        return "The connection can't edit: in its settings tick Read, Update and Insert content."
    if "multiple data sources" in text:
        return "A database got a second data source in Notion: remove it (or delete that database, it is recreated)."
    return ""


class NotionClient:
    def __init__(self, token: str = "", page: str = "", session=None, sleep=time.sleep,
                 timezone_name: str = "Africa/Cairo"):
        self.token = (token or "").strip()
        self.page_id = notion_id(page)
        self.enabled = bool(self.token and self.page_id)
        self.session = session or requests.Session()
        self.sleep = sleep
        self.tz = ZoneInfo(timezone_name)
        self.site_keys: list[str] = []
        self.db: dict[str, str] = {}
        self.props: dict[str, dict[str, tuple[str, str]]] = {}  # database -> {lower name: (name, type)}
        self.last_error: str | None = None

    # -------------------------------------------------------------------- HTTP

    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        headers = {"Authorization": f"Bearer {self.token}", "Notion-Version": VERSION,
                   "Content-Type": "application/json"}
        for attempt in range(4):
            try:
                resp = self.session.request(method, API + path, headers=headers, json=body, timeout=30)
            except requests.RequestException as exc:
                if attempt < 2:
                    self.sleep(2 * (attempt + 1))
                    continue
                self.last_error = f"network error: {exc.__class__.__name__}"
                raise NotionError(0, "network", exc.__class__.__name__) from exc
            if (resp.status_code == 429 or resp.status_code >= 500) and attempt < 3:
                try:
                    wait = float(resp.headers.get("Retry-After") or 0)
                except ValueError:
                    wait = 0.0
                self.sleep(min(max(wait, attempt + 1.0), 30.0))
                continue
            try:
                data = resp.json()
            except ValueError:
                data = {}
            if resp.status_code >= 400:
                error = NotionError(resp.status_code, str(data.get("code") or "error"),
                                    str(data.get("message") or getattr(resp, "text", ""))[:300])
                self.last_error = str(error)
                raise error
            self.last_error = None
            return data
        raise NotionError(0, "retries", "Notion kept answering 'busy'")

    def _query(self, db_id: str, body: dict):
        cursor = None
        while True:
            data = self.call("POST", f"/databases/{db_id}/query", {**body, **({"start_cursor": cursor} if cursor else {})})
            for page in data.get("results") or []:
                if not (page.get("archived") or page.get("in_trash")):
                    yield page
            if not data.get("has_more") or not data.get("next_cursor"):
                return
            cursor = data["next_cursor"]

    # ------------------------------------------------------------------- setup

    def connect(self) -> None:
        """Find the two databases inside your page; create them, or their missing columns."""
        if self.db:
            return
        if not self.enabled:
            raise NotionError(0, "not_configured", "NOTION_TOKEN and NOTION_PAGE are needed")
        found: dict[str, str] = {}
        cursor = None
        while True:
            path = f"/blocks/{self.page_id}/children?page_size=100" + (f"&start_cursor={cursor}" if cursor else "")
            data = self.call("GET", path)
            for block in data.get("results") or []:
                if block.get("type") != "child_database":
                    continue
                title = str((block.get("child_database") or {}).get("title") or "").strip().lower()
                for name in SCHEMAS:
                    if title == name.lower() and name not in found:
                        found[name] = block["id"]
            if not data.get("has_more") or not data.get("next_cursor"):
                break
            cursor = data["next_cursor"]
        ids = {}
        for name in SCHEMAS:
            ids[name] = found.get(name) or self._create(name)
            self._ensure_columns(name, ids[name])
        self.db = ids

    def _schema(self, name: str) -> dict:
        schema = copy.deepcopy(SCHEMAS[name])
        sites = [{"name": key[:100]} for key in self.site_keys if "," not in key]
        if name == WATCHLIST:
            schema["Sites"]["multi_select"]["options"] = sites
        elif name == DEALS:
            schema["Site"]["select"]["options"] = sites
        return schema

    def _create(self, name: str) -> str:
        data = self.call("POST", "/databases", {
            "parent": {"type": "page_id", "page_id": self.page_id},
            "is_inline": True,
            "title": [{"type": "text", "text": {"content": name}}],
            "properties": self._schema(name),
        })
        log.info("created the %s database in Notion", name)
        return data["id"]

    def _ensure_columns(self, name: str, db_id: str) -> None:
        data = self.call("GET", f"/databases/{db_id}")
        have = {k.lower(): (k, (v or {}).get("type", "")) for k, v in (data.get("properties") or {}).items()}
        schema = self._schema(name)
        missing = {k: v for k, v in schema.items() if k.lower() not in have and "title" not in v}
        if missing:
            self.call("PATCH", f"/databases/{db_id}", {"properties": missing})
            for column, spec in missing.items():
                have[column.lower()] = (column, next(iter(spec)))
            log.info("added %d column(s) to the %s database", len(missing), name)
        title = next((entry for entry in have.values() if entry[1] == "title"), None)
        logical_title = next(k for k, v in schema.items() if "title" in v)
        if title:
            have[logical_title.lower()] = title  # whatever you renamed the title column to
        self.props[name] = have

    # ----------------------------------------------------------------- writing

    def _value(self, kind: str, value):
        if kind == "title":
            return {"title": _rich(value)}
        if kind == "rich_text":
            return {"rich_text": _rich(value)}
        if kind == "number":
            try:
                return {"number": None if value in (None, "") else float(value)}
            except (TypeError, ValueError):
                return {"number": None}
        if kind == "select":
            return {"select": {"name": str(value)[:100]} if value else None}
        if kind == "multi_select":
            values = value if isinstance(value, (list, tuple)) else str(value or "").split(",")
            return {"multi_select": [{"name": str(v).strip()[:100]} for v in values if str(v).strip()]}
        if kind == "checkbox":
            return {"checkbox": bool(value)}
        if kind == "url":
            return {"url": str(value) if value else None}
        if kind == "date":
            return {"date": {"start": value} if value else None}
        return None  # a column whose type you changed: left alone

    def _properties(self, name: str, values: dict) -> dict:
        out = {}
        have = self.props.get(name) or {}
        for column, value in values.items():
            actual = have.get(column.lower())
            if actual:
                prop = self._value(actual[1], value)
                if prop is not None:
                    out[actual[0]] = prop
        return out

    def _when(self, ts) -> str | None:
        return datetime.fromtimestamp(float(ts), self.tz).isoformat(timespec="seconds") if ts else None

    def _deal_values(self, rec: dict) -> dict:
        def fraction(pct):
            return round(pct / 100, 4) if pct else None
        return {
            "Item": rec.get("item"), "Site": rec.get("site"), "Status": rec.get("status"),
            "Price": rec.get("price"), "Previous price": rec.get("previous_price"), "Was": rec.get("was"),
            "Store discount": fraction(rec.get("store_discount")), "Below usual": fraction(rec.get("vs_usual")),
            "Target": rec.get("target"), "Usual price": rec.get("usual"), "Lowest seen": rec.get("lowest"),
            "Real discount": rec.get("real"), "Why": rec.get("why") or [], "In stock": rec.get("in_stock"),
            "Currency": rec.get("currency"), "Seller": rec.get("seller"), "Listing": rec.get("listing"),
            "Link": rec.get("link"), "Found": self._when(rec.get("found")), "Updated": self._when(rec.get("updated")),
            "Key": rec.get("key"),
        }

    def find_deal(self, key: str) -> str | None:
        self.connect()
        column = self.props[DEALS].get("key")
        if not column or column[1] != "rich_text":
            return None
        data = self.call("POST", f"/databases/{self.db[DEALS]}/query",
                         {"filter": {"property": column[0], "rich_text": {"equals": key}}, "page_size": 1})
        results = [p for p in data.get("results") or [] if not (p.get("archived") or p.get("in_trash"))]
        return results[0]["id"] if results else None

    def create_deal(self, rec: dict) -> str:
        self.connect()
        data = self.call("POST", "/pages", {"parent": {"database_id": self.db[DEALS]},
                                            "properties": self._properties(DEALS, self._deal_values(rec))})
        return data["id"]

    def update_deal(self, page_id: str, rec: dict) -> bool:
        """Update a Deals row in place. False when the row is gone (you deleted it in Notion)."""
        self.connect()
        try:
            data = self.call("PATCH", f"/pages/{page_id}",
                             {"properties": self._properties(DEALS, self._deal_values(rec))})
        except NotionError as exc:
            text = str(exc).lower()
            if exc.status == 404 or (exc.status == 400 and ("archived" in text or "trash" in text)):
                return False
            raise
        return not (data.get("archived") or data.get("in_trash"))

    def add_watchlist_item(self, data: dict) -> str:
        self.connect()
        target = data.get("target_price")
        values = {"Name": data.get("name") or "", "Target price": None if target in ("", None) else target,
                  "Link": data.get("url") or None, "Notes": data.get("notes") or ""}
        page = self.call("POST", "/pages", {"parent": {"database_id": self.db[WATCHLIST]},
                                            "properties": self._properties(WATCHLIST, values)})
        return page["id"]

    def add_site(self, data: dict) -> str:
        self.connect()
        values = {"Name": data.get("name") or data.get("key") or "",
                  "Key": data.get("key") or "", "URL": data.get("url") or None,
                  "Enabled": data.get("enabled", True), "Currency": data.get("currency") or "EGP"}
        page = self.call("POST", "/pages", {"parent": {"database_id": self.db[SITES]},
                                            "properties": self._properties(SITES, values)})
        return page["id"]

    def set_latest(self, page_id: str, text: str) -> None:
        self.connect()
        props = self._properties(WATCHLIST, {"Latest prices": text})
        if props:
            self.call("PATCH", f"/pages/{page_id}", {"properties": props})

    # ----------------------------------------------------------------- reading

    def read_watchlist(self) -> list[dict]:
        """Your Watchlist rows, oldest first, as plain dicts (see watchlist.build_item)."""
        self.connect()
        rows = []
        order = {"page_size": 100, "sorts": [{"timestamp": "created_time", "direction": "ascending"}]}
        for n, page in enumerate(self._query(self.db[WATCHLIST], order), start=1):
            props = page.get("properties") or {}
            by_name = {k.lower(): v for k, v in props.items()}
            title = next((v for v in props.values() if (v or {}).get("type") == "title"), None)

            def get(column):
                return value_of(by_name.get(column.lower()))

            def number(column):
                value = get(column)
                return "" if value is None else value

            sites = get("Sites")
            rows.append({
                "id": get("ID") or "", "name": value_of(title) or "", "url": get("Link") or "",
                "target_price": number("Target price"), "priority": get("Priority") or "",
                "enabled": not bool(get("Paused")),
                "sites": ", ".join(sites) if isinstance(sites, list) else (sites or ""),
                "exclude": get("Exclude") or "", "must_include": get("Must include") or "",
                "min_price": number("Min price"), "max_price": number("Max price"),
                "currency": get("Currency") or "", "notes": get("Notes") or "",
                "_row": n, "_page": page.get("id", ""),
            })
        return rows

    def read_sites(self) -> list[dict]:
        """Sites to search, with enabled/currency/key fields accepted by `load_sites`."""
        self.connect()
        rows = []
        for page in self._query(self.db[SITES], {"page_size": 100}):
            props = page.get("properties") or {}
            by_name = {k.lower(): v for k, v in props.items()}
            title = next((v for v in props.values() if (v or {}).get("type") == "title"), None)

            def get(column):
                return value_of(by_name.get(column.lower()))

            enabled = get("Enabled")
            rows.append({"key": get("Key") or value_of(title) or "", "url": get("URL") or "",
                         "enabled": True if enabled is None else bool(enabled),
                         "currency": get("Currency") or ""})
        return rows


class PendingDeals:
    """Deals rows waiting for Notion, keyed by item@site: only the newest version of a row waits."""

    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "pending_deals.json"
        data = read_json(self.path, {})
        self.pending: dict[str, dict] = data if isinstance(data, dict) else {}

    def add(self, record: dict) -> None:
        self.pending[record["key"]] = record
        self.save()

    def done(self, key: str) -> None:
        self.pending.pop(key, None)

    def save(self) -> None:
        write_json_atomic(self.path, self.pending)
