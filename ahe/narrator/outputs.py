"""The audiobook and the video: a scope's narrations, cut into one file.

Both are built from the cache that the slide show fills, by ffmpeg. The
audiobook is the narrations in order with a pause between them and a chapter
mark per card; the video is the same sound under a still picture of each
card. Cards not narrated yet are narrated first -- if asked to, since each
costs a model call and a speech call -- or left out.

One export at a time, on a thread of its own; the page polls its progress.
"""

from __future__ import annotations

import datetime
import html
import json
import re
import shutil
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from . import mp3
from .capture import Capturer
from ..renderer import RenderedCard
from .narrate import PARALLEL, Narrator, Narration
from .text import _spoken_side
from .usage import chat_cost, speech_cost

VIDEO_WIDTH = 1280
VIDEO_HEIGHT = 720
# A card with nothing to say is held for this long
HOLD_SECONDS = 2.0
# More than this and the export is a project, not a click
MAX_CARDS = 2000
# What a narration costs, when there is nothing yet to average over: the
# system prompt and a card's text in, the words of the script out.
PROMPT_TOKENS = 450
TOKENS_PER_WORD = 1.4


class ExportError(Exception):
    pass


def ffmpeg_path(configured: str = "") -> str | None:
    return shutil.which(configured or "ffmpeg")


class Exporter:
    def __init__(self, narrator: Narrator, exports_dir: Path, ffmpeg: str = "ffmpeg") -> None:
        self.narrator = narrator
        self.exports_dir = exports_dir
        self.ffmpeg = ffmpeg
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._process: subprocess.Popen | None = None
        self._state: dict[str, Any] = {"phase": "idle"}

    # Before: what it would take
    ##################################################################

    def estimate(
        self, cards: list[RenderedCard], config: dict, seconds: int, voice: str, speed: float = 1.0
    ) -> dict:
        prices = config.get("prices", {})
        ready = 0
        ready_seconds = 0.0
        for card in cards:
            duration = self.narrator.cached_duration(card, config, seconds, voice)
            if duration is not None:
                ready += 1
                ready_seconds += duration or HOLD_SECONDS
        missing = len(cards) - ready
        words = seconds * self.narrator.pace.rate()
        per_card = (chat_cost(prices, config["text_model"], PROMPT_TOKENS, int(words * TOKENS_PER_WORD)) or 0) + (
            speech_cost(prices, config["tts_model"], int(words * 6), seconds) or 0
        )
        return {
            "total": len(cards),
            "ready": ready,
            "missing": missing,
            "cost": round(missing * per_card, 2),
            "priced": chat_cost(prices, config["text_model"], 1, 1) is not None
            and speech_cost(prices, config["tts_model"], 1, 1) is not None,
            "minutes": round(((ready_seconds + missing * seconds) / speed + len(cards) * (config["gap"] + float(config.get("reveal_gap") or 0))) / 60, 1),
            "too_many": len(cards) > MAX_CARDS,
            "max": MAX_CARDS,
        }

    # Running
    ##################################################################

    def start(
        self,
        kind: str,
        cards: list[RenderedCard],
        config: dict,
        seconds: int,
        voice: str,
        narrate_missing: bool,
        title: str,
        *,
        speed: float = 1.0,
        frame_url: Callable[[int, str], str] | None = None,
        capturer: Capturer | None = None,
    ) -> dict:
        if kind not in ("audio", "video"):
            raise ExportError("no such export")
        if kind == "video" and (frame_url is None or capturer is None):
            raise ExportError("the video export needs a way to draw the cards")
        if not ffmpeg_path(self.ffmpeg):
            raise ExportError("ffmpeg was not found; install it, or set its path in the config")
        if len(cards) > MAX_CARDS:
            raise ExportError(f"that is {len(cards)} cards; an export takes at most {MAX_CARDS}")
        if not cards:
            raise ExportError("nothing to export")
        with self._lock:
            if self._thread and self._thread.is_alive():
                raise ExportError("an export is already running")
            self._cancel = threading.Event()
            self._state = {
                "phase": "narrating", "kind": kind, "done": 0, "total": len(cards),
                "skipped": 0, "error": "", "file": "", "title": title, "started": time.time(),
            }
            self._thread = threading.Thread(
                target=self._run,
                args=(kind, cards, config, seconds, voice, narrate_missing, title, frame_url, capturer,
                      min(3.0, max(0.5, speed)), self._cancel),
                name="narrator-export", daemon=True,
            )
            self._thread.start()
        return self.status()

    def cancel(self) -> dict:
        self._cancel.set()
        process = self._process
        if process is not None:
            try:
                process.kill()
            except OSError:
                pass
        return self.status()

    def status(self) -> dict:
        state = dict(self._state)
        state["running"] = bool(self._thread and self._thread.is_alive())
        return state

    def _set(self, **changes: Any) -> None:
        self._state.update(changes)

    def _run(self, kind, cards, config, seconds, voice, narrate_missing, title, frame_url, capturer, speed, cancel) -> None:
        try:
            with tempfile.TemporaryDirectory(prefix="narrator-export-") as tmp:
                work = Path(tmp)
                pieces = self._narrate(cards, config, seconds, voice, narrate_missing, cancel)
                if cancel.is_set():
                    raise ExportError("cancelled")
                if not pieces:
                    raise ExportError("none of these cards has a narration yet")
                name = self._output_name(title, "mp3" if kind == "audio" else "mp4")
                self.exports_dir.mkdir(parents=True, exist_ok=True)
                book = self._audiobook(pieces, config["gap"], float(config.get("reveal_gap") or 0), title, work, work / "book.mp3", speed)
                if kind == "audio":
                    shutil.move(str(book), str(self.exports_dir / name))
                else:
                    self._capture(pieces, frame_url, capturer, work, cancel)
                    if cancel.is_set():
                        raise ExportError("cancelled")
                    self._video(pieces, title, work, book, self.exports_dir / name)
                self._set(phase="done", file=name, finished=time.time())
        except ExportError as exc:
            self._set(phase="cancelled" if str(exc) == "cancelled" else "failed", error=str(exc))
        except Exception as exc:  # pragma: no cover - ffmpeg and friends
            self._set(phase="failed", error=f"{type(exc).__name__}: {exc}")

    # Stages
    ##################################################################

    def _narrate(self, cards, config, seconds, voice, narrate_missing, cancel) -> list[dict]:
        """(card, narration) for every card that has or gets one, in order."""
        results: dict[int, Narration | None] = {}

        def one(index: int, card: RenderedCard) -> None:
            if cancel.is_set():
                return
            duration = self.narrator.cached_duration(card, config, seconds, voice)
            if duration is None and not narrate_missing:
                results[index] = None
            else:
                try:
                    results[index] = self.narrator.narrate(card, config, seconds, voice)
                except Exception as exc:
                    results[index] = None
                    self._set(error=f"card {card.card_id}: {exc}")
            self._set(done=self._state["done"] + 1)

        with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
            for index, card in enumerate(cards):
                pool.submit(one, index, card)
        pieces = []
        for index, card in enumerate(cards):
            narration = results.get(index)
            if narration is None:
                self._set(skipped=self._state["skipped"] + 1)
                continue
            pieces.append({"card": card, "narration": narration})
        return pieces

    def _segments(self, piece: dict, gap: float, reveal_gap: float, speed: float) -> list[tuple[str, Path | None, float]]:
        """What a card plays, in order: (side on screen, audio or silence, seconds).

        The narration, then the pause before the next card; a card with
        nothing to say is held for a moment. Speech is divided by the speed,
        since the whole stream is stretched afterwards; the silences are
        written longer by the same factor and come out right. The side is
        the video's business: the front for the first reveal_gap seconds of
        the narration, the back from then on -- as the page shows it.
        """
        narration = piece["narration"]
        segments: list[tuple[str, Path | None, float]] = []
        if narration.duration > 0:
            segments.append(("a", narration.audio, narration.duration / speed))
        else:
            segments.append(("a", None, HOLD_SECONDS))
        if gap > 0:
            segments.append(("a", None, gap))
        piece["reveal_at"] = min(reveal_gap, segments[0][2])
        return segments

    def _audiobook(self, pieces, gap, reveal_gap, title, work: Path, out: Path, speed: float = 1.0) -> Path:
        rate = 24000
        for piece in pieces:
            found = mp3.sample_rate(piece["narration"].audio)
            if found:
                rate = found
                break
        silences: dict[float, Path] = {}

        def silence(seconds: float) -> Path:
            key = round(seconds, 3)
            if key not in silences:
                silences[key] = self._silence(work / f"silence-{len(silences)}.mp3", rate, seconds * speed)
            return silences[key]

        entries = []
        position = 0.0
        chapters = []
        for index, piece in enumerate(pieces):
            segments = self._segments(piece, gap, reveal_gap, speed)
            piece["segments"] = segments
            piece["start"] = position
            spoken_length = sum(length for _, _, length in segments) - (gap if gap > 0 else 0)
            chapters.append((position, position + spoken_length, _chapter_title(index, piece["card"])))
            for _, audio, length in segments:
                entries.append(audio if audio is not None else silence(length))
                position += length
        listing = work / "audio.txt"
        listing.write_text("".join(f"file '{_ffpath(path)}'\n" for path in entries), "utf-8")
        meta = work / "meta.txt"
        meta.write_text(_ffmetadata(title, chapters), "utf-8")
        self._set(phase="encoding")
        self._ffmpeg([
            "-f", "concat", "-safe", "0", "-i", str(listing), "-i", str(meta),
            "-map", "0:a", "-map_metadata", "1", "-id3v2_version", "3",
            *(["-filter:a", f"atempo={speed:.3f}"] if abs(speed - 1.0) > 0.01 else []),
            "-c:a", "libmp3lame", "-q:a", "3", str(out),
        ])
        return out

    def _capture(self, pieces, frame_url, capturer, work: Path, cancel) -> None:
        self._set(phase="capturing", done=0, total=len(pieces))
        for index, piece in enumerate(pieces):
            if cancel.is_set():
                return
            card = piece["card"]
            piece["images"] = {}
            for side in (("q", "a") if piece.get("reveal_at", 0) > 0 else ("a",)):
                png = capturer(frame_url(card.card_id, side), VIDEO_WIDTH, VIDEO_HEIGHT)
                path = work / f"card-{index:05d}-{side}.png"
                path.write_bytes(png)
                piece["images"][side] = path
            self._set(done=index + 1)

    def _video(self, pieces, title, work: Path, book: Path, out: Path) -> None:
        lines = []
        total = 0.0
        last = None
        for piece in pieces:
            # The front for the first seconds of the card, the back from then on
            front_left = piece.get("reveal_at", 0) if "q" in piece["images"] else 0
            for _, _, length in piece["segments"]:
                if front_left > 0:
                    front = min(front_left, length)
                    lines.append(f"file '{_ffpath(piece['images']['q'])}'\nduration {front:.3f}\n")
                    total += front
                    length -= front
                    front_left -= front
                if length > 0:
                    lines.append(f"file '{_ffpath(piece['images']['a'])}'\nduration {length:.3f}\n")
                    total += length
            last = piece["images"]["a"]
        # The concat demuxer drops the last duration unless the file is named again.
        lines.append(f"file '{_ffpath(last)}'\n")
        listing = work / "video.txt"
        listing.write_text("".join(lines), "utf-8")
        meta = work / "meta.txt"
        self._set(phase="encoding")
        self._ffmpeg([
            "-f", "concat", "-safe", "0", "-i", str(listing), "-i", str(book), "-i", str(meta),
            "-map", "0:v", "-map", "1:a", "-map_metadata", "2",
            "-vf", f"scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=decrease,"
                   f"pad={VIDEO_WIDTH}:{VIDEO_HEIGHT}:(ow-iw)/2:(oh-ih)/2:color=white,format=yuv420p",
            "-r", "10", "-c:v", "libx264", "-preset", "veryfast", "-tune", "stillimage",
            "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
            "-t", f"{total:.3f}", str(out),
        ])

    # Pieces
    ##################################################################

    def _silence(self, path: Path, rate: int, seconds: float) -> Path:
        self._ffmpeg([
            "-f", "lavfi", "-i", f"anullsrc=r={rate}:cl=mono", "-t", f"{seconds:.3f}",
            "-c:a", "libmp3lame", "-q:a", "9", str(path),
        ])
        return path

    def _ffmpeg(self, args: list[str]) -> None:
        binary = ffmpeg_path(self.ffmpeg)
        if not binary:
            raise ExportError("ffmpeg was not found")
        self._process = subprocess.Popen(
            [binary, "-y", "-hide_banner", "-loglevel", "error", *args],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        try:
            _, err = self._process.communicate()
        finally:
            code = self._process.returncode
            self._process = None
        if self._cancel.is_set():
            raise ExportError("cancelled")
        if code != 0:
            raise ExportError("ffmpeg failed: " + err.decode("utf-8", "replace").strip()[-400:])

    def _output_name(self, title: str, extension: str) -> str:
        slug = re.sub(r"[^\w]+", "-", title, flags=re.UNICODE).strip("-")[:60] or "narrator"
        stamp = datetime.datetime.now().strftime("%Y-%m-%d-%H%M")
        return f"{slug}-{stamp}.{extension}"


def _chapter_title(index: int, card: RenderedCard) -> str:
    text = _spoken_side(card.question) or _spoken_side(card.answer)
    text = re.sub(r"\[\.\.\.\]", "…", text)
    return f"{index + 1}. {text[:70]}".strip() if text else f"{index + 1}."


def _ffmetadata(title: str, chapters: list[tuple[float, float, str]]) -> str:
    lines = [";FFMETADATA1", f"title={_ffmeta_escape(title)}", ""]
    for start, end, name in chapters:
        lines += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={int(start * 1000)}",
                  f"END={int(end * 1000)}", f"title={_ffmeta_escape(name)}", ""]
    return "\n".join(lines)


def _ffmeta_escape(text: str) -> str:
    return re.sub(r"([=;#\\\n])", r"\\\1", text)


def _ffpath(path: Path) -> str:
    return str(path).replace("'", r"'\''")


# The card as one page for the picture
######################################################################

# On the revealed back, the cloze asked on this card -- Anki marks it with
# class "cloze", the note's other deletions "cloze-inactive" -- is lifted
# out, whatever the note type's own colours: this is the one thing the
# narration is about.
FOCUS_CSS = """
body.narrator-revealed .cloze { background: rgba(255, 200, 0, .32); box-shadow: 0 0 0 3px rgba(255, 200, 0, .32); border-radius: 3px; }
"""


def frame_document(card: RenderedCard, css: str, shared: dict, night: bool = False, side: str = "a") -> str:
    """The card as one page, sized to the video frame: its front, or its back.

    The same pieces the slide show puts into its frames, with one more
    stylesheet: the page fills the frame, and a card taller than it is scaled
    down rather than cut off. The back carries the front above it when the
    back does not already repeat it, as the slide show shows it.
    """
    sides = []
    if side == "q":
        sides.append(f'<div class="narrator-side">{card.question}</div>')
    elif not card.front_redundant:
        sides.append(f'<div class="narrator-side">{card.question}</div>')
        sides.append('<hr class="narrator-divider">')
        sides.append(f'<div class="narrator-side">{card.answer_only or card.answer}</div>')
    else:
        sides.append(f'<div class="narrator-side">{card.answer}</div>')
    body_class = card.body_class + (" nightMode night_mode" if night else "") + (" narrator-revealed" if side == "a" else "")
    html_class = ' class="night-mode"' if night else ""
    fit = """
html, body { margin: 0; min-height: 100%; }
body { display: flex; align-items: center; justify-content: center; box-sizing: border-box;
       min-height: 100vh; padding: 24px 40px; }
#qa { width: 100%; transform-origin: top center; }
.narrator-divider { border: 0; border-top: 1px dashed rgba(128,128,128,.6); margin: 14px 0; }
""" + FOCUS_CSS
    fit_script = """
(function () {
  function fit() {
    var qa = document.getElementById("qa");
    var room = window.innerHeight - 48;
    var need = qa.scrollHeight;
    if (need > room) {
      var scale = room / need;
      qa.style.transform = "scale(" + scale + ")";
      qa.style.marginBottom = -(need - room) + "px";
    }
  }
  window.addEventListener("load", function () { setTimeout(fit, 300); setTimeout(fit, 1500); });
})();
"""
    mathjax = ""
    if card.mathjax and shared.get("mathjaxUrl"):
        config = {
            "tex": {"displayMath": [["\\[", "\\]"]], "processEscapes": False,
                    "processEnvironments": False, "processRefs": False,
                    "packages": {"[+]": ["noerrors", "mathtools"], "[-]": ["textmacros"]}},
            "loader": {"load": ["[tex]/noerrors", "[tex]/mathtools"],
                       "paths": {"mathjax": shared.get("mathjaxDir", "")}},
            "startup": {"typeset": True},
        }
        mathjax = (f"<script>window.MathJax={json.dumps(config)};</script>"
                   f'<script id="MathJax-script" async src="{html.escape(shared["mathjaxUrl"])}"></script>')
    jquery = ""
    if card.jquery and shared.get("jqueryUrl"):
        jquery = f'<script src="{html.escape(shared["jqueryUrl"])}"></script>'
    return (
        f'<!doctype html><html{html_class} dir="ltr"><head><meta charset="utf-8">'
        f"<script>{shared['shimJs']}</script>"
        f"<style>:root{{--ahe-vh:{VIDEO_HEIGHT}px}}</style>"
        f"<style>{shared['theme']}</style><style>{shared['reviewer']}</style>"
        f"<style>{shared['frame']}</style><style>{css}</style><style>{fit}</style>"
        f"{jquery}{mathjax}</head>"
        f'<body class="{html.escape(body_class)}"><div id="qa">{"".join(sides)}</div>'
        f"<script>window.AHE_FRAME_ID='video';window.AHE_INTERACTIVE=false;"
        f"window.AHE_REVEALED=null;window.AHE_SIDE={json.dumps(side)};</script><script>{shared['frameJs']}</script>"
        f"<script>{fit_script}</script></body></html>"
    )
