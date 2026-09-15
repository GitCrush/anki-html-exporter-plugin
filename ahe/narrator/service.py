"""Opening the narrator from the export dialog.

The narrator is a page of the live server; the dialog starts that server as
it does for the live view, and this points the browser at the page -- on
the scope the dialog shows, so the slide show begins on those cards.
"""

from __future__ import annotations

import urllib.parse
import webbrowser

from .. import live
from . import config as narrator_config
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
    server = live.running()
    if server is None:
        return
    cfg = narrator_config.get_config()
    if cfg.get("share"):
        server.tls_share(True)
    # Opened on nothing in particular, the page picks up where it was left
    address = url(query or cfg.get("last_search", ""))
    if address:
        webbrowser.open(address)
