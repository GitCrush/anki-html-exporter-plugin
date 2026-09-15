#!/usr/bin/env python3
"""Driving the exported page in a real browser, without Anki.

The export is mostly JavaScript, and until now none of it could be tested: the
add-on only runs inside Anki, and Anki cannot be scripted from a terminal. But
almost nothing the page does needs a collection. ``RenderedCard`` is a plain
dataclass, so a set of cards can be made up; ``ExportWriter`` turns those into
the very document a real export produces; and the live view is the same
document with a small server behind it, which a stubbed ``fetch`` can play.

So both pages are built here from invented cards, a short script is put into
each to work the controls and report what happened, and headless Chromium runs
the result. What that catches is the class of bug that is otherwise found by a
user: a control that hides every card, a frame that is never released, a link
that navigates the card away.

    python3 tests/run.py            all of them
    python3 tests/run.py shuffle    only tests whose name contains "shuffle"

Needs Chromium on PATH. Nothing else -- not Anki, not a collection, no network.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# The renderer imports two names from Anki and uses neither on this path.
_anki = types.ModuleType("anki")
_sound = types.ModuleType("anki.sound")
_sound.AV_REF_RE = re.compile(r"\[anki:(?P<param>[^\]]+)\]")


class _SoundOrVideoTag:  # only ever used as a type
    pass


_sound.SoundOrVideoTag = _SoundOrVideoTag
sys.modules.setdefault("anki", _anki)
sys.modules.setdefault("anki.sound", _sound)

from ahe import qr  # noqa: E402
from ahe.renderer import SPECIAL_NAMES, RenderedCard  # noqa: E402
from ahe.writer import ExportOptions, ExportWriter  # noqa: E402

SHARE_URL = "http://192.168.178.42:41234/?key=Xk3mQp7ZvL9tRw2NbY4sHe"

BROWSERS = ["chromium-browser", "chromium", "google-chrome", "google-chrome-stable"]
DECKS = ["Anatomie::Herz", "Anatomie::Lunge", "Physiologie"]


def browser() -> str:
    for name in BROWSERS:
        found = shutil.which(name)
        if found:
            return found
    print("No Chromium on PATH — tried: " + ", ".join(BROWSERS))
    raise SystemExit(2)


# Cards
######################################################################


def fake_cards(count: int) -> list[RenderedCard]:
    """Cards that carry the things the page has to cope with.

    Card 0 holds a picture wrapped in an empty link, which is how several note
    types make an image clickable, and a cloze deletion with its answer in
    ``data-cloze``, which is how Anki writes one.
    """
    cards = []
    for index in range(count):
        extra = ""
        if index == 0:
            extra = (
                '<a href=""><img src="picture.png" alt="a picture"></a>'
                '<span data-cloze="Herzmuskel">[...]</span>'
            )
        cards.append(
            RenderedCard(
                card_id=1000 + index,
                note_id=2000 + index,
                ord=0,
                body_class="card card1",
                notetype_id=42 if index % 2 else 43,
                notetype_name="Cloze" if index % 2 else "Basic",
                template_name="Card 1",
                deck=DECKS[index % 3],
                subdeck=DECKS[index % 3],
                tags=[f"tag::gruppe{index % 2}", "gemeinsam"],
                question=f"<p>Frage {index}</p>{extra}",
                answer=f"<p>Frage {index}</p><hr id=answer><p>Antwort {index}</p>",
                answer_only=f"<p>Antwort {index}</p>",
                fields=[
                    {"name": "Vorderseite", "html": f"Frage {index}", "text": f"Frage {index}"},
                    {"name": "Rückseite", "html": f"Antwort {index}", "text": f"Antwort {index}"},
                ],
                special=[{"name": "State", "value": "Review"}],
                sort_text=f"Frage {index} Antwort {index}",
            )
        )
    return cards


NOTETYPE_CSS = {42: ".card{font-size:20px}", 43: ".card{font-size:20px}"}


# Pages
######################################################################

CATCH = """
<script>
window.__err = [];
window.addEventListener("error", function (e) { window.__err.push(String(e.message)); });
function report(fields) {
  fields.err = window.__err;
  document.title = "RESULT " + JSON.stringify(fields);
  /* When the page is being held at a phone's width by a frame around it, the
     frame is what gets read. */
  if (window.parent !== window) {
    window.parent.postMessage({ probe: fields }, "*");
  }
}
function shown() { return document.querySelectorAll(".ahe-card:not([hidden])").length; }
function mountedFrames() { return document.querySelectorAll(".ahe-slot[data-mounted]").length; }
/* Headless Chromium under --virtual-time-budget fast-forwards timers but never
   runs the compositor, so scrolling fires no scroll event and no
   IntersectionObserver callback -- the page would sit there while the test
   scrolled past it and every assertion would pass for the wrong reason.
   Nudging the search box is the way in that does not depend on rendering: it
   runs the same mountVisible() and sweep() that a scroll would. */
function nudge() {
  var search = document.querySelector('[data-control="search"]');
  if (search) { search.dispatchEvent(new Event("input", { bubbles: true })); }
}

/* A reader scrolls; a jump to the end mounts nothing on the way and would let
   a page that never releases a frame pass for one that does. */
function scrollThrough(steps, done) {
  var at = 0;
  (function step() {
    if (at++ >= steps) { setTimeout(done, 400); return; }
    window.scrollBy(0, window.innerHeight);
    nudge();
    setTimeout(step, 150);
  })();
}
</script>
"""


def build_export(directory: Path, count: int, driver: str) -> Path:
    options = ExportOptions(output_dir=directory, title="Testexport")
    result = ExportWriter(options).write(fake_cards(count), NOTETYPE_CSS, (0, 0, []))
    html = result.path.read_text(encoding="utf-8")
    html = html.replace("</body>", CATCH + driver + "</body>")
    result.path.write_text(html, encoding="utf-8")
    return result.path


def build_live(directory: Path, count: int, driver: str) -> Path:
    """The live shell, with a stubbed server standing in for Anki."""
    directory.mkdir(parents=True, exist_ok=True)
    cards = fake_cards(count)
    writer = ExportWriter(ExportOptions(output_dir=None, title="Live-Test"))
    fragments = [writer.card_fragment(c, i + 1, len(cards)) for i, c in enumerate(cards)]

    for name in ("shell.css", "shell.js", "tailwind.css"):
        shutil.copy(ROOT / "ahe" / "web" / name, directory / name)

    stub = (
        "<script>\nvar FRAGMENTS = "
        + json.dumps(fragments, ensure_ascii=False)
        + ";\nvar CSS = "
        + json.dumps({str(k): v for k, v in NOTETYPE_CSS.items()})
        + ";\nvar QR = "
        + json.dumps({"shared": True, "url": SHARE_URL, "svg": qr.as_svg(qr.encode(SHARE_URL))})
        + ";\n"
        + """
var qrCalls = 0;
window.fetch = function (url) {
  var u = String(url), body = {};
  if (u.indexOf("/api/tree") === 0) {
    body = { decks: ["Anatomie", "Anatomie::Herz", "Anatomie::Lunge", "Physiologie"],
             tags: ["tag::gruppe0", "tag::gruppe1"], notetypes: ["Basic", "Cloze"] };
  } else if (u.indexOf("/api/scope") === 0) {
    /* What the page asked the collection for, which is the whole point of a
       control that changes the scope. */
    window.__scopeQ = decodeURIComponent(
      (u.match(/[?&]q=([^&]*)/) || [0, ""])[1].replace(/\\+/g, " "));
    body = { query: "x", total: FRAGMENTS.length };
  } else if (u.indexOf("/api/shuffle") === 0) {
    /* What the server does: deal the whole scope, not the loaded page. */
    for (var k = FRAGMENTS.length - 1; k > 0; k--) {
      var j = Math.floor(Math.random() * (k + 1));
      var swap = FRAGMENTS[k]; FRAGMENTS[k] = FRAGMENTS[j]; FRAGMENTS[j] = swap;
    }
    body = { query: "x", total: FRAGMENTS.length };
  } else if (u.indexOf("/api/qr") === 0) {
    /* #lateshare: not shared when first asked, shared from then on -- the
       switch having been flipped in Anki while this window stayed open. */
    qrCalls++;
    body = (location.hash === "#lateshare" && qrCalls === 1) ? { shared: false } : QR;
  } else if (u.indexOf("/api/cards") === 0) {
    var m = u.match(/offset=(\\d+)/), offset = m ? Number(m[1]) : 0;
    body = { cards: FRAGMENTS.slice(offset, offset + 25), css: offset ? {} : CSS,
             offset: offset, total: FRAGMENTS.length, errors: [] };
  }
  return Promise.resolve({ ok: true,
    json: function () { return Promise.resolve(body); },
    text: function () { return Promise.resolve(""); } });
};
</script>
"""
    )

    # What the server reads out of the collection before serving the page.
    html = writer.live_shell("./", ["Vorderseite", "Rückseite"], list(SPECIAL_NAMES))
    html = html.replace('<script src="./shell.js">', CATCH + stub + '<script src="./shell.js">')
    html = html.replace("</body>", driver + "</body>")
    page = directory / "index.html"
    page.write_text(html, encoding="utf-8")
    return page


# Running
######################################################################


def run(page: Path, budget: int = 8000, fragment: str = "") -> dict:
    output = subprocess.run(
        [
            browser(),
            "--headless",
            "--disable-gpu",
            "--no-sandbox",
            "--window-size=1200,900",
            f"--virtual-time-budget={budget}",
            "--dump-dom",
            page.as_uri() + fragment,
        ],
        capture_output=True,
        text=True,
        timeout=120,
    ).stdout
    match = re.search(r"<title>RESULT (\{.*?\})</title>", output, re.S)
    if not match:
        title = re.search(r"<title>(.*?)</title>", output, re.S)
        raise AssertionError(
            "the page never reported: " + (title.group(1)[:200] if title else "no title")
        )
    payload = json.loads(match.group(1).replace("&quot;", '"'))
    if payload.get("err"):
        raise AssertionError("the page raised: " + "; ".join(payload["err"]))
    return payload


def run_framed(page: Path, width: int, budget: int = 9000) -> dict:
    """The same, at a width a window cannot be made to have.

    Chromium will not open a window narrower than about five hundred pixels, so
    a phone's viewport cannot be had by resizing one. A frame of exactly that
    width is a real viewport for the document inside it -- media queries,
    ``innerWidth`` and layout all follow it -- so the page is measured in there
    and reports up through the frame boundary.
    """
    outer = page.parent.parent / "framed.html"
    outer.write_text(
        "<!doctype html><meta charset=utf-8><title>waiting</title>"
        "<style>html,body{margin:0}iframe{width:%dpx;height:844px;border:0;display:block}"
        "</style><iframe src=\"%s\"></iframe><script>"
        "window.addEventListener('message', function (e) {"
        "  if (e.data && e.data.probe) {"
        "    document.title = 'RESULT ' + JSON.stringify(e.data.probe); } });"
        "</script>" % (width, page.parent.name + "/" + page.name),
        encoding="utf-8",
    )
    return run(outer, budget=budget)


# The tests
######################################################################

TESTS: list[tuple[str, object]] = []


def test(name: str):
    def keep(function):
        TESTS.append((name, function))
        return function

    return keep


@test("export: shuffle keeps every card on the page")
def _(workspace: Path) -> None:
    driver = """
<script>
setTimeout(function () {
  var before = shown();
  document.querySelector('[data-control="shuffle"]').click();
  setTimeout(function () {
    report({ before: before, after: shown(),
             empty: document.querySelector(".ahe-empty").hidden });
  }, 400);
}, 600);
</script>
"""
    result = run(build_export(workspace, 400, driver))
    assert result["before"] == 400, result
    assert result["after"] == 400, result
    assert result["empty"] is True, result


@test("export: shuffle keeps the filtered set")
def _(workspace: Path) -> None:
    driver = """
<script>
setTimeout(function () {
  document.querySelector('[data-control="nav"]').click();
  document.querySelector('.ahe-nav-row input[type=checkbox]').click();
  var before = shown();
  document.querySelector('[data-control="shuffle"]').click();
  setTimeout(function () {
    report({ before: before, after: shown(),
             empty: document.querySelector(".ahe-empty").hidden });
  }, 400);
}, 600);
</script>
"""
    result = run(build_export(workspace, 400, driver))
    assert 0 < result["before"] < 400, result
    assert result["after"] == result["before"], result
    assert result["empty"] is True, result


@test("live: shuffle deals the whole scope, not the loaded page")
def _(workspace: Path) -> None:
    # Two things at once. The export's text filter must not run here -- the
    # search box holds an Anki search, which appears in no card's own text and
    # once hid every card. And the deal has to reach past the page that has
    # been loaded, which only the server can do.
    driver = """
<script>
function positions() {
  var out = [];
  document.querySelectorAll(".ahe-card").forEach(function (a) {
    out.push(Number(a.getAttribute("data-cid")) - 1000);
  });
  return out;
}
setTimeout(function () {
  var search = document.querySelector('[data-control="search"]');
  search.value = 'deck:"Anatomie::Herz" is:due';
  search.dispatchEvent(new Event("input", { bubbles: true }));
  setTimeout(function () {
    var before = positions();
    document.querySelector('[data-control="shuffle"]').click();
    setTimeout(function () {
      var after = positions();
      report({ before: before, after: after, shown: shown(),
               fromBeyondThePage: after.filter(function (p) { return p >= 25; }).length,
               empty: document.querySelector(".ahe-empty").hidden });
    }, 700);
  }, 700);
}, 600);
</script>
"""
    result = run(build_live(workspace, 40, driver))
    assert result["before"] == list(range(25)), result
    assert result["shown"] == len(result["after"]) == 25, result
    assert result["empty"] is True, result
    # Of forty cards a page holds twenty-five, so a deal over the whole scope
    # cannot help but bring some of the other fifteen forward.
    assert result["fromBeyondThePage"] > 0, result


@test("live: the page hands out its own address as a code")
def _(workspace: Path) -> None:
    driver = """
<script>
setTimeout(function () {
  var button = document.querySelector('[data-control="qr"]');
  var hiddenAtFirst = button.hidden;
  var search = document.querySelector('[data-control="search"]');
  search.value = "is:due";
  search.dispatchEvent(new Event("input", { bubbles: true }));
  setTimeout(function () {
    button.click();
    setTimeout(function () {
      var layer = document.querySelector(".ahe-qr");
      report({ hiddenAtFirst: hiddenAtFirst,
               visible: !!layer,
               hasCode: !!(layer && layer.querySelector("svg")),
               address: layer ? (layer.querySelector(".ahe-qr-url") || {}).textContent : "" });
    }, 400);
  }, 700);
}, 600);
</script>
"""
    result = run(build_live(workspace, 40, driver))
    assert result["hiddenAtFirst"] is False, "the button should be shown in the live view"
    assert result["visible"] is True, result
    assert result["hasCode"] is True, result
    assert result["address"].startswith("http://192.168."), result


@test("live: the code appears without reopening once the share goes on")
def _(workspace: Path) -> None:
    # The switch is in Anki and this window is told nothing when it is flipped,
    # so the message has to stop being true by itself.
    driver = """
<script>
setTimeout(function () {
  var search = document.querySelector('[data-control="search"]');
  search.value = "is:due";
  search.dispatchEvent(new Event("input", { bubbles: true }));
  setTimeout(function () {
    document.querySelector('[data-control="qr"]').click();
    setTimeout(function () {
      var layer = document.querySelector(".ahe-qr");
      var saidNotShared = layer.textContent.indexOf("only being served") !== -1;
      /* Longer than one retry, and nothing is clicked in between. */
      setTimeout(function () {
        var now = document.querySelector(".ahe-qr");
        report({ saidNotShared: saidNotShared,
                 sameLayer: now === layer,
                 hasCode: !!(now && now.querySelector("svg")) });
      }, 3000);
    }, 500);
  }, 700);
}, 600);
</script>
"""
    result = run(build_live(workspace, 40, driver), budget=14000, fragment="#lateshare")
    assert result["saidNotShared"] is True, result
    assert result["sameLayer"] is True, "it should not have needed reopening"
    assert result["hasCode"] is True, result


@test("export: no address to hand out, so no code")
def _(workspace: Path) -> None:
    driver = """
<script>
setTimeout(function () {
  var button = document.querySelector('[data-control="qr"]');
  report({ present: !!button, hidden: button ? button.hidden : null });
}, 500);
</script>
"""
    result = run(build_export(workspace, 20, driver))
    assert result["present"] is True, result
    assert result["hidden"] is True, "an export is a file; it has no address"


@test("live: fields can be picked one by one, as in an export")
def _(workspace: Path) -> None:
    # The live view has no cards to derive the list from, so it was built with
    # none -- which left a switch that turned every field on and no way to say
    # which.
    driver = """
<script>
setTimeout(function () {
  var button = document.querySelector('[data-control="fields"]');
  var menu = button.closest(".ahe-menu");
  var caret = menu ? menu.querySelector("[data-menu-toggle]") : null;
  if (caret) { caret.click(); }
  setTimeout(function () {
    var boxes = menu ? menu.querySelectorAll("input[data-field-name]") : [];
    report({ hasCaret: !!caret, fields: boxes.length,
             specials: document.querySelectorAll("input[data-special-name]").length });
  }, 300);
}, 700);
</script>
"""
    result = run(build_live(workspace, 40, driver))
    assert result["hasCaret"] is True, "no way to open the list"
    assert result["fields"] == 2, result
    assert result["specials"] == len(SPECIAL_NAMES), result


@test("live: the scope it was opened on becomes ticks, and the panel is in charge")
def _(workspace: Path) -> None:
    # The export dialog sends the live view off with "deck:…" "tag:…" and a
    # search. Left in the search box those were ANDed with whatever the panel
    # ticked next, so ticking another deck asked for cards in two decks at
    # once -- none. Now the deck and tag terms land as ticks; only the rest
    # stays typed; a tick replaces what the dialog chose.
    driver = """
<script>
function rowFor(text) {
  var rows = document.querySelectorAll(".ahe-nav .ahe-nav-node");
  for (var i = 0; i < rows.length; i++) {
    var label = rows[i].querySelector(".ahe-nav-label");
    if (label && label.textContent.trim() === text) { return rows[i]; }
  }
  return null;
}
function tick(text, on) {
  var row = rowFor(text);
  var box = row && row.querySelector('input[type="checkbox"]');
  if (box && box.checked !== on) { box.click(); }
  return !!box;
}
setTimeout(function () {
  var out = { search: document.querySelector('[data-control="search"]').value,
              scope: window.__scopeQ,
              filter: document.querySelector('[data-control="nav"]').getAttribute("aria-pressed") };
  document.querySelector('[data-control="nav"]').click();
  /* the deck tree folds; open Anatomie to reach its children */
  var parent = rowFor("Anatomie");
  var twist = parent && parent.querySelector("button");
  if (twist) { twist.click(); }
  out.herzTicked = !!(rowFor("Herz") && rowFor("Herz").querySelector("input").checked);
  out.tagTicked = !!(rowFor("gruppe0") && rowFor("gruppe0").querySelector("input").checked);
  tick("Herz", false);
  tick("Lunge", true);
  setTimeout(function () {
    out.after = window.__scopeQ;
    report(out);
  }, 600);
}, 900);
</script>
"""
    result = run(build_live(workspace, 40, driver),
                 fragment='?q=%22deck%3AAnatomie%3A%3AHerz%22%20%22tag%3Atag%3A%3Agruppe0%22%20is%3Adue')
    assert result["search"] == "is:due", "only the search should stay typed: " + str(result)
    assert 'deck:"Anatomie::Herz"' in result["scope"] and 'tag:"tag::gruppe0"' in result["scope"] and "is:due" in result["scope"], result
    assert result["filter"] == "true", result
    assert result["herzTicked"] is True and result["tagTicked"] is True, "the seeded terms did not become ticks: " + str(result)
    assert 'deck:"Anatomie::Lunge"' in result["after"] and 'deck:"Anatomie::Herz"' not in result["after"], "the decks were not swapped: " + str(result)
    assert "is:due" in result["after"] and 'tag:"tag::gruppe0"' in result["after"], "the rest of the scope was lost: " + str(result)


@test("no deck box in the bar: the panel is the one place for decks")
def _(workspace: Path) -> None:
    driver = """
<script>
setTimeout(function () {
  report({ box: document.querySelectorAll('[data-control="deck"]').length });
}, 400);
</script>
"""
    result = run(build_live(workspace, 6, driver))
    assert result["box"] == 0, str(result)


@test("frames are released once a card is scrolled well past")
def _(workspace: Path) -> None:
    driver = """
<script>
setTimeout(function () {
  var atTop = mountedFrames();
  var most = atTop;
  var watch = setInterval(function () { most = Math.max(most, mountedFrames()); }, 100);
  scrollThrough(60, function () {
    clearInterval(watch);
    report({ atTop: atTop, most: most, atEnd: mountedFrames() });
  });
}, 900);
</script>
"""
    result = run(build_export(workspace, 400, driver), budget=30000)
    assert result["atTop"] > 0, result
    # MOUNT_BUDGET is 40 articles, so at most two frames each. Before the
    # recycler existed this grew with every card that was ever scrolled past,
    # which is what brought a large export down.
    assert result["most"] <= 80, result
    assert result["atEnd"] <= 80, result


@test("a revealed cloze comes back after the card is recycled")
def _(workspace: Path) -> None:
    driver = """
<script>
function firstSlot() { return document.querySelector('.ahe-card .ahe-slot'); }
function firstCloze() {
  var frame = firstSlot().querySelector("iframe");
  if (!frame || !frame.contentDocument) { return null; }
  return frame.contentDocument.querySelector("[data-cloze]");
}
setTimeout(function () {
  var span = firstCloze();
  if (span) { span.click(); }
  var opened = span ? span.getAttribute("data-ahe-open") === "1" : false;
  scrollThrough(40, function () {
    var released = !firstSlot().hasAttribute("data-mounted");
    window.scrollTo(0, 0);
    nudge();
    /* Long enough for the frame to be built and for setupInteractive to have
       had one of its later passes. */
    setTimeout(function () {
      var back = firstCloze();
      report({ opened: opened, released: released,
               restored: !!(back && back.getAttribute("data-ahe-open") === "1") });
    }, 3000);
  });
}, 1500);
</script>
"""
    result = run(build_export(workspace, 400, driver), budget=30000)
    assert result["opened"] is True, result
    assert result["released"] is True, "the card was never recycled: " + str(result)
    assert result["restored"] is True, result


# A 2x1 PNG, so the picture has a size to draw over.
PIXEL = (
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAIAAAABCAYAAAD0In+KAAAADklEQVQIW2P4"
    "z8DwHwAFAAH/o9NhsQAAAABJRU5ErkJggg=="
)

# Anki's own image occlusion note type, as the template of 23.10 wrote it:
# the masks as data-shape divs, the picture in a container the reviewer's
# bundle draws a canvas over, and a call by the name that bundle had then.
OCCLUSION = (
    '<div style="display:none">'
    '<div class="cloze" data-shape="rect" data-left=".1" data-top=".1" data-width=".3" '
    'data-height=".3" data-ordinal="1" data-occludeInactive="1"></div>'
    '<div class="cloze-inactive" data-shape="rect" data-left=".5" data-top=".5" data-width=".3" '
    'data-height=".3" data-ordinal="2" data-occludeInactive="1"></div>'
    "</div>"
    '<div id="err"></div>'
    '<div id="image-occlusion-container"><img src="' + PIXEL + '">'
    '<canvas id="image-occlusion-canvas"></canvas></div>'
    "<script>try { anki.setupImageCloze(); } catch (exc) { "
    'document.getElementById("err").textContent = String(exc); }</script>'
)


@test("an image occlusion is drawn even with revealing switched off")
def _(workspace: Path) -> None:
    cards = fake_cards(2)
    card = cards[0]
    cards[0] = RenderedCard(
        **{**card.__dict__, "question": OCCLUSION, "answer": OCCLUSION, "answer_only": OCCLUSION}
    )
    options = ExportOptions(output_dir=workspace, title="Occlusion", interactive=False)
    result = ExportWriter(options).write(cards, NOTETYPE_CSS, (0, 0, []))
    driver = """
<script>
setTimeout(function () {
  var frame = document.querySelector('.ahe-card .ahe-slot iframe');
  var doc = frame && frame.contentDocument;
  var box = doc && doc.getElementById("image-occlusion-container");
  report({
    templateErr: doc ? doc.getElementById("err").textContent : "no frame",
    shapes: doc ? doc.querySelectorAll(".ahe-io-svg .ahe-io-shape").length : -1,
    height: box ? box.getBoundingClientRect().height : -1
  });
}, 2500);
</script>
"""
    html = result.path.read_text(encoding="utf-8").replace("</body>", CATCH + driver + "</body>")
    result.path.write_text(html, encoding="utf-8")
    out = run(result.path)
    assert out["templateErr"] == "", "the template reported: " + str(out)
    assert out["shapes"] == 2, "masks not drawn: " + str(out)
    assert out["height"] > 10, "the picture has no height: " + str(out)


PHONE = """
<script>
setTimeout(function () {
  var doc = document.documentElement;
  var out = { innerWidth: window.innerWidth, pageWidth: doc.scrollWidth,
              overflows: doc.scrollWidth > window.innerWidth + 1 };
  out.inputFont = getComputedStyle(document.querySelector('[data-control="search"]')).fontSize;
  var tools = document.querySelector(".ahe-bar-tools");
  out.toolsScroll = tools.scrollWidth > tools.clientWidth;
  out.barHeight = Math.round(document.querySelector(".ahe-bar").getBoundingClientRect().height);
  document.querySelector('[data-control="nav"]').click();
  var nav = document.querySelector(".ahe-nav").getBoundingClientRect();
  out.navFits = nav.right <= window.innerWidth + 1;
  document.querySelector('[data-control="nav"]').click();
  /* A frame whose content is wider than the frame is content nobody can
     reach: the frames carry scrolling="no". */
  out.cutOff = null;
  document.querySelectorAll(".ahe-card .ahe-slot iframe").forEach(function (fr) {
    if (!fr.contentDocument) { return; }
    var fd = fr.contentDocument.documentElement;
    if (fd.scrollWidth > fd.clientWidth + 1) {
      out.cutOff = { viewport: fd.clientWidth, content: fd.scrollWidth };
    }
  });
  report(out);
}, 2500);
</script>
"""


def wide_card_export(directory: Path, driver: str) -> Path:
    """An export holding one card a phone cannot fit -- a nine hundred pixel
    table, which is what a shared deck's note types do."""
    cards = fake_cards(6)
    wide = (
        '<table style="width:900px;border-collapse:collapse"><tr>'
        '<td style="border:1px solid #999">a table laid out at 900px</td></tr></table>'
    )
    cards[1].question = "<p>Wide</p>" + wide
    cards[1].answer = cards[1].question + '<hr id=answer><p>Answer</p>'
    cards[1].answer_only = "<p>Answer</p>"
    options = ExportOptions(output_dir=directory, title="Phone")
    result = ExportWriter(options).write(cards, NOTETYPE_CSS, (0, 0, []))
    html = result.path.read_text(encoding="utf-8")
    result.path.write_text(html.replace("</body>", CATCH + driver + "</body>"), encoding="utf-8")
    return result.path


@test("phone: nothing runs off the side at 390 px")
def _(workspace: Path) -> None:
    result = run_framed(wide_card_export(workspace, PHONE), 390)
    assert result["innerWidth"] == 390, result
    assert result["overflows"] is False, "the page is wider than the screen: " + str(result)
    assert result["navFits"] is True, "the filter panel hangs off the edge: " + str(result)
    assert result["toolsScroll"] is True, "the tool line should scroll, not wrap"
    # Sticky, so its height is spent for the whole of the reading.
    assert result["barHeight"] <= 120, "the bar has grown a third line: " + str(result)


@test("phone: the live bar keeps its two lines")
def _(workspace: Path) -> None:
    # The phone measurements above are taken on an export; the live view's
    # head line carries the QR control as well, so it is measured here.
    driver = """
<script>
setTimeout(function () {
  var doc = document.documentElement;
  var bar = document.querySelector(".ahe-bar");
  var head = document.querySelector(".ahe-bar-head").getBoundingClientRect();
  report({ innerWidth: window.innerWidth,
           overflows: doc.scrollWidth > window.innerWidth + 1,
           barHeight: Math.round(bar.getBoundingClientRect().height),
           headHeight: Math.round(head.height),
           searchFits: document.querySelector('[data-control="search"]').getBoundingClientRect().right <= window.innerWidth + 1 });
}, 2000);
</script>
"""
    result = run_framed(build_live(workspace / "live", 6, driver), 390)
    assert result["innerWidth"] == 390, result
    assert result["overflows"] is False, "the page is wider than the screen: " + str(result)
    assert result["searchFits"] is True, "the search box hangs off the edge: " + str(result)
    assert result["headHeight"] <= 60, "the head line wrapped: " + str(result)


@test("phone: a card wider than the screen can still be reached")
def _(workspace: Path) -> None:
    result = run_framed(wide_card_export(workspace, PHONE), 390)
    assert result["cutOff"] is None, "content is cut off with no way to it: " + str(result)
    # Safari on iOS zooms the page when a smaller field is tapped.
    assert result["inputFont"] == "16px", result


@test("a link inside a card does not navigate the card")
def _(workspace: Path) -> None:
    # A frame written with srcdoc has no address of its own, so <a href="">
    # resolved against the export and loaded the whole page into the card.
    driver = """
<script>
setTimeout(function () {
  var frame = document.querySelector('.ahe-card .ahe-slot iframe');
  var doc = frame && frame.contentDocument;
  var image = doc && doc.querySelector("img");
  if (image) { image.click(); }
  setTimeout(function () {
    var after = frame.contentDocument;
    report({ clicked: !!image,
             stillACard: !!(after && after.getElementById("qa")),
             swallowedThePage: after ? after.querySelectorAll(".ahe-card").length : -1 });
  }, 800);
}, 1000);
</script>
"""
    result = run(build_export(workspace, 20, driver))
    assert result["clicked"] is True, result
    assert result["stillACard"] is True, result
    assert result["swallowedThePage"] == 0, result


# Entry point
######################################################################


SWIPE_HELPERS = """
/* The frame's report of a finger, as the shell receives it. */
function frameSwipe(article, dx) {
  var frame = article.querySelector(".ahe-slot iframe");
  var id = frame.getAttribute("data-ahe-id");
  window.postMessage({ source: "ahe-frame", id: id, type: "swipe", phase: "move", dx: dx / 2 }, "*");
  window.postMessage({ source: "ahe-frame", id: id, type: "swipe", phase: "move", dx: dx }, "*");
  window.postMessage({ source: "ahe-frame", id: id, type: "swipe", phase: "end", dx: dx }, "*");
}
/* A finger on the card's own header, which this document hears itself. */
function chromeSwipe(article, dx) {
  var target = article.querySelector("header") || article.firstElementChild;
  function touch(type, x) {
    var t = new Touch({ identifier: 1, target: target, clientX: x, clientY: 100 });
    target.dispatchEvent(new TouchEvent(type, { bubbles: true, cancelable: true,
      touches: type === "touchend" ? [] : [t], changedTouches: [t] }));
  }
  touch("touchstart", 300);
  touch("touchmove", 300 + dx / 4);
  touch("touchmove", 300 + dx / 2);
  touch("touchmove", 300 + dx);
  touch("touchend", 300 + dx);
}
function countText() { return document.querySelector('[data-control="count"]').textContent; }
"""


@test("phone: a card swiped off the frame leaves the export, and comes back on undo")
def _(workspace: Path) -> None:
    driver = "<script>" + SWIPE_HELPERS + """
setTimeout(function () {
  var first = document.querySelector(".ahe-card");
  var width = first.offsetWidth;
  /* Half a swipe springs back. */
  frameSwipe(first, -Math.round(width * 0.15));
  setTimeout(function () {
    var stayed = shown();
    frameSwipe(first, -Math.round(width * 0.6));
    setTimeout(function () {
      var out = { stayed: stayed, after: shown(), count: countText(),
                  hidden: first.hidden, undo: !!document.querySelector('[data-control="undo"]') };
      document.querySelector('[data-control="undo"]').click();
      setTimeout(function () {
        out.back = shown(); out.countBack = countText();
        report(out);
      }, 200);
    }, 800);
  }, 400);
}, 1500);
</script>
"""
    result = run(build_export(workspace, 30, driver))
    assert result["stayed"] == 30, result
    assert result["after"] == 29 and result["hidden"] is True, result
    assert result["count"] == "29 cards", result
    assert result["undo"] is True, result
    assert result["back"] == 30 and result["countBack"] == "30 cards", result


@test("phone: a card swiped off its header leaves the live page, and the rest close up")
def _(workspace: Path) -> None:
    driver = "<script>" + SWIPE_HELPERS + """
setTimeout(function () {
  var search = document.querySelector('[data-control="search"]');
  search.value = "deck:Anatomie";
  search.dispatchEvent(new Event("input", { bubbles: true }));
}, 600);
setTimeout(function () {
  var cards = document.querySelectorAll(".ahe-card");
  var second = cards[1], third = cards[2];
  var before = shown(), countBefore = countText();
  chromeSwipe(second, Math.round(second.offsetWidth * 0.6));
  setTimeout(function () {
    var now = document.querySelectorAll(".ahe-card");
    var out = { before: before, countBefore: countBefore, after: now.length, count: countText(),
                gone: !second.parentNode, closedUp: now[1] === third };
    document.querySelector('[data-control="undo"]').click();
    setTimeout(function () {
      var back = document.querySelectorAll(".ahe-card");
      out.back = back.length; out.inPlace = back[1] === second; out.countBack = countText();
      report(out);
    }, 200);
  }, 800);
}, 2500);
</script>
"""
    result = run(build_live(workspace, 40, driver), budget=12000)
    # The first page of the scope is on the page; the swiped card leaves both
    # the loaded and the total count, so the numbers still add up.
    assert result["before"] == 25 and result["countBefore"] == "25 of 40 cards", result
    assert result["after"] == 24 and result["gone"] is True, result
    assert result["closedUp"] is True, result
    assert result["count"] == "24 of 39 cards", result
    assert result["back"] == 25 and result["inPlace"] is True, result
    assert result["countBack"] == "25 of 40 cards", result


@test("suspended and buried cards stay out unless asked for")
def _(workspace: Path) -> None:
    from ahe.export import build_search
    from ahe.live_server import LiveView

    class Collection:
        query = ""

        def find_cards(self, query: str, order: bool = False) -> list[int]:
            self.query = query
            return []

    # The dialog's search: excluded by default, kept on request, and left
    # alone when the search asks for those cards itself.
    assert build_search("D", ["t"], "").endswith("-is:suspended -is:buried")
    assert build_search("D", ["t"], "", include_hidden=True) == '"deck:D" "tag:t"'
    assert build_search(None, [], "is:suspended") == "(is:suspended)"
    assert build_search(None, [], "") == ""

    # The live view applies the same rule on the server, so it holds for a
    # scope the reader picks in the page as well.
    collection = Collection()
    LiveView(ExportOptions()).set_scope(collection, "deck:D")
    assert collection.query == "(deck:D) -is:suspended -is:buried", collection.query
    LiveView(ExportOptions(), include_hidden=True).set_scope(collection, "deck:D")
    assert collection.query == "deck:D", collection.query


def main() -> int:
    wanted = sys.argv[1] if len(sys.argv) > 1 else ""
    chosen = [(n, f) for n, f in TESTS if wanted in n]
    if not chosen:
        print(f"no test matches {wanted!r}")
        return 2

    failures = 0
    for name, function in chosen:
        with tempfile.TemporaryDirectory(prefix="ahe-test-") as temporary:
            try:
                function(Path(temporary) / "page")
            except Exception as error:  # a failed check or a page that died
                failures += 1
                print(f"  ✗ {name}\n      {error}")
            else:
                print(f"  ✓ {name}")

    print(f"\n{len(chosen) - failures}/{len(chosen)} passed")

    # The narrator has a suite of its own -- one script, end to end -- which
    # runs after these when nothing narrower was asked for.
    for suite in ("narrator", "hypnagog"):
        if wanted in ("", suite):
            print(f"\n{suite}:")
            result = subprocess.run([sys.executable, str(Path(__file__).with_name(f"{suite}.py"))])
            if result.returncode:
                failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
