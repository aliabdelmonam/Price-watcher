#!/usr/bin/env python3
"""Hourly keep-alive and watchdog (GitHub Actions). Never fetches prices.

1. Ask Hugging Face for the Space state.
2. Sleeping/paused/stopped -> restart it, tell you on Telegram.
3. Build or runtime error -> tell you, try one restart.
4. Running -> GET /health with your HF token (this request is also the keep-alive ping).
5. /health fails or reports a stale loop -> restart, tell you.
6. Healthy -> stay silent.

Env: HF_TOKEN, SPACE_ID (you/price-watcher), SPACE_URL (https://you-price-watcher.hf.space),
     TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
"""
from __future__ import annotations

import os
import sys
import time

import requests

ASLEEP = {"SLEEPING", "PAUSED", "STOPPED"}
BROKEN = {"BUILD_ERROR", "RUNTIME_ERROR", "CONFIG_ERROR", "NO_APP_FILE"}


def env(name: str) -> str:
    return os.environ.get(name, "").strip()


def telegram(text: str) -> None:
    token, chat = env("TELEGRAM_TOKEN"), env("TELEGRAM_CHAT_ID")
    print(text)
    if not (token and chat):
        return
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat, "text": text, "link_preview_options": {"is_disabled": True}},
                      timeout=20)
    except requests.RequestException as exc:
        print(f"telegram failed: {exc}")


def stage_of(api, space_id: str) -> str:
    stage = api.get_space_runtime(space_id).stage
    return str(getattr(stage, "value", stage)).upper()


def check_health(url: str, token: str) -> tuple[bool, str]:
    for attempt in range(2):
        try:
            resp = requests.get(f"{url.rstrip('/')}/health", headers={"Authorization": f"Bearer {token}"},
                                timeout=45)
            if resp.status_code == 200:
                return True, "ok"
            reason = f"HTTP {resp.status_code}"
            try:
                data = resp.json()
                reason += f", last cycle {data.get('seconds_since_last_cycle')} s ago"
            except ValueError:
                pass
        except requests.RequestException as exc:
            reason = f"no answer ({exc.__class__.__name__})"
        if attempt == 0:
            time.sleep(20)
    return False, reason


def main(api=None) -> int:
    token, space_id, space_url = env("HF_TOKEN"), env("SPACE_ID"), env("SPACE_URL")
    if not (token and space_id and space_url):
        print("HF_TOKEN, SPACE_ID and SPACE_URL must be set")
        return 2
    if api is None:
        from huggingface_hub import HfApi

        api = HfApi(token=token)
    logs = f"https://huggingface.co/spaces/{space_id}?logs=container"
    stage = stage_of(api, space_id)
    print(f"space stage: {stage}")
    if stage in ASLEEP:
        api.restart_space(space_id)
        telegram(f"😴 Space was {'asleep' if stage == 'SLEEPING' else stage.lower()}, restarting it.")
        return 0
    if stage in BROKEN:
        telegram(f"🔴 Space crashed ({stage}). Trying one restart. Logs: {logs}")
        try:
            api.restart_space(space_id)
        except Exception as exc:
            telegram(f"🔴 restart failed: {exc}")
        return 1
    if stage != "RUNNING":
        print("building or starting; nothing to do this hour")
        return 0
    healthy, reason = check_health(space_url, token)
    if healthy:
        print("healthy")
        return 0
    api.restart_space(space_id)
    telegram(f"🟠 Loop stuck ({reason}), Space restarted. Logs: {logs}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
