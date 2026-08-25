"""Rendering cards exactly the way the reviewer does.

Everything here goes through :meth:`anki.cards.Card.render_output`, i.e. the
same Rust template engine the reviewer uses.  That gives us cloze deletions,
conditional sections, ``{{FrontSide}}``, the built-in filters (``hint``,
``furigana``, ``text``, ``cloze``, ...), add-on supplied filters and LaTeX
image generation for free -- none of which the previous AnkiConnect based
implementation could reproduce.

What the reviewer does *around* the template engine is reimplemented here:
AV references become HTML5 players instead of ``pycmd`` buttons, and
``[[type:Field]]`` is expanded the way ``Reviewer.typeAnsFilter`` would.
"""

from __future__ import annotations

import datetime
import html
import re
from dataclasses import dataclass, field
from typing import Any

from anki.sound import AV_REF_RE, SoundOrVideoTag

TYPE_ANS_RE = re.compile(r"\[\[type:(.+?)\]\]")
ANSWER_HR_RE = re.compile(r"""(?i)<hr\s+id=["']?answer["']?\s*/?>""")
MATHJAX_RE = re.compile(r"\\\(|\\\[|\\begin\{")
# Anki's reviewer publishes jQuery as `$` / `jQuery`; shared note types use it
# heavily, so a frame whose template does has to get the same global.
JQUERY_RE = re.compile(r"(?<![\w$])(?:jQuery|\$)\s*[({]")

FLAG_NAMES = {
    0: "",
    1: "Red",
    2: "Orange",
    3: "Green",
    4: "Blue",
    5: "Pink",
    6: "Turquoise",
    7: "Purple",
}

_QUEUE_LABELS = {
    -3: "Buried",
    -2: "Buried",
    -1: "Suspended",
    0: "New",
    1: "Learning",
    2: "Review",
    3: "Learning",
    4: "Preview",
}


# Every entry the details strip can hold, in the order it is built. The live
# view needs this without having any cards in hand -- see _special_fields,
# which produces exactly these names.
SPECIAL_NAMES = [
    "Deck",
    "Subdeck",
    "Note type",
    "Card",
    "Card ID",
    "Note ID",
    "Created",
    "Modified",
    "State",
    "Due",
    "Interval",
    "Reviews",
    "Flag",
]


@dataclass
class RenderedCard:
    card_id: int
    note_id: int
    ord: int
    body_class: str
    notetype_id: int
    notetype_name: str
    template_name: str
    deck: str
    subdeck: str
    tags: list[str]
    question: str
    answer: str
    answer_only: str
    fields: list[dict[str, str]] = field(default_factory=list)
    special: list[dict[str, str]] = field(default_factory=list)
    mathjax: bool = False
    jquery: bool = False
    front_redundant: bool = False
    sort_text: str = ""


class CardRenderer:
    """Renders cards into export-ready HTML fragments."""

    def __init__(
        self,
        col: Any,
        media: Any,
        *,
        platform_class: str | None = None,
        apply_gui_hooks: bool = False,
    ) -> None:
        self.col = col
        self.media = media
        self.apply_gui_hooks = apply_gui_hooks
        self.platform_class = platform_class or _platform_class()
        # note type id -> stylesheet, deduplicated across the whole export
        self.notetype_css: dict[int, str] = {}
        self.errors: list[str] = []

    # Public API
    ##################################################################

    def render(self, card: Any) -> RenderedCard:
        out = card.render_output()
        note = card.note()
        notetype = card.note_type()
        notetype_id = int(notetype["id"])

        if notetype_id not in self.notetype_css:
            self.notetype_css[notetype_id] = self.media.rewrite_css(out.css or "")

        question = self._prepare_side(card, out.question_text, out.question_av_tags, True)
        answer = self._prepare_side(card, out.answer_text, out.answer_av_tags, False)

        deck = self.col.decks.name(card.current_deck_id())
        template = card.template()

        return RenderedCard(
            card_id=int(card.id),
            note_id=int(card.nid),
            ord=int(card.ord),
            body_class=f"card card{card.ord + 1} {self.platform_class} fancy",
            notetype_id=notetype_id,
            notetype_name=notetype["name"],
            template_name=template.get("name", ""),
            deck=deck,
            subdeck=deck.rsplit("::", 1)[-1],
            tags=[t for t in note.tags if t],
            question=question,
            answer=answer,
            answer_only=_strip_front_side(question, answer),
            fields=self._note_fields(note),
            special=self._special_fields(card, note, notetype, template, deck),
            mathjax=bool(MATHJAX_RE.search(question) or MATHJAX_RE.search(answer)),
            jquery=bool(JQUERY_RE.search(question) or JQUERY_RE.search(answer)),
            front_redundant=_front_is_redundant(question, answer),
            sort_text=_plain_text(question)[:200],
        )

    # Card sides
    ##################################################################

    def _prepare_side(
        self, card: Any, text: str, av_tags: list[Any], question_side: bool
    ) -> str:
        text = self._type_answer(card, text, question_side)
        if self.apply_gui_hooks:
            text = self._run_card_will_show(card, text, question_side)
        text = self.media.rewrite_html(text)
        return self._expand_av_refs(text, av_tags)

    def _run_card_will_show(self, card: Any, text: str, question_side: bool) -> str:
        """Let add-ons post-process the card the way the reviewer does.

        Anki runs this hook last; we run it before media collection instead, so
        that files an add-on pulls in still end up in the export.
        """
        try:
            from aqt import gui_hooks

            kind = "reviewQuestion" if question_side else "reviewAnswer"
            return gui_hooks.card_will_show(text, card, kind)
        except Exception as exc:
            self.errors.append(f"card_will_show hook failed on card {card.id}: {exc}")
            return text

    def _expand_av_refs(self, text: str, av_tags: list[Any]) -> str:
        """Replace ``[anki:play:q:0]`` markers with real media elements.

        Anki inserts ``pycmd``-driven replay buttons here; in a browser we want
        something that actually plays, so we emit ``<audio>``/``<video>``.
        """

        def repl(match: re.Match) -> str:
            index = int(match.group(3))
            if index >= len(av_tags):
                return ""
            tag = av_tags[index]
            if isinstance(tag, SoundOrVideoTag):
                return self.media.player_html(tag.filename)
            # TTSTag: nothing to play offline, so show what would be spoken.
            spoken = html.escape(getattr(tag, "field_text", ""))
            lang = html.escape(getattr(tag, "lang", ""))
            return f'<span class="ahe-tts" data-lang="{lang}">{spoken}</span>'

        return AV_REF_RE.sub(repl, text)

    def _type_answer(self, card: Any, text: str, question_side: bool) -> str:
        """Expand ``{{type:...}}`` placeholders, mirroring the reviewer."""
        match = TYPE_ANS_RE.search(text)
        if not match:
            return text

        expected, font, size, combining = self._type_target(card, match.group(1))

        if question_side:
            if expected is None:
                return TYPE_ANS_RE.sub(
                    '<span class="ahe-warning">unknown field</span>', text
                )
            if not expected:
                return TYPE_ANS_RE.sub("", text)
            box = (
                '<center><input type="text" id="typeans" class="ahe-typeans" readonly '
                f"style=\"font-family: '{html.escape(font, quote=True)}'; "
                f'font-size: {size}px;"></center>'
            )
            return TYPE_ANS_RE.sub(lambda _m: box, text)

        if not expected:
            return TYPE_ANS_RE.sub("", text)

        # Anki moves the q/a separator in front of the comparison so that a
        # template using {{FrontSide}} still reads top to bottom.
        without_hr, replaced = ANSWER_HR_RE.subn("", text, count=1)
        try:
            comparison = self.col.compare_answer(expected, "", combining)
        except Exception:
            comparison = f'<span class="typeMissed">{html.escape(expected)}</span>'

        block = (
            f"<div style=\"font-family: '{html.escape(font, quote=True)}'; "
            f'font-size: {size}px">{comparison}</div>'
        )
        if replaced:
            block = f"<hr id=answer>{block}"
        return TYPE_ANS_RE.sub(lambda _m: block, without_hr)

    def _type_target(
        self, card: Any, spec: str
    ) -> tuple[str | None, str, int, bool]:
        """Resolve ``type:``/``type:cloze:``/``type:nc:`` to its field value."""
        cloze_index = None
        combining = True
        if spec.startswith("cloze:"):
            cloze_index = card.ord + 1
            spec = spec.split(":", 1)[1]
        if spec.startswith("nc:"):
            combining = False
            spec = spec.split(":", 1)[1]

        note = card.note()
        for fld in card.note_type()["flds"]:
            if fld["name"] != spec:
                continue
            value = note[fld["name"]]
            if cloze_index is not None:
                try:
                    value = self.col.extract_cloze_for_typing(value, cloze_index) or ""
                except Exception:
                    value = ""
            return value, fld.get("font", "Arial"), int(fld.get("size", 20)), combining
        return None, "Arial", 20, combining

    # Metadata
    ##################################################################

    def _note_fields(self, note: Any) -> list[dict[str, str]]:
        fields = []
        for name, value in note.items():
            fields.append(
                {
                    "name": name,
                    "html": _sanitize_field_html(self.media.rewrite_html(value)),
                    "text": _plain_text(value),
                }
            )
        return fields

    def _special_fields(
        self, card: Any, note: Any, notetype: dict, template: dict, deck: str
    ) -> list[dict[str, str]]:
        # The order below is SPECIAL_NAMES; the two have to agree, because an
        # export derives the list from the cards it wrote while the live view
        # has no cards to derive it from and uses the constant.
        entries = [
            ("Deck", deck),
            ("Subdeck", deck.rsplit("::", 1)[-1]),
            ("Note type", notetype["name"]),
            ("Card", template.get("name", "")),
            ("Card ID", str(card.id)),
            ("Note ID", str(note.id)),
            ("Created", _timestamp(note.id)),
            ("Modified", _timestamp(note.mod * 1000)),
            ("State", self._card_state(card)),
            ("Due", self._due(card)),
            ("Interval", _interval(card)),
            ("Reviews", f"{card.reps} ({card.lapses} lapses)"),
            ("Flag", FLAG_NAMES.get(card.user_flag(), "")),
        ]
        return [{"name": name, "value": value} for name, value in entries if value]

    def _card_state(self, card: Any) -> str:
        return _QUEUE_LABELS.get(int(card.queue), "")

    def _due(self, card: Any) -> str:
        try:
            if int(card.type) != 2 or int(card.queue) < 0:
                return ""
            today = self.col.sched.today
            due_date = datetime.date.today() + datetime.timedelta(days=card.due - today)
            return due_date.isoformat()
        except Exception:
            return ""


# Helpers
######################################################################


def _platform_class() -> str:
    try:
        from anki.utils import is_mac, is_win

        if is_win:
            return "isWin"
        if is_mac:
            return "isMac"
    except Exception:
        pass
    return "isLin"


def _strip_front_side(question: str, answer: str) -> str:
    """The answer without the repeated question.

    Templates conventionally separate the two with ``<hr id=answer>``; when
    that is missing we fall back to dropping a literal question prefix, which
    is what the browser's answer column does.
    """
    parts = ANSWER_HR_RE.split(answer, maxsplit=1)
    if len(parts) == 2:
        return parts[1]
    if question and answer.startswith(question):
        return answer[len(question) :]
    return answer


_SCRIPT_BLOCK_RE = re.compile(r"(?is)<script\b[^>]*>.*?</script\s*>|<script\b[^>]*/?>")
_STYLE_BLOCK_RE = re.compile(r"(?is)<style\b[^>]*>.*?</style\s*>")
_EVENT_ATTR_RE = re.compile(
    r"""(?is)\son[a-z]+\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)"""
)


def _sanitize_field_html(html_text: str) -> str:
    """Make a raw field value safe to place in the export's own document.

    Card sides live in their own frame and may do as they please, but field
    values are shown in the shell, where a stray <style> would restyle the whole
    page and a <script> would run against it.
    """
    html_text = _SCRIPT_BLOCK_RE.sub("", html_text)
    html_text = _STYLE_BLOCK_RE.sub("", html_text)
    return _EVENT_ATTR_RE.sub("", html_text)


_WORD_RE = re.compile(r"\w+", re.UNICODE)
# The reviewer's cloze placeholder; it is the one part of a question that is
# deliberately absent from the answer.
_CLOZE_PLACEHOLDER_RE = re.compile(r"\[\s*\.\.\.\s*\]|\[…\]")


def _front_is_redundant(question: str, answer: str, threshold: float = 0.85) -> bool:
    """Does the answer already contain everything the question says?

    Standard templates put ``{{FrontSide}}`` on the back and cloze templates
    render the same text with the deletions revealed, so showing both sides
    means showing the question twice. Templates whose back carries different
    information -- a plain "Capital of France?" / "Paris" pair, say -- do need
    both. Comparing word coverage catches both cases, including clozes in the
    middle of a sentence, where the answer is not a superstring of the question.
    """
    q_text = _CLOZE_PLACEHOLDER_RE.sub(" ", _plain_text(question))
    question_words = _WORD_RE.findall(q_text.casefold())
    if len(question_words) < 3:
        # Too little to judge; assume the sides differ and show both.
        return False
    answer_words = set(_WORD_RE.findall(_plain_text(answer).casefold()))
    covered = sum(1 for word in question_words if word in answer_words)
    return covered / len(question_words) >= threshold


def _plain_text(text: str) -> str:
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>|</(p|div|li|tr|h[1-6])>", " ", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _timestamp(millis: int) -> str:
    try:
        return datetime.datetime.fromtimestamp(millis / 1000).strftime("%Y-%m-%d %H:%M")
    except (OSError, OverflowError, ValueError):
        return ""


def _interval(card: Any) -> str:
    ivl = int(card.ivl or 0)
    if ivl <= 0:
        return ""
    if ivl < 30:
        return f"{ivl} d"
    if ivl < 365:
        return f"{ivl / 30.0:.1f} mo"
    return f"{ivl / 365.0:.1f} y"
