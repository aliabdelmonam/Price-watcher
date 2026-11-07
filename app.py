"""Hugging Face Space entry point: a tiny web server plus the checking loop in a background thread.

GET /        "watcher running" (the keep-alive ping can use it)
GET /health  heartbeat JSON; HTTP 500 if the loop has not finished a cycle in 3 x interval
GET /test    smoke test: one product page per store and one search per site
             -> worked / captcha / blocked / missing price
             (/test?url=<product url> tests a single link)

Run exactly one worker (Dockerfile does), or there would be two loops and double alerts.
Set the Space variable LOOP_ENABLED=0 to serve only these endpoints (cloud smoke test).
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse

from watcher.loop import Watcher, setup_logging, smoke_test

setup_logging()
watcher = Watcher()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    watcher.start_background()
    yield
    watcher.stop()


app = FastAPI(title="price-watcher", lifespan=lifespan)


@app.get("/", response_class=PlainTextResponse)
def root() -> str:
    return "watcher running"


@app.get("/health")
def health() -> JSONResponse:
    data, healthy = watcher.health()
    return JSONResponse(data, status_code=200 if healthy else 500)


@app.get("/test")
def test(url: str | None = None) -> JSONResponse:
    items = watcher.rt.active_items() if watcher.rt.items_all is not None else None
    return JSONResponse(smoke_test(watcher.cfg, url=url, items=items, sites=watcher.rt.sites))
