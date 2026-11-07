"""Turn Watchlist rows into Items.

A row WITHOUT a url is searched on every site in watchlist.json "sites" (or only the ones in its
'sites' column); on each site the cheapest listing whose title matches the name is tracked.
A row WITH a url tracks that exact product page.
Every source is watched, together: your Notion Watchlist (DATA_DIR/watchlist_cache.json, its last
good copy, while Notion is unreachable), the "items" in watchlist.json, and items added with /add
while Notion was unreachable (DATA_DIR/watchlist_overrides.json). An item listed in several places
is watched once, with the settings of the first: Notion, then watchlist.json, then /add.
A watchlist.json item is a name, a link, or an object with the Notion column names
({"name": ..., "target_price": ...} or {"Name": ..., "Target price": ...}).
A bad row is skipped with one Telegram warning; it is picked up as soon as you fix it.
"""
from __future__ import annotations

import re
from pathlib import Path

from .adapters import default_currency, default_item_id, get_adapter, is_short_link, store_for_url
from .models import PRIORITIES, Item
from .prices import parse_amount, parse_bool
from .state import read_json, write_json_atomic

PRIORITY_ALIASES = {"medium": "normal", "med": "normal", "mid": "normal", "": "normal", "h": "high", "l": "low"}
# watchlist.json keys written like the Notion columns ("Target price", "Must include", "Link"...)
FIELD_ALIASES = {"title": "name", "product": "name", "link": "url", "target": "target_price", "site": "sites",
                 "stores": "sites", "must": "must_include", "note": "notes", "disabled": "paused"}
SOURCES = (("Watchlist", "Notion"), ("watchlist.json", "watchlist.json"), ("/add", "/add"))


def clean_id(value) -> str:
    return re.sub(r"[^a-z0-9_-]+", "-", str(value or "").strip().lower()).strip("-")[:60]


def search_id(name: str) -> str:
    return ("s-" + clean_id(name))[:60]


def split_words(value) -> tuple:
    return tuple(p.strip().lower() for p in re.split(r"[,;\n]+", str(value or "")) if p.strip())


def _amount(row: dict, column: str, where: str, problems: list[str]):
    raw = row.get(column)
    if raw in (None, ""):
        return None, True
    value = parse_amount(raw)
    if value is None:
        problems.append(f"{where}: {column} {str(raw)!r} is not a number, item skipped")
        return None, False
    return value, True


def normalize_row(row) -> dict | None:
    """A watchlist row as a dict with the field names build_item reads; None if it is not an item."""
    if isinstance(row, str):
        text = row.strip()
        if not text:
            return None
        return {"url": text} if re.match(r"https?://", text, re.I) else {"name": text}
    if not isinstance(row, dict):
        return None
    out: dict = {}
    for key, value in row.items():
        name = str(key).strip()
        if not name.startswith("_"):
            name = re.sub(r"[\s-]+", "_", name.lower())
            name = FIELD_ALIASES.get(name, name)
        if isinstance(value, (list, tuple)):  # "sites": ["noon", "jumia"]
            value = ", ".join(str(v).strip() for v in value if str(v).strip())
        out[name] = value
    if "paused" in out:
        out["enabled"] = parse_bool(out.get("enabled"), True) and not parse_bool(out.pop("paused"), False)
    return out


def source_of(item: Item) -> str:
    """Where an item came from: Notion, watchlist.json or /add."""
    return next((name for prefix, name in SOURCES if (item.row or "").startswith(prefix + " ")), "Notion")


def merge_items(*groups: list[Item]) -> list[Item]:
    """Items of several sources, each watched once: the same id, link or searched name is one item,
    and the first source's row wins."""
    items: list[Item] = []
    ids: set[str] = set()
    urls: set[str] = set()
    names: set[str] = set()
    for group in groups:
        for item in group:
            name = item.name.casefold()
            if item.id in ids or (item.url and item.url in urls) or (item.is_search and name in names):
                continue
            items.append(item)
            ids.add(item.id)
            if item.url:
                urls.add(item.url)
            if item.is_search:
                names.add(name)
    return items


def build_item(row: dict, label: str, problems: list[str], resolve=None) -> Item | None:
    url = str(row.get("url") or "").strip()
    name = " ".join(str(row.get("name") or "").split())
    if not url and not name:
        return None  # empty row
    if not parse_bool(row.get("enabled"), True):
        return None
    where = f"{label} ({name})" if name else label
    values = {}
    for column in ("target_price", "min_price", "max_price"):
        values[column], ok = _amount(row, column, where, problems)
        if not ok:
            return None
    priority = str(row.get("priority") or "").strip().lower()
    priority = PRIORITY_ALIASES.get(priority, priority)
    if priority not in PRIORITIES:
        problems.append(f"{where}: priority {priority!r} unknown, using normal")
        priority = "normal"
    shared = dict(
        target_price=values["target_price"], min_price=values["min_price"], max_price=values["max_price"],
        priority=priority, alert_on_restock=parse_bool(row.get("alert_on_restock"), True),
        notes=str(row.get("notes") or ""), only_seller=str(row.get("only_seller") or "").strip(), row=label,
        search_query=" ".join(str(row.get("query") or "").split()), sites=split_words(row.get("sites")),
        must_include=str(row.get("must_include") or "").strip(), exclude=str(row.get("exclude") or "").strip(),
        source_page=str(row.get("_page") or ""),
    )
    if not url:
        return Item(id=clean_id(row.get("id")) or search_id(name), name=name, url="", store="search",
                    currency=str(row.get("currency") or "EGP").strip().upper(), **shared)
    if not url.lower().startswith(("http://", "https://")):
        problems.append(f"{where}: url must start with https:// (paste the plain link, not a smart chip)")
        return None
    if is_short_link(url) and resolve is not None:
        url = resolve(url)
    store = store_for_url(url)
    adapter = get_adapter(store)
    original = url
    url = adapter.canonical_url(url)
    return Item(id=clean_id(row.get("id")) or default_item_id(url), name=name or adapter.name_from_url(original),
                url=url, store=store, named=bool(name),
                currency=str(row.get("currency") or default_currency(url)).strip().upper(), **shared)


def build_items(rows: list, source: str, problems: list[str], resolve=None) -> list[Item]:
    items: list[Item] = []
    seen: dict[str, str] = {}
    for index, raw in enumerate(rows or [], start=1):
        row = normalize_row(raw)
        label = f"{source} row {(row or {}).get('_row', index)}"
        if row is None:
            if raw not in (None, "", {}):
                problems.append(f"{label}: {str(raw)[:40]!r} is not an item: write a name, a link, "
                                'or an object with a "name"')
            continue
        item = build_item(row, label, problems, resolve)
        if item is None:
            continue
        if item.id in seen:
            problems.append(f"{label}: id {item.id!r} already used by {seen[item.id]}, row skipped")
            continue
        seen[item.id] = label
        items.append(item)
    return items


class Overrides:
    """Items added from Telegram while Notion was unreachable."""

    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "watchlist_overrides.json"
        self.dirty = False

    def rows(self) -> list[dict]:
        data = read_json(self.path, [])
        return data if isinstance(data, list) else []

    def add(self, row: dict) -> None:
        rows = self.rows()
        rows.append(row)
        write_json_atomic(self.path, rows)
        self.dirty = True


def save_cache(data_dir: Path, rows: list[dict]) -> Path:
    path = Path(data_dir) / "watchlist_cache.json"
    write_json_atomic(path, rows)
    return path


def load_cache(data_dir: Path) -> list[dict] | None:
    data = read_json(Path(data_dir) / "watchlist_cache.json", None)
    return data if isinstance(data, list) else None
