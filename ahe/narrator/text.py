"""Turning a card into the words the model is given.

The rendered sides carry the note type's markup and scripts, and a cloze note
renders every card from the same fields -- so the model gets the card stripped
to what could be read out: both sides as plain text, and the fields by name.
"""

from __future__ import annotations

import hashlib
import re

from ..renderer import RenderedCard, _plain_text

# Field names whose content is bookkeeping rather than knowledge. Matched
# case-insensitively as a whole name.
SKIP_FIELDS = {
    "id", "uuid", "guid", "note id", "ankihub_id", "source", "sources", "quelle", "quellen",
    "one by one", "lecture notes", "date stamp", "datum", "amboss-link", "amboss link",
}
MAX_FIELD_CHARS = 3000

# Shared decks (Ankizin, Ankiphil, AnKing) print {{Tags}} into the card, in
# a panel the reviewer keeps folded: an element named after the tags, and in
# it the hierarchical tags themselves. Neither is for the listener.
TAG_BLOCK_RE = re.compile(
    r'(?is)<(div|span)\b[^>]*\b(?:id|class)="[^"]*\btags?[-_ ]?(?:container|list|hint|box)?\b[^"]*"[^>]*>'
    r"(?:(?!</\1>).)*</\1>"
)
TAG_TOKEN_RE = re.compile(r"(?<!\S)#\S*::\S*")
# Controls the template draws (hint buttons, "Toggle All"), and the note ids
# some templates print next to them.
BUTTON_RE = re.compile(r"(?is)<button\b[^>]*>.*?</button>")
NOTE_ID_RE = re.compile(r"(?<!\d)\d{13}(?!\d)")
# What is left of a template's header once its buttons are gone: a few short
# labels between bars ("| AMBOSS |"), and the invisible spaces around them.
HEADER_RE = re.compile(r"^(?:[^|\n]{0,16}\|\s*)+")
INVISIBLE_RE = re.compile(r"[\u200b\u200c\u200d\ufeff]")
CLOZE_RE = re.compile(r"\{\{c\d+::(.*?)(?:::[^}]*?)?\}\}", re.S)


def _spoken_side(html_text: str) -> str:
    text = _plain_text(BUTTON_RE.sub(" ", TAG_BLOCK_RE.sub(" ", html_text)))
    text = INVISIBLE_RE.sub("", NOTE_ID_RE.sub("", TAG_TOKEN_RE.sub("", text)))
    text = re.sub(r"\s+", " ", text).strip()
    return HEADER_RE.sub("", text).strip()


def _field_text(text: str) -> str:
    text = CLOZE_RE.sub(r"\1", text)
    return re.sub(r"\s+", " ", TAG_TOKEN_RE.sub("", text)).strip()[:MAX_FIELD_CHARS]


def card_text(card: RenderedCard, seen: dict | None = None) -> dict:
    """The card as the model should see it, plus a hash to cache by.

    ``seen`` is what the vision model found on an image occlusion card: the
    label under the mask and where the structure sits. It goes into the
    prompt -- and so into the hash, so each card of the note gets its own
    script.
    """
    question = _spoken_side(card.question)
    answer = _spoken_side(card.answer_only or card.answer)
    fields = [
        (f["name"], _field_text(f["text"]))
        for f in card.fields
        if f["name"].strip().lower() not in SKIP_FIELDS and f["text"].strip()
    ]
    fields = [(name, text) for name, text in fields if text]

    # The deck name is the one piece of bookkeeping that helps: it names the
    # subject. Tags and note type are the collection's own filing and, in a
    # shared deck, can run to a kilobyte of paths per card.
    lines = []
    if card.deck:
        lines.append(f"Deck: {card.deck}")
    lines.append("")
    lines.append("FRONT: " + (question or "(empty)"))
    lines.append("BACK: " + (answer or "(empty)"))
    if fields:
        lines.append("")
        lines.append("FIELDS:")
        for name, text in fields:
            lines.append(f"- {name}: {text}")
    if seen:
        lines.append("")
        lines.append("IMAGE OCCLUSION -- what the mask on this card hides, read off the picture:")
        lines.append(f"HIDDEN LABEL: {seen.get('label') or '(no readable label)'}")
        if seen.get("context"):
            lines.append(f"WHERE AND WHAT: {seen['context']}")
        if seen.get("others"):
            lines.append(f"OTHER LABELS IN THE FIGURE: {seen['others']}")

    prompt = "\n".join(lines)
    digest = hashlib.sha1(prompt.encode("utf-8")).hexdigest()[:16]
    # How much the card says, in words: the side the reader sees, plus the
    # question when it is not repeated there. The narration's natural length
    # follows from this, not from the clock.
    content = answer if card.front_redundant or not question else question + " " + answer
    if not content.strip():
        content = " ".join(text for _, text in fields)
    if seen:
        content += " " + " ".join(str(seen.get(k) or "") for k in ("label", "context"))
    return {
        "prompt": prompt, "hash": digest, "empty": not (question or answer or fields or seen),
        "content_words": len(content.split()),
    }

