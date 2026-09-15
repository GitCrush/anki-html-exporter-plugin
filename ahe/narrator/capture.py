"""A card as a picture, for the video export.

The card is a web page, so something has to draw it. First choice is the
web engine Anki itself runs on -- the add-on is already inside it, and it
draws cards the way the reviewer does. It has to be asked on the GUI thread,
and a view that is never shown on screen may hand back a blank picture; when
it does, a Chromium on the machine is tried, headless.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Callable

CHROMIUM_NAMES = [
    "chromium-browser", "chromium", "google-chrome", "google-chrome-stable", "chrome",
    "brave-browser", "microsoft-edge",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "C:/Program Files/Google/Chrome/Application/chrome.exe",
    "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
]


class CaptureError(Exception):
    pass


Capturer = Callable[[str, int, int], bytes]


def chromium() -> str | None:
    for name in CHROMIUM_NAMES:
        found = shutil.which(name) or (name if Path(name).is_file() else None)
        if found:
            return found
    return None


def capture_with_chromium(url: str, width: int, height: int, binary: str | None = None) -> bytes:
    """Headless Chromium's own screenshot of the page.

    The page is drawn in place -- not in a frame that has to grow to its
    content -- so what the budget of virtual time leaves is the finished card.
    """
    binary = binary or chromium()
    if not binary:
        raise CaptureError("no Chromium or Chrome found to draw the cards with")
    with tempfile.TemporaryDirectory(prefix="narrator-shot-") as tmp:
        out = Path(tmp) / "card.png"
        subprocess.run(
            [binary, "--headless=new", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
             f"--window-size={width},{height}", "--virtual-time-budget=6000",
             f"--screenshot={out}", url],
            capture_output=True, timeout=120,
        )
        if not out.is_file():
            raise CaptureError("Chromium produced no picture")
        return out.read_bytes()


def capture_with_anki(url: str, width: int, height: int, timeout: float = 30.0) -> bytes:
    """Anki's QtWebEngine, asked on the GUI thread, in a view kept off screen."""
    from aqt import mw
    from aqt.qt import QBuffer, QIODevice, QTimer, QUrl, Qt, QWebEngineView

    done = threading.Event()
    result: dict = {}

    def on_main() -> None:
        view = QWebEngineView()
        view.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        view.resize(width, height)
        view.show()

        def finish(ok: bool) -> None:
            def grab() -> None:
                try:
                    if not ok:
                        raise CaptureError("the card page did not load")
                    image = view.grab().toImage()
                    buffer = QBuffer()
                    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
                    image.save(buffer, "PNG")
                    result["png"] = bytes(buffer.data())
                except Exception as exc:  # pragma: no cover - Qt specifics
                    result["error"] = exc
                finally:
                    view.deleteLater()
                    done.set()
            # Fonts, pictures and MathJax settle after loadFinished.
            QTimer.singleShot(800, grab)

        view.loadFinished.connect(finish)
        view.load(QUrl(url))

    mw.taskman.run_on_main(on_main)
    if not done.wait(timeout):
        raise CaptureError("Anki did not draw the card in time")
    if "error" in result:
        raise result["error"]
    return result["png"]


def looks_blank(png: bytes) -> bool:
    """A picture that is one colour all over is no picture of a card.

    PNG is zlib-compressed; a flat image compresses to next to nothing, so
    the size alone tells. A real card, even a plain one, has text.
    """
    return len(png) < 4000


def default_capturer(prefer_anki: bool = True) -> Capturer:
    """The first drawing method that works, remembered for the rest of the run."""
    state: dict = {"use": None}

    def capture(url: str, width: int, height: int) -> bytes:
        if state["use"] == "chromium":
            return capture_with_chromium(url, width, height)
        if state["use"] == "anki":
            return capture_with_anki(url, width, height)
        errors = []
        if prefer_anki:
            try:
                png = capture_with_anki(url, width, height)
                if not looks_blank(png):
                    state["use"] = "anki"
                    return png
                errors.append("Anki's web view drew a blank picture")
            except Exception as exc:
                errors.append(f"Anki's web view: {exc}")
        try:
            png = capture_with_chromium(url, width, height)
            state["use"] = "chromium"
            return png
        except Exception as exc:
            errors.append(str(exc))
        raise CaptureError("; ".join(errors))

    return capture
