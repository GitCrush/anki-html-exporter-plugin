"""The rendering state behind the narrator's page.

The live view of the export has one of its own; this one differs in what it
keeps -- the order the cards come in, and every card rendered so far, which
the narration looks up by id -- so it is a second object on the same server,
built from the same renderer.
"""

from __future__ import annotations

import random
from typing import Any

from ..export import without_hidden
from ..media_export import MediaCollector
from ..renderer import CardRenderer, RenderedCard

MEDIA_PREFIX = "/media/"


class View:
    """The rendering state behind the page.

    Only ever touched from the collection thread, so it needs no locking of
    its own. It holds the renderer -- which accumulates note type stylesheets
    -- the card ids the current scope resolved to, and the cards rendered so
    far, which the narration endpoint looks up by id.
    """

    def __init__(self) -> None:
        self._renderer: CardRenderer | None = None
        self.query = ""
        self.order = "browser"
        self.card_ids: list[int] = []
        self.rendered: dict[int, RenderedCard] = {}

    def renderer(self, col: Any, apply_gui_hooks: bool) -> CardRenderer:
        if self._renderer is None:
            media = MediaCollector(col.media.dir(), None, link_prefix=MEDIA_PREFIX)
            self._renderer = CardRenderer(col, media, apply_gui_hooks=apply_gui_hooks)
        return self._renderer

    def set_scope(self, col: Any, query: str, include_hidden: bool, order: str = "browser") -> dict:
        query = query.strip()
        self.query = query
        self.order = order
        if query:
            search = query if include_hidden else without_hidden(query)
            # order=True is the sort the user set in Anki's own browser.
            ids = list(col.find_cards(search, order=True))
            if order == "random":
                random.shuffle(ids)
            elif order in ORDER_SQL and ids:
                ids = _sorted_by_sql(col, ids, ORDER_SQL[order])
            self.card_ids = ids
        else:
            self.card_ids = []
        return {"query": query, "order": order, "total": len(self.card_ids)}

    def ensure_scope(self, col: Any, cfg: dict, query: str) -> None:
        """The scope the page is reading, resolved if it is not the current one.

        The page says which scope a request belongs to, so the answer does not
        depend on the server having been asked in the right order. The order
        is part of the scope: a random deal is dealt again only when the
        scope changes, so the sequence stays put while it is being read.
        """
        if query.strip() != self.query or cfg["order"] != self.order:
            self.set_scope(col, query, cfg["include_hidden"], cfg["order"])

    def page(
        self, col: Any, cfg: dict, query: str, offset: int, limit: int, have_css: set[str]
    ) -> dict:
        self.ensure_scope(col, cfg, query)
        renderer = self.renderer(col, cfg["apply_gui_hooks"])
        total = len(self.card_ids)
        cards = []
        errors = []
        for card_id in self.card_ids[offset : offset + limit]:
            try:
                rendered = self.render(col, cfg, card_id)
            except Exception as exc:  # one broken card must not empty the page
                errors.append(f"card {card_id}: {exc}")
                continue
            cards.append(_card_payload(rendered))
        css = {
            str(ntid): text
            for ntid, text in renderer.notetype_css.items()
            if str(ntid) not in have_css
        }
        return {"cards": cards, "css": css, "offset": offset, "total": total, "errors": errors}

    def ids(self, col: Any, cfg: dict, query: str, offset: int, count: int) -> list[int]:
        self.ensure_scope(col, cfg, query)
        return self.card_ids[offset : offset + count]

    def render(self, col: Any, cfg: dict, card_id: int) -> RenderedCard:
        rendered = self.rendered.get(card_id)
        if rendered is None:
            renderer = self.renderer(col, cfg["apply_gui_hooks"])
            rendered = renderer.render(col.get_card(card_id))
            self.rendered[card_id] = rendered
        return rendered

    def tree(self, col: Any) -> dict:
        decks = sorted(
            (entry.name.replace("\x1f", "::") for entry in col.decks.all_names_and_ids()),
            key=str.lower,
        )
        notetypes = sorted((entry.name for entry in col.models.all_names_and_ids()), key=str.lower)
        return {"decks": decks, "tags": sorted(col.tags.all(), key=str.lower), "notetypes": notetypes}


# The SQL behind each order, over Anki's cards (c) and notes (n) tables.
# Learning order: cards in learning first (queue 1 and 3), then reviews by
# due day, then new cards by position -- within each group, c.due is the
# right key, though it means a timestamp, a day and a position in turn.
ORDER_SQL = {
    "learning": "case when c.queue in (1, 3) then 0 when c.queue = 2 then 1 else 2 end, c.due, c.id",
    "created": "n.id, c.ord",
}


def _sorted_by_sql(col: Any, ids: list[int], order: str) -> list[int]:
    """The ids in the order the SQL gives them.

    find_cards takes an order clause too, but flips it when the browser's sort
    is set to descending; a query of our own is not subject to that.
    """
    found: list[int] = []
    # One literal list per chunk; a scope of ten thousand cards is a few
    # statements, and none of them is long enough to trouble SQLite.
    for start in range(0, len(ids), 5000):
        chunk = ",".join(str(i) for i in ids[start : start + 5000])
        found.extend(
            col.db.list(
                f"select c.id from cards c join notes n on c.nid = n.id "
                f"where c.id in ({chunk}) order by {order}"
            )
        )
    if len(ids) <= 5000:
        return found
    # Chunks were sorted on their own; merge them by sorting once more over
    # the positions each chunk assigned.
    rank = {card_id: position for position, card_id in enumerate(found)}
    return sorted(ids, key=lambda i: rank.get(i, len(found)))


def _card_payload(card: RenderedCard) -> dict:
    payload = {
        "id": card.card_id,
        "ntid": str(card.notetype_id),
        "bodyClass": card.body_class,
        "q": card.question,
        "a": card.answer,
        "mathjax": card.mathjax,
        "jquery": card.jquery,
        "frontRedundant": card.front_redundant,
    }
    if card.answer_only != card.answer:
        payload["aOnly"] = card.answer_only
    return payload
