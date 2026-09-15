#!/usr/bin/env python3
"""Hypnagog without Anki: the extraction, the endpoint, and the page.

    python3 tests/hypnagog.py

Needs Chromium on PATH. No Anki, no network.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import types
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_anki = types.ModuleType("anki")
_sound = types.ModuleType("anki.sound")
_sound.AV_REF_RE = re.compile(r"\[anki:(?P<param>[^\]]+)\]")
_sound.SoundOrVideoTag = type("SoundOrVideoTag", (), {})
sys.modules.setdefault("anki", _anki)
sys.modules.setdefault("anki.sound", _sound)

from ahe import live_server as server_module  # noqa: E402
from ahe.hypnagog import extract  # noqa: E402
from ahe.hypnagog.routes import Module  # noqa: E402

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(("  ok   " if condition else "  FAIL ") + name + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(name)


# Extraction
######################################################################

print("Extraction")
fields = {"Text": "Der {{c1::linke Ventrikel}} pumpt in die {{c2::Aorta::Gefäß}}. <b>Merke!</b>", "Extra": ""}
first = extract.extract_cloze_items(fields, 0)[0]
check("cloze: the deletion asked is the key", first["key"] == "linke Ventrikel", str(first))
check("cloze: the text has every deletion resolved", first["text"] == "Der linke Ventrikel pumpt in die Aorta. Merke!", first["text"])
check("cloze: the others show their hint", first["cloze"] == "Der linke Ventrikel pumpt in die [Gefäß]. Merke!", first["cloze"])
second = extract.extract_cloze_items(fields, 1)[0]
check("cloze: the second card asks the second deletion", second["key"] == "Aorta" and second["cloze"] == "Der [...] pumpt in die Aorta. Merke!", str(second))
check("cloze: a card without its deletion gives nothing", extract.extract_cloze_items(fields, 5) == [])
basic = extract.extract_basic_items({"Front": "Which chamber pumps into the aorta?", "Back": "The <i>left</i> ventricle. ID: 1234567890123"})[0]
check("basic: question is the key, answer the text, junk trimmed", basic["key"] == "Which chamber pumps into the aorta?" and basic["text"] == "The left ventricle.", str(basic))

# A fake collection
######################################################################

NOTES = {
    1: ("cloze", ["Der {{c1::linke Ventrikel}} pumpt in die {{c2::Aorta}}.", ""]),
    2: ("basic", ["Which chamber pumps into the aorta?", "The left ventricle."]),
    3: ("basic", ["", ""]),
}
CARDS = {1001: (1, 0), 1002: (1, 1), 1003: (2, 0), 1004: (3, 0)}


class FakeNote:
    def __init__(self, fields):
        self.fields = fields


class FakeCard:
    def __init__(self, cid):
        nid, self.ord = CARDS[cid]
        kind, fields = NOTES[nid]
        self._note = FakeNote(fields)
        self._model = {"type": 1 if kind == "cloze" else 0,
                       "flds": [{"name": "Text"}, {"name": "Extra"}] if kind == "cloze" else [{"name": "Front"}, {"name": "Back"}]}

    def note(self):
        return self._note

    def note_type(self):
        return self._model


SEARCHES: list[str] = []


class FakeCol:
    class media:
        @staticmethod
        def dir():
            return "/nonexistent"

    def find_cards(self, search, order=False):
        SEARCHES.append(search)
        return list(CARDS)

    def get_card(self, cid):
        return FakeCard(cid)


items = extract.find_items(FakeCol(), "deck:Anatomie", "due", 10)
check("items dealt from the scope, empty notes skipped", len(items) == 3 and all(i["text"] for i in items), str(items))
check("the source becomes a search term, hidden cards stay out", SEARCHES[-1] == "((deck:Anatomie) is:due) -is:suspended -is:buried", SEARCHES[-1])

# The server
######################################################################

print("Server")
TMP = Path(tempfile.mkdtemp(prefix="hypnagog-test-"))
STORED: dict = {}
srv = server_module.LiveServer(None, server_module.CollectionAccess(lambda: FakeCol()), tls_dir=TMP / "tls")
srv.hypnagog = Module(srv, config_reader=lambda: Module.__init__.__globals__["with_defaults"](STORED), config_writer=STORED.update)
url = srv.start()
base = url.split("/?")[0]
key = srv.token


def get(path: str) -> tuple[int, bytes]:
    request = urllib.request.Request(base + path, headers={"Cookie": f"ahe_key={key}"})
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def post(path: str, payload: dict) -> tuple[int, bytes]:
    request = urllib.request.Request(base + path, data=json.dumps(payload).encode(), method="POST",
                                     headers={"Cookie": f"ahe_key={key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(request) as response:
        return response.status, response.read()


status, body = get("/hypnagog")
check("page served", status == 200 and b"Hypnagog" in body and b"/assets/hypnagog/hypnagog.js" in body)
status, body = get("/assets/hypnagog/hypnagog.js")
check("engine served", status == 200 and b"class Hypnagog" in body)
status, body = get("/api/hypnagog/items?q=deck:Anatomie&source=new&max=2")
data = json.loads(body)
check("items endpoint", status == 200 and len(data["items"]) == 2 and data["source"] == "new" and "is:new" in SEARCHES[-1], body[:120].decode())
status, body = post("/api/hypnagog/config", {"show_ms": 1000, "visual_mode": 1, "card_source": "leeches", "max_cards": 9999, "bogus": 1})
cfg = json.loads(body)
check("config saved and bounded", cfg["show_ms"] == 1000 and cfg["visual_mode"] == 1 and cfg["card_source"] == "leeches" and cfg["max_cards"] == 500 and "bogus" not in cfg and STORED["show_ms"] == 1000, body.decode()[:160])

# The page
######################################################################

print("Page")
BROWSERS = ["chromium-browser", "chromium", "google-chrome", "google-chrome-stable"]
browser = next((shutil.which(b) for b in BROWSERS if shutil.which(b)), None)
if not browser:
    check("chromium on PATH", False)
else:
    probe = """
<script>
setTimeout(function () {
  var tries = 0;
  var timer = setInterval(function () {
    tries++;
    var box = document.querySelector('.px-mode-clean, .px-mode-polybius');
    /* the idle screen comes first; the sequence starts a second and a half in */
    if ((box && tries > 40) || tries > 80) {
      clearInterval(timer);
      var report = { started: !!box, sheetHiddenAtStart: document.getElementById('options').hidden,
        mode: box ? box.className : '', text: box ? box.textContent.slice(0, 200) : '',
        error: document.getElementById('error').textContent };
      /* The sheet on request: the engine pauses under it, and goes on when it closes */
      document.getElementById('options-open').click();
      report.sheetShown = !document.getElementById('options').hidden;
      report.pausedUnderSheet = !!(window.hypnagog && window.hypnagog.paused);
      report.buttonSaysApply = document.getElementById('start').textContent;
      document.getElementById('options-close').click();
      report.resumed = !!(window.hypnagog && !window.hypnagog.paused);
      var pre = document.createElement('pre');
      pre.id = 'hypnagog-probe';
      pre.textContent = JSON.stringify(report);
      document.body.appendChild(pre);
    }
  }, 100);
}, 500);
</script>
"""
    index_path = ROOT / "ahe" / "hypnagog" / "web" / "index.html"
    original = index_path.read_bytes()
    index_path.write_text(original.decode().replace("</body>", probe + "</body>"))
    try:
        result = subprocess.run(
            [browser, "--headless=new", "--disable-gpu", "--no-sandbox", "--autoplay-policy=no-user-gesture-required",
             "--virtual-time-budget=12000", "--dump-dom", url.replace("/?key=", "/hypnagog?key=") + "&q=deck:Anatomie"],
            capture_output=True, text=True, timeout=90,
        )
    finally:
        index_path.write_bytes(original)
    match = re.search(r'<pre id="hypnagog-probe">(.*?)</pre>', result.stdout, re.S)
    if not match:
        check("probe ran", False, (result.stderr[-600:] + result.stdout[-600:]))
    else:
        import html as html_module

        report = json.loads(html_module.unescape(match.group(1)))
        check("presentation starts by itself, the sheet out of the way", report["started"] and report["sheetHiddenAtStart"], str(report)[:200])
        check("the sheet comes on request and pauses the engine", report["sheetShown"] and report["pausedUnderSheet"] and report["buttonSaysApply"] == "Apply" and report["resumed"], str(report)[:300])
        check("the look chosen in the config", report["mode"] == "px-mode-clean", report["mode"])
        check("a card's text on screen", "Ventrikel" in report["text"] or "ventricle" in report["text"], report["text"][:120])

srv.stop()
shutil.rmtree(TMP, ignore_errors=True)
print()
if failures:
    print(f"{len(failures)} failed: " + ", ".join(failures))
    raise SystemExit(1)
print("all good")
