"""Browsing the collection in a browser, live, while Anki runs.

This is the other side of the export rather than a feature of its own, so it
is operated from the export dialog: the same cards, the same page, the same
settings -- only nothing is written anywhere and the scope stays changeable.
The reader picks decks and tags in the page itself and can type an Anki search,
and what comes back is what the collection holds at that moment.

The server keeps running once it has been started, so the dialog can be closed
and the browser tab stays useful. It stops when it is stopped, or when the
profile it was serving closes.
"""

from __future__ import annotations

import urllib.parse
import webbrowser
from typing import Any

from aqt import gui_hooks, mw

from .live_server import CollectionAccess, LiveServer, LiveView
from .hypnagog.routes import Module as HypnagogModule
from .narrator.routes import Module as NarratorModule
from .writer import ExportOptions

_running: LiveServer | None = None


def is_running() -> bool:
    return _running is not None


def running() -> LiveServer | None:
    return _running


def start(
    options: ExportOptions, share: bool = False, include_hidden: bool = False
) -> LiveServer:
    """Start serving, replacing whatever was running before.

    A server that is already running hands its key and its port to the one
    replacing it, so switching the network share on or off does not pull the
    page out from under a tab that is already open. Nothing is given away by
    that: the key never left this machine, and what actually takes the network
    share back is the socket no longer listening on that interface.
    """
    global _running
    token = _running.token if _running is not None else None
    port = _running.port if _running is not None else 0
    # The narrator's state -- its caches, the talks, an export under way --
    # is carried over to the server replacing this one.
    narrator = _running.narrator if _running is not None else None
    tls_on = _running.tls_share_urls() != [] if _running is not None else False
    stop()

    host = "0.0.0.0" if share else "127.0.0.1"
    view = LiveView(options, include_hidden=include_hidden)
    access = CollectionAccess(lambda: mw.col)
    try:
        _running = LiveServer(view, access, host=host, port=port, token=token)
        _running.start()
    except OSError:
        # Something else took the port in the meantime; any port will do.
        _running = LiveServer(view, access, host=host, token=token)
        _running.start()
    if narrator is None:
        narrator = NarratorModule(_running)
    narrator.server = _running
    _running.narrator = narrator
    _running.hypnagog = HypnagogModule(_running)
    if share or tls_on:
        _running.tls_share(True)
    return _running


def stop() -> None:
    global _running
    if _running is not None:
        _running.stop()
        _running = None


def url(query: str = "") -> str | None:
    """The address for this machine, optionally opening on a scope.

    A live view that opens on the cards the dialog was pointed at saves the
    reader from starting at an empty page.
    """
    if _running is None:
        return None
    address = _running.url()
    if query.strip():
        address += "&q=" + urllib.parse.quote(query.strip())
    return address


def share_urls(query: str = "") -> list[str]:
    """Every address another device might reach this machine by."""
    if _running is None:
        return []
    suffix = "&q=" + urllib.parse.quote(query.strip()) if query.strip() else ""
    return [address + suffix for address in _running.share_urls()]


def open_in_browser(query: str = "") -> None:
    address = url(query)
    if address:
        webbrowser.open(address)


def _stop_on_profile_close(*_args: Any) -> None:
    """The collection is about to go; there is nothing left to serve."""
    stop()


gui_hooks.profile_will_close.append(_stop_on_profile_close)
