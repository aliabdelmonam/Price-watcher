"""The real Notion client against an in-memory imitation of the Notion API."""
import json as jsonlib
import uuid

import pytest

from helpers import FIVE_MIN, NOON_Q, R60, SITES, Harness, noon_results
from watcher.notion import API, DEALS, SITES, WATCHLIST, NotionClient, NotionError, notion_id

PAGE = "https://www.notion.so/myspace/Price-Watcher-1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d?pvs=4"
PAGE_ID = "1a2b3c4d-5e6f-7a8b-9c0d-1e2f3a4b5c6d"


class Resp:
    def __init__(self, status, data=None, headers=None):
        self.status_code, self._data, self.headers = status, data or {}, headers or {}
        self.text = jsonlib.dumps(self._data)

    def json(self):
        return self._data


class FakeAPI:
    """Just enough of api.notion.com: page children, databases, queries and pages."""

    def __init__(self):
        self.blocks = []   # databases inside the page: {"id", "title"}
        self.dbs = {}      # id -> {column: type}
        self.pages = {}    # id -> {"db", "props", "archived"}
        self.calls = []
        self.fail = []     # status codes for the next calls

    def request(self, method, url, headers=None, json=None, timeout=None):
        assert headers["Notion-Version"] == "2022-06-28" and headers["Authorization"] == "Bearer ntn_test"
        path = url[len(API):]
        self.calls.append((method, path.split("?")[0]))
        if self.fail:
            return Resp(self.fail.pop(0), {"code": "rate_limited", "message": "slow down"}, {"Retry-After": "0"})
        parts = path.split("?")[0].strip("/").split("/")
        if parts[0] == "blocks":
            results = [{"type": "child_database", "id": b["id"], "child_database": {"title": b["title"]}}
                       for b in self.blocks]
            return Resp(200, {"results": results, "has_more": False})
        if parts == ["databases"]:
            db_id = str(uuid.uuid4())
            self.dbs[db_id] = {name: next(iter(spec)) for name, spec in json["properties"].items()}
            self.blocks.append({"id": db_id, "title": json["title"][0]["text"]["content"]})
            return Resp(200, {"id": db_id})
        if parts[0] == "databases" and len(parts) == 2:
            if method == "PATCH":
                self.dbs[parts[1]].update({name: next(iter(spec)) for name, spec in json["properties"].items()})
            return Resp(200, {"properties": {n: {"type": t} for n, t in self.dbs[parts[1]].items()}})
        if parts[0] == "databases" and parts[2:] == ["query"]:
            rows = [(pid, p) for pid, p in self.pages.items() if p["db"] == parts[1] and not p["archived"]]
            if json.get("filter"):
                column, wanted = json["filter"]["property"], json["filter"]["rich_text"]["equals"]
                rows = [(pid, p) for pid, p in rows
                        if "".join(x["plain_text"] for x in p["props"].get(column, {}).get("rich_text", [])) == wanted]
            return Resp(200, {"results": [{"id": pid, "properties": p["props"]} for pid, p in rows], "has_more": False})
        if parts == ["pages"]:
            pid = str(uuid.uuid4())
            self.pages[pid] = {"db": json["parent"]["database_id"], "props": {}, "archived": False}
            self.set(pid, json["properties"])
            return Resp(200, {"id": pid})
        if parts[0] == "pages" and method == "PATCH":
            page = self.pages.get(parts[1])
            if page is None:
                return Resp(404, {"code": "object_not_found", "message": "Could not find page"})
            if page["archived"]:
                return Resp(400, {"code": "validation_error", "message": "Can't edit block that is archived."})
            self.set(parts[1], json["properties"])
            return Resp(200, {"id": parts[1]})
        return Resp(400, {"code": "invalid_request", "message": f"unexpected {method} {path}"})

    def set(self, pid, props):
        page = self.pages[pid]
        for name, value in props.items():
            kind = self.dbs[page["db"]][name]           # writing an unknown column fails the test
            raw = value[kind]
            if kind in ("title", "rich_text"):
                raw = [{"plain_text": x["text"]["content"]} for x in raw]
            page["props"][name] = {"type": kind, kind: raw}

    def rows(self, db_id):
        return [p for p in self.pages.values() if p["db"] == db_id and not p["archived"]]


def client_for(api=None):
    api = api or FakeAPI()
    sleeps = []
    client = NotionClient("ntn_test", PAGE, session=api, sleep=sleeps.append)
    client.site_keys = ["noon", "amazon_eg"]
    return client, api, sleeps


def test_page_ids_from_links():
    assert notion_id(PAGE) == PAGE_ID
    assert notion_id("1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d") == PAGE_ID == notion_id(PAGE_ID)
    assert notion_id("https://www.notion.so/1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d?v=99") == PAGE_ID
    assert notion_id("hello") is None and notion_id("") is None


def test_databases_are_created_once_and_found_afterwards():
    client, api, _ = client_for()
    client.connect()
    assert sorted(b["title"] for b in api.blocks) == ["Deals", "Sites", "Watchlist"]
    again, _, _ = client_for(api)
    again.connect()
    assert api.calls.count(("POST", "/databases")) == 3


def test_sites_can_be_added_and_read():
    client, _, _ = client_for()
    client.add_site({"name": "noon", "key": "noon", "url": "https://www.noon.com/egypt-en/",
                     "enabled": True, "currency": "EGP"})
    [site] = client.read_sites()
    assert site == {"key": "noon", "url": "https://www.noon.com/egypt-en/",
                    "enabled": True, "currency": "EGP"}


def test_missing_columns_are_added():
    api = FakeAPI()
    api.dbs["old"] = {"Item": "title", "Price": "number"}
    api.blocks.append({"id": "old", "title": "Deals"})
    client, _, _ = client_for(api)
    client.connect()
    assert {"Status", "Link", "Key", "Found", "Store discount"} <= set(api.dbs["old"])


def test_watchlist_rows_are_read():
    client, api, _ = client_for()
    client.add_watchlist_item({"name": "Soundcore R60i NC", "target_price": 1200})
    second = client.add_watchlist_item({"name": "Old one", "target_price": ""})
    api.set(second, {"Paused": {"checkbox": True}, "Sites": {"multi_select": [{"name": "noon"}, {"name": "jumia"}]}})
    first, other = client.read_watchlist()
    assert (first["name"], first["target_price"], first["enabled"], first["url"]) == ("Soundcore R60i NC", 1200, True, "")
    assert (other["enabled"], other["sites"], other["target_price"]) == (False, "noon, jumia", "")
    assert first["_page"] and other["_page"] == second


def test_deal_rows_are_found_updated_and_never_duplicated():
    client, api, _ = client_for()
    rec = {"key": "s-x@noon", "item": "X", "site": "noon", "status": "Active", "price": 1394.95, "was": 1570,
           "store_discount": 11.1, "why": ["ON SALE"], "in_stock": True, "link": "https://www.noon.com/x",
           "found": 1793990000.0, "updated": 1793990000.0}
    pid = client.create_deal(rec)
    assert client.find_deal("s-x@noon") == pid and client.find_deal("other@noon") is None
    assert client.update_deal(pid, dict(rec, price=1300)) is True
    [row] = api.rows(client.db[DEALS])
    assert row["props"]["Price"]["number"] == 1300 and row["props"]["Store discount"]["number"] == 0.111
    assert row["props"]["Found"]["date"]["start"].endswith("+02:00")
    api.pages[pid]["archived"] = True                               # you deleted the row in Notion
    assert client.update_deal(pid, rec) is False and client.find_deal("s-x@noon") is None


def test_busy_answers_are_retried_and_errors_explained():
    client, api, sleeps = client_for()
    api.fail = [429, 503]
    client.connect()
    assert len(sleeps) == 2 and client.db[WATCHLIST]
    bad, api2, _ = client_for()
    api2.fail = [401]
    with pytest.raises(NotionError) as err:
        bad.connect()
    assert err.value.status == 401


def test_the_watcher_with_real_notion_calls_writes_each_deal_once(tmp_path):
    client, api, _ = client_for()
    client.add_watchlist_item({"name": "Soundcore R60i NC", "target_price": 1200})
    setup_calls = len(api.calls)
    h = Harness(tmp_path, None, {NOON_Q: noon_results(("N70284977V", R60, 1570, 1394.95))},
                sites=SITES[:1], notion=client)
    h.cycle()
    for _ in range(10):
        h.cycle(FIVE_MIN)
    [deal] = api.rows(client.db[DEALS])
    assert deal["props"]["Status"]["select"]["name"] == "Active"
    assert deal["props"]["Key"]["rich_text"][0]["plain_text"] == "s-soundcore-r60i-nc@noon"
    assert len(h.tg.texts("ALREADY ON SALE")) == 1
    page_writes = [c for c in api.calls[setup_calls:] if c[0] in ("POST", "PATCH") and c[1].startswith("/pages")]
    assert len(page_writes) == 2                                    # the deal row + Latest prices, once each
