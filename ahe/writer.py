"""Assembling the export document.

The page is written as static HTML for everything the reader may want to
search, print or copy -- metadata, note fields, the card list itself -- while
the card sides are handed to the shell script as data and mounted into frames
on demand.  Storing them as data rather than as markup keeps each note type's
stylesheet in the file exactly once instead of once per card.
"""

from __future__ import annotations

import base64
import datetime
import html
import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

from . import assets
from .renderer import FLAG_NAMES, RenderedCard

WEB_DIR = Path(__file__).with_name("web")

# Tailwind ships no base reset here -- see tailwind/input.css for why -- so a
# button has to be told not to look like a native one.
def _deck_picker() -> str:
    """The deck being read, and the way to another one. Live view only.

    An export is a fixed set of cards -- there is no other deck to switch to,
    so this is not written into one at all. In the live view the collection is
    all there, and the panel can already reach every deck of it; what it cannot
    do is say *where you are*. It is a drawer, it ticks several decks at once,
    and nothing in the bar names the one on screen. So the bar carries the deck
    itself, and switching is a click and a name rather than opening a panel and
    hunting for a row.

    It starts hidden and is shown once the collection has said what decks there
    are: a box offering nothing is worse than no box.
    """
    return """<span class="ahe-menu ahe-deck" data-control="deck" hidden>
<button class="ahe-deck-button" data-menu-toggle aria-haspopup="listbox" aria-expanded="false" title="Choose which deck is being read"><span class="ahe-deck-name" data-control="deck-name">All decks</span><span class="ahe-deck-caret" aria-hidden="true">▾</span></button>
<div class="ahe-menu-panel ahe-deck-panel">
<div class="ahe-deck-find"><input type="search" data-control="deck-find" placeholder="Find a deck…" aria-label="Find a deck"></div>
<div class="ahe-deck-list" role="listbox" data-control="deck-list"></div>
</div>
</span>"""


def _nav_panel() -> str:
    """The filter panel's shell, empty until the page fills it.

    Both documents carry it -- the export and the live view -- and it has to be
    the same markup in both, because one script drives it.

    Open and closed are a class on the root element rather than the ``hidden``
    attribute: ``hidden`` means ``display: none``, and there is nothing to
    animate between a box that exists and one that does not.
    """
    return f"""<div class="ahe-nav-backdrop fixed inset-0 z-30 bg-black/40 print:hidden"></div>
<aside class="ahe-nav fixed inset-y-0 left-0 z-40 flex w-[min(360px,88vw)] flex-col border-r border-solid border-line bg-surface shadow-2xl print:hidden" aria-label="Filter">
<div class="flex items-center gap-2.5 border-b border-solid border-line px-3.5 py-3">
<strong>Filter</strong>
<span class="ahe-nav-count text-xs text-muted"></span>
<button class="ahe-nav-close ml-auto cursor-pointer appearance-none rounded-md border-0 bg-transparent px-2 text-xl leading-tight text-muted hover:bg-surface-2 hover:text-ink" data-nav-close title="Close">×</button>
</div>
<div class="ahe-nav-body flex-1 overflow-y-auto pt-1.5 pb-3"></div>
<div class="flex items-center gap-2 border-t border-solid border-line px-3.5 py-2.5">
<button data-nav-reset class="{_NAV_BUTTON}">Reset all</button>
<span class="flex-1"></span>
<button data-nav-expand="1" title="Unfold every branch" class="{_NAV_BUTTON}">Expand</button>
<button data-nav-expand="0" title="Fold every branch" class="{_NAV_BUTTON}">Collapse</button>
</div>
</aside>"""


_NAV_BUTTON = (
    "cursor-pointer appearance-none rounded-lg border border-solid border-line "
    "bg-surface px-3 py-1.5 [font:inherit] text-ink hover:bg-surface-2"
)
MATHJAX_REL_DIR = "js/vendor/mathjax"
MATHJAX_REL_JS = f"{MATHJAX_REL_DIR}/tex-chtml-full.js"
JQUERY_REL_JS = "js/vendor/jquery.min.js"


@dataclass
class ExportOptions:
    # None for the live view, which serves its pages instead of writing them
    output_dir: Path | None = None
    title: str = "Anki Export"
    single_file: bool = False
    embed_mathjax: bool = True
    download_remote: bool = False
    # run gui_hooks.card_will_show, as the reviewer does; off by default
    # because those hooks are written for the reviewer, not for an export
    apply_gui_hooks: bool = False
    # initial state of the mass toggles. The synthetic blocks -- header row,
    # details strip, note fields -- start closed; they are reference material,
    # not part of the card.
    view_mode: str = "auto"
    show_meta: bool = False
    show_special: bool = False
    show_fields: bool = False
    dark: bool = False
    # Let the reader uncover a card the way the reviewer does: click a cloze
    # deletion or an occlusion mask to lift it.
    interactive: bool = True
    # Start in study mode, where only the front is on screen and the answer
    # waits for a click.
    study: bool = False
    # None means "every field"; otherwise only these names are written
    included_fields: set[str] | None = None
    included_special: set[str] | None = None
    extra_note: str = ""


@dataclass
class ExportResult:
    path: Path
    card_count: int
    media_count: int
    media_bytes: int
    missing_media: list[str] = field(default_factory=list)
    mathjax_embedded: bool = False


class ExportWriter:
    def __init__(self, options: ExportOptions) -> None:
        self.options = options
        # set by _facets(), which has to run before the cards are written
        self._facet_index: Callable[[str, str], int] = _no_facets

    # Public API
    ##################################################################

    def write(
        self,
        cards: Sequence[RenderedCard],
        notetype_css: dict[int, str],
        media_stats: tuple[int, int, Iterable[str]],
    ) -> ExportResult:
        options = self.options
        out_dir = Path(options.output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        needs_mathjax = any(card.mathjax for card in cards)
        mathjax_url = ""
        mathjax_dir = ""
        mathjax_source = ""
        mathjax_embedded = False

        if needs_mathjax and options.embed_mathjax:
            if options.single_file:
                payload = assets.read(assets.MATHJAX_JS)
                if payload is not None:
                    mathjax_source = base64.b64encode(payload).decode("ascii")
                    mathjax_embedded = True
            else:
                if assets.copy_mathjax(out_dir / MATHJAX_REL_DIR):
                    mathjax_url = MATHJAX_REL_JS
                    mathjax_dir = MATHJAX_REL_DIR
                    mathjax_embedded = True

        jquery_url = ""
        jquery_source = ""
        if any(card.jquery for card in cards):
            payload = assets.read(assets.JQUERY_JS)
            if payload is not None:
                if options.single_file:
                    jquery_source = base64.b64encode(payload).decode("ascii")
                else:
                    (out_dir / JQUERY_REL_JS).parent.mkdir(parents=True, exist_ok=True)
                    (out_dir / JQUERY_REL_JS).write_bytes(payload)
                    jquery_url = JQUERY_REL_JS

        shared = {
            "theme": assets.theme_variables_css(),
            "reviewer": assets.reviewer_css(),
            "frame": _read_web("frame.css"),
            "frameJs": _read_web("frame.js"),
            "shimJs": _read_web("shim.js"),
        }

        payload = {
            "meta": {
                "id": _slug(options.title),
                "title": options.title,
                "generated": datetime.datetime.now().isoformat(timespec="seconds"),
                "viewMode": options.view_mode,
                "showMeta": options.show_meta,
                "showSpecial": options.show_special,
                "showFields": options.show_fields,
                "dark": options.dark,
                "interactive": options.interactive,
                "study": options.study,
                "mathjaxUrl": mathjax_url,
                "mathjaxDir": mathjax_dir,
                "jqueryUrl": jquery_url,
            },
            "shared": shared,
            "css": {str(ntid): css for ntid, css in notetype_css.items()},
            "facets": self._facets(cards),
            "cards": [self._card_payload(card) for card in cards],
        }
        if mathjax_source:
            payload["mathjaxSource"] = mathjax_source
        if jquery_source:
            payload["jquerySource"] = jquery_source

        # Only list what the document actually contains, so no switch is dead.
        field_names = [
            name
            for name in _ordered_names(
                n for card in cards for n in (f["name"] for f in card.fields)
            )
            if options.included_fields is None or name in options.included_fields
        ]
        special_names = _ordered_names(
            name for card in cards for name in (s["name"] for s in card.special)
        )
        hidden_special = [
            name
            for name in special_names
            if options.included_special is not None
            and name not in options.included_special
        ]
        payload["meta"]["hiddenSpecial"] = hidden_special

        document = self._document(
            cards, payload, field_names, special_names, set(hidden_special), out_dir
        )

        index = out_dir / "index.html"
        index.write_text(document, encoding="utf-8")

        media_count, media_bytes, missing = media_stats
        return ExportResult(
            path=index,
            card_count=len(cards),
            media_count=media_count,
            media_bytes=media_bytes,
            missing_media=sorted(missing),
            mathjax_embedded=mathjax_embedded,
        )

    # Payload
    ##################################################################

    def _facets(self, cards: Sequence[RenderedCard]) -> dict:
        """Deck, note type, tag and state of every card, as index tables.

        The navigation panel filters over these, and a full export can run to
        thousands of cards -- writing "Medicine::Anatomy::Upper limb" out once
        per card would cost more than the rest of the payload together.
        """
        tables: dict[str, list[str]] = {
            "decks": [],
            "notetypes": [],
            "templates": [],
            "tags": [],
            "states": [],
        }
        lookup: dict[str, dict[str, int]] = {key: {} for key in tables}

        def index(table: str, value: str) -> int:
            known = lookup[table]
            if value not in known:
                known[value] = len(tables[table])
                tables[table].append(value)
            return known[value]

        self._facet_index = index
        for card in cards:
            index("decks", card.deck)
            index("notetypes", card.notetype_name)
            index("templates", card.template_name)
            index("states", _special_value(card, "State"))
            for tag in card.tags:
                index("tags", tag)
        return tables

    def _card_facets(self, card: RenderedCard) -> dict:
        index = self._facet_index
        flag_name = _special_value(card, "Flag")
        return {
            "deck": index("decks", card.deck),
            "notetype": index("notetypes", card.notetype_name),
            "template": index("templates", card.template_name),
            "state": index("states", _special_value(card, "State")),
            "tags": [index("tags", tag) for tag in card.tags],
            "flag": next(
                (num for num, name in FLAG_NAMES.items() if name == flag_name), 0
            ),
        }

    def _card_payload(self, card: RenderedCard) -> dict:
        haystack = " ".join(
            [card.sort_text]
            + [f["text"] for f in card.fields]
            + card.tags
            + [card.deck, card.notetype_name, card.template_name, str(card.card_id)]
        ).lower()
        payload = {
            "id": card.card_id,
            "ntid": str(card.notetype_id),
            "bodyClass": card.body_class,
            "q": card.question,
            "a": card.answer,
            "mathjax": card.mathjax,
            "jquery": card.jquery,
            "frontRedundant": card.front_redundant,
            "search": haystack,
        }
        # Only when there is genuinely something to trim. A cloze answer is
        # the question revealed and many templates do not repeat the front at
        # all, so for most cards the two are the same string -- and writing it
        # twice was a third of the payload in an export of real decks.
        if card.answer_only != card.answer:
            payload["aOnly"] = card.answer_only
        if self._facet_index is not _no_facets:
            payload["facets"] = self._card_facets(card)
        return payload

    # Pieces the live view assembles itself
    ##################################################################

    def card_fragment(self, card: RenderedCard, index: int, total: int) -> dict:
        """One card as the export writes it, ready to be inserted.

        The live view could build this markup in JavaScript, but then there
        would be two card templates to keep in step. Handing over the very
        markup the export writes keeps everything downstream of it -- the
        per-card toggles, study mode, shuffling, the print layout -- working
        without a second implementation.
        """
        return {
            "id": card.card_id,
            "html": self._card_html(card, index, total),
            "payload": self._card_payload(card),
        }

    def shared_assets(self) -> dict:
        return {
            "theme": assets.theme_variables_css(),
            "reviewer": assets.reviewer_css(),
            "frame": _read_web("frame.css"),
            "frameJs": _read_web("frame.js"),
            "shimJs": _read_web("shim.js"),
        }

    def live_shell(
        self,
        asset_prefix: str,
        field_names: list[str] | None = None,
        special_names: list[str] | None = None,
    ) -> str:
        """The export page with no cards in it yet.

        Everything the reader operates -- the control bar, the panel, the
        toggles -- is the same; only the cards arrive later, from the server.

        An export lists the fields it actually wrote, which it knows because it
        wrote them. Here they come from the collection instead: without them
        the *Fields* and *Details* controls would be switches with nothing
        behind them, and every field would be shown with no way to say
        otherwise.
        """
        options = self.options
        fields = [
            name
            for name in (field_names or [])
            if options.included_fields is None or name in options.included_fields
        ]
        specials = list(special_names or [])
        hidden_special = [
            name
            for name in specials
            if options.included_special is not None
            and name not in options.included_special
        ]
        payload = {
            "meta": {
                "id": "live",
                "title": options.title,
                "live": True,
                "viewMode": options.view_mode,
                "showMeta": options.show_meta,
                "showSpecial": options.show_special,
                "showFields": options.show_fields,
                "dark": options.dark,
                "interactive": options.interactive,
                "study": options.study,
                "hiddenSpecial": hidden_special,
                # An export copies these in beside the document; the live view
                # serves them out of Anki's own assets. Either way a card that
                # asks for them gets them -- a formula typesets, and a note
                # type written against the reviewer's jQuery finds it there.
                "mathjaxUrl": f"{asset_prefix}vendor/mathjax/tex-chtml-full.js",
                "mathjaxDir": f"{asset_prefix}vendor/mathjax",
                "jqueryUrl": f"{asset_prefix}vendor/jquery.min.js",
            },
            "shared": self.shared_assets(),
            "css": {},
            "cards": [],
        }

        root_classes = [f"mode-{options.view_mode}", "ahe-live"]
        for enabled, name in (
            (options.show_meta, "show-meta"),
            (options.show_special, "show-special"),
            (options.show_fields, "show-fields"),
            (options.dark, "ahe-dark"),
            (options.study, "ahe-study"),
        ):
            if enabled:
                root_classes.append(name)

        return f"""<!doctype html>
<html lang="en" class="{' '.join(root_classes)}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(options.title)}</title>
<link rel="stylesheet" href="{asset_prefix}tailwind.css">
<link rel="stylesheet" href="{asset_prefix}shell.css">
</head>
<body>
{self._bar(0, fields, specials, set(hidden_special), live=True)}
{_nav_panel()}
<main>
<p class="ahe-empty" hidden>No card matches the current search.</p>
</main>
<script type="application/json" id="ahe-data">{_json_for_script(payload)}</script>
<script src="{asset_prefix}shell.js"></script>
</body>
</html>
"""

    # Document
    ##################################################################

    def _document(
        self,
        cards: Sequence[RenderedCard],
        payload: dict,
        field_names: list[str],
        special_names: list[str],
        hidden_special: set[str],
        out_dir: Path,
    ) -> str:
        options = self.options
        # Tailwind first: it defines only what the navigation panel uses, and
        # shell.css supplies the colour tokens those utilities resolve against.
        tailwind_css = _read_web("tailwind.css")
        shell_css = _read_web("shell.css")
        shell_js = _read_web("shell.js")

        if options.single_file:
            head_style = f"<style>\n{tailwind_css}\n</style>\n<style>\n{shell_css}\n</style>"
            tail_script = f"<script>\n{shell_js}\n</script>"
        else:
            (out_dir / "css").mkdir(exist_ok=True)
            (out_dir / "js").mkdir(exist_ok=True)
            (out_dir / "css" / "tailwind.css").write_text(tailwind_css, encoding="utf-8")
            (out_dir / "css" / "shell.css").write_text(shell_css, encoding="utf-8")
            (out_dir / "js" / "shell.js").write_text(shell_js, encoding="utf-8")
            head_style = (
                '<link rel="stylesheet" href="css/tailwind.css">\n'
                '<link rel="stylesheet" href="css/shell.css">'
            )
            tail_script = '<script src="js/shell.js"></script>'

        root_classes = [f"mode-{options.view_mode}"]
        for enabled, name in (
            (options.show_meta, "show-meta"),
            (options.show_special, "show-special"),
            (options.show_fields, "show-fields"),
            (options.dark, "ahe-dark"),
            (options.study, "ahe-study"),
        ):
            if enabled:
                root_classes.append(name)

        data_json = _json_for_script(payload)

        return f"""<!doctype html>
<html lang="en" class="{' '.join(root_classes)}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(options.title)}</title>
{head_style}
</head>
<body>
{self._bar(len(cards), field_names, special_names, hidden_special)}
{_nav_panel()}
<main>
{''.join(self._card_html(card, i + 1, len(cards)) for i, card in enumerate(cards))}
<p class="ahe-empty" hidden>No card matches the current search.</p>
</main>
<script type="application/json" id="ahe-data">{data_json}</script>
{tail_script}
</body>
</html>
"""

    def _bar(
        self,
        count: int,
        field_names: list[str],
        special_names: list[str],
        hidden_special: set[str],
        live: bool = False,
    ) -> str:
        options = self.options
        note = (
            f'<span class="ahe-chip">{html.escape(options.extra_note)}</span>'
            if options.extra_note
            else ""
        )
        # Two lines by design rather than by accident: the first says where you
        # are and what you are looking for, the second what you see and what
        # you do. A single wrapping row let the window width decide which
        # control landed in which line, so the same bar had a different shape
        # at every size.
        return f"""<header class="ahe-bar">
<div class="ahe-bar-line ahe-bar-head">
<span class="ahe-title">{html.escape(options.title)}</span>
{_deck_picker() if live else ""}
<span class="ahe-count" data-control="count">{count} cards</span>
{note}
<span class="ahe-spacer"></span>
<button class="ahe-solid" data-control="qr" aria-expanded="false" hidden title="Show this page's address as a code, to open it on a phone">QR</button>
<button class="ahe-solid" data-control="nav" aria-expanded="false" title="Filter by deck, tag, note type, state or flag">Filter…</button>
<input type="search" data-control="search" placeholder="Search…" aria-label="Search cards">
</div>
<div class="ahe-bar-line ahe-bar-tools">
<span class="ahe-segmented" role="group" aria-label="Card sides">
<button data-mode="auto" title="Anki's answer side, which normally carries the question with it; the question is shown separately only for cards whose answer does not contain it">Auto</button>
<button data-mode="qa" title="Question and answer separately, with the repeated question trimmed from the answer">Q + A</button>
<button data-mode="a" title="Only the answer, without the question Anki repeats on the back">A</button>
<button data-mode="q" title="Only the question">Q</button>
</span>
<span class="ahe-sep"></span>
<span class="ahe-group">
<button data-control="meta" aria-pressed="{_pressed(options.show_meta)}" title="Show deck, note type and tags in each card's header">Info</button>
{_split(
    f'<button data-control="special" aria-pressed="{_pressed(options.show_special)}"'
    ' title="Show scheduling data and IDs">Details</button>',
    _menu_panel("special-name", special_names, hidden_special),
    "Choose which details are shown",
)}
{_split(
    f'<button data-control="fields" aria-pressed="{_pressed(options.show_fields)}"'
    ' title="Show the raw note fields">Fields</button>',
    _menu_panel("field-name", field_names, set(), extra_actions=(
        '<button data-fields-expand="1">Expand all</button>'
        '<button data-fields-expand="0">Collapse all</button>'
    )),
    "Choose which fields are shown",
)}
</span>
<span class="ahe-sep"></span>
<span class="ahe-group">
<button data-control="study" aria-pressed="{_pressed(options.study)}" title="Study mode: only the front is shown and the answer waits for a click. Space reveals, arrow keys move between cards.">Study</button>
<button data-control="interactive" aria-pressed="{_pressed(options.interactive)}" title="Let cloze deletions and occlusion masks be lifted by clicking them">Reveal</button>
<button data-control="shuffle" title="Deal the cards again in a random order. The export's own order comes back on reload.">Shuffle</button>
</span>
<span class="ahe-sep"></span>
<span class="ahe-group">
<button data-control="dark" aria-pressed="{_pressed(options.dark)}" title="Switch cards and page to night mode">Night</button>
</span>
<span class="ahe-sep"></span>
{_print_menu()}
</div>
</header>"""

    def _card_html(self, card: RenderedCard, index: int, total: int) -> str:
        options = self.options
        anchor = f"card-{card.card_id}"

        tags = _tags_html(card.tags)
        flag = ""
        flag_name = next(
            (s["value"] for s in card.special if s["name"] == "Flag"), ""
        )
        if flag_name:
            number = next(
                (num for num, name in FLAG_NAMES.items() if name == flag_name), 0
            )
            flag = f'<span class="ahe-chip ahe-flag-{number}">⚑ {html.escape(flag_name)}</span>'

        # Every special field is written out and merely starts hidden if it was
        # not selected -- unlike note fields, they cost nothing, and leaving
        # them out of the document would make the export's own switches dead.
        specials = card.special
        fields = [
            entry
            for entry in card.fields
            if (
                options.included_fields is None
                or entry["name"] in options.included_fields
            )
            and entry["html"].strip()
        ]

        # A wrapping strip of key/value chips reads far better in a preview than
        # a two-column table that pushes the next card off screen.
        special_html = "".join(
            f'<span class="ahe-special-row" data-special="{html.escape(entry["name"], quote=True)}">'
            f'<span class="ahe-special-key">{html.escape(entry["name"])}</span>'
            f'<span class="ahe-special-value">{html.escape(entry["value"])}</span></span>'
            for entry in specials
        )
        # Fields are collapsed: expanded, they repeat the whole card a second
        # time and bury the card boundaries.
        fields_html = "".join(
            f'<details class="ahe-field" data-field="{html.escape(entry["name"], quote=True)}">'
            f'<summary><span class="ahe-field-name">{html.escape(entry["name"])}</span>'
            f'<span class="ahe-field-peek">{html.escape(_peek(entry["text"]))}</span></summary>'
            f'<div class="ahe-field-value">{entry["html"]}</div></details>'
            for entry in fields
        )

        return f"""<article class="ahe-card" id="{anchor}" data-cid="{card.card_id}">
<div class="ahe-meta">
<span class="ahe-index">{index}<span class="ahe-index-total">/{total}</span></span>
<a href="#{anchor}" title="Card ID">#{card.card_id}</a>
<span class="ahe-chip">{html.escape(card.deck)}</span>
<span class="ahe-chip">{html.escape(card.notetype_name)}{_dot(card.template_name)}</span>
{flag}{tags}
<span class="ahe-card-toggles">
<button data-card-toggle="q" title="Front of this card">Q</button>
<button data-card-toggle="a" title="Back of this card">A</button>
<button data-card-toggle="s" title="Details of this card">i</button>
<button data-card-toggle="f" title="Fields of this note">F</button>
</span>
</div>
<section class="ahe-side" data-side="q"><div class="ahe-side-head">Front</div>
<div class="ahe-slot"><div class="ahe-placeholder">…</div></div></section>
<div class="ahe-reveal"><button data-card-reveal type="button">Show answer</button></div>
<section class="ahe-side" data-side="a"><div class="ahe-side-head">Back</div>
<div class="ahe-slot"><div class="ahe-placeholder">…</div></div></section>
<div class="ahe-special"><span class="ahe-block-ref">Card {index}/{total}</span>{special_html}</div>
<div class="ahe-fields"><div class="ahe-fields-head">Note fields<span class="ahe-block-ref">card {index}/{total}</span></div>{fields_html}</div>
</article>
"""


# Helpers
######################################################################


def _no_facets(table: str, value: str) -> int:
    raise RuntimeError("facet tables must be built before the card payloads")


def _special_value(card: RenderedCard, name: str) -> str:
    return next((s["value"] for s in card.special if s["name"] == name), "")


def _read_web(name: str) -> str:
    return (WEB_DIR / name).read_text(encoding="utf-8")


def _pressed(value: bool) -> str:
    return "true" if value else "false"


def _dot(text: str) -> str:
    return f" · {html.escape(text)}" if text else ""


def _tags_html(tags: list[str]) -> str:
    """Header tags: tag families when collapsed, full paths when expanded.

    Shared decks put twenty deeply nested tags on a note. Printed in full they
    take over the header; printed as leaves they repeat themselves, because the
    same leaf shows up under several parents. The top-level component is the
    part that actually tells the reader something at a glance.
    """
    if not tags:
        return ""

    roots = _ordered_names(tag.split("::", 1)[0] for tag in tags)
    summary = "".join(
        f'<span class="ahe-tag">#{html.escape(root)}</span>' for root in roots
    )
    full = "".join(
        f'<span class="ahe-tag">#{html.escape(tag)}</span>' for tag in tags
    )
    label = f"{len(tags)} tag" + ("s" if len(tags) != 1 else "")
    return (
        f'<span class="ahe-tags-summary">{summary}</span>'
        f'<span class="ahe-tags-full">{full}</span>'
        f'<button class="ahe-chip ahe-tags-toggle" data-tags-toggle '
        f'title="Show every tag in full" data-label="{label}">{label}</button>'
    )


def _peek(text: str, limit: int = 120) -> str:
    """One-line teaser for a collapsed field."""
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def _menu_panel(
    attribute: str,
    names: list[str],
    hidden: set[str],
    extra_actions: str = "",
) -> str:
    """The check list behind a control. Empty when there is nothing to list."""
    if not names:
        return ""
    entries = "".join(
        f'<label><input type="checkbox" data-{attribute}="{html.escape(name, quote=True)}"'
        f'{"" if name in hidden else " checked"}> {html.escape(name)}</label>'
        for name in names
    )
    extra = (
        f'<div class="ahe-menu-actions">{extra_actions}</div>' if extra_actions else ""
    )
    return f"""<div class="ahe-menu-panel">
<div class="ahe-menu-actions">
<button data-menu-all="on">All</button><button data-menu-all="off">None</button>
</div>
{extra}
{entries}
</div>"""


def _split(lead: str, panel: str, title: str) -> str:
    """A switch and the list it applies to, as one control.

    They were two buttons standing side by side -- ``Details`` and
    ``Details…`` -- which said nothing about belonging together. With nothing
    to list, the switch stands alone rather than growing a triangle that opens
    an empty box.
    """
    if not panel:
        return lead
    escaped = html.escape(title, quote=True)
    return (
        f'<span class="ahe-menu ahe-split">{lead}'
        f'<button class="ahe-caret" data-menu-toggle title="{escaped}"'
        f' aria-label="{escaped}">▾</button>{panel}</span>'
    )


PRINT_SIZES = [
    ("large", "Large", "One card per row, at the largest size that still fits"),
    ("medium", "Medium", "Two cards per row"),
    ("small", "Small", "Two cards per row, tighter"),
]


def _print_menu() -> str:
    """Printing as one control instead of three.

    ``Print: large`` was a button carrying its own state in its name and
    cycling through the others when pressed -- the one control in the bar you
    had to click to find out what it did.
    """
    sizes = "".join(
        f'<button data-print-size="{key}" title="{html.escape(hint, quote=True)}">{label}</button>'
        for key, label, hint in PRINT_SIZES
    )
    return f"""<span class="ahe-menu ahe-split">
<button data-control="print" title="Lay the cards out for paper at the chosen density, then open the print dialog. Use this rather than Ctrl+P.">Print</button>
<button class="ahe-caret" data-menu-toggle title="Print density and loading" aria-label="Print density and loading">▾</button>
<div class="ahe-menu-panel ahe-print-panel">
<div class="ahe-menu-head">Density</div>
<div class="ahe-print-sizes" role="group" aria-label="Print density">{sizes}</div>
<div class="ahe-menu-actions">
<button data-control="load-all" title="Render every card, so the browser's own search and a plain Ctrl+P cover all of them">Load every card</button>
</div>
</div>
</span>"""


def _ordered_names(names: Iterable[str]) -> list[str]:
    """Unique names, keeping the order they first appear in."""
    seen: dict[str, None] = {}
    for name in names:
        seen.setdefault(name, None)
    return list(seen)


def _json_for_script(payload: dict) -> str:
    """JSON that is safe to embed in a ``<script>`` element."""
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "export"


def clear_previous_export(directory: Path) -> None:
    """Remove artefacts of an earlier export so stale media do not pile up."""
    for name in ("index.html", "css", "js", "media"):
        target = directory / name
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        elif target.exists():
            target.unlink()
