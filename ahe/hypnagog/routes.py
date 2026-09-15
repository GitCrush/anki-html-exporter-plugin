"""Hypnagog's part of the live server: one page, one endpoint.

``/hypnagog`` is the page; ``/api/hypnagog/items`` deals the items for a
scope, ``/api/hypnagog/config`` reads and writes the settings. Hung on the
server like the narrator, and asked first for every request.
"""

from __future__ import annotations

import json
from http import HTTPStatus
from pathlib import Path
from typing import Any, Callable

from .. import config as addon_config
from . import extract

PAGE = "/hypnagog"
API = "/api/hypnagog/"
ASSET = "/assets/hypnagog/"
WEB = Path(__file__).with_name("web")
MAX_CARDS = 500

DEFAULTS: dict[str, Any] = {
    "prime_ms": 80,
    "show_ms": 2500,
    "consolidate_ms": 800,
    "round_pause_ms": 1200,
    # 1: clean -- sans-serif, no effects, no audio; 2: "Polybius" -- CRT,
    # particles, ambient audio
    "visual_mode": 2,
    "max_cards": 50,
    # due, new, all, leeches, failed_24h, failed_7d
    "card_source": "due",
    "progressive_speed": False,
    "include_hidden": False,
}
STATIC = {"hypnagog.js": "text/javascript; charset=utf-8", "app.js": "text/javascript; charset=utf-8"}


def get_config() -> dict:
    return with_defaults(addon_config.get_config().get("hypnagog"))


def with_defaults(stored: Any) -> dict:
    config = dict(DEFAULTS)
    if isinstance(stored, dict):
        config.update({k: v for k, v in stored.items() if k in DEFAULTS})
    return config


def save_config(config: dict) -> None:
    whole = addon_config.get_config()
    whole["hypnagog"] = {k: v for k, v in config.items() if k in DEFAULTS}
    addon_config.save_config(whole)


def apply_changes(config: dict, changes: dict) -> dict:
    updated = dict(config)
    for key, value in changes.items():
        if key not in DEFAULTS or value is None:
            continue
        default = DEFAULTS[key]
        try:
            if isinstance(default, bool):
                value = bool(value)
            elif isinstance(default, int):
                value = int(value)
            else:
                value = str(value).strip()
        except (TypeError, ValueError):
            continue
        if key == "card_source" and value not in extract.SOURCES:
            continue
        if key == "visual_mode":
            value = 1 if value == 1 else 2
        if key == "max_cards":
            value = min(MAX_CARDS, max(1, value))
        updated[key] = value
    return updated


class Module:
    def __init__(
        self,
        server: Any,
        *,
        config_reader: Callable[[], dict] = get_config,
        config_writer: Callable[[dict], None] = save_config,
    ) -> None:
        self.server = server
        self.read_config = config_reader
        self.write_config = config_writer

    def stop(self) -> None:
        pass

    def handle(self, h: Any, method: str, path: str, params: dict) -> bool:
        if path == PAGE:
            h._send(HTTPStatus.OK, "text/html; charset=utf-8", (WEB / "index.html").read_bytes(), h.cookie_header())
        elif path.startswith(ASSET):
            name = path[len(ASSET):]
            mime = STATIC.get(name)
            if mime is None or not (WEB / name).is_file():
                _error(h, HTTPStatus.NOT_FOUND, "no such asset")
            else:
                h._send(HTTPStatus.OK, mime, (WEB / name).read_bytes())
        elif path == API + "config" and method == "GET":
            _json(h, self.read_config())
        elif path == API + "config" and method == "POST":
            length = int(h.headers.get("Content-Length") or 0)
            try:
                changes = json.loads(h.rfile.read(length) or b"{}")
            except ValueError:
                _error(h, HTTPStatus.BAD_REQUEST, "not JSON")
                return True
            if not isinstance(changes, dict):
                _error(h, HTTPStatus.BAD_REQUEST, "expected an object")
                return True
            updated = apply_changes(self.read_config(), changes)
            self.write_config(updated)
            _json(h, updated)
        elif path == API + "items":
            self._items(h, params)
        else:
            return False
        return True

    def _items(self, h: Any, params: dict) -> None:
        cfg = self.read_config()
        query = (params.get("q") or [""])[0]
        source = (params.get("source") or [cfg["card_source"]])[0]
        if source not in extract.SOURCES:
            source = cfg["card_source"]
        try:
            limit = min(MAX_CARDS, max(1, int((params.get("max") or [cfg["max_cards"]])[0])))
        except (TypeError, ValueError):
            limit = cfg["max_cards"]
        items = self.server.collection.run(
            lambda col: extract.find_items(col, query, source, limit, cfg["include_hidden"])
        )
        _json(h, {"items": items, "query": query, "source": source})


def _json(h: Any, payload: Any) -> None:
    h._send(HTTPStatus.OK, "application/json; charset=utf-8", json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def _error(h: Any, status: HTTPStatus, message: str) -> None:
    h._send(status, "application/json; charset=utf-8", json.dumps({"error": message}).encode("utf-8"))
