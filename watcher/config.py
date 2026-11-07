"""Settings and secrets.

Secrets come from environment variables: a .env file on the laptop, "secrets" on the
Hugging Face Space and in GitHub Actions. Tunable settings, the sites to search and the
fallback items live in watchlist.json.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field, fields
from pathlib import Path

from .sites import DEFAULT_SITES

log = logging.getLogger("watcher")

ROOT = Path(__file__).resolve().parent.parent
RETIRED = {"alert_cooldown_minutes", "new_low_min_drop_percent", "status_update_minutes"}  # older settings: ignored quietly


def load_dotenv(path: Path | None = None) -> None:
    """Tiny .env reader: KEY=VALUE per line. Real environment variables win."""
    path = path or ROOT / ".env"
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = re.sub(r"\s+#.*$", "", value)
        if key:
            os.environ.setdefault(key, value)


load_dotenv()


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def env_flag(name: str, default: bool) -> bool:
    raw = env(name).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def detect_running_on() -> str:
    if env("RUNNING_ON"):
        return re.sub(r"[^a-z0-9_-]", "", env("RUNNING_ON").lower()) or "laptop"
    if env("SPACE_ID"):  # set by Hugging Face inside every Space
        return "space"
    return "laptop"


@dataclass
class Settings:
    interval_seconds: float = 300            # tracked links (rows with a url) are read this often
    search_interval_seconds: float = 300     # each item is searched on each site this often
    burst_interval_seconds: float = 60
    jitter_seconds: float = 30
    burst_windows: list = field(default_factory=lambda: ["23:50-01:30", "11:50-12:30"])
    timezone: str = "Africa/Cairo"
    # what counts as a deal
    store_discount_percent: float = 0.0       # any reduction from the store's own 'was' price = on sale
    drop_percent_alert: float = 15           # this much below the usual price = price drop
    require_real_discount: bool = False      # sales and drops above your target need a YES verdict
    real_discount_percent: float = 10        # "real" = at least this far below the 14-day median
    min_history_days: float = 3              # before this, compare with the first price seen
    median_days: float = 14
    # when you hear about a deal again (never for the same deal at the same price)
    deeper_drop_percent: float = 0.0         # any distinct lower price is a new message
    repeat_after_hours: float = 24           # a deal back within this, same price or higher: quiet
    notify_deal_ended: bool = False          # also message when a deal ends (Notion always shows it)
    # search mode
    match_min_ratio: float = 1.0             # share of the item's words a listing title must contain
    exclude_words: list = field(default_factory=list)  # added to the built-in accessory words
    accessory_price_ratio: float = 0.35      # matches under this x the median match are ignored
    fx_rates: dict = field(default_factory=dict)       # {"USD": 50}: 1 USD = 50 EGP, to compare targets
    announce_new_items: bool = True          # one message when a newly added item was first searched
    # plumbing
    low_priority_every: int = 3
    failure_alert_after: int = 3
    sanity_min_ratio: float = 0.2
    sanity_max_ratio: float = 3.0
    store_pause_seconds: list = field(default_factory=lambda: [2, 6])
    request_timeout_seconds: float = 15
    watchlist_refresh_minutes: float = 5
    history_sync_minutes: float = 30
    manual_burst_minutes: float = 120
    fetch_backend: dict = field(default_factory=dict)  # {"noon": "impersonate"} etc.

    @classmethod
    def from_dict(cls, data: dict) -> "Settings":
        known = {f.name: f for f in fields(cls)}
        kwargs = {}
        for key, value in (data or {}).items():
            if key.startswith("_") or key in RETIRED:
                continue  # "_comment" keys are allowed
            if key not in known:
                log.warning("watchlist.json: unknown setting %r ignored", key)
                continue
            kwargs[key] = value
        settings = cls(**kwargs)
        for name in ("interval_seconds", "burst_interval_seconds", "jitter_seconds", "search_interval_seconds"):
            setattr(settings, name, max(0.0, float(getattr(settings, name))))
        settings.interval_seconds = max(30.0, settings.interval_seconds)
        settings.burst_interval_seconds = max(20.0, settings.burst_interval_seconds)
        settings.search_interval_seconds = max(60.0, settings.search_interval_seconds)
        settings.exclude_words = [str(w) for w in settings.exclude_words or []]
        rates = {}
        for code, value in (settings.fx_rates or {}).items():
            try:
                rates[str(code).upper()] = float(value)
            except (TypeError, ValueError):
                log.warning("watchlist.json: fx_rates %r is not a number, ignored", code)
        settings.fx_rates = rates
        settings.low_priority_every = max(1, int(settings.low_priority_every))
        settings.failure_alert_after = max(1, int(settings.failure_alert_after))
        return settings


@dataclass
class Config:
    telegram_token: str
    telegram_chat_id: str
    notion_token: str
    notion_page: str
    hf_token: str
    hf_dataset: str
    running_on: str
    data_dir: Path
    watchlist_file: Path
    watchlist_items: list
    sites: list
    stores: set
    exclude_stores: set
    bot_commands: bool
    loop_enabled: bool
    user_agent: str
    settings: Settings

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_token and self.telegram_chat_id)

    @property
    def notion_enabled(self) -> bool:
        return bool(self.notion_token and self.notion_page)

    @property
    def hf_enabled(self) -> bool:
        return bool(self.hf_token and self.hf_dataset)

    def handles_store(self, store: str) -> bool:
        if self.stores and store not in self.stores:
            return False
        return store not in self.exclude_stores


def split_list(value: str | None) -> set:
    return {part.strip().lower() for part in (value or "").replace(";", ",").split(",") if part.strip()}


def read_watchlist_file(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path.name} is not valid JSON (line {exc.lineno}): {exc.msg}") from exc


def load_config(*, stores: str | None = None, exclude_stores: str | None = None) -> Config:
    running_on = detect_running_on()
    default_data = Path("/tmp/data") if running_on == "space" else ROOT / "data"
    watchlist_file = Path(env("WATCHLIST_FILE") or ROOT / "watchlist.json")
    raw = read_watchlist_file(watchlist_file)
    sites = raw["sites"] if isinstance(raw.get("sites"), list) else list(DEFAULT_SITES)
    if env("SITES"):
        sites = [s.strip() for s in env("SITES").split(",") if s.strip()]
    settings_data = dict(raw.get("settings") or {})
    if env("FETCH_BACKEND"):
        try:
            backend_overrides = json.loads(env("FETCH_BACKEND"))
            if isinstance(backend_overrides, dict):
                settings_data["fetch_backend"] = {**(settings_data.get("fetch_backend") or {}),
                                                   **backend_overrides}
            else:
                log.warning("FETCH_BACKEND must be a JSON object; ignoring it")
        except json.JSONDecodeError:
            log.warning("FETCH_BACKEND is not valid JSON; ignoring it")
    return Config(
        telegram_token=env("TELEGRAM_TOKEN"),
        telegram_chat_id=env("TELEGRAM_CHAT_ID"),
        notion_token=env("NOTION_TOKEN"),
        notion_page=env("NOTION_PAGE"),
        hf_token=env("HF_TOKEN"),
        hf_dataset=env("HF_DATASET"),
        running_on=running_on,
        data_dir=Path(env("DATA_DIR") or default_data),
        watchlist_file=watchlist_file,
        watchlist_items=list(raw.get("items") or []),
        sites=sites,
        stores=split_list(stores if stores is not None else env("STORES")),
        exclude_stores=split_list(exclude_stores if exclude_stores is not None else env("EXCLUDE_STORES")),
        bot_commands=env_flag("BOT_COMMANDS", True),
        loop_enabled=env_flag("LOOP_ENABLED", True),
        user_agent=env("USER_AGENT"),
        settings=Settings.from_dict(settings_data),
    )
