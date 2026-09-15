"""Serving the collection to a browser while Anki is running.

The export writes a fixed set of cards to disk. This does the opposite: nothing
is copied anywhere, and the scope can be changed at any time -- pick another
deck, another tag, type an Anki search -- and the page shows what that scope
holds right now. The reader is a normal browser, which is what keeps printing,
find-in-page and the browser's own zoom available.

Two things make that safe to do from a web server:

*Threads.* Anki keeps collection work off the GUI thread and serialises it
through a pool of exactly one worker (``TaskManager._collection_executor``, and
its docstring: "Tasks that access the collection are serialized"). This does the
same with its own single worker, so a request never touches Qt and the interface
never waits for one. ``taskman.run_in_background`` itself is not usable here --
it is meant to be called from the main thread and says so.

*Reach.* The server binds to the loopback interface, and every request has to
carry a token that is minted per session. Opening the view to the local network
is a deliberate, separate step.
"""

from __future__ import annotations

import gzip
import json
import mimetypes
import random
import re
import secrets
import socket
import ssl
import threading
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from . import assets
from . import qr
from .export import without_hidden
from .narrator import tls
from .media_export import MediaCollector
from .renderer import SPECIAL_NAMES, CardRenderer
from .writer import ExportOptions, ExportWriter

WEB_DIR = Path(__file__).with_name("web")
MEDIA_PREFIX = "/media/"
ASSET_PREFIX = "/assets/"
COOKIE_NAME = "ahe_key"

# How long a request may wait for the collection thread. Long enough to sit out
# a sync or a big search, short enough that a wedged worker surfaces as an
# error rather than a hung tab.
COLLECTION_TIMEOUT = 60.0

# Cards are rendered a page at a time, as the reader scrolls.
PAGE_SIZE = 25
MAX_PAGE_SIZE = 200

# Below this, compressing costs more than it saves.
GZIP_THRESHOLD = 2048

ASSETS = {
    "shell.js": "text/javascript; charset=utf-8",
    "shell.css": "text/css; charset=utf-8",
    "tailwind.css": "text/css; charset=utf-8",
}

# An export copies MathJax and jQuery in next to the document; here they are
# served out of Anki's own web assets under this prefix instead. Without them a
# formula would not typeset and every note type that expects the reviewer's
# jQuery global -- AnKing, Ankizin and the AMBOSS types among them -- would run
# into an error on its first line.
VENDOR_PREFIX = "vendor/"
VENDOR_ROOT = "js/vendor/"


class CollectionClosed(Exception):
    """Raised when a request arrives while no collection is open."""


class CollectionAccess:
    """Runs everything that touches the collection on one dedicated thread."""

    def __init__(self, provider: Callable[[], Any]) -> None:
        self._provider = provider
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="ahe-live-col"
        )

    def run(self, work: Callable[[Any], Any]) -> Any:
        future = self._executor.submit(self._with_collection, work)
        return future.result(timeout=COLLECTION_TIMEOUT)

    def _with_collection(self, work: Callable[[Any], Any]) -> Any:
        col = self._provider()
        if col is None:
            raise CollectionClosed()
        return work(col)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False)


class LiveView:
    """The rendering state behind one live view.

    Only ever touched from the collection thread, so it needs no locking of its
    own. It holds the renderer -- which accumulates note type stylesheets -- and
    the card ids the current scope resolved to.
    """

    def __init__(self, options: ExportOptions, include_hidden: bool = False) -> None:
        self.options = options
        # Suspended and buried cards stay out unless asked for, as in an
        # export. It is applied here rather than written into the address,
        # so it survives every change of scope the reader makes in the page.
        self.include_hidden = include_hidden
        self.writer = ExportWriter(options)
        self._renderer: CardRenderer | None = None
        self._names: dict | None = None
        self.query = ""
        self.card_ids: list[int] = []

    def renderer(self, col: Any) -> CardRenderer:
        if self._renderer is None:
            media = MediaCollector(
                col.media.dir(), None, link_prefix=MEDIA_PREFIX
            )
            self._renderer = CardRenderer(
                col, media, apply_gui_hooks=self.options.apply_gui_hooks
            )
        return self._renderer

    # Scope
    ##################################################################

    def set_scope(self, col: Any, query: str) -> dict:
        """Resolve a search to the cards it matches."""
        query = query.strip()
        self.query = query
        if query:
            search = query if self.include_hidden else without_hidden(query)
            # order=True is the sort the user set in Anki's own browser, which
            # is the order they expect to be reading in.
            self.card_ids = list(col.find_cards(search, order=True))
        else:
            self.card_ids = []
        return {"query": query, "total": len(self.card_ids)}

    def page(
        self,
        col: Any,
        offset: int,
        limit: int,
        have_css: set[str],
        query: str | None = None,
    ) -> dict:
        # The reader says which scope the page belongs to, so the answer does
        # not depend on the server having been asked in the right order -- or
        # on it being the same server that was asked last.
        if query is not None and query.strip() != self.query:
            self.set_scope(col, query)
        renderer = self.renderer(col)
        total = len(self.card_ids)
        wanted = self.card_ids[offset : offset + limit]

        cards = []
        errors = []
        for position, card_id in enumerate(wanted):
            try:
                rendered = renderer.render(col.get_card(card_id))
            except Exception as exc:  # one broken card must not empty the page
                errors.append(f"card {card_id}: {exc}")
                continue
            cards.append(
                self.writer.card_fragment(rendered, offset + position + 1, total)
            )

        css = {
            str(ntid): text
            for ntid, text in renderer.notetype_css.items()
            if str(ntid) not in have_css
        }
        return {
            "cards": cards,
            "css": css,
            "offset": offset,
            "total": total,
            "errors": errors,
        }

    def shuffle(self, col: Any, query: str | None = None) -> dict:
        """Deal the current scope again.

        The page holds only what it has scrolled to, so shuffling there would
        deal the first two dozen cards and leave everything after them in the
        collection's order. The order of a scope is this view's to decide, and
        this is the only place the whole scope exists -- so it is decided here
        and the page loads it again from the start.

        Nothing is written: the collection has an order of its own and keeps
        it. This is the order the reading happens in.
        """
        if query is not None and query.strip() != self.query:
            self.set_scope(col, query)
        random.shuffle(self.card_ids)
        return {"query": self.query, "total": len(self.card_ids)}

    # The panel's contents, straight from the collection
    ##################################################################

    def names(self, col: Any) -> dict:
        """Every field name in the collection, and the details strip's entries.

        Read once: a collection with hundreds of note types would otherwise
        pay for this on every reload, and note types do not change while a
        window is open.
        """
        if self._names is None:
            fields: set[str] = set()
            for entry in col.models.all_names_and_ids():
                notetype = col.models.get(entry.id)
                if notetype:
                    fields.update(f["name"] for f in notetype["flds"])
            self._names = {
                "fields": sorted(fields, key=str.lower),
                "special": list(SPECIAL_NAMES),
            }
        return self._names

    def tree(self, col: Any) -> dict:
        decks = sorted(
            (entry.name.replace("\x1f", "::") for entry in col.decks.all_names_and_ids()),
            key=str.lower,
        )
        notetypes = sorted(
            (entry.name for entry in col.models.all_names_and_ids()), key=str.lower
        )
        return {
            "decks": decks,
            "tags": sorted(col.tags.all(), key=str.lower),
            "notetypes": notetypes,
        }


class LiveServer:
    """A small HTTP server in front of one :class:`LiveView`."""

    def __init__(
        self,
        view: LiveView,
        collection: CollectionAccess,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        token: str | None = None,
        tls_dir: Path | None = None,
    ) -> None:
        self.view = view
        self.collection = collection
        # Handed in when only the interface is changing. Keeping the key and
        # the port means a tab that is already open goes on working -- opening
        # the view to the network is not a reason to take it away from the
        # machine it was opened on.
        self.token = token or secrets.token_urlsafe(18)
        self._host = host
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._requested_port = port
        # The narrator and Hypnagog, when attached: further pages on this server
        self.narrator: Any = None
        self.hypnagog: Any = None
        # A listener on the network speaking TLS, for a phone that needs a
        # secure page (its microphone); only while asked for.
        self._tls: ThreadingHTTPServer | None = None
        self._tls_thread: threading.Thread | None = None
        self._tls_dir = tls_dir or Path(__file__).resolve().parent.parent / "user_files" / "tls"
        self.tls_share_error = ""

    # Lifecycle
    ##################################################################

    def start(self) -> str:
        handler = _make_handler(self)
        self._httpd = ThreadingHTTPServer((self._host, self._requested_port), handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="ahe-live-http", daemon=True
        )
        self._thread.start()
        return self.url()

    def modules(self) -> list:
        return [m for m in (self.narrator, self.hypnagog) if m is not None]

    def stop(self) -> None:
        for module in self.modules():
            module.stop()
        self.tls_share(False)
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        self.collection.shutdown()

    # The network, over TLS
    ##################################################################

    def tls_share(self, on: bool) -> None:
        """Answer on the network over TLS as well, or stop doing so.

        A second server on a port of its own, with the add-on's certificate:
        a browser opens the microphone only on a secure page, and a phone
        reaching this machine by its address gets one only this way. The
        listeners already running are left alone.
        """
        if on and self._tls is None:
            try:
                cert, key = tls.ensure_certificate(self._tls_dir, _lan_addresses())
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                context.load_cert_chain(cert, key)
                httpd = ThreadingHTTPServer(("0.0.0.0", 0), _make_handler(self))
                httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
                httpd.daemon_threads = True
            except Exception as exc:
                self.tls_share_error = f"could not open the network listener: {exc}"
                return
            self.tls_share_error = ""
            self._tls = httpd
            self._tls_thread = threading.Thread(target=httpd.serve_forever, name="ahe-live-https", daemon=True)
            self._tls_thread.start()
        elif not on and self._tls is not None:
            shared, self._tls = self._tls, None
            threading.Thread(target=lambda: (shared.shutdown(), shared.server_close()), daemon=True).start()

    def tls_share_urls(self) -> list[str]:
        """``https://address:port`` for every address a phone might use; none while off."""
        if self._tls is None:
            return []
        port = self._tls.server_address[1]
        return [f"https://{address}:{port}" for address in _lan_addresses()]

    def certificate(self) -> Path | None:
        cert = self._tls_dir / "cert.pem"
        return cert if cert.is_file() else None

    @property
    def port(self) -> int:
        return self._httpd.server_address[1] if self._httpd else 0

    def url(self, host: str | None = None) -> str:
        return f"http://{host or '127.0.0.1'}:{self.port}/?key={self.token}"

    def share_urls(self) -> list[str]:
        """Every address a second device might reach this machine by.

        Empty while the server is bound to loopback, where no such address
        exists.
        """
        if self._host in ("127.0.0.1", "localhost", "::1"):
            return []
        return [self.url(address) for address in _lan_addresses()]

    def share_url(self) -> str | None:
        urls = self.share_urls()
        return urls[0] if urls else None


def _lan_addresses() -> list[str]:
    """This machine's addresses, most plausible first.

    Which one a guest can actually reach depends on the network *they* are on,
    and that cannot be known from here: a VPN can hold the default route while
    the phone in question sits on the wifi, and container bridges add addresses
    that lead nowhere. So every candidate is offered rather than one guessed,
    with the ranking putting home networks ahead of the ranges virtual
    interfaces tend to occupy.
    """
    found: list[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.append(info[4][0])
    except OSError:
        pass

    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # Connecting a datagram socket picks an interface without sending
        # anything; the destination only has to be routable-looking.
        probe.connect(("192.0.2.1", 9))  # TEST-NET-1, deliberately unreachable
        found.append(probe.getsockname()[0])
    except OSError:
        pass
    finally:
        probe.close()

    candidates: list[str] = []
    for address in found:
        if address.startswith(("127.", "169.254.")) or address in candidates:
            continue
        candidates.append(address)
    return sorted(candidates, key=_reachability_rank)


def _reachability_rank(address: str) -> int:
    if address.startswith("192.168."):
        return 0
    if address.startswith("10."):
        return 1
    # 172.16/12 is where Docker and friends put their bridges
    return 2


def _make_handler(server: LiveServer):
    class Handler(BaseHTTPRequestHandler):
        server_version = "AnkiHtmlExporterLive"
        protocol_version = "HTTP/1.1"

        # Routing
        ##############################################################

        def do_GET(self) -> None:  # noqa: N802 (the base class names it)
            parsed = urllib.parse.urlparse(self.path)
            path = urllib.parse.unquote(parsed.path)
            params = urllib.parse.parse_qs(parsed.query)

            if not self._authorised(params):
                self._text(HTTPStatus.FORBIDDEN, "wrong or missing key")
                return

            try:
                if any(module.handle(self, "GET", path, params) for module in server.modules()):
                    return
                if path == "/":
                    self._page(params)
                elif path.startswith(ASSET_PREFIX):
                    self._asset(path[len(ASSET_PREFIX) :])
                elif path.startswith(MEDIA_PREFIX):
                    self._media(path[len(MEDIA_PREFIX) :])
                elif path == "/api/scope":
                    self._scope(params)
                elif path == "/api/cards":
                    self._cards(params)
                elif path == "/api/shuffle":
                    self._shuffle(params)
                elif path == "/api/qr":
                    self._qr(params)
                elif path == "/api/tree":
                    self._json(server.collection.run(server.view.tree))
                else:
                    self._text(HTTPStatus.NOT_FOUND, "no such path")
            except CollectionClosed:
                self._text(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "Anki has no collection open at the moment",
                )
            except TimeoutError:
                self._text(
                    HTTPStatus.GATEWAY_TIMEOUT,
                    "Anki is busy with something else — try again in a moment",
                )
            except BrokenPipeError:
                pass  # the reader navigated away mid-response
            except Exception as exc:  # pragma: no cover - last resort
                self._text(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

        def do_POST(self) -> None:  # noqa: N802
            """Only the narrator takes anything in; the export's page asks."""
            parsed = urllib.parse.urlparse(self.path)
            path = urllib.parse.unquote(parsed.path)
            params = urllib.parse.parse_qs(parsed.query)
            if not self._authorised(params):
                self._text(HTTPStatus.FORBIDDEN, "wrong or missing key")
                return
            try:
                if not any(module.handle(self, "POST", path, params) for module in server.modules()):
                    self._text(HTTPStatus.NOT_FOUND, "no such path")
            except CollectionClosed:
                self._text(HTTPStatus.SERVICE_UNAVAILABLE, "Anki has no collection open at the moment")
            except TimeoutError:
                self._text(HTTPStatus.GATEWAY_TIMEOUT, "Anki is busy with something else — try again in a moment")
            except BrokenPipeError:
                pass
            except Exception as exc:  # pragma: no cover - last resort
                self._text(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

        def cookie_header(self) -> list[tuple[str, str]]:
            """The key as a cookie, for the requests a page makes after it."""
            return [("Set-Cookie", f"{COOKIE_NAME}={server.token}; Path=/; SameSite=Lax; HttpOnly")]

        def _authorised(self, params: dict) -> bool:
            given = (params.get("key") or [None])[0]
            if given is None:
                cookies = self.headers.get("Cookie", "")
                match = re.search(rf"{COOKIE_NAME}=([A-Za-z0-9_-]+)", cookies)
                given = match.group(1) if match else None
            return bool(given) and secrets.compare_digest(given, server.token)

        # Responses
        ##############################################################

        def _page(self, params: dict) -> None:
            names = server.collection.run(server.view.names)
            body = server.view.writer.live_shell(
                ASSET_PREFIX, names["fields"], names["special"]
            ).encode("utf-8")
            # The key travels on as a cookie, so that every later request --
            # including a picture inside a card's frame -- carries it without
            # the address having to be rewritten.
            self._send(HTTPStatus.OK, "text/html; charset=utf-8", body, self.cookie_header())

        def _asset(self, name: str) -> None:
            if name.startswith(VENDOR_PREFIX):
                self._vendor(name[len(VENDOR_PREFIX) :])
                return
            mime = ASSETS.get(name)
            path = WEB_DIR / name
            if mime is None or not path.is_file():
                self._text(HTTPStatus.NOT_FOUND, "no such asset")
                return
            self._send(HTTPStatus.OK, mime, path.read_bytes())

        def _vendor(self, name: str) -> None:
            """A file out of Anki's own ``js/vendor`` tree.

            MathJax loads its components by relative path, so this has to be a
            subtree rather than a fixed list -- but only that subtree: the name
            is checked before it is used.
            """
            parts = [part for part in name.split("/") if part]
            if not parts or any(part in ("..", ".") for part in parts):
                self._text(HTTPStatus.FORBIDDEN, "not a vendor file")
                return
            payload = assets.read(VENDOR_ROOT + "/".join(parts))
            if payload is None:
                self._text(HTTPStatus.NOT_FOUND, "no such file")
                return
            mime = mimetypes.guess_type(parts[-1])[0] or "application/octet-stream"
            self._send(HTTPStatus.OK, mime, payload)

        def _media(self, name: str) -> None:
            # Media filenames are flat; a path is not ours to serve.
            if not name or "/" in name or "\\" in name or name.startswith("."):
                self._text(HTTPStatus.FORBIDDEN, "not a media file")
                return
            directory = server.collection.run(lambda col: col.media.dir())
            path = Path(directory) / name
            if not path.is_file():
                self._text(HTTPStatus.NOT_FOUND, "no such file")
                return
            mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
            self._send(HTTPStatus.OK, mime, path.read_bytes())

        def _scope(self, params: dict) -> None:
            query = (params.get("q") or [""])[0]
            result = server.collection.run(
                lambda col: server.view.set_scope(col, query)
            )
            self._json(result)

        def _qr(self, params: dict) -> None:
            """This page's address as a code, for handing it to a phone.

            Only while the view is open to the network: a code for the loopback
            address would send the other device to itself. The scope travels
            with it, so the phone opens on the cards being read here.
            """
            addresses = server.share_urls()
            if not addresses:
                self._json({"shared": False})
                return
            address = addresses[0]
            query = (params.get("q") or [""])[0].strip()
            if query:
                address += "&q=" + urllib.parse.quote(query)
            try:
                drawing = qr.as_svg(qr.encode(address))
            except qr.TooLong:
                # A scope written out in full can outgrow the code; the address
                # without it still opens the view.
                address = addresses[0]
                drawing = qr.as_svg(qr.encode(address))
            self._json({"shared": True, "url": address, "svg": drawing})

        def _shuffle(self, params: dict) -> None:
            query = params.get("q")
            result = server.collection.run(
                lambda col: server.view.shuffle(col, query[0] if query else None)
            )
            self._json(result)

        def _cards(self, params: dict) -> None:
            offset = max(0, _int(params.get("offset"), 0))
            limit = min(MAX_PAGE_SIZE, max(1, _int(params.get("limit"), PAGE_SIZE)))
            have = set(filter(None, (params.get("have") or [""])[0].split(",")))
            query = params.get("q")
            wanted = query[0] if query else None
            result = server.collection.run(
                lambda col: server.view.page(col, offset, limit, have, wanted)
            )
            self._json(result)

        def _json(self, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._send(HTTPStatus.OK, "application/json; charset=utf-8", body)

        def _text(self, status: HTTPStatus, message: str) -> None:
            self._send(status, "text/plain; charset=utf-8", message.encode("utf-8"))

        def _send(
            self,
            status: HTTPStatus,
            mime: str,
            body: bytes,
            extra_headers: list[tuple[str, str]] | None = None,
            cache: bool = False,
        ) -> None:
            encoding = None
            # A card page is a couple of hundred kilobytes of HTML and the deck
            # and tag lists are megabytes of shared prefixes -- both shrink by
            # an order of magnitude, which is the difference between a
            # comfortable and a painful phone on the far side of a wifi.
            if (
                len(body) > GZIP_THRESHOLD
                and "gzip" in self.headers.get("Accept-Encoding", "")
                and not mime.startswith(("image/", "video/", "audio/"))
            ):
                body = gzip.compress(body, 6)
                encoding = "gzip"

            self.send_response(status)
            self.send_header("Content-Type", mime)
            if encoding:
                self.send_header("Content-Encoding", encoding)
            self.send_header("Content-Length", str(len(body)))
            # The collection changes under the reader's feet; nothing here may
            # be kept -- except what is immutable by name, a narration's audio.
            self.send_header("Cache-Control", "private, max-age=86400" if cache else "no-store")
            for name, value in extra_headers or []:
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            """Anki's console is not a request log."""

    return Handler


def _int(values: list[str] | None, fallback: int) -> int:
    try:
        return int((values or [""])[0])
    except (TypeError, ValueError):
        return fallback
