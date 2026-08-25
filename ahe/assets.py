"""Access to the web assets Anki itself serves to its reviewer webview.

An exported card is only pixel-identical to the reviewer if it is styled by the
same rules, so we ship copies of Anki's ``reviewer.css``, the theme variables
from ``webview.css`` and (optionally) the MathJax bundle alongside the export.

Where those files live depends on how Anki was installed:

* source checkouts, ``pip install aqt`` and the uv based launcher keep them on
  disk below ``aqt_data_path()/web``;
* the packaged builds embed them in the executable, where only the running
  Anki can reach them -- but that same Anki serves them over its media server
  at ``http://127.0.0.1:<port>/_anki/...`` for its own webviews.

``read()`` tries both, in that order, and callers fall back to the vendored
minimal copies below if neither works.
"""

from __future__ import annotations

import urllib.request
from pathlib import Path

REVIEWER_CSS = "css/reviewer.css"
WEBVIEW_CSS = "css/webview.css"
MATHJAX_JS = "js/vendor/mathjax/tex-chtml-full.js"
MATHJAX_FONT_DIR = "js/vendor/mathjax/output/chtml/fonts/woff-v2"
# Anki's reviewer bundle publishes jQuery as a global, and a great many shared
# note types (AnKing, Ankizin, ...) rely on it in their template scripts.
JQUERY_JS = "js/vendor/jquery.min.js"

# Used when we can only fetch files over HTTP and therefore cannot list the
# font directory. Stable across MathJax 3.x.
MATHJAX_FONTS = (
    "MathJax_AMS-Regular.woff",
    "MathJax_Calligraphic-Bold.woff",
    "MathJax_Calligraphic-Regular.woff",
    "MathJax_Fraktur-Bold.woff",
    "MathJax_Fraktur-Regular.woff",
    "MathJax_Main-Bold.woff",
    "MathJax_Main-Italic.woff",
    "MathJax_Main-Regular.woff",
    "MathJax_Math-BoldItalic.woff",
    "MathJax_Math-Italic.woff",
    "MathJax_Math-Regular.woff",
    "MathJax_SansSerif-Bold.woff",
    "MathJax_SansSerif-Italic.woff",
    "MathJax_SansSerif-Regular.woff",
    "MathJax_Script-Regular.woff",
    "MathJax_Size1-Regular.woff",
    "MathJax_Size2-Regular.woff",
    "MathJax_Size3-Regular.woff",
    "MathJax_Size4-Regular.woff",
    "MathJax_Typewriter-Regular.woff",
    "MathJax_Vector-Bold.woff",
    "MathJax_Vector-Regular.woff",
    "MathJax_Zero.woff",
)

_HTTP_TIMEOUT = 15

# Verbatim copy of Anki 25.02's css/reviewer.css, used when neither lookup
# works (eg. an export driven from a headless script without a media server).
FALLBACK_REVIEWER_CSS = """\
hr{background-color:#737373}
body{margin:20px;overflow-wrap:break-word;background-size:cover;\
background-repeat:no-repeat;background-position:top;background-attachment:fixed}
body.nightMode{background-color:var(--canvas);color:var(--fg)}
img{max-width:100%;max-height:95vh}
li{text-align:start}
pre{text-align:left}
#typeans{width:100%;box-sizing:border-box}
code#typeans{white-space:pre-wrap;font-variant-ligatures:none}
.typeGood{background:#afa;color:#000}
.typeBad{color:#000;background:#faa}
.typeMissed{color:#000;background:#ccc}
button{margin:1em .5em}
.replay-button{text-decoration:none;display:inline-flex;vertical-align:middle;margin:3px}
.replay-button svg{width:40px;height:40px}
.replay-button svg circle{fill:#fff;stroke:#414141}
.replay-button svg path{fill:#414141}
.nightMode .latex{filter:invert(100%)}
.drawing{zoom:50%}
.nightMode img.drawing{filter:invert(1) hue-rotate(180deg)}
#image-occlusion-container{position:relative;margin:0 auto;max-height:calc(95vh - 40px)}
#image-occlusion-container img{position:absolute;top:0;left:0;width:100%;height:100%;\
max-width:unset;max-height:unset}
#image-occlusion-canvas{position:absolute;top:0;left:0;width:100%;height:100%}
"""

# Minimal subset of Anki's theme variables, for the same fallback case.
FALLBACK_THEME_CSS = """\
:root{--fg:#020202;--fg-subtle:#737373;--fg-faint:#afafaf;--fg-link:#1d4ed8;\
--canvas:#f5f5f5;--canvas-elevated:#fff;--canvas-inset:#fff;--canvas-code:#fff;\
--border:#c4c4c4;--border-subtle:#e4e4e4;--highlight-bg:#86b7fe;--highlight-fg:#000;\
--selected-bg:rgba(214,214,214,.5);--selected-fg:#000;color-scheme:light}
:root.night-mode{--fg:#fcfcfc;--fg-subtle:#858585;--fg-faint:#545454;--fg-link:#bfdbfe;\
--canvas:#2c2c2c;--canvas-elevated:#363636;--canvas-inset:#2c2c2c;--canvas-code:#252525;\
--border:#202020;--border-subtle:#252525;--highlight-bg:#3b82f6;--highlight-fg:#fff;\
--selected-bg:rgba(84,84,84,.5);--selected-fg:#fff;color-scheme:dark}
"""


# Locating the files
######################################################################


def data_web_dir() -> Path | None:
    """The on-disk ``_anki`` web root, if this Anki keeps one."""
    try:
        from aqt.utils import aqt_data_path

        root = Path(aqt_data_path()) / "web"
    except Exception:
        return None
    return root if root.is_dir() else None


def _http_root() -> str | None:
    try:
        import aqt

        port = aqt.mw.mediaServer.getPort()
    except Exception:
        return None
    return f"http://127.0.0.1:{port}/_anki/"


_cache: dict[str, bytes | None] = {}


def read(rel_path: str) -> bytes | None:
    """Return the contents of ``_anki/<rel_path>``, or None if unavailable."""
    if rel_path in _cache:
        return _cache[rel_path]
    data = _read_uncached(rel_path)
    _cache[rel_path] = data
    return data


def _read_uncached(rel_path: str) -> bytes | None:
    root = data_web_dir()
    if root is not None:
        candidate = root / rel_path
        if candidate.is_file():
            try:
                return candidate.read_bytes()
            except OSError:
                pass

    base = _http_root()
    if base:
        try:
            with urllib.request.urlopen(base + rel_path, timeout=_HTTP_TIMEOUT) as resp:
                if resp.status == 200:
                    return resp.read()
        except Exception:
            pass
    return None


def read_text(rel_path: str) -> str | None:
    data = read(rel_path)
    return data.decode("utf-8", "replace") if data is not None else None


# Stylesheets
######################################################################


def reviewer_css() -> str:
    css = read_text(REVIEWER_CSS)
    return css if css else FALLBACK_REVIEWER_CSS


def theme_variables_css() -> str:
    """The ``:root`` / ``:root.night-mode`` custom property blocks.

    Note type stylesheets and reviewer.css both reference variables such as
    ``--canvas`` and ``--fg``; without them night mode renders unstyled.
    """
    css = read_text(WEBVIEW_CSS)
    if not css:
        return FALLBACK_THEME_CSS
    extracted = _leading_root_rules(css)
    return extracted or FALLBACK_THEME_CSS


def _strip_css_comments(text: str) -> str:
    out = []
    i = 0
    while True:
        start = text.find("/*", i)
        if start == -1:
            out.append(text[i:])
            return "".join(out)
        out.append(text[i:start])
        end = text.find("*/", start + 2)
        if end == -1:
            return "".join(out)
        i = end + 2


def _leading_root_rules(css: str) -> str:
    """Collect the run of ``:root`` rules at the top of a stylesheet.

    Anki emits its colour and prop definitions first (``:root`` followed by
    ``:root.night-mode``, twice) and only then the actual widget styling, which
    we do not want in an export.
    """
    rules: list[str] = []
    pos = 0
    while pos < len(css):
        open_brace = css.find("{", pos)
        if open_brace == -1:
            break
        selector = _strip_css_comments(css[pos:open_brace]).strip()
        if selector not in (":root", ":root.night-mode"):
            break
        depth = 0
        close_brace = -1
        for idx in range(open_brace, len(css)):
            if css[idx] == "{":
                depth += 1
            elif css[idx] == "}":
                depth -= 1
                if depth == 0:
                    close_brace = idx
                    break
        if close_brace == -1:
            break
        rules.append(f"{selector}{{{css[open_brace + 1:close_brace]}}}")
        pos = close_brace + 1
    return "\n".join(rules)


# Bundled scripts
######################################################################


def copy_file(rel_path: str, dest: Path) -> bool:
    data = read(rel_path)
    if data is None:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return True


def copy_mathjax(dest_dir: Path) -> bool:
    """Mirror the parts of Anki's MathJax bundle a browser actually needs.

    That is the ``tex-chtml-full`` bundle plus the CHTML web fonts -- roughly
    1.7 MB, as opposed to 4 MB for the full directory, whose remainder only
    serves Anki's accessibility features.
    """
    main = read(MATHJAX_JS)
    if main is None:
        return False

    dest_dir.mkdir(parents=True, exist_ok=True)
    (dest_dir / "tex-chtml-full.js").write_bytes(main)

    font_dest = dest_dir / "output" / "chtml" / "fonts" / "woff-v2"
    font_dest.mkdir(parents=True, exist_ok=True)

    names: tuple[str, ...] | list[str] = MATHJAX_FONTS
    root = data_web_dir()
    if root is not None:
        on_disk = root / MATHJAX_FONT_DIR
        if on_disk.is_dir():
            names = sorted(p.name for p in on_disk.iterdir() if p.is_file())

    for name in names:
        data = read(f"{MATHJAX_FONT_DIR}/{name}")
        if data is not None:
            (font_dest / name).write_bytes(data)
    return True
