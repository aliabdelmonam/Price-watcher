"""check_all() end to end with fake stores, Telegram and Notion: each deal is reported once."""
import json

from helpers import CAPTCHA, FIVE_MIN, Harness, amazon_page, amazon_url, cairo_ts, row
from watcher.commands import BotCommands

A1, A2, A3 = "B0TEST0001", "B0TEST0002", "B0TEST0003"
OOS = "Currently unavailable."


def set_price(h, asin, price, **kw):
    h.fetcher.pages[asin] = amazon_page(price, **kw)


# ----------------------------------------------------------------- no repeats

def test_first_run_reports_deals_already_running(tmp_path):
    h = Harness(tmp_path, [row("a1", amazon_url(A1), 6500), row("a2", amazon_url(A2), 3000),
                           row("a3", amazon_url(A3), 1000)],
                {A1: amazon_page(5999), A2: amazon_page(4000, was=6000), A3: amazon_page(1200)})
    h.cycle()
    assert len(h.tg.texts("ALREADY ON SALE")) == 2
    assert len(h.tg.texts("First check done")) == 1
    assert len(h.notion.rows_for()) == 2 and {r["status"] for r in h.notion.rows_for()} == {"Active"}
    [a2] = h.notion.rows_for("a2")
    assert (a2["price"], a2["was"], a2["store_discount"], a2["why"]) == (4000, 6000, 33.3, ["ON SALE"])
    assert a2["link"] == "https://www.amazon.eg/dp/B0TEST0002" and a2["listing"] == "Test Product"
    h.tg.sent.clear()
    writes = h.notion.writes
    for _ in range(4):
        h.cycle(FIVE_MIN)
    assert h.tg.sent == [] and h.notion.writes == writes


def test_running_every_five_minutes_never_repeats(tmp_path):
    h = Harness(tmp_path, [row("x", amazon_url(A1), 6000)], {A1: amazon_page(5800, was=7000)})
    for _ in range(30):                                          # two and a half hours
        h.cycle(FIVE_MIN)
    messages = [t for t in h.tg.sent if "First check" not in t]
    assert len(messages) == 1 and len(h.notion.rows_for()) == 1 and h.notion.writes == 1


def test_scheduled_once_runs_never_repeat(tmp_path):
    h = Harness(tmp_path, [row("x", amazon_url(A1), 6000), row("bad", amazon_url(A2), "abc")],
                {A1: amazon_page(5800), A2: amazon_page(100)}, boot=False)
    for _ in range(6):                                           # like Task Scheduler / cron every 5 minutes
        h.once_run(FIVE_MIN)
    assert len(h.tg.texts("ALREADY ON SALE")) == 1 and len(h.tg.texts("First check done")) == 1
    assert len(h.tg.texts("not a number")) == 1                  # a warning is not repeated either
    assert len(h.notion.rows_for()) == 1 and h.notion.writes == 1


def test_raising_the_target_gives_one_target_hit(tmp_path):
    h = Harness(tmp_path, [row("x", amazon_url(A1), 5000)], {A1: amazon_page(5999)})
    h.cycle()
    h.notion.rows[0]["target_price"] = 6500
    for _ in range(4):
        h.cycle(FIVE_MIN)
    [hit] = h.tg.texts("TARGET HIT")
    assert "Your target: 6,500 ✅" in hit
    [rec] = h.notion.rows_for()
    assert rec["why"] == ["TARGET HIT"] and rec["target"] == 6500


def test_price_changes_inside_a_deal_update_the_row_quietly(tmp_path):
    h = Harness(tmp_path, [row("x", amazon_url(A1), 6000)], {A1: amazon_page(6500)})
    h.cycle()
    set_price(h, A1, 5900)
    h.cycle(FIVE_MIN)
    set_price(h, A1, 5950)                                       # small change: Notion only
    h.cycle(FIVE_MIN)
    assert len(h.tg.texts("TARGET HIT")) == 1 and h.notion.rows_for()[0]["price"] == 5950
    set_price(h, A1, 5500)                                       # >= 5% under the last message
    h.cycle(FIVE_MIN)
    [lower] = h.tg.texts("EVEN LOWER")
    assert "Down from 5,900" in lower
    assert len(h.notion.rows_for()) == 1 and h.notion.rows_for()[0]["price"] == 5500


def test_deal_ends_comes_back_quietly_and_is_new_after_a_day(tmp_path):
    h = Harness(tmp_path, [row("x", amazon_url(A1), 6000)], {A1: amazon_page(6500)})
    h.cycle()
    set_price(h, A1, 5900)
    h.cycle(FIVE_MIN)
    h.fetcher.pages[A1] = amazon_page(None, stock=OOS)
    h.cycle(FIVE_MIN)
    assert h.notion.rows_for()[0]["status"] == "Sold out" and h.notion.rows_for()[0]["price"] == 5900
    for price in (5900, 6500):                                   # back at the same price, then its regular price
        set_price(h, A1, price)
        h.cycle(FIVE_MIN)
    assert len(h.tg.texts("TARGET HIT")) == 1 and h.tg.texts("DEAL ENDED") == []
    assert h.notion.rows_for()[0]["status"] == "Ended"
    h.clock.advance(25 * 3600)
    set_price(h, A1, 5900)                                       # the same deal a day later: worth telling
    h.cycle(FIVE_MIN)
    assert len(h.tg.texts("TARGET HIT")) == 2 and len(h.notion.rows_for()) == 1
    assert h.notion.rows_for()[0]["status"] == "Active"


def test_deal_ended_messages_are_optional(tmp_path):
    h = Harness(tmp_path, [row("x", amazon_url(A1), 6000)], {A1: amazon_page(5900)}, notify_deal_ended=True)
    h.cycle()
    set_price(h, A1, 6300)
    h.cycle(FIVE_MIN)
    [ended] = h.tg.texts("DEAL ENDED")
    assert "Now 6,300" in ended


def test_a_deal_row_you_delete_in_notion_stays_deleted_until_the_next_deal(tmp_path):
    h = Harness(tmp_path, [row("x", amazon_url(A1), 6000)], {A1: amazon_page(5900)})
    h.cycle()
    [pid] = list(h.notion.deals)
    h.notion.deals[pid]["_deleted"] = True
    set_price(h, A1, 5850)
    h.cycle(FIVE_MIN)
    set_price(h, A1, 6300)
    h.cycle(FIVE_MIN)
    assert h.notion.rows_for() == []
    h.clock.advance(25 * 3600)
    set_price(h, A1, 5900)
    h.cycle(FIVE_MIN)
    assert len(h.notion.rows_for()) == 1 and len(h.tg.texts("TARGET HIT")) == 1


def test_notion_down_never_blocks_telegram_and_rows_wait(tmp_path):
    h = Harness(tmp_path, [row("x", amazon_url(A1), 6000)], {A1: amazon_page(6500)})
    h.cycle()
    h.notion.fail = True
    set_price(h, A1, 5900)
    h.cycle(FIVE_MIN)
    h.cycle(FIVE_MIN)
    assert len(h.tg.texts("TARGET HIT")) == 1 and h.notion.rows_for() == []
    assert len(json.loads((h.data_dir / "pending_deals.json").read_text())) == 1
    assert len(h.tg.texts("Can't write to Notion")) == 1 and len(h.tg.texts("Can't read your Notion")) == 1
    h.notion.fail = False
    h.cycle(FIVE_MIN)
    assert len(h.notion.rows_for()) == 1 and json.loads((h.data_dir / "pending_deals.json").read_text()) == {}


def test_latest_prices_are_written_only_when_they_change(tmp_path):
    h = Harness(tmp_path, [row("x", amazon_url(A1), 5000)], {A1: amazon_page(5999, was=6200)})
    h.cycle()
    assert h.notion.latest["wl-1"] == "amazon_eg: 5,999 EGP (-3%)"
    calls = h.notion.latest_calls
    h.cycle(FIVE_MIN)
    assert h.notion.latest_calls == calls
    set_price(h, A1, 4900)
    h.cycle(FIVE_MIN)
    assert h.notion.latest["wl-1"] == "amazon_eg: 4,900 EGP ✅"


# --------------------------------------------------------------- deal rules

def test_big_drop_before_history_then_even_lower(tmp_path):
    h = Harness(tmp_path, [row("x", amazon_url(A1))], {A1: amazon_page(10000)})
    h.cycle()
    set_price(h, A1, 8000)
    h.cycle(FIVE_MIN)
    [drop] = h.tg.texts("PRICE DROP")
    assert "not enough history yet" in drop
    h.cycle(FIVE_MIN)
    assert len(h.tg.texts("PRICE DROP")) == 1
    set_price(h, A1, 7500)
    h.cycle(FIVE_MIN)
    [lower] = h.tg.texts("EVEN LOWER")
    assert "Down from 8,000" in lower


def test_real_discount_is_judged_by_own_median(tmp_path):
    h = Harness(tmp_path, [row("x", amazon_url(A1))], {A1: amazon_page(10000)})
    for _ in range(14):                                          # 3.5 days of history
        h.cycle(advance=6 * 3600)
    set_price(h, A1, 8000)
    h.cycle(advance=6 * 3600)
    [msg] = h.tg.texts("PRICE DROP")
    assert "real discount ✅" in msg and "Lowest price seen so far" in msg
    [rec] = h.notion.rows_for()
    assert rec["real"] == "YES" and rec["usual"] == 10000 and rec["vs_usual"] == 20.0


def test_require_real_discount_ignores_inflated_was_prices(tmp_path):
    h = Harness(tmp_path, [row("x", amazon_url(A1))], {A1: amazon_page(10000)}, require_real_discount=True)
    for _ in range(14):
        h.cycle(advance=6 * 3600)
    set_price(h, A1, 10000, was=15000)                           # "was 15,000" but the usual price is 10,000
    h.cycle(advance=6 * 3600)
    assert [t for t in h.tg.sent if "ON SALE" in t] == []


def test_cant_read_after_three_failures_then_readable_again(tmp_path):
    h = Harness(tmp_path, [row("x", amazon_url(A1), 5000)], {A1: CAPTCHA})
    h.cycle()
    h.cycle(FIVE_MIN)
    assert h.tg.texts("CAN'T READ") == []
    h.cycle(FIVE_MIN)
    h.cycle(FIVE_MIN)
    assert len(h.tg.texts("CAN'T READ")) == 1
    set_price(h, A1, 5500)
    h.cycle(FIVE_MIN)
    assert len(h.tg.texts("readable again")) == 1


def test_suspicious_price_is_not_trusted(tmp_path):
    h = Harness(tmp_path, [row("x", amazon_url(A1), 5000)], {A1: amazon_page(6000)})
    for _ in range(6):
        h.cycle(FIVE_MIN)
    set_price(h, A1, 900)                                        # e.g. an accessory or installment price
    h.cycle(FIVE_MIN)
    h.cycle(FIVE_MIN)
    assert len(h.tg.texts("SUSPICIOUS PRICE")) == 1 and h.tg.texts("TARGET HIT") == []
    set_price(h, A1, 6000)
    h.cycle(FIVE_MIN)
    assert len(h.tg.texts("looks normal again")) == 1


def test_only_seller_ignores_other_sellers(tmp_path):
    h = Harness(tmp_path, [row("x", amazon_url(A1), 6000, only_seller="Amazon.eg")],
                {A1: amazon_page(5000, seller="Cheap Reseller")})
    h.cycle()
    assert h.tg.texts("ALREADY ON SALE") == []
    set_price(h, A1, 5500, seller="Amazon.eg")
    h.cycle(FIVE_MIN)
    assert len(h.tg.texts("TARGET HIT")) == 1


def test_same_product_at_several_stores_names_the_cheapest(tmp_path):
    h = Harness(tmp_path, [row("a", amazon_url(A1), 6000, name="Sony XM5"), row("b", amazon_url(A2), 6000, name="Sony XM5")],
                {A1: amazon_page(5800), A2: amazon_page(6500)})
    h.cycle()
    [alert] = h.tg.texts("ALREADY ON SALE")
    assert "Others:" in alert


# -------------------------------------------------------------- scheduling

def test_low_priority_is_checked_less_often(tmp_path):
    h = Harness(tmp_path, [row("hi", amazon_url(A1), 1000, priority="high"), row("no", amazon_url(A2), 1000),
                           row("lo", amazon_url(A3), 1000, priority="low")],
                {A1: amazon_page(7000), A2: amazon_page(7000), A3: amazon_page(7000)}, interval_seconds=180)
    for _ in range(7):
        h.cycle(advance=190)
    assert (h.calls(A1), h.calls(A2), h.calls(A3)) == (7, 7, 3)


def test_burst_window_checks_high_priority_every_minute(tmp_path):
    h = Harness(tmp_path, [row("hi", amazon_url(A1), 1000, priority="high"), row("no", amazon_url(A2), 1000)],
                {A1: amazon_page(7000), A2: amazon_page(7000)}, start=cairo_ts(2026, 11, 27, 0, 0))
    assert h.rt.in_burst() and h.rt.current_interval() == 60
    for _ in range(6):
        h.cycle(advance=60)
    assert (h.calls(A1), h.calls(A2)) == (6, 2)


def test_health_goes_bad_when_the_loop_stalls(tmp_path):
    h = Harness(tmp_path, [row("x", amazon_url(A1), 5000)], {A1: amazon_page(5999)})
    h.cycle()
    data, ok = h.rt.health()
    assert ok and data["items_ok"] == 1 and data["seconds_since_last_cycle"] == 0
    h.clock.advance(3 * 300 + 31)
    assert h.rt.health()[1] is False


# ---------------------------------------------------------------- commands

def test_bot_commands(tmp_path):
    h = Harness(tmp_path, [row("x", amazon_url(A1), 5000)],
                {A1: amazon_page(5999), "B0NEWITEM1": amazon_page(4000, title="New Speaker")})
    h.cycle()
    bot = BotCommands(h.rt)
    assert "Items: 1" in bot.handle("/status")
    assert "X" in bot.handle("/list")
    reply = bot.handle("/add https://www.amazon.eg/-/en/Some-Speaker/dp/B0NEWITEM1/ref=abc 4500")
    assert "Added" in reply and "Notion Watchlist" in reply and h.rt.request_check_now
    assert h.notion.rows[-1]["url"] == "https://www.amazon.eg/dp/B0NEWITEM1"
    h.cycle(FIVE_MIN)
    [alert] = h.tg.texts("ALREADY ON SALE")
    assert "New Speaker" in alert                                # name taken from the page title
    assert "Already tracking" in bot.handle("/add https://www.amazon.eg/dp/B0NEWITEM1 4000")
    assert "not a number" in bot.handle("/add https://www.amazon.eg/dp/B0X 12abc")
    bot.handle("/pause")
    before = len(h.fetcher.calls)
    h.cycle(FIVE_MIN)
    assert len(h.fetcher.calls) == before
    bot.handle("/resume")
    h.cycle(FIVE_MIN)
    assert len(h.fetcher.calls) > before
    assert "Burst on" in bot.handle("/burst on 30") and h.rt.in_burst()
    bot.handle("/burst off")
    assert not h.rt.in_burst()
    assert "Unknown command" in bot.handle("/nope")


def test_bad_watchlist_row_warns_once_and_again_if_it_breaks_again(tmp_path):
    h = Harness(tmp_path, [row("x", amazon_url(A1), "abc")], {A1: amazon_page(5999)})
    h.cycle()
    h.cycle(FIVE_MIN)
    assert h.tg.texts("not a number") == []                      # reported at start, not again
    h.notion.rows[0]["target_price"] = 6000
    h.cycle(FIVE_MIN)
    h.notion.rows[0]["target_price"] = "abc"
    h.cycle(FIVE_MIN)
    assert len(h.tg.texts("not a number")) == 1


# -------------------------------------------------------- drops and noise

def test_the_stores_was_price_is_not_a_price_drop(tmp_path):
    h = Harness(tmp_path, [row("x", amazon_url(A1))], {A1: amazon_page(1515, was=2333)})
    h.cycle()
    [sale] = h.tg.texts("ALREADY ON SALE")
    assert "Why: on sale" in sale and "price drop" not in sale
    for price in (1529.9, 1535, 1534.9, 1534.7):                 # what amazon_eg did on Nov 7
        set_price(h, A1, price, was=2333)
        h.cycle(FIVE_MIN)
    [rec] = h.notion.rows_for()
    assert (rec["status"], rec["why"], rec["previous_price"]) == ("Active", ["ON SALE"], None)
    assert [t for t in h.tg.sent if "First check" not in t] == [sale]
    set_price(h, A1, 1535)                                       # the 'was' price is gone: the sale is over
    h.cycle(FIVE_MIN)
    assert h.notion.rows_for()[0]["status"] == "Ended"


def test_small_price_wobbles_are_not_drops_and_real_drops_are(tmp_path):
    h = Harness(tmp_path, [row("x", amazon_url(A1))], {A1: amazon_page(1535)})
    h.cycle()
    h.tg.sent.clear()
    for price in (1534.9, 1534.7, 1530, 1533):
        set_price(h, A1, price)
        h.cycle(FIVE_MIN)
    assert h.tg.sent == [] and h.notion.rows_for() == []
    set_price(h, A1, 1450)                                       # 5.4% under the regular price
    h.cycle(FIVE_MIN)
    [drop] = h.tg.texts("PRICE DROP")
    assert "Previous price: 1,533" in drop
    for price in (1449, 1440):                                   # less than 1% under the alert: Notion only
        set_price(h, A1, price)
        h.cycle(FIVE_MIN)
    assert h.tg.texts("EVEN LOWER") == [] and h.notion.rows_for()[0]["price"] == 1440
    set_price(h, A1, 1400)
    h.cycle(FIVE_MIN)
    [lower] = h.tg.texts("EVEN LOWER")
    assert "Down from 1,450 in the last alert" in lower
    set_price(h, A1, 1530)                                       # about the regular price again: over
    h.cycle(FIVE_MIN)
    assert h.notion.rows_for()[0]["status"] == "Ended" and len(h.tg.sent) == 2


def test_a_drop_lasts_until_the_price_recovers(tmp_path):
    h = Harness(tmp_path, [row("x", amazon_url(A1), 6000)], {A1: amazon_page(6500)})
    h.cycle()
    set_price(h, A1, 5900)
    h.cycle(FIVE_MIN)
    set_price(h, A1, 6300)                                       # over the target, still under the regular 6,500
    h.cycle(FIVE_MIN)
    [rec] = h.notion.rows_for()
    assert (rec["status"], rec["why"], rec["previous_price"]) == ("Active", ["PRICE DROP"], 6500)
    assert len(h.tg.texts("TARGET HIT")) == 1 and h.tg.texts("EVEN LOWER") == []


def test_saved_state_from_the_old_rules_is_repaired(tmp_path):
    h = Harness(tmp_path, [row("x", amazon_url(A1))], {A1: amazon_page(1534.7, was=2333)}, boot=False)
    day_ago = h.clock() - 86400
    old = {"first_checked_at": day_ago, "last_check_at": day_ago, "last_ok_at": day_ago, "last_price": 1534.7,
           "reference_price": 2333.0, "last_list_price": 2333.0, "last_in_stock": True, "deal_open": True,
           "deal_started_at": day_ago, "deal_alert_price": 1515.0, "deal_reasons": ["STORE_SALE", "PRICE_DROP"]}
    path = h.data_dir / "state" / "laptop.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"version": 1, "globals": {"first_summary_sent": True}, "items": {"x": old}}))
    h.rt = h.new_runtime()
    h.cycle(FIVE_MIN)
    st = h.rt.state.items["x"]
    assert (st.reference_price, st.deal_reasons, h.tg.sent) == (1534.7, ["STORE_SALE"], [])
    set_price(h, A1, 1534.7)                                     # the 'was' price goes: no endless PRICE DROP
    h.cycle(FIVE_MIN)
    assert not h.rt.state.items["x"].deal_open


# -------------------------------------------------- where the items come from

R50 = "Soundcore R50i NC True Wireless Earbuds, Black"


def noon_page():
    from helpers import R60, noon_results
    return noon_results(("N70284977V", R60, 1570, 1394.95), ("N50000000V", R50, 1100, 999))


def test_items_from_notion_and_watchlist_json_are_both_watched(tmp_path):
    h = Harness(tmp_path, [row("", "", None, name="Soundcore R60i NC")], {"noon.com/egypt-en/search/?q=": noon_page()},
                sites=["https://www.noon.com/egypt-en/"], items=["Soundcore R50i NC"])
    assert [(i.name, i.row) for i in h.rt.search_items()] == [
        ("Soundcore R60i NC", "Watchlist row 1"), ("Soundcore R50i NC", "watchlist.json row 1")]
    assert h.rt.watchlist_source == "Notion Watchlist (1) + watchlist.json (1)"
    assert "Items: Soundcore R60i NC (Notion) · Soundcore R50i NC (watchlist.json)" in h.rt.startup_message()
    h.cycle()
    assert {"s-soundcore-r60i-nc@noon", "s-soundcore-r50i-nc@noon"} <= set(h.rt.state.items)
    assert {r["key"] for r in h.notion.rows_for()} == {"s-soundcore-r60i-nc@noon", "s-soundcore-r50i-nc@noon"}


def test_an_item_in_both_places_is_watched_once_with_the_notion_settings(tmp_path):
    h = Harness(tmp_path, [row("", "", 1200, name="Soundcore R60i NC")], {}, sites=["https://www.noon.com/egypt-en/"],
                items=[{"Name": "soundcore r60i nc", "Target price": 999}])
    [item] = h.rt.search_items()
    assert (item.name, item.target_price, h.rt.watchlist_source) == ("Soundcore R60i NC", 1200, "Notion Watchlist (1)")


def test_an_empty_notion_watchlist_still_watches_watchlist_json(tmp_path):
    h = Harness(tmp_path, [], {"noon.com/egypt-en/search/?q=": noon_page()}, sites=["https://www.noon.com/egypt-en/"],
                items=["Soundcore R50i NC"])
    assert [i.name for i in h.rt.search_items()] == ["Soundcore R50i NC"]   # used to watch nothing
    h.cycle()
    assert "s-soundcore-r50i-nc@noon" in h.rt.state.items


def test_watchlist_json_edits_are_picked_up_without_a_restart(tmp_path):
    import os

    h = Harness(tmp_path, [], {}, sites=["https://www.noon.com/egypt-en/"], boot=False)
    path = h.data_dir / "watchlist.json"
    h.data_dir.mkdir(parents=True)

    def write(text, step):
        path.write_text(text, encoding="utf-8")
        stamp = 1_800_000_000_000_000_000 + step * 1_000_000_000
        os.utime(path, ns=(stamp, stamp))

    write(json.dumps({"items": ["Soundcore R50i NC"]}), 1)
    h.rt = h.new_runtime()
    assert [i.name for i in h.rt.search_items()] == ["Soundcore R50i NC"]
    write(json.dumps({"items": ["Soundcore R50i NC", {"name": "Sony WH-1000XM5", "sites": ["noon"]}]}), 2)
    h.cycle(FIVE_MIN)
    assert [i.name for i in h.rt.search_items()] == ["Soundcore R50i NC", "Sony WH-1000XM5"]
    write("{ half-saved", 3)
    h.cycle(FIVE_MIN)
    h.cycle(FIVE_MIN)
    assert len(h.tg.texts("not valid JSON")) == 1                       # one warning, items kept
    assert [i.name for i in h.rt.search_items()] == ["Soundcore R50i NC", "Sony WH-1000XM5"]
