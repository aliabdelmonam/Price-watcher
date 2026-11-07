import importlib
import importlib.util
import sys
import types
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from watcher.config import Settings
from watcher.fetch import Fetcher
from watcher.history import History, StatsBook
from watcher.models import Reading
from watcher.notify import esc, split_message
from watcher.runtime import window_active
from watcher.state import StateStore
from watcher.sync import DatasetSync
from watcher.watchlist import build_items

ROOT = Path(__file__).resolve().parent.parent


def test_watchlist_rows_are_validated():
    rows = [
        {"url": "https://www.amazon.eg/dp/B0TEST0001", "target_price": "6,500", "name": "Good"},
        {"url": "https://www.amazon.eg/dp/B0TEST0002", "target_price": "abc", "name": "Bad target"},
        {"url": "https://www.amazon.eg/dp/B0TEST0003", "enabled": False, "name": "Off"},
        {"url": "www.noon.com/egypt-en/x/N1234567A/p/", "name": "No scheme"},
        {"url": "https://www.amazon.eg/dp/B0TEST0001?tag=x", "name": "Duplicate"},
        {"url": "", "name": "empty row"},
        {"url": "https://www.jumia.com.eg/philips-kettle-123456.html", "priority": "Medium", "target_price": 7000},
    ]
    problems: list[str] = []
    items = build_items(rows, "Watchlist", problems)
    assert [i.name for i in items] == ["Good", "empty row", "philips kettle"]
    assert items[0].target_price == 6500 and items[0].id == "amz-b0test0001" and items[0].named
    assert items[1].is_search and items[1].id == "s-empty-row"        # a row without a url is searched
    assert items[2].priority == "normal" and items[2].store == "jumia" and not items[2].named
    assert len(problems) == 3


def test_html_is_escaped():
    assert esc("<b>&") == "&lt;b&gt;&amp;"


def test_burst_windows():
    tz = ZoneInfo("Africa/Cairo")

    def at(h, m, d=27):
        return datetime(2026, 11, d, h, m, tzinfo=tz)

    assert window_active("23:50-01:30", at(0, 30)) and window_active("23:50-01:30", at(23, 55))
    assert not window_active("23:50-01:30", at(12, 0))
    assert window_active("11:50-12:30", at(12, 0))
    assert window_active("2026-11-27 09:00-11:00", at(10, 0))
    assert not window_active("2026-11-27 09:00-11:00", at(10, 0, d=28))
    assert window_active("nonsense", at(1, 0)) is None


def test_stats_median_window_and_lowest(tmp_path):
    hist = History(tmp_path, "laptop")
    t0, day = 1_790_000_000, 86400
    for d, price in enumerate([5000] + [9000] * 20):
        hist.add(t0 + d * day, "x", "amazon_eg", Reading(price=price, in_stock=True, ok=True))
    hist.add(t0 + 21 * day, "x", "amazon_eg", Reading(ok=False, error="blocked"))
    hist.flush()
    book = StatsBook(14)
    assert book.load(hist) == 21
    snap = book.get("x").snapshot(t0 + 21 * day)
    assert (snap.median, snap.lowest, snap.first_price, snap.history_days) == (9000, 5000, 5000, 21)


def test_state_merges_devices_newest_wins(tmp_path):
    laptop = StateStore(tmp_path, "laptop")
    laptop.item("x").last_check_at = 100
    laptop.item("x").deal_open = True
    laptop.save()
    space = StateStore(tmp_path, "space")
    space.item("x").last_check_at = 50
    space.save()
    fresh = StateStore(tmp_path, "space")
    fresh.load()
    assert fresh.items["x"].deal_open is True


def test_dataset_restore_keeps_own_newer_files(tmp_path):
    data, remote = tmp_path / "data", tmp_path / "remote"
    (data / "history").mkdir(parents=True)
    (remote / "history").mkdir(parents=True)
    (remote / "state").mkdir()
    (remote / ".cache" / "huggingface").mkdir(parents=True)
    (data / "history" / "laptop-2026-11-06.csv").write_text("local, longer content")
    (remote / "history" / "laptop-2026-11-06.csv").write_text("short")
    (remote / "history" / "space-2026-11-07.csv").write_text("space rows")
    (remote / "state" / "space.json").write_text("{}")
    (remote / ".cache" / "huggingface" / "meta").write_text("x")
    assert DatasetSync("token", "me/repo", data, "laptop")._merge(remote) == 2
    assert (data / "history" / "laptop-2026-11-06.csv").read_text() == "local, longer content"
    assert (data / "history" / "space-2026-11-07.csv").exists() and not (data / ".cache").exists()


def test_long_messages_are_split():
    text = "\n".join(f"line {i} " + "x" * 100 for i in range(100))
    parts = split_message(text, 1000)
    assert all(len(p) <= 1000 for p in parts) and "\n".join(parts) == text


def test_fetcher_reads_saved_pages():
    page = ROOT / "tests" / "fixtures" / "synthetic_jumia.html"
    assert "Redmi" in Fetcher(Settings()).get("jumia", str(page)).text
    assert Fetcher(Settings()).get("jumia", page.as_uri()).status == 200


def test_settings_ignore_comments_and_clamp():
    s = Settings.from_dict({"_comment": "x", "interval_seconds": 5, "burst_interval_seconds": 1})
    assert s.interval_seconds == 30 and s.burst_interval_seconds == 20


def test_app_endpoints(tmp_path, monkeypatch):
    for name, value in {"LOOP_ENABLED": "0", "DATA_DIR": str(tmp_path), "TELEGRAM_TOKEN": "",
                        "NOTION_TOKEN": "", "HF_TOKEN": ""}.items():
        monkeypatch.setenv(name, value)
    sys.modules.pop("app", None)
    app = importlib.import_module("app")
    from fastapi.testclient import TestClient

    with TestClient(app.app) as client:
        assert client.get("/").text == "watcher running"
        assert client.get("/health").status_code == 200


def _load_watchdog():
    spec = importlib.util.spec_from_file_location("watchdog_script", ROOT / "scripts" / "watchdog.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeApi:
    def __init__(self, stage):
        self.stage, self.restarts = stage, 0

    def get_space_runtime(self, space_id):
        return types.SimpleNamespace(stage=self.stage)

    def restart_space(self, space_id):
        self.restarts += 1


def test_watchdog(monkeypatch):
    wd = _load_watchdog()
    for name, value in {"HF_TOKEN": "x", "SPACE_ID": "me/pw", "SPACE_URL": "https://me-pw.hf.space"}.items():
        monkeypatch.setenv(name, value)
    messages: list[str] = []
    monkeypatch.setattr(wd, "telegram", messages.append)
    api = FakeApi("SLEEPING")
    assert wd.main(api) == 0 and api.restarts == 1 and "asleep" in messages[-1]
    monkeypatch.setattr(wd, "check_health", lambda url, token: (True, "ok"))
    api, count = FakeApi("RUNNING"), len(messages)
    assert wd.main(api) == 0 and api.restarts == 0 and len(messages) == count   # healthy: silent
    monkeypatch.setattr(wd, "check_health", lambda url, token: (False, "HTTP 500"))
    api = FakeApi("RUNNING")
    assert wd.main(api) == 1 and api.restarts == 1 and "stuck" in messages[-1]
    api = FakeApi("RUNTIME_ERROR")
    assert wd.main(api) == 1 and api.restarts == 1 and "crashed" in messages[-1]


def test_history_files_keep_their_columns_and_new_ones_record_the_listing(tmp_path):
    import csv as _csv
    from datetime import datetime as _dt, timezone as _tz

    from watcher.history import History
    from watcher.models import Reading

    folder = tmp_path / "history"
    folder.mkdir()
    old = folder / "laptop-2026-11-07.csv"
    old.write_text("timestamp_utc,item_id,store,price,list_price,in_stock,seller,ok,error\r\n", encoding="utf-8")
    history = History(tmp_path, "laptop")
    ts = _dt(2026, 11, 7, 12, tzinfo=_tz.utc).timestamp()
    reading = Reading(price=1355, list_price=1800, in_stock=True, ok=True)
    history.add(ts, "s-x@jumia", "jumia", reading, url="https://www.jumia.com.eg/x-1.html")
    history.add(ts + 86400, "s-x@jumia", "jumia", reading, url="https://www.jumia.com.eg/x-1.html")
    history.flush()
    assert [len(r) for r in _csv.reader(old.open(newline="", encoding="utf-8"))] == [9, 9]   # same columns
    [row] = list(_csv.DictReader((folder / "laptop-2026-11-08.csv").open(newline="", encoding="utf-8")))
    assert (row["price"], row["url"]) == ("1355", "https://www.jumia.com.eg/x-1.html")


def test_watchlist_json_items_accept_names_links_and_notion_style_fields():
    from watcher.watchlist import build_items, source_of

    problems = []
    rows = ["Soundcore R50i NC", "https://www.amazon.eg/-/en/Speaker/dp/B0TEST0001/ref=x",
            {"Name": "Sony WH-1000XM5", "Target price": "12,000", "Sites": ["noon", "jumia"], "Must include": "xm5"},
            {"name": "Paused one", "paused": True}, 42]
    search, link, sony = build_items(rows, "watchlist.json", problems)
    assert (search.name, search.is_search, source_of(search)) == ("Soundcore R50i NC", True, "watchlist.json")
    assert (link.url, link.store) == ("https://www.amazon.eg/dp/B0TEST0001", "amazon_eg")
    assert (sony.target_price, sony.sites, sony.must_include) == (12000, ("noon", "jumia"), "xm5")
    assert len(problems) == 1 and "row 5" in problems[0]


def test_quick_items_merges_every_source(tmp_path):
    import json as _json

    from helpers import make_cfg
    from watcher.loop import quick_items
    from watcher.watchlist import source_of

    (tmp_path / "watchlist_overrides.json").write_text(_json.dumps([{"name": "Sony XM5", "url": ""}]))
    items = quick_items(make_cfg(tmp_path, items=["Soundcore R50i NC", "soundcore r50i nc"]))
    assert [(i.name, source_of(i)) for i in items] == [("Soundcore R50i NC", "watchlist.json"), ("Sony XM5", "/add")]
