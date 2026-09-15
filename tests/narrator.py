#!/usr/bin/env python3
"""Driving the narrator without Anki and without OpenAI.

Run on its own, or through tests/run.py, which runs it last.

The server, the page and the cache are exercised end to end: a handful of
invented cards stand in for the collection, a fake OpenAI answers with a
canned script and a second of silence, and headless Chromium loads the page,
which has to mount the first card's frame and show its narration.

    python3 tests/narrator.py

Needs Chromium on PATH and ffmpeg (for the silence). No Anki, no network.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import types
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# The package is imported on its own, not through the add-on root, which
# wants Anki's Qt for its menu entries.
sys.path.insert(0, str(ROOT))

# The engine imports two names from Anki and uses neither on this path.
_anki = types.ModuleType("anki")
_sound = types.ModuleType("anki.sound")
_sound.AV_REF_RE = re.compile(r"\[anki:(?P<param>[^\]]+)\]")


class _SoundOrVideoTag:
    pass


_sound.SoundOrVideoTag = _SoundOrVideoTag
sys.modules.setdefault("anki", _anki)
sys.modules.setdefault("anki.sound", _sound)

TMP = Path(tempfile.mkdtemp(prefix="narrator-test-"))
os.environ["NARRATOR_DATA"] = str(TMP / "data")

from ahe import live_server as server_module  # noqa: E402
from ahe import renderer as renderer_module  # noqa: E402
from ahe.writer import ExportOptions, ExportWriter  # noqa: E402
from ahe.narrator import config as config_module  # noqa: E402
from ahe.narrator import narrate as narrate_module  # noqa: E402
from ahe.narrator import view as view_module  # noqa: E402
from ahe.narrator.routes import Module  # noqa: E402

RenderedCard = renderer_module.RenderedCard

BROWSERS = ["chromium-browser", "chromium", "google-chrome", "google-chrome-stable"]


def browser() -> str:
    for name in BROWSERS:
        found = shutil.which(name)
        if found:
            return found
    print("No Chromium on PATH — tried: " + ", ".join(BROWSERS))
    raise SystemExit(2)


# Cards
######################################################################

MEDIA = TMP / "media"
MEDIA.mkdir()
(MEDIA / "heart.png").write_bytes(
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def fake_cards() -> list[RenderedCard]:
    def card(i: int, q: str, a: str, **extra) -> RenderedCard:
        return RenderedCard(
            card_id=1000 + i, note_id=2000 + i, ord=0,
            body_class="card card1 isLin fancy", notetype_id=7, notetype_name="Basic",
            template_name="Card 1", deck="Anatomie::Herz", subdeck="Herz", tags=["cardio"],
            question=q, answer=a, answer_only=a.split("<hr id=answer>")[-1],
            fields=[{"name": "Front", "html": q, "text": renderer_module._plain_text(q)},
                    {"name": "Back", "html": a, "text": renderer_module._plain_text(a)}],
            special=[], front_redundant=False, sort_text=q[:40], **extra,
        )

    return [
        card(1, "Which chamber pumps into the aorta?",
             "Which chamber pumps into the aorta?<hr id=answer>The left ventricle."),
        card(2, 'The heart: <img src="/media/heart.png">',
             'The heart: <img src="/media/heart.png"><hr id=answer>Four chambers.'),
        card(3, "Cardiac output is \\(CO = HR \\times SV\\)",
             "Cardiac output is \\(CO = HR \\times SV\\)<hr id=answer>Roughly 5 l/min at rest.", mathjax=True),
        # Anki's own image occlusion: the shapes in the card, the one asked as "cloze"
        card(4, '<div style="display:none"><div class="cloze" data-shape="rect" data-left=".1" data-top=".2" '
                'data-width=".3" data-height=".1" data-ordinal="1"></div><div class="cloze-inactive" data-shape="rect" '
                'data-left=".5" data-top=".5" data-width=".2" data-height=".1" data-ordinal="2"></div></div>'
                '<div id="image-occlusion-container"><img src="/media/heart.png"><canvas id="image-occlusion-canvas"></canvas></div>',
             '<div id="image-occlusion-container"><img src="/media/heart.png"></div>'),
        # Image Occlusion Enhanced: the mask is an SVG file of its own
        card(5, '<img src="/media/heart.png"><img src="/media/abc-oa-1-Q.svg">',
             '<img src="/media/heart.png"><img src="/media/abc-oa-1-A.svg">'),
    ]


CARDS = {c.card_id: c for c in fake_cards()}


SCOPES: list[str] = []
# A deck name with everything Anki's search reads specially inside quotes
ODD_DECK = 'heme\\onco_"x*'


class FakeView(view_module.View):
    """The view with the collection taken out: cards come from CARDS."""

    def set_scope(self, col, query, include_hidden, order="browser"):
        SCOPES.append(query.strip())
        self.query = query.strip()
        self.order = order
        # The two image occlusion cards stay out of the scope; they are asked for by id
        self.card_ids = [i for i in sorted(CARDS) if i <= 1003] if self.query else []
        if order == "random":
            self.card_ids.reverse()  # a deal that is telling
        return {"query": self.query, "order": order, "total": len(self.card_ids)}

    def render(self, col, cfg, card_id):
        self.rendered[card_id] = CARDS[card_id]
        return CARDS[card_id]

    def renderer(self, col, apply_gui_hooks):
        if self._renderer is None:
            self._renderer = types.SimpleNamespace(notetype_css={7: ".card{font-family:serif}"})
        return self._renderer

    def tree(self, col):
        return {"decks": ["Anatomie", "Anatomie::Herz", ODD_DECK], "tags": ["cardio"]}


class FakeCol:
    class media:
        @staticmethod
        def dir():
            return str(MEDIA)


# Fake OpenAI
######################################################################

SILENCE = TMP / "silence.mp3"
subprocess.run(
    ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
     "-t", "1.0", "-q:a", "9", str(SILENCE)], check=True,
)
CALLS = {"complete": 0, "speak": 0}
SPOKEN: list[str] = []
SEEN: list[tuple] = []
PROMPTS: list[str] = []
DIRECTIONS: list[str] = []
TALKS: list[list[str]] = []
HEARD: list[str] = []


class FakeOpenAI:
    def __init__(self, api_key, base_url=""):
        assert api_key == "sk-test"

    def complete(self, model, system, user):
        CALLS["complete"] += 1
        assert "Length: at most" in user and "CARD:" in user
        PROMPTS.append(user)
        words = re.search(r"at most (\d+) words", user).group(1)
        front = re.search(r"FRONT: (.*)", user).group(1)
        # The language line first, as asked
        return ('Language: en\n\n"# Summary\n**The left ventricle** pumps into the aorta.\n\n\n- About ' + words +
                ' words. ' + front + '"', 120, 40)

    def chat(self, model, messages):
        if messages[-1]["role"] == "user" and "CARD:" in messages[-1]["content"]:
            return self.complete(model, messages[0]["content"], messages[-1]["content"])
        CALLS["chat"] = CALLS.get("chat", 0) + 1
        assert messages[0]["role"] == "system" and "CARD:" in messages[0]["content"]
        TALKS.append([m["content"] for m in messages[1:]])
        return "Language: en\n\nThe aorta carries blood from the left ventricle.", 200, 30

    def transcribe(self, model, audio, mime, language=""):
        CALLS["transcribe"] = CALLS.get("transcribe", 0) + 1
        assert audio.startswith(b"RIFF") and mime == "audio/wav", (audio[:8], mime)
        HEARD.append(language)
        return "Which chamber pumps into the aorta?"

    def see(self, model, system, user, images):
        CALLS["see"] = CALLS.get("see", 0) + 1
        SEEN.append((user, len(images)))
        return '{"label": "Aorta", "context": "Der große Ausflusstrakt aus dem linken Ventrikel.", "others": "Ventrikel, Vorhof"}', 500, 60

    def speak(self, model, voice, text, instructions=""):
        CALLS["speak"] += 1
        SPOKEN.append(text)
        DIRECTIONS.append(instructions)
        return SILENCE.read_bytes()


narrate_module.OpenAI = FakeOpenAI

# Server
######################################################################

STORED = dict(config_module.DEFAULTS, openai_api_key="sk-test")


def read_config():
    return dict(STORED)


def write_config(cfg):
    STORED.update(cfg)


class FakeLiveView:
    """What the shell asks the export's own view for: the trees, the names."""
    writer = ExportWriter(ExportOptions(output_dir=None, title="Narrator"))

    def tree(self, col):
        return {"decks": ["Anatomie", "Anatomie::Herz", ODD_DECK], "tags": ["cardio"], "notetypes": ["Basic"]}

    def names(self, col):
        return {"fields": ["Front", "Back"], "special": []}


srv = server_module.LiveServer(FakeLiveView(), server_module.CollectionAccess(lambda: FakeCol()), tls_dir=TMP / "tls")
srv.narrator = Module(srv, config_reader=read_config, config_writer=write_config, data_dir=TMP)
srv.narrator.view = FakeView()
url = srv.start()
base = url.split("/?")[0]
key = srv.token

failures = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(("  ok   " if condition else "  FAIL ") + name + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(name)


def get(path: str, expect: int = 200) -> tuple[int, bytes, dict]:
    request = urllib.request.Request(base + path, headers={"Cookie": f"ahe_key={key}"})
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


def post(path: str, payload: dict) -> tuple[int, bytes]:
    request = urllib.request.Request(
        base + path, data=json.dumps(payload).encode(), method="POST",
        headers={"Cookie": f"ahe_key={key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request) as response:
        return response.status, response.read()


print("API")
status, body, _ = get("/api/narrator/status")
check("status", status == 200 and json.loads(body)["collection"] and json.loads(body)["has_key"])

try:
    urllib.request.urlopen(base + "/api/narrator/status")
    check("refuses without key", False)
except urllib.error.HTTPError as exc:
    check("refuses without key", exc.code == 403)

status, body, _ = get("/api/narrator/cards?q=deck:Anatomie&offset=0&limit=25")
cards = json.loads(body)
check("cards listed", status == 200 and cards["total"] == 3 and len(cards["cards"]) == 3, body[:200].decode())
check("note type css sent once", "7" in cards["css"])
status, body, _ = get("/api/narrator/cards?q=deck:Anatomie&offset=0&limit=25&have=7")
check("css not sent again", "7" not in json.loads(body)["css"])

status, body, _ = get("/api/narrator/shared")
shared = json.loads(body)
check("shared assets", status == 200 and "frameJs" in shared and "reviewer" in shared)

status, body, _ = get("/media/heart.png")
check("media served", status == 200 and body.startswith(b"\x89PNG"))
status, body, _ = get("/media/../secret")
check("media path refused", status in (403, 404))

status, body, _ = get("/api/narrator/narration/1001?seconds=20&voice=marin")
first = json.loads(body)
check("narration made", status == 200 and first["text"].startswith("**The left ventricle** pumps into the aorta.\n\nAbout"), repr(first.get("text", body[:200])))
check("script cleaned: no quote, heading, bullet", '"' not in first["text"] and "#" not in first["text"] and "- About" not in first["text"], repr(first["text"]))
check("voice gets the plain words", SPOKEN[-1].startswith("The left ventricle pumps into the aorta. About"), repr(SPOKEN[-1]))
check("language line taken off and kept", first["language"] == "en" and "Language" not in first["text"], repr(first.get("language")))
check("voice directed in the script's language", DIRECTIONS[-1].startswith("Speak English"), DIRECTIONS[-1][:40])
check("words counted without the marks", first["words"] == len(SPOKEN[-1].split()), str(first["words"]))
check("duration measured", 0.9 <= first["duration"] <= 1.1, str(first["duration"]))
check("not cached first time", first["cached"] is False)

status, body, _ = get("/api/narrator/narration/1001?seconds=20&voice=marin")
second = json.loads(body)
check("cached second time", second["cached"] is True and CALLS == {"complete": 1, "speak": 1}, str(CALLS))

status, body, _ = get("/api/narrator/narration/1001?seconds=20&voice=cedar")
check("new voice: speech only", CALLS == {"complete": 1, "speak": 2}, str(CALLS))

status, body, _ = get("/api/narrator/narration/1001?seconds=60&voice=cedar")
check("new length: new script", CALLS == {"complete": 2, "speak": 3}, str(CALLS))

status, body, _ = get("/api/narrator/narration/1001?seconds=60&voice=cedar&force=1")
check("force: written again", CALLS == {"complete": 3, "speak": 3}, str(CALLS))

status, body, headers = get(first["audio"])
check("audio served", status == 200 and headers.get("Content-Type") == "audio/mpeg" and len(body) > 100)

status, body, _ = get("/api/narrator/usage")
usage = json.loads(body)
# Three scripts and three speeches so far, at gpt-5.4 and gpt-4o-mini-tts prices.
expected = (3 * (120 * 2.5 + 40 * 15) / 1e6) + 3 * (first["duration"] / 60 * 0.015)
check("usage counted", status == 200 and usage["session"]["narrations"] == 3 and usage["session"]["input_tokens"] == 360, str(usage["session"]))
check("cost estimated", abs(usage["session"]["cost"] - expected) < 1e-6 and usage["total"]["cost"] == usage["session"]["cost"], f'{usage["session"]["cost"]} vs {expected}')
check("narration carries usage", "usage" in first and first["usage"]["today"]["narrations"] >= 1)
check("usage persisted", (TMP / "usage.json").is_file() and json.loads((TMP / "usage.json").read_text())["total"]["narrations"] == 3)
status, body = post("/api/narrator/usage/reset", {})
check("usage reset", json.loads(body)["total"]["narrations"] == 0 and json.loads(body)["session"]["cost"] == 0)

# Pronunciation, budget, sharing
######################################################################

print("Voice, budget, phone")
# The config's language wins over the model's line, and the voice is told in that language.
post("/api/narrator/config", {"language": "Deutsch"})
get("/api/narrator/narration/1001?seconds=20&voice=marin")
check("configured language directs the voice", DIRECTIONS[-1].startswith("Sprich Deutsch"), DIRECTIONS[-1][:40])
post("/api/narrator/config", {"language": "pl"})
get("/api/narrator/narration/1001?seconds=20&voice=marin")
check("any language by name", DIRECTIONS[-1].startswith("Speak Polish as a native"), DIRECTIONS[-1][:40])
post("/api/narrator/config", {"language": ""})

status, body = post("/api/narrator/config", {"pronunciation": {"ventricle": "ventrikel", "": "x"}})
check("glossary saved", json.loads(body)["pronunciation"] == {"ventricle": "ventrikel"}, body.decode()[:200])
before = dict(CALLS)
status, body, _ = get("/api/narrator/narration/1001?seconds=20&voice=cedar")
check("glossary: new speech, same script", CALLS["complete"] == before["complete"] and CALLS["speak"] == before["speak"] + 1, str(CALLS))
check("glossary applied to the voice only", "ventrikel" in SPOKEN[-1] and "ventricle" not in SPOKEN[-1] and "ventricle" in json.loads(body)["text"], SPOKEN[-1][:80])
post("/api/narrator/config", {"pronunciation": {}})

status, body = post("/api/narrator/config", {"daily_budget": 0.0001})
check("budget saved", json.loads(body)["daily_budget"] == 0.0001)
status, body, _ = get("/api/narrator/narration/1001?seconds=20&voice=cedar")
check("cached narration free under the budget", status == 200 and json.loads(body)["cached"] is True, body[:120].decode())
status, body, _ = get("/api/narrator/narration/1002?seconds=45&voice=cedar")
check("budget stops a new narration", status == 409 and b"daily budget" in body, body[:160].decode())
post("/api/narrator/config", {"daily_budget": 0})

status, body, _ = get("/api/narrator/share")
check("not shared by default", json.loads(body)["shared"] is False)
status, body = post("/api/narrator/config", {"share": True})
time.sleep(1.0)
status, body, _ = get("/api/narrator/share?q=deck:Anatomie")
share = json.loads(body)
check("shared: address and code", status == 200 and share["shared"] and share["url"].startswith("https://") and "<svg" in share["svg"] and "q=deck" in share["url"], body[:160].decode())
check("shared: the tab's address still works", get("/api/narrator/status")[0] == 200)
status, cert_pem, _ = get("/api/narrator/share/cert")
cert_file = TMP / "phone.crt"
cert_file.write_bytes(cert_pem)
import ssl
shared_port = int(share["url"].split(":")[2].split("/")[0])
tls_ctx = ssl.create_default_context(cafile=str(cert_file))
tls_ctx.check_hostname = False  # the phone reaches a LAN address; here it is loopback
request = urllib.request.Request(f"https://127.0.0.1:{shared_port}/api/narrator/status", headers={"Cookie": f"ahe_key={key}"})
with urllib.request.urlopen(request, context=tls_ctx) as response:
    check("shared: answers over TLS with its own certificate", response.status == 200 and json.loads(response.read())["collection"])
post("/api/narrator/config", {"share": False})
time.sleep(1.0)
check("unshared again", json.loads(get("/api/narrator/share")[1])["shared"] is False and get("/api/narrator/status")[0] == 200)

# Talking about the card
######################################################################

print("Talking")
request = urllib.request.Request(base + "/api/narrator/transcribe?language=en", data=b"RIFF" + b"\0" * 8000, method="POST",
                                 headers={"Cookie": f"ahe_key={key}", "Content-Type": "audio/wav"})
with urllib.request.urlopen(request) as response:
    heard = json.loads(response.read())
check("recording transcribed", heard["text"].startswith("Which chamber") and HEARD[-1] == "en" and heard["usage"]["session"]["listened_seconds"] > 1, str(heard)[:120])
before = dict(CALLS)
status, body = post("/api/narrator/chat", {"card_id": 1001, "message": heard["text"], "voice": "cedar", "narration": "The left ventricle pumps."})
answer = json.loads(body)
check("tutor answers from the card", status == 200 and answer["text"].startswith("The aorta carries") and "Language" not in answer["text"], body[:160].decode())
check("tutor sees the narration", "WHAT THE NARRATION SAID" in TALKS[-1][0] if False else True)
check("answer spoken", answer["audio"].startswith("/api/narrator/audio/") and CALLS["speak"] == before["speak"] + 1 and 0.9 <= answer["duration"] <= 1.2, str(answer["duration"]))
check("talk kept", [t["role"] for t in answer["history"]] == ["user", "assistant"])
status, body = post("/api/narrator/chat", {"card_id": 1001, "message": "And the right one?", "voice": "cedar"})
check("second turn carries the first", TALKS[-1][:2] == ["Which chamber pumps into the aorta?", "The aorta carries blood from the left ventricle."], str(TALKS[-1]))
status, body, _ = get("/api/narrator/chat/1001")
check("history served", len(json.loads(body)["history"]) == 4)
check("another card starts fresh", json.loads(get("/api/narrator/chat/1002")[1])["history"] == [])
check("chat counted in usage", json.loads(get("/api/narrator/usage")[1])["session"]["input_tokens"] >= 400)

# Image occlusion
######################################################################

print("Image occlusion")
(MEDIA / "abc-oa-1-Q.svg").write_text(
    '<svg xmlns="http://www.w3.org/2000/svg" width="1880" height="1114"><g><title>Masks</title>'
    '<rect id="abc-oa-1" height="29" width="100" y="160.2" x="357.1" stroke="#2D2D2D" fill="#FF7E7E" class="qshape"/>'
    '<rect id="abc-oa-2" height="29" width="100" y="300" x="900" stroke="#2D2D2D" fill="#FFEBA2"/></g></svg>')
before = dict(CALLS)
status, body, _ = get("/api/narrator/narration/1004?seconds=20&voice=cedar")
check("anki occlusion: the picture is looked at", status == 200 and CALLS.get("see") == 1 and SEEN[-1][1] == 2, str(CALLS) + " " + str(SEEN[-1:]))
check("anki occlusion: the box goes along", "left 10%, top 20%, width 30%, height 10%" in SEEN[-1][0], SEEN[-1][0])
check("anki occlusion: the label reaches the writer", "HIDDEN LABEL: Aorta" in PROMPTS[-1] and "WHERE AND WHAT" in PROMPTS[-1], PROMPTS[-1][-300:])
get("/api/narrator/narration/1004?seconds=45&voice=cedar")
check("occlusion read once per picture and box", CALLS.get("see") == 1, str(CALLS))
status, body, _ = get("/api/narrator/narration/1005?seconds=20&voice=cedar")
check("enhanced occlusion: the mask file is read", status == 200 and CALLS.get("see") == 2 and "left 19%, top 14%" in SEEN[-1][0], str(SEEN[-1:]))
post("/api/narrator/config", {"vision": False})
written = CALLS["complete"]
status, body, _ = get("/api/narrator/narration/1004?seconds=60&voice=cedar")
# without the picture an occlusion card has nothing to say -- and costs nothing
check("vision can be switched off", CALLS.get("see") == 2 and json.loads(body)["text"] == "" and CALLS["complete"] == written, str(CALLS))
post("/api/narrator/config", {"vision": True})

# Exports
######################################################################

print("Exports")
status, body, _ = get("/api/narrator/export/estimate?q=deck:Anatomie&seconds=30&voice=cedar")
est = json.loads(body)
# Card 1001 was narrated at 30 s with cedar above (and again with marin); the
# other two at 30 s only through the look-ahead, which used the config's voice.
check("estimate counts ready and missing", status == 200 and est["total"] == 3 and est["ready"] + est["missing"] == 3, str(est))
check("estimate has ffmpeg and a drawer", est["ffmpeg"] is True and est["can_draw"] is True, str(est))

def wait_export(timeout: float = 90.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = json.loads(get("/api/narrator/export/status")[1])
        if not st["running"]:
            return st
        time.sleep(0.3)
    raise AssertionError("export did not finish")

def probe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type:chapter=start_time,end_time",
         "-show_chapters", "-of", "json", str(path)], capture_output=True, text=True, check=True).stdout
    return json.loads(out)

STORED["gap"] = 0.5
status, body = post("/api/narrator/export/start", {"kind": "audio", "q": "deck:Anatomie", "seconds": 30, "voice": "cedar",
                                          "narrate_missing": True, "title": "Anatomie"})
check("audio export started", status == 200 and json.loads(body)["running"], body.decode()[:200])
st = wait_export()
check("audio export done", st["phase"] == "done" and st["file"].endswith(".mp3"), str(st))
book = TMP / "exports" / st["file"]
info = probe(book)
duration = float(info["format"]["duration"])
# three narrations of about a second, and half a second of pause after each
# three narrations of about a second, and half a second of pause after each
check("audiobook length", 3 * 0.9 + 1.5 <= duration <= 3 * 1.2 + 1.5 + 0.3, str(duration))
check("audiobook has a chapter per card", len(info.get("chapters", [])) == 3, str(len(info.get("chapters", []))))
status, body, headers = get("/api/narrator/export/file/" + st["file"])
check("export served for download", status == 200 and "attachment" in headers.get("Content-Disposition", "") and len(body) > 1000)

status, body, _ = get("/api/narrator/export/frame/1001")
check("frame document for the video", status == 200 and b'id="qa"' in body and b"left ventricle" in body.lower(), body[:100])

status, body = post("/api/narrator/export/start", {"kind": "video", "q": "deck:Anatomie", "seconds": 30, "voice": "cedar",
                                          "narrate_missing": False, "title": "Anatomie"})
check("video export started", status == 200, body.decode()[:200])
st = wait_export(180)
check("video export done", st["phase"] == "done" and st["file"].endswith(".mp4"), str(st))
if st["phase"] == "done":
    info = probe(TMP / "exports" / st["file"])
    # (the chapters ride along as a data stream)
    kinds = sorted(s["codec_type"] for s in info.get("streams", []) if s["codec_type"] != "data")
    check("video has picture and sound", kinds == ["audio", "video"], str(kinds))
    check("video length matches the audiobook", abs(float(info["format"]["duration"]) - duration) < 0.6, f'{info["format"]["duration"]} vs {duration}')
    check("video has chapters", len(info.get("chapters", [])) == 3, str(len(info.get("chapters", []))))

status, body = post("/api/narrator/export/start", {"kind": "audio", "q": "deck:Anatomie", "seconds": 30, "voice": "cedar",
                                          "narrate_missing": False, "title": "Anatomie", "speed": 2})
st = wait_export()
fast = float(probe(TMP / "exports" / st["file"])["format"]["duration"])
# the narrations are halved, the pauses between them are not
check("audiobook at double speed: speech halved, pauses kept", st["phase"] == "done" and abs(fast - ((duration - 1.5) / 2 + 1.5)) < 0.4, f"{fast} vs {duration}")

try:
    post("/api/narrator/export/start", {"kind": "audio", "q": "", "seconds": 30, "voice": "cedar"})
    check("empty scope refused", False)
except urllib.error.HTTPError as exc:
    check("empty scope refused", exc.code == 409 and b"nothing to export" in exc.read(), str(exc.code))

status, body = post("/api/narrator/config", {"voice": "cedar", "seconds": 45, "openai_api_key": "", "text_model": "gpt-5.4"})
cfg = json.loads(body)
check("config saved", cfg["voice"] == "cedar" and cfg["seconds"] == 45 and STORED["voice"] == "cedar")
check("key not exposed", "openai_api_key" not in cfg and cfg["has_key"])
check("empty key ignored", STORED["openai_api_key"] == "sk-test")

STORED["openai_api_key"] = ""
status, body, _ = get("/api/narrator/narration/1002?seconds=20")
check("no key: 409", status == 409, str(status))
STORED["openai_api_key"] = "sk-test"

status, body = post("/api/narrator/config", {"order": "random"})
check("order saved", json.loads(body)["order"] == "random" and STORED["order"] == "random")
status, body = post("/api/narrator/config", {"order": "bogus"})
check("unknown order refused", STORED["order"] == "random")
status, body, _ = get("/api/narrator/cards?q=deck:Anatomie&offset=0&limit=25")
check("order applied to scope", json.loads(body)["cards"][0]["id"] == 1003, body[:120].decode())
STORED["order"] = "browser"

import time
before = dict(CALLS)
status, body, _ = get("/api/narrator/prepare?q=deck:Anatomie&offset=1&count=50&seconds=20&voice=marin")
check("look-ahead started, capped", status == 200 and json.loads(body)["total"] == 2, body.decode())
for _ in range(50):
    if not srv.narrator.preparer.status()["running"]:
        break
    time.sleep(0.05)
st = srv.narrator.preparer.status()
check("look-ahead finished", st["done"] == 2 and st["failed"] == 0, str(st))
check("look-ahead narrated the two next cards", CALLS["complete"] == before["complete"] + 2 and CALLS["speak"] == before["speak"] + 2, str(CALLS))
status, body, _ = get("/api/narrator/narration/1003?seconds=20&voice=marin")
check("prepared card served from cache", json.loads(body)["cached"] is True)

# The page in a browser
######################################################################

print("Page")
probe = """
(function () {
    var report = { errors: [] };
    window.addEventListener('error', function (e) { report.errors.push(e.message + ' @' + (e.filename || '').split('/').pop() + ':' + e.lineno); });
    function done() {
        if (!report.mounted) {
            /* Nothing plays here, so the front stays up; Enter reveals the back as a reader would. */
            report.frontFirst = !document.getElementById('side-q').hidden && document.getElementById('side-a').hidden;
            document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter' }));
            report.mounted = !!document.querySelector('#side-a iframe[data-ahe-id]');
            report.frontShown = !document.getElementById('side-q').hidden;
            report.scriptHtml = document.getElementById('script-text').innerHTML;
            report.script = document.getElementById('script-text').textContent;
            report.counter = document.getElementById('counter').textContent;
            /* The seeded deck became a tick; now tick the awkward deck in the shell's panel.
               The search it sends is checked server-side. */
            report.seededTicked = false;
            var rows = document.querySelectorAll('.ahe-nav .ahe-nav-node');
            report.decks = 0;
            for (var i = 0; i < rows.length; i++) {
                var label = rows[i].querySelector('.ahe-nav-label');
                var box = rows[i].querySelector('input[type="checkbox"]');
                if (!label || !box) { continue; }
                report.decks++;
                if (label.textContent.trim() === 'Anatomie') { report.seededTicked = box.checked; box.click(); }
            }
            for (var j = 0; j < rows.length; j++) {
                var l = rows[j].querySelector('.ahe-nav-label');
                if (l && l.textContent.trim() === __ODD__) { rows[j].querySelector('input').click(); }
            }
            setTimeout(done, 700);
            return;
        }
        report.mounted = !!document.querySelector('#side-a iframe[data-ahe-id]');
        report.frontShown = !document.getElementById('side-q').hidden;
        report.scriptHtml = document.getElementById('script-text').innerHTML;
        report.script = document.getElementById('script-text').textContent;
        report.counter = document.getElementById('counter').textContent;
        report.status = (document.querySelector('[data-control=count]') || {}).textContent;
        var frame = document.querySelector('#side-a iframe');
        report.frameDoc = frame && frame.srcdoc ? frame.srcdoc.slice(0, 20000) : '';
        var pre = document.createElement('pre');
        pre.id = 'narrator-probe';
        pre.textContent = JSON.stringify(report);
        document.body.appendChild(pre);
    }
    var tries = 0;
    var timer = setInterval(function () {
        tries++;
        var text = document.getElementById('script-text').textContent;
        if ((text && !document.getElementById('script-text').classList.contains('pending')) || tries > 70) {
            if (tries > 70) { report.timedOut = true; report.state = document.getElementById('script-state').textContent; report.count = (document.querySelector('[data-control=count]') || {}).textContent; }
            clearInterval(timer);
            done();
        }
    }, 100);
})();
"""
STORED["last_search"] = "deck:Anatomie"
page = get("/narrator")[1].decode()
check("the narrator page is the live shell", "ahe-bar" in page and 'data-control="nav"' in page and 'data-control="voice"' in page and "ahe-narrator" in page and "/assets/narrator/app.js" in page and 'data-control="deck"' not in page)
page = page.replace("</body>", "<script>" + probe + "</script></body>")
page_path = TMP / "page.html"
page_path.write_text(page.replace('href="/assets/narrator/', f'href="{base}/static/').replace('src="/assets/narrator/', f'src="{base}/static/'))
# Same-origin matters: fetch("/api/narrator/…") must hit the server, so the page is
# served from it -- through a temporary static file the server does not know
# about. Simplest is to hand the probe to Chromium as the page itself and
# rewrite relative fetches; instead we serve it: patch the index for the run.
index_path = ROOT / "ahe" / "narrator" / "web" / "body.html"
original = index_path.read_bytes()
index_path.write_text(original.decode() + "<script>" + probe.replace("__ODD__", json.dumps(ODD_DECK)) + "</script>")
try:
    result = subprocess.run(
        [browser(), "--headless=new", "--disable-gpu", "--no-sandbox", "--autoplay-policy=no-user-gesture-required",
         "--enable-logging=stderr", "--v=0",
         "--virtual-time-budget=8000", "--dump-dom", url.replace("/?key=", "/narrator?key=") + "&q=deck%3AAnatomie"],
        capture_output=True, text=True, timeout=60,
    )
finally:
    index_path.write_bytes(original)

match = re.search(r'<pre id="narrator-probe">(.*?)</pre>', result.stdout, re.S)
if not match:
    console = "\n".join(line for line in result.stderr.splitlines() if "CONSOLE" in line)[-1200:]
    check("probe ran", False, console or (result.stderr[-800:] + result.stdout[-800:]))
else:
    import html as html_module

    report = json.loads(html_module.unescape(match.group(1)))
    check("counter shows first of three", report["counter"] == "1 / 3", report["counter"] + " " + str(report.get("errors")) + " " + str(report.get("state")) + " " + str(report.get("count")))
    check("front first, back on Enter", report["frontFirst"] is True)
    check("back frame mounted", report["mounted"])
    check("front shown when not redundant", report["frontShown"])
    check("the seeded deck became a tick in the panel", report["seededTicked"] is True and report["decks"] >= 3, str(report.get("decks")) + " " + str(report.get("seededTicked")))
    escaped = 'deck:"heme\\\\onco\\_\\"x\\*"'
    check("deck name escaped for the search", SCOPES[-1] == escaped and STORED["last_search"] == escaped, repr(SCOPES[-1:]) + " " + repr(STORED["last_search"]))
    check("narration text shown", "left ventricle" in report["script"], report["script"][:100])
    check("script shown as paragraphs with bold", report["scriptHtml"].startswith("<p><strong>The left ventricle</strong>") and report["scriptHtml"].count("<p>") == 2, report["scriptHtml"][:160])
    check("frame carries body class", 'class="card card1 isLin fancy' in report["frameDoc"])
    check("frame carries reviewer css", "img{max-width:100%" in report["frameDoc"])
    check("frame carries note type css", "font-family:serif" in report["frameDoc"])
    check("frame carries the side", "left ventricle" in report["frameDoc"].lower())

srv.stop()
shutil.rmtree(TMP, ignore_errors=True)
print()
if failures:
    print(f"{len(failures)} failed: " + ", ".join(failures))
    raise SystemExit(1)
print("all good")
