"""The narrator's part of the live server.

The live server serves the export's page; this adds a second page,
``/narrator``, and everything it asks for under ``/api/narrator/``. It is
one object hung on the server -- the narrations, their cache, the look-ahead,
the tutor, the exports -- and one method the server's request handler calls
first for every request: :meth:`Module.handle`, which answers what is its
and leaves the rest alone.
"""

from __future__ import annotations

import json
import mimetypes
import re
import urllib.parse
from http import HTTPStatus
from pathlib import Path
from typing import Any, Callable

from .. import assets, qr
from ..renderer import RenderedCard
from ..writer import ExportOptions, ExportWriter
from . import capture
from . import config as config_module
from . import outputs
from .chat import Tutor
from .narrate import BudgetExceeded, Narrator, Preparer
from .openai_api import OpenAIError
from .outputs import MAX_CARDS, ExportError, Exporter, frame_document
from .view import View

PAGE = "/narrator"
API = "/api/narrator/"
ASSET = "/assets/narrator/"
WEB = Path(__file__).with_name("web")
ENGINE_WEB = Path(__file__).parent.parent / "web"
VENDOR_URL = "/assets/vendor/"

PAGE_SIZE = 25
MAX_PAGE_SIZE = 200
# The most cards a look-ahead may narrate in advance
MAX_LOOKAHEAD = 10
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")

STATIC = {
    "app.js": "text/javascript; charset=utf-8",
    "app.css": "text/css; charset=utf-8",
}
SPEEDS = [1, 1.25, 1.5, 1.75, 2]


def shared_assets() -> dict:
    """What every card frame is built with: the same pieces the export uses."""
    return {
        "theme": assets.theme_variables_css(),
        "reviewer": assets.reviewer_css(),
        "frame": (ENGINE_WEB / "frame.css").read_text("utf-8"),
        "frameJs": (ENGINE_WEB / "frame.js").read_text("utf-8"),
        "shimJs": (ENGINE_WEB / "shim.js").read_text("utf-8"),
        "mathjaxUrl": f"{VENDOR_URL}mathjax/tex-chtml-full.js",
        "mathjaxDir": f"{VENDOR_URL}mathjax",
        "jqueryUrl": f"{VENDOR_URL}jquery.min.js",
    }


def _inside_anki() -> bool:
    try:
        import aqt  # noqa: F401

        return aqt.mw is not None
    except Exception:
        return False


class Module:
    """The narrator, attached to one live server."""

    def __init__(
        self,
        server: Any,
        *,
        config_reader: Callable[[], dict] = config_module.get_config,
        config_writer: Callable[[dict], None] = config_module.save_config,
        data_dir: Path | None = None,
        capturer: capture.Capturer | None = None,
    ) -> None:
        self.server = server
        self.view = View()
        self.read_config = config_reader
        self.write_config = config_writer
        self.data_dir = data_dir or config_module.user_files_dir()
        self.narrator = Narrator(self.data_dir / "cache")
        self.preparer = Preparer(self.narrator)
        self.tutor = Tutor(self.narrator)
        self.exporter = Exporter(
            self.narrator, self.data_dir / "exports",
            ffmpeg=self.read_config().get("ffmpeg") or "ffmpeg",
        )
        self.capturer = capturer or capture.default_capturer(prefer_anki=_inside_anki())

    def config(self) -> dict:
        return self.read_config()

    def update_config(self, changes: dict) -> dict:
        updated = config_module.apply_changes(self.read_config(), changes)
        self.write_config(updated)
        return updated

    def stop(self) -> None:
        self.preparer.stop()
        self.exporter.cancel()

    # Routing
    ##################################################################

    def handle(self, h: Any, method: str, path: str, params: dict) -> bool:
        """Answer the request if it is the narrator's; say whether it was."""
        if path == PAGE:
            self._page(h)
        elif path.startswith(ASSET):
            self._static(h, path[len(ASSET):])
        elif path.startswith(API):
            try:
                if method == "GET":
                    self._get(h, path[len(API):], params)
                else:
                    self._post(h, path[len(API):], params)
            except BudgetExceeded as exc:
                _error(h, HTTPStatus.CONFLICT, str(exc))
        else:
            return False
        return True

    def _get(self, h: Any, path: str, params: dict) -> None:
        if path == "config":
            _json(h, config_module.public(self.config()))
        elif path == "status":
            self._status(h)
        elif path == "shared":
            _json(h, shared_assets())
        elif path == "tree":
            _json(h, self.server.collection.run(self.view.tree))
        elif path == "scope":
            self._scope(h, params)
        elif path == "cards":
            self._cards(h, params)
        elif path.startswith("narration/"):
            self._narration(h, path[len("narration/"):], params)
        elif path == "prepare":
            self._prepare(h, params)
        elif path.startswith("audio/"):
            self._audio(h, path[len("audio/"):])
        elif path == "usage":
            _json(h, self.narrator.ledger.snapshot())
        elif path == "share":
            self._share(h, params)
        elif path == "share/cert":
            self._certificate(h)
        elif path.startswith("chat/"):
            self._chat_history(h, path[len("chat/"):])
        elif path == "export/estimate":
            self._export_estimate(h, params)
        elif path == "export/status":
            _json(h, self.exporter.status())
        elif path.startswith("export/frame/"):
            self._export_frame(h, path[len("export/frame/"):], params)
        elif path.startswith("export/file/"):
            self._export_file(h, path[len("export/file/"):])
        else:
            _error(h, HTTPStatus.NOT_FOUND, "no such path")

    def _post(self, h: Any, path: str, params: dict) -> None:
        if path == "config":
            changes = _body_json(h)
            if changes is None:
                return
            updated = self.update_config(changes)
            if "share" in changes:
                self.server.tls_share(bool(updated.get("share")))
            _json(h, config_module.public(updated))
        elif path == "usage/reset":
            self.narrator.ledger.reset()
            _json(h, self.narrator.ledger.snapshot())
        elif path == "transcribe":
            self._transcribe(h, params)
        elif path == "chat":
            self._chat(h)
        elif path == "export/start":
            self._export_start(h)
        elif path == "export/cancel":
            _json(h, self.exporter.cancel())
        else:
            _error(h, HTTPStatus.NOT_FOUND, "no such path")

    # The page
    ##################################################################

    def _page(self, h: Any) -> None:
        """The live shell in its narrator mode: the export's bar and panel, the narrator below."""
        cfg = self.config()
        view = self.server.view
        names = self.server.collection.run(view.names) if view is not None else {"fields": [], "special": []}
        writer = view.writer if view is not None else ExportWriter(ExportOptions(output_dir=None, title="Narrator"))
        narrator = {
            "title": "Narrator",
            "body": (WEB / "body.html").read_text("utf-8"),
            "asset_prefix": ASSET,
            "qr_path": API + "share",
            "dark": writer.options.dark,
            "orders": list(config_module.ORDERS.items()),
            "order": cfg["order"],
            "durations": [(s, f"{s} s") for s in config_module.DURATIONS],
            "seconds": cfg["seconds"],
            "voices": [(v, v) for v in config_module.VOICES],
            "voice": cfg["voice"],
            "speeds": [(s, f"{s}×") for s in SPEEDS],
            "speed": cfg.get("speed", 1),
        }
        page = writer.live_shell("/assets/", names["fields"], names["special"], narrator=narrator)
        # The key travels on as a cookie, so that every later request --
        # including a picture inside a card's frame -- carries it.
        h._send(HTTPStatus.OK, "text/html; charset=utf-8", page.encode("utf-8"), h.cookie_header())

    def _static(self, h: Any, name: str) -> None:
        mime = STATIC.get(name)
        path = WEB / name
        if mime is None or not path.is_file():
            _error(h, HTTPStatus.NOT_FOUND, "no such asset")
            return
        h._send(HTTPStatus.OK, mime, path.read_bytes())

    def _status(self, h: Any) -> None:
        try:
            self.server.collection.run(lambda col: col.media.dir())
            collection = True
        except Exception:
            collection = False
        _json(h, {"collection": collection, "has_key": bool(self.config()["openai_api_key"])})

    # Cards
    ##################################################################

    def _scope(self, h: Any, params: dict) -> None:
        query = (params.get("q") or [""])[0]
        cfg = self.config()
        _json(h, self.server.collection.run(
            lambda col: self.view.set_scope(col, query, cfg["include_hidden"], cfg["order"])
        ))

    def _cards(self, h: Any, params: dict) -> None:
        offset = max(0, _int(params.get("offset"), 0))
        limit = min(MAX_PAGE_SIZE, max(1, _int(params.get("limit"), PAGE_SIZE)))
        have = set(filter(None, (params.get("have") or [""])[0].split(",")))
        query = (params.get("q") or [""])[0]
        cfg = self.config()
        _json(h, self.server.collection.run(
            lambda col: self.view.page(col, cfg, query, offset, limit, have)
        ))

    def _render(self, cfg: dict, card_id: int) -> RenderedCard:
        """A card, rendered on the collection thread -- and the media folder noted."""
        def work(col: Any) -> RenderedCard:
            if self.narrator.media_dir is None:
                self.narrator.media_dir = col.media.dir()
            return self.view.render(col, cfg, card_id)
        return self.server.collection.run(work)

    def _choice(self, params: dict, cfg: dict) -> tuple[int, str]:
        seconds = min(600, max(3, _int(params.get("seconds"), cfg["seconds"])))
        voice = (params.get("voice") or [""])[0]
        if voice not in config_module.VOICES:
            voice = cfg["voice"]
        return seconds, voice

    # Narrating
    ##################################################################

    def _narration(self, h: Any, raw_id: str, params: dict) -> None:
        try:
            card_id = int(raw_id)
        except ValueError:
            _error(h, HTTPStatus.NOT_FOUND, "not a card id")
            return
        cfg = self.config()
        if not cfg["openai_api_key"]:
            _error(h, HTTPStatus.CONFLICT, "no OpenAI API key configured")
            return
        seconds, voice = self._choice(params, cfg)
        force = (params.get("force") or ["0"])[0] in ("1", "true")
        card = self._render(cfg, card_id)
        try:
            result = self.narrator.narrate(card, cfg, seconds, voice, force=force)
        except BudgetExceeded:
            raise
        except Exception as exc:
            _error(h, HTTPStatus.BAD_GATEWAY, f"narration failed: {exc}")
            return
        payload = result.as_json(f"{API}audio/{result.audio.name}")
        payload["usage"] = self.narrator.ledger.snapshot()
        _json(h, payload)

    def _prepare(self, h: Any, params: dict) -> None:
        """Narrate a few cards ahead of the reader, in the background.

        Deliberately a look-ahead and nothing more: every narration costs a
        model call and a speech call, and a run over a whole deck that is
        then not listened to would be money spent for nothing.
        """
        cfg = self.config()
        if not cfg["openai_api_key"]:
            _error(h, HTTPStatus.CONFLICT, "no OpenAI API key configured")
            return
        query = (params.get("q") or [""])[0]
        offset = max(0, _int(params.get("offset"), 0))
        count = min(MAX_LOOKAHEAD, max(1, _int(params.get("count"), 4)))
        seconds, voice = self._choice(params, cfg)
        ids = self.server.collection.run(lambda col: self.view.ids(col, cfg, query, offset, count))

        self.preparer.start(ids, lambda card_id: self._render(cfg, card_id), cfg, seconds, voice)
        _json(h, self.preparer.status())

    def _audio(self, h: Any, name: str) -> None:
        if not SAFE_NAME_RE.match(name) or not name.endswith(".mp3"):
            _error(h, HTTPStatus.NOT_FOUND, "no such audio")
            return
        path = self.narrator.cache_dir / name
        if not path.is_file():
            _error(h, HTTPStatus.NOT_FOUND, "no such audio")
            return
        h._send(HTTPStatus.OK, "audio/mpeg", path.read_bytes(), cache=True)

    # Talking about the card
    ##################################################################

    def _transcribe(self, h: Any, params: dict) -> None:
        """A recording from the page's microphone, as text."""
        cfg = self.config()
        if not cfg["openai_api_key"]:
            _error(h, HTTPStatus.CONFLICT, "no OpenAI API key configured")
            return
        length = int(h.headers.get("Content-Length") or 0)
        if length <= 0 or length > 25 * 1024 * 1024:
            _error(h, HTTPStatus.BAD_REQUEST, "no recording, or too long a one")
            return
        audio = h.rfile.read(length)
        mime = (h.headers.get("Content-Type") or "audio/webm").split(";")[0].strip()
        language = (params.get("language") or [""])[0]
        self.narrator._check_budget(cfg)
        try:
            text = self.narrator.client(cfg).transcribe(cfg["stt_model"], audio, mime, language)
        except OpenAIError as exc:
            _error(h, HTTPStatus.BAD_GATEWAY, str(exc))
            return
        # The endpoint reports no length; opus at 32 kbit/s is about 4 kB a second.
        self.narrator.ledger.add_transcription(cfg.get("prices", {}), cfg["stt_model"], len(audio) / 4000)
        _json(h, {"text": text, "usage": self.narrator.ledger.snapshot()})

    def _chat(self, h: Any) -> None:
        body = _body_json(h)
        if body is None:
            return
        cfg = self.config()
        if not cfg["openai_api_key"]:
            _error(h, HTTPStatus.CONFLICT, "no OpenAI API key configured")
            return
        try:
            card_id = int(body.get("card_id"))
        except (TypeError, ValueError):
            _error(h, HTTPStatus.BAD_REQUEST, "not a card id")
            return
        voice = str(body.get("voice") or "")
        if voice not in config_module.VOICES:
            voice = cfg["voice"]
        card = self._render(cfg, card_id)
        try:
            result = self.tutor.ask(card, str(body.get("narration") or ""), str(body.get("message") or ""), cfg, voice)
        except ValueError as exc:
            _error(h, HTTPStatus.BAD_REQUEST, str(exc))
            return
        except BudgetExceeded:
            raise
        except Exception as exc:
            _error(h, HTTPStatus.BAD_GATEWAY, f"the tutor did not answer: {exc}")
            return
        _json(h, {
            "text": result["text"], "audio": f"{API}audio/{result['audio'].name}",
            "duration": result["duration"], "language": result["language"],
            "history": result["history"], "usage": self.narrator.ledger.snapshot(),
        })

    def _chat_history(self, h: Any, raw_id: str) -> None:
        try:
            card_id = int(raw_id)
        except ValueError:
            _error(h, HTTPStatus.NOT_FOUND, "not a card id")
            return
        _json(h, {"history": self.tutor.history(card_id)})

    # The phone
    ##################################################################

    def _share(self, h: Any, params: dict) -> None:
        """The page's address for another device, as a QR code.

        Only while the server speaks TLS on the network; the scope on screen
        travels along, so the phone opens on the cards being read here.
        """
        addresses = [f"{base}{PAGE}?key={self.server.token}" for base in self.server.tls_share_urls()]
        if not addresses:
            _json(h, {"shared": False, "error": self.server.tls_share_error or
                      "This page is only being served to this machine.", "enable": API + "config"})
            return
        address = addresses[0]
        query = (params.get("q") or [""])[0].strip()
        if query:
            address += "&q=" + urllib.parse.quote(query)
        try:
            drawing = qr.as_svg(qr.encode(address))
        except qr.TooLong:
            address = addresses[0]
            drawing = qr.as_svg(qr.encode(address))
        _json(h, {
            "shared": True, "url": address, "urls": addresses, "svg": drawing,
            "note": "The page comes over HTTPS with the add-on's own certificate, so the phone warns once "
                    "that it is not trusted; go on past the warning and the microphone works there too.",
            "cert": API + "share/cert",
        })

    def _certificate(self, h: Any) -> None:
        cert = self.server.certificate()
        if cert is None:
            _error(h, HTTPStatus.NOT_FOUND, "no certificate yet")
            return
        h._send(HTTPStatus.OK, "application/x-x509-ca-cert", cert.read_bytes(),
                [("Content-Disposition", 'attachment; filename="anki-narrator.crt"')])

    # Exports
    ##################################################################

    def _export_cards(self, params: dict, cfg: dict) -> list[RenderedCard]:
        """Every card of the scope, rendered -- the export needs them all."""
        query = (params.get("q") or [""])[0]

        def work(col: Any) -> list[RenderedCard]:
            if self.narrator.media_dir is None:
                self.narrator.media_dir = col.media.dir()
            return [self.view.render(col, cfg, card_id) for card_id in self.view.ids(col, cfg, query, 0, MAX_CARDS + 1)]

        return self.server.collection.run(work)

    def _export_estimate(self, h: Any, params: dict) -> None:
        cfg = self.config()
        seconds, voice = self._choice(params, cfg)
        cards = self._export_cards(params, cfg)
        estimate = self.exporter.estimate(cards, cfg, seconds, voice, float(cfg.get("speed") or 1.0))
        estimate["ffmpeg"] = bool(outputs.ffmpeg_path(cfg.get("ffmpeg", "")))
        estimate["can_draw"] = _inside_anki() or bool(capture.chromium())
        _json(h, estimate)

    def _export_start(self, h: Any) -> None:
        body = _body_json(h)
        if body is None:
            return
        params = {key: [str(value)] for key, value in body.items()}
        cfg = self.config()
        if not cfg["openai_api_key"] and body.get("narrate_missing"):
            _error(h, HTTPStatus.CONFLICT, "no OpenAI API key configured")
            return
        seconds, voice = self._choice(params, cfg)
        cards = self._export_cards(params, cfg)
        night = "1" if body.get("night") else "0"
        title = str(body.get("title") or body.get("q") or "Anki Narrator").strip()
        base = self.server.url().split("/?")[0]

        def frame_url(card_id: int, side: str = "a") -> str:
            return f"{base}{API}export/frame/{card_id}?key={self.server.token}&night={night}&side={side}"

        try:
            status = self.exporter.start(
                str(body.get("kind") or "audio"), cards, cfg, seconds, voice,
                bool(body.get("narrate_missing")), title,
                speed=float(body.get("speed") or cfg.get("speed") or 1.0),
                frame_url=frame_url, capturer=self.capturer,
            )
        except ExportError as exc:
            _error(h, HTTPStatus.CONFLICT, str(exc))
            return
        _json(h, status)

    def _export_frame(self, h: Any, raw_id: str, params: dict) -> None:
        try:
            card_id = int(raw_id)
        except ValueError:
            _error(h, HTTPStatus.NOT_FOUND, "not a card id")
            return
        cfg = self.config()
        night = (params.get("night") or ["0"])[0] == "1"
        side = "q" if (params.get("side") or ["a"])[0] == "q" else "a"

        def build(col: Any) -> str:
            card = self.view.render(col, cfg, card_id)
            css = self.view.renderer(col, cfg["apply_gui_hooks"]).notetype_css.get(card.notetype_id, "")
            return frame_document(card, css, shared_assets(), night, side)

        body = self.server.collection.run(build).encode("utf-8")
        # The pictures inside need the key too; Chromium keeps the cookie.
        h._send(HTTPStatus.OK, "text/html; charset=utf-8", body, h.cookie_header())

    def _export_file(self, h: Any, name: str) -> None:
        if not SAFE_NAME_RE.match(name):
            _error(h, HTTPStatus.NOT_FOUND, "no such export")
            return
        path = self.exporter.exports_dir / name
        if not path.is_file():
            _error(h, HTTPStatus.NOT_FOUND, "no such export")
            return
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        h._send(HTTPStatus.OK, mime, path.read_bytes(),
                [("Content-Disposition", f'attachment; filename="{name}"')])


# Plumbing
######################################################################


def _json(h: Any, payload: Any) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    h._send(HTTPStatus.OK, "application/json; charset=utf-8", body)


def _error(h: Any, status: HTTPStatus, message: str) -> None:
    body = json.dumps({"error": message}).encode("utf-8")
    h._send(status, "application/json; charset=utf-8", body)


def _body_json(h: Any) -> dict | None:
    length = int(h.headers.get("Content-Length") or 0)
    try:
        body = json.loads(h.rfile.read(length) or b"{}")
    except ValueError:
        _error(h, HTTPStatus.BAD_REQUEST, "not JSON")
        return None
    if not isinstance(body, dict):
        _error(h, HTTPStatus.BAD_REQUEST, "expected an object")
        return None
    return body


def _int(values: list[str] | None, fallback: int) -> int:
    try:
        return int((values or [""])[0])
    except (TypeError, ValueError):
        return fallback
