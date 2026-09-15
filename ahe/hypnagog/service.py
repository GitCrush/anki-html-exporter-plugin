"""Opening Hypnagog from the export dialog: the live server's page for it."""

from __future__ import annotations

import urllib.parse
import webbrowser

from .. import live
from .routes import PAGE


def url(query: str = "") -> str | None:
    server = live.running()
    if server is None:
        return None
    address = server.url().replace("/?key=", PAGE + "?key=")
    if query.strip():
        address += "&q=" + urllib.parse.quote(query.strip())
    return address


def open_in_browser(query: str = "") -> None:
    address = url(query)
    if address:
        webbrowser.open(address)
