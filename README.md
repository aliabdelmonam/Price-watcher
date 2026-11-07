---
title: price-watcher
emoji: 🏷️
colorFrom: green
colorTo: blue
sdk: docker
app_port: 7860
pinned: false
---

# Black Friday price watcher

You list the products you want by **name** in Notion, and the **sites** to look on. Every 5 minutes a crawler searches every site for every item. It finds the listing that really is your product (any color or seller, never a case or a spare part). It tells you **only about discounts, and only once**: one Telegram message, plus one row in a Notion **Deals** database that stays up to date. Everything runs on free tiers.

| part | where | job |
|---|---|---|
| Crawler | Hugging Face Space (Docker), or your laptop | loops over items × sites, decides what's a discount |
| Notifier | Telegram bot | one message per deal; `/commands` |
| Sites + Watchlist + Deals | Notion | Sites and products you edit from your phone; Deals: one row per item per site, kept current |
| History | private HF dataset | every price seen; judges whether a discount is real |
| Keep-alive + watchdog | GitHub Actions | hourly: keeps the Space awake, restarts it if asleep, crashed or stuck |

> Keep the YAML block at the top of this file: Hugging Face reads it to build the Space.

**Timeline:**
- Tonight (Nov 6): sections 1–5, on the laptop.
- Nov 7: sections 6–7 (Space and watchdog).
- Nov 8: tune matches and sites (section 10).
- Nov 9–10: drills (section 9).
- Nov 11–25: daily routine.
- Nov 26–30: burst mode.
- Dec 1: `/pause`.

## What counts as a discount, and why you never get repeats

Every 5 minutes, for each item on each site, the crawler searches, keeps the listings whose title matches, and takes the cheapest one in stock. That listing is a **deal** if any of these is true:

- **TARGET HIT:** it's at or below your `Target price`.
- **ON SALE:** the site shows its own "was" price higher than the current price.
- **PRICE DROP:** the price fell at least 1% (`min_drop_percent`) below its regular price on that site: the last price that wasn't a drop, never the store's own "was" price. It stays a deal until the price comes back up. This catches sales the store doesn't label.

Price moves under 1% are noise (Amazon prices wobble by a few piastres), so they never send anything.

Every alert also says whether the discount is **real**: compared with your own history, not the store's possibly inflated "was" price.

**No repeats, by design.** Each item on each site has one deal at a time:

- **Telegram:** one message when the deal starts. Another when the price falls at least 1% below the price in the last message ("⬇️ EVEN LOWER").
- **Notion:** one row in **Deals** per item per site, created at its first deal and then **updated in place**: price, was-price, discount, status Active / Ended / Sold out.
- **Same deal back:** if a deal ends and returns within 24 hours at the same price, the row goes back to Active quietly, with no new message.
- **Nothing changed:** no Notion write and no message. Running every 5 minutes for days, restarting the laptop, or running it from a scheduler never repeats anything. Even warnings are sent once.
- **Deleted rows:** if you delete a Deals row in Notion, it stays deleted until that item has a new deal.
- **Listing gone:** if a deal's listing is missing from a site's results 3 checks in a row, its row turns Ended (one or two misses are treated as a glitch).
- **Two near-equal listings:** the listing tracked last time is kept unless another one is at least 1% cheaper, so they never swap back and forth.

## Project layout

```
price-watcher/
├── app.py                 FastAPI for the Space: /, /health, /test, starts the loop thread
├── run_local.py           the same loop on the laptop, plus --items, --search, --once, --dry-run, --test
├── watchlist.json         the sites to search and the settings
├── watcher/
│   ├── crawler.py         the item × site loop
│   ├── search/            per-site search readers: amazon.py, noon.py, jumia.py, generic.py, common.py
│   ├── alerts.py          what's a deal, one message per deal, the Deals row content
│   ├── notion.py          Notion: Sites, Watchlist, Deals databases
│   ├── core.py            check_all(): one cycle
│   ├── notify.py          Telegram          · commands.py   /status /list /add /pause /burst /check
│   ├── sites.py, adapters/, history.py, state.py, sync.py, runtime.py, loop.py, config.py, ...
├── scripts/               setup_notion.py, check_space.py, watchdog.py (GitHub Actions)
├── tests/                 pytest suite (no network needed)
└── Dockerfile, requirements.txt, .env.example, .github/workflows/keepalive.yml
```

## 1. Install (10 minutes)

You need Python 3.11+ and Git.

```
cd price-watcher
python -m venv .venv
.venv\Scripts\activate          (Windows)    |   source .venv/bin/activate   (macOS / Linux)
pip install -r requirements.txt
pip install playwright
playwright install chromium
python -m pytest                (all tests should pass)
copy .env.example .env          (Windows)    |   cp .env.example .env        (macOS / Linux)
```

Put the code in a **private** GitHub repo (`git init`, `git add .`, `git commit -m "price watcher"`, `git branch -M main`, `git remote add origin https://github.com/<you>/price-watcher.git`, `git push -u origin main`). `.env` and `data/` are git-ignored, so no secret is committed.

## 2. Telegram bot (15 minutes)

1. In Telegram open **@BotFather**, send `/newbot`, pick a name and a username ending in `bot`. Put the token in `.env` as `TELEGRAM_TOKEN`.
2. Open a chat with your new bot and send it any message (bots can't write first).
3. Open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and copy the number after `"chat":{"id":` into `.env` as `TELEGRAM_CHAT_ID`.
4. Test: `python -c "from watcher.notify import send; send('hello')"`. Your phone should buzz.

## 3. Notion (10 minutes, no billing, no card)

1. **Create a connection.** Go to **notion.so/profile/integrations**: in Notion, Settings → Connections → *Develop or manage integrations*. Newer workspaces call these "connections", at app.notion.com/developers/connections. Create a new one: name it `Price Watcher`, pick your workspace, type **Internal**, and save. You need to be the workspace owner, which you are in your own workspace.
2. In its settings, make sure **Read content, Update content and Insert content** are ticked.
3. Copy its secret: *Internal Integration Secret*, sometimes labeled *Installation access token*. It starts with `ntn_`. Put it in `.env` as `NOTION_TOKEN`.
4. In Notion, create an empty page called **Price Watcher**.
5. **Connect the page:** on that page click **•••** (top right) → **Connections** → add `Price Watcher`. The other way works too: the connection's **Access** tab → Edit access → pick the page. Without this step every call fails with "object_not_found".
6. Copy the page link (**•••** → **Copy link**) into `.env` as `NOTION_PAGE`.
7. Run:

   ```
   python scripts/setup_notion.py --example
   ```

   It creates three databases inside your page: **Sites**, **Watchlist**, and **Deals**. It copies the default site URLs from `watchlist.json` into Sites and adds a Soundcore R60i NC example with no target price. Open the page on your phone to edit Sites and Watchlist; Deals fill in by themselves.

## 4. Tell it what to watch

### Items: the Watchlist database in Notion

One row per product. Only **Name** is required:

| column | example | notes |
|---|---|---|
| Name | `Soundcore R60i NC` | what to search. Write it like the store would: brand + model. Words like *earbuds* or *black* are optional |
| Target price | *(leave empty)* | optional: alerts when any site reaches the price. Leave empty for discount and price-drop alerts only |
| Sites | `noon`, `jumia` | optional: only these sites. Empty = all sites |
| Priority | `normal` | `high`: searched every minute in burst windows · `normal`: every 5 min · `low`: every 15 min |
| Paused | ☐ | tick to stop watching without deleting the row |
| Exclude | `max, mini` | words that rule a listing out, e.g. `max` so "iPhone 16 Pro" never matches "Pro Max" |
| Must include | `r60i nc` | words every listing title must have, replacing the automatic rule |
| Min price / Max price | `600` | ignore listings outside this range (a last guard against accessories) |
| Link | | leave empty to search. Paste a product link only to follow **that exact page** instead |
| Notes | | free text |
| Latest prices | | **written by the watcher**: e.g. `noon 1,394.95 EGP (-11%) · amazon_eg not listed`, updated only when it changes |

From Telegram: `/add soundcore r60i nc` adds a row without a target price. The watcher re-reads the Watchlist every 5 minutes. A bad row (a target that isn't a number) is skipped, and you get one ⚠️ message naming it.

### Items: also in watchlist.json

The items under `"items"` in `watchlist.json` are watched too, **together with** your Notion Watchlist and anything added with `/add`. An item listed in several places is watched once, with the Notion row's settings. Edits to `watchlist.json` items are picked up within 5 minutes, without a restart (a half-saved file is ignored, with one ⚠️). Each item is a name, a link, or an object with the Notion column names:

```json
"items": [
  "Soundcore R50i NC",
  {"name": "Sony WH-1000XM5", "target_price": 12000, "sites": ["noon", "amazon_eg"], "exclude": "case"},
  {"url": "https://www.amazon.eg/dp/B0XXXXXXXX", "target_price": 6500, "priority": "high"}
]
```

To stop watching an item, delete it (or set `"paused": true`) where it is listed; a Notion row is paused with its **Paused** box. `python run_local.py --items` lists every item that will be watched and where it came from, and the 🟢 startup message names them.

### What you get: the Deals database

One row per item per site that has had a deal: Item, Site, **Status** (Active / Ended / Sold out), **Price**, **Previous price**, **Was** (the store's old price), **Store discount** (%), **Below usual** (% under your 14-day history), Target, Usual price, Lowest seen, **Real discount** (YES / NO / TOO EARLY), **Why** (TARGET HIT, ON SALE, PRICE DROP), In stock, Seller, Listing (the store's title), **Link**, Found, Updated. Filter on Status = Active to see what's on sale right now.

### Sites: the Sites database in Notion

One row per configured store. **Name** and **URL** are required. Check **Enabled** to search a site; clear it to pause that site. **Currency** is optional and is inferred from known store URLs when possible. The initial rows are copied from `watchlist.json` the first time you run `setup_notion.py`. Enable or pause configured sites from your phone; add new sites to `watchlist.json` first. A configured site without a Sites row is searched as configured, and a Sites row for a site that isn't in `watchlist.json` (e.g. an old Amazon US row) is ignored. Rows are matched by Key, Name or URL. `watchlist.json` provides the local fallback if Notion is unavailable.

How sites work:

- **Short names:** each site gets one, used in alerts and in the Sites column: `noon`, `amazon_eg`, `jumia`, `amazon_com`. Any other site is named by its domain, e.g. `btech.com`.
- **Readers:** Noon, Amazon (any country) and Jumia Egypt have dedicated readers. Any other site is tried as a Shopify store, then a WooCommerce store, then through its normal search page.
- **Switching a site off:** use `{"url": "https://www.amazon.com/", "enabled": false}`.
- **amazon.com prices are in USD:** add `"fx_rates": {"USD": 50}` to compare them with EGP targets. Without it, amazon.com still reports store sales and price drops, but never "target hit".

## 5. Go live tonight on the laptop

First check which items will be watched and where each one comes from (Notion, watchlist.json or /add):

```
python run_local.py --items
```

Then see what each site returns for your item and which listing would be picked:

```
python run_local.py --search "soundcore r60i nc"
```

Then:

```
python run_local.py --test       each site's search: worked / captcha / blocked / no products found
python run_local.py --dry-run    every item on every site: what would be picked (sends and saves nothing)
python run_local.py              runs until Ctrl+C, checking every 5 minutes
```

The first check sends **🏷 ALREADY ON SALE** for anything already discounted, creates its Deals row, and sends one summary. After that you only hear about new deals.

**Two ways to run it every 5 minutes.** Both never repeat anything.
- **Keep it running (recommended):** `python run_local.py`. Telegram commands work. Keep the laptop plugged in and awake (Windows: Settings → System → Power & battery → Screen and sleep → Never; macOS: `caffeinate -i python run_local.py`).
- **Or let a scheduler start one check every 5 minutes:** `python run_local.py --once`. Telegram commands need the always-running version (or the Space).
  - Windows Task Scheduler: Create Task → Triggers → New → Daily, *Repeat task every 5 minutes* for *Indefinitely* → Actions → Program: `C:\path\to\price-watcher\.venv\Scripts\python.exe`, Arguments: `run_local.py --once`, Start in: `C:\path\to\price-watcher`.
  - macOS / Linux cron: `*/5 * * * * cd /path/to/price-watcher && .venv/bin/python run_local.py --once >> data/once.log 2>&1`

## 6. Move to the Hugging Face Space (Nov 7)

1. **History dataset:**
   - Create a token with **write** permission (huggingface.co → Settings → Access Tokens).
   - Add `HF_TOKEN=...` and `HF_DATASET=yourname/price-history` to `.env`, then restart the laptop watcher.
   - It creates the private dataset and uploads history every 30 minutes.
2. **Create the Space:** New Space, SDK **Docker** (blank), **CPU basic** (free), **Private**.
3. **Secrets and variables:** in Space → Settings → Variables and secrets, add these **secrets**: `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`, `NOTION_TOKEN`, `NOTION_PAGE`, `HF_TOKEN`, `HF_DATASET`. Add the **variable** `LOOP_ENABLED` = `0`, which serves the endpoints only, with no checking yet.
4. **Push the code:**
   - Run `git remote add space https://huggingface.co/spaces/<you>/price-watcher`, then `git push --force space main`.
   - Use `--force` the first time only. Username: your HF username. Password: your HF token.
5. **Cloud smoke test:**
   - Add `SPACE_URL=https://<you>-price-watcher.hf.space` to `.env`, then run `python scripts/check_space.py test`.
   - Each site is searched **from Hugging Face's servers**. Sites that worked stay on the Space; the rest stay on the laptop.
6. **Go live:**
   - Delete `LOOP_ENABLED`. Telegram says 🟢 watcher started on space.
   - If a site failed the smoke test, add the variable `EXCLUDE_STORES` = `amazon_eg` (comma-separated for several).
   - On the laptop, either stop the watcher, or keep it only for the blocked sites with `STORES=amazon_eg` and `BOT_COMMANDS=0` in its `.env`. Each site must be searched from exactly one place.

## 7. Keep-alive and watchdog on GitHub (Nov 7)

`.github/workflows/keepalive.yml` runs every hour at :17:

- Asleep or paused: it restarts the Space (😴).
- Crashed: one restart (🔴).
- Loop stuck: restart (🟠).
- Healthy: no message.

Setup:

1. GitHub repo → Settings → Secrets and variables → Actions. **Secrets**: `HF_TOKEN`, `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`. **Variables**: `SPACE_ID` = `<you>/price-watcher`, `SPACE_URL` = `https://<you>-price-watcher.hf.space`.
2. Actions → keepalive → Run workflow. It should finish green, with no message.
3. Drill: Space → Settings → Pause, then run the workflow again. You get "😴 Space was paused, restarting it."

## 8. Through the sales

- **Daily:**
  - `/status` should show a last check a few minutes ago and nothing failing.
  - In Notion, filter Deals on Status = Active, and on Real discount = YES for the real bargains.
  - If something was the wrong product, fix that row the same day (section 10).
- **Burst windows:** add announced sale times to `burst_windows` in `watchlist.json` (Cairo time) and push.
  - Daily: `"23:50-01:30"`.
  - One date only: `"2026-11-27 09:00-11:00"`.
  - Short notice: `/burst on 90`.
- **Nov 26–30:** keep the phone charged and unmuted, and save payment methods in every store app. When an alert comes in, tap its link and buy yourself; the watcher never buys anything.
- **Dec 1:** `/pause`.

## 9. Testing

`python -m pytest` runs the whole suite without network. It covers:

- Every site reader on sample pages, and title matching (R60i NC vs R50i, cases, replacement parts).
- The crawler loop and the deal rules.
- **No repeats:** 30 checks in a row give 1 message and 1 Notion row, and repeated scheduled runs repeat nothing.
- The Notion client against an imitation of the Notion API: databases created once, rows found and updated, never duplicated.
- Notion outages, commands, `/health` and the watchdog.

Drills by Nov 10:
- **Notion outage:** disconnect the page from the connection (••• → Connections). You get one ⚠️ message, and Telegram alerts keep coming. Deals rows wait in `data/pending_deals.json` and are written once you reconnect, still one row per item per site.
- **Site outage:** add `"https://example.invalid/"` to `sites`. After 3 checks you get one ⚠️ CAN'T SEARCH; then remove it.
- **Restart:** stop and restart mid-run. Nothing repeats, and history continues.
- **Dataset restore:** stop, delete `data/`, and start again. The history and the memory of what was sent come back from the dataset.

## 10. Sites, matching and fixing wrong results

**The wrong listing is picked** (`--search` shows why each listing was kept or dropped):
- An accessory or a different model: put its distinguishing word in **Exclude** (e.g. `max`, `lite`, `case`), or set **Min price**.
- You want one exact model: use **Must include**, e.g. `playstation 5 slim`.

**"not listed" although the site sells it:** that site words the title differently. `--search` shows which word is `missing`; remove it from the Name.

**A site shows `blocked` or `captcha`:** use its normal browser interface manually, or use an official feed/API if the retailer provides one and your account is authorized. The watcher does not solve CAPTCHAs or evade access controls. Browser rendering is available for ordinary JavaScript pages with `FETCH_BACKEND={"amazon_eg":"browser","jumia":"browser","noon":"browser_visible"}` in the local `.env`; it uses a regular Chromium browser, waits for the provider's product cards, and reports a 403 as blocked. Install it with `pip install playwright` and `playwright install chromium`. `browser_visible` opens the store page on your desktop during each search; if you close that window, it opens again at the next search.

## 11. Reference

### Messages

| message | when |
|---|---|
| 🏷 ALREADY ON SALE | the first time an item is found on a site, it's already a deal |
| 🔥 TARGET HIT / 🏷 ON SALE / 📉 PRICE DROP | a new deal starts (the headline is its main reason) |
| ⬇️ EVEN LOWER | during a deal, the price fell at least 1% below the last notified price |
| ↩️ DEAL ENDED | only with `"notify_deal_ended": true`; otherwise Notion shows Ended / Sold out |
| ⚠️ CAN'T SEARCH / CAN'T READ | a site or page failed 3 checks in a row (once); ✅ when it works again |
| ⚠️ SUSPICIOUS PRICE | under 20% or over 300% of the usual price: not trusted, check it yourself |
| 🔎 Now searching | once, after a newly added item was searched everywhere |

### Telegram commands

`/status` `/list` `/add <name> [@target]` `/add <url> <target>` `/pause` `/resume` `/burst on [minutes]` `/burst off` `/check` `/help`

### Settings (`watchlist.json` → `settings`; include only what you change)

| setting | default | |
|---|---|---|
| search_interval_seconds / interval_seconds | 300 / 300 | how often items are searched / tracked links are read |
| store_discount_percent | 0 | any positive difference from the site's "was" price = ON SALE |
| drop_percent_alert | 15 | labels a drop vs your history as a strong discount; any drop from the last saved price can still alert |
| require_real_discount | false | true: ON SALE and PRICE DROP also need a YES verdict |
| min_drop_percent | 1 | price changes smaller than this are noise: never a PRICE DROP or EVEN LOWER. 0 = every piastre counts |
| deeper_drop_percent | 0 | EVEN LOWER needs this much under the last message (never less than min_drop_percent) |
| repeat_after_hours | 24 | a deal back within this, at the same price or higher, is not sent again |
| notify_deal_ended | false | also message when a deal ends |
| exclude_words, match_min_ratio | `[]`, 1.0 | matching for every item |
| fx_rates | `{}` | e.g. `{"USD": 50}` for amazon.com targets |
| burst_interval_seconds, burst_windows | 60, `["23:50-01:30","11:50-12:30"]` | |
| fetch_backend | `{}` | per site: `requests`, `impersonate`, `browser` |

Also: `jitter_seconds`, `timezone`, `real_discount_percent`, `min_history_days`, `low_priority_every`, `failure_alert_after`, `accessory_price_ratio`, `announce_new_items`, `store_pause_seconds`, `request_timeout_seconds`, `watchlist_refresh_minutes`, `history_sync_minutes`.

### Environment variables

`TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`, `NOTION_TOKEN`, `NOTION_PAGE`, `HF_TOKEN`, `HF_DATASET`; optional `SITES`, `STORES` / `EXCLUDE_STORES`, `BOT_COMMANDS`, `LOOP_ENABLED`, `RUNNING_ON`, `USER_AGENT`, `SPACE_URL`.

### Files the watcher writes

These live in `data/` (laptop) or `/tmp/data` (Space) and are synced to the dataset:

- `history/<device>-<date>.csv`: every price seen; for searched items the `url` column says which listing was picked.
- `state/<device>.json`: what was seen and what was already sent. This is what prevents repeats.
- `watchlist_cache.json`: a copy of your Notion Watchlist, used when Notion is unreachable.

Not synced: `pending_deals.json`, the Deals rows waiting for Notion.

## 12. Troubleshooting

| sign | fix |
|---|---|
| setup_notion.py: 404 object_not_found | the page isn't connected: page → ••• → Connections → add Price Watcher |
| setup_notion.py: 401 | wrong or revoked token: copy the `ntn_` secret again |
| setup_notion.py: 403 | tick Read, Update and Insert content in the connection's settings |
| alert for the wrong product | `--search "<name>"`, then Exclude / Must include / Min price on that row |
| item "not listed" on a site that sells it | a word of the Name is missing in that site's titles: shorten the Name |
| a site's price jumps between two values | two listings are being picked: `--search` lists both, the history `url` column shows which; add the other one's distinguishing word to Exclude |
| ⚠️ CAN'T SEARCH a site | blocked: `impersonate` backend, else search it from the laptop |
| no messages at all | normal when nothing is discounted: the Latest prices column shows current prices |
| every alert twice | a site is searched from two places: `STORES` / `EXCLUDE_STORES` |
| `/health` returns 500 | the watchdog restarts the Space within the hour; Space → Settings → Restart does it now |
| token leaked | revoke it (BotFather `/revoke`, Notion connection → refresh secret, HF settings) |
