"""Driving a full export: find cards, render them, write the document."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from .media_export import MEDIA_SUBDIR, MediaCollector
from .renderer import CardRenderer, RenderedCard
from .writer import ExportOptions, ExportResult, ExportWriter, clear_previous_export

ProgressCallback = Callable[[int, int], bool]
"""Called with (done, total); return False to abort the export."""

ORDERS = {
    "deck": "Deck, then creation order",
    "created": "Creation order",
    "card_id": "Card ID",
    "due": "Due date",
}


class ExportAborted(Exception):
    pass


@dataclass
class ExportRequest:
    options: ExportOptions
    search: str = ""
    card_ids: Sequence[int] | None = None
    order: str = "deck"
    errors: list[str] = field(default_factory=list)


# A suspended or buried card is one the user has taken out of the way, and an
# Anki search returns it all the same -- so an export of a deck was showing
# cards its owner had deliberately set aside.
HIDDEN_TERMS = "-is:suspended -is:buried"
_HIDDEN_RE = re.compile(r"(?<![\w:-])is:(suspended|buried)\b", re.IGNORECASE)


def without_hidden(search: str) -> str:
    """The search, narrowed to cards that are neither suspended nor buried.

    A search that asks for those states itself (``is:suspended``, not the
    negated form) is left alone; adding the exclusion would match nothing.
    """
    search = search.strip()
    if not search or _HIDDEN_RE.search(search):
        return search
    return f"({search}) {HIDDEN_TERMS}"


def build_search(
    deck: str | None,
    tags: Sequence[str],
    extra: str = "",
    include_hidden: bool = False,
) -> str:
    """Compose an Anki search from the dialog's filter widgets."""
    parts: list[str] = []
    if deck:
        parts.append(f'"deck:{_escape(deck)}"')
    for tag in tags:
        if tag:
            parts.append(f'"tag:{_escape(tag)}"')
    extra = extra.strip()
    if extra:
        parts.append(f"({extra})")
    search = " ".join(parts)
    return search if include_hidden else without_hidden(search)


def _escape(text: str) -> str:
    """A deck or tag name as it goes inside a quoted search term.

    Inside the quotes Anki still reads the backslash, the quote and the
    wildcards ``*`` and ``_`` specially -- and shared decks are full of
    underscores (``Ankizin::M1_Vorklinik``) and the odd asterisk.
    """
    return re.sub(r'[\\"*_]', lambda m: "\\" + m.group(0), text)


def find_card_ids(col: Any, request: ExportRequest) -> list[int]:
    if request.card_ids is not None:
        return list(request.card_ids)
    if not request.search.strip():
        return []
    return list(col.find_cards(request.search))


def run_export(
    col: Any,
    request: ExportRequest,
    progress: ProgressCallback | None = None,
) -> ExportResult:
    options = request.options
    out_dir = Path(options.output_dir)
    clear_previous_export(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    card_ids = find_card_ids(col, request)
    total = len(card_ids)
    if not total:
        return ExportResult(path=out_dir / "index.html", card_count=0, media_count=0, media_bytes=0)

    media = MediaCollector(
        col.media.dir(),
        None if options.single_file else out_dir / MEDIA_SUBDIR,
        inline=options.single_file,
        download_remote=options.download_remote,
    )
    renderer = CardRenderer(col, media, apply_gui_hooks=options.apply_gui_hooks)

    rendered: list[RenderedCard] = []
    for index, card_id in enumerate(card_ids):
        if progress is not None and not progress(index, total):
            raise ExportAborted()
        try:
            rendered.append(renderer.render(col.get_card(card_id)))
        except Exception as exc:  # a single broken card must not kill the export
            request.errors.append(f"card {card_id}: {exc}")

    _sort(rendered, request.order)

    if progress is not None and not progress(total, total):
        raise ExportAborted()

    writer = ExportWriter(options)
    result = writer.write(
        rendered,
        renderer.notetype_css,
        (media.copied, media.copied_bytes, media.missing),
    )
    request.errors.extend(renderer.errors)
    return result


def _sort(cards: list[RenderedCard], order: str) -> None:
    if order == "created":
        cards.sort(key=lambda c: (c.note_id, c.ord))
    elif order == "card_id":
        cards.sort(key=lambda c: c.card_id)
    elif order == "due":
        cards.sort(key=lambda c: (_due_key(c), c.card_id))
    else:
        cards.sort(key=lambda c: (c.deck.lower(), c.note_id, c.ord))


def _due_key(card: RenderedCard) -> str:
    for entry in card.special:
        if entry["name"] == "Due":
            return entry["value"]
    return "￿"
