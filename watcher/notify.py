"""Telegram: send alerts and read your /commands.

Quick test from the repo folder:
    python -c "from watcher.notify import send; send('hello')"
"""
from __future__ import annotations

import html
import logging
import re
import time

import requests

log = logging.getLogger("watcher.notify")

MAX_LEN = 4000  # Telegram's limit is 4096 characters


def esc(text) -> str:
    return html.escape(str(text if text is not None else ""), quote=False)


def short_title(title, limit: int = 60) -> str:
    title = " ".join(str(title or "").split())
    return title if len(title) <= limit else title[: limit - 1].rstrip() + "…"


def _plain(text: str) -> str:
    return html.unescape(re.sub(r"</?(b|i|u|s|code|pre|a)(\s[^>]*)?>", "", text))


def split_message(text: str, limit: int = MAX_LEN) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks, current = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) + 1 > limit:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks


class Telegram:
    def __init__(self, token: str, chat_id: str, session: requests.Session | None = None):
        self.token = token
        self.chat_id = str(chat_id or "")
        self.enabled = bool(token and chat_id)
        self.session = session or requests.Session()
        self.last_error: str | None = None

    def call(self, method: str, payload: dict, timeout: float = 20) -> dict | None:
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        for attempt in range(2):
            try:
                resp = self.session.post(url, json=payload, timeout=timeout)
                data = resp.json()
            except Exception as exc:
                # Telegram embeds the bot token in the request URL; requests errors echo it.
                self.last_error = f"network: {str(exc).replace(self.token, '[redacted]')}"
                if attempt == 0 and method != "getUpdates":
                    time.sleep(2)
                    continue
                return None
            if data.get("ok"):
                self.last_error = None
                return data
            self.last_error = str(data.get("description") or data)
            retry_after = (data.get("parameters") or {}).get("retry_after")
            if data.get("error_code") == 429 and retry_after and attempt == 0:
                time.sleep(min(float(retry_after), 30))
                continue
            return data
        return None

    def send(self, text: str, link: str | None = None) -> bool:
        """Send an HTML message. With `link`, Telegram shows that page's preview (product photo)."""
        if not self.enabled:
            log.info("[telegram disabled] %s", _plain(text))
            return False
        ok = True
        for chunk in split_message(text):
            payload = {
                "chat_id": self.chat_id,
                "text": chunk,
                "parse_mode": "HTML",
                "link_preview_options": {"is_disabled": not link, **({"url": link} if link else {})},
            }
            data = self.call("sendMessage", payload)
            if data and not data.get("ok") and "parse" in str(data.get("description", "")).lower():
                payload.pop("parse_mode")
                payload["text"] = _plain(chunk)
                data = self.call("sendMessage", payload)
            if not (data and data.get("ok")):
                log.error("telegram send failed: %s", self.last_error)
                ok = False
        return ok

    def get_updates(self, offset: int | None, timeout: int = 0) -> list[dict] | None:
        payload = {"timeout": int(timeout), "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = offset
        data = self.call("getUpdates", payload, timeout=timeout + 15)
        if not data or not data.get("ok"):
            return None
        return data.get("result") or []


def send(text: str) -> bool:
    """One-off send using TELEGRAM_TOKEN / TELEGRAM_CHAT_ID from .env or the environment."""
    from .config import load_config

    cfg = load_config()
    if not cfg.telegram_enabled:
        print("TELEGRAM_TOKEN and TELEGRAM_CHAT_ID are not set (put them in .env).")
        return False
    bot = Telegram(cfg.telegram_token, cfg.telegram_chat_id)
    ok = bot.send(esc(text))
    print("sent" if ok else f"failed: {bot.last_error}")
    return ok
