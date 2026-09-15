"""What the presentation plays: a fact per card, out of the note's fields.

A cloze note gives the text with every deletion resolved, the deletion this
card asks for as the key -- blanked, then revealed -- and the text with the
other deletions shown and this one blanked. A basic note gives its first
field as the question and its second as the answer. Both come out as plain
text: the presentation sets its own type, at its own size, and a template's
markup has no place in it. The bookkeeping some shared decks leave in a
field -- ids, AMBOSS and AnkiHub tokens -- is trimmed off the end.
"""

from __future__ import annotations

import html as html_mod
import random
import re
from typing import Any

from ..export import without_hidden

_CLOZE_RE = re.compile(r"\{\{c(\d+)::(.+?)(?:::(.+?))?\}\}", re.DOTALL)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_MULTI_SPACE_RE = re.compile(r"\s+")
_TRAILING_JUNK_RE = re.compile(
    r"[\s,;|]+"
    r"(?:"
    r"(?:#?\d{5}[\d\s,./\-]*)"
    r"|(?:[a-f0-9\-]{12,})"
    r"|(?:ID:?\s*\S+)"
    r")"
    r"\s*$",
    re.IGNORECASE
)


def strip_html(text: str) -> str:
    text = _HTML_TAG_RE.sub(" ", text)
    text = html_mod.unescape(text)
    text = re.sub(r"\[(?:sound|image):[^\]]*\]", "", text)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    text = _MULTI_SPACE_RE.sub(" ", text)
    return text.strip()


def clean_extracted_text(text: str) -> str:
    text = re.sub(r"(?:AMBOSS|ankihub|AnkiHub)\S*", "", text, flags=re.IGNORECASE)
    text = text.strip()
    for _ in range(3):
        cleaned = _TRAILING_JUNK_RE.sub("", text).strip()
        if cleaned == text or len(cleaned) < 10:
            break
        text = cleaned
    text = re.sub(r"[,;|]+\s*$", "", text).strip()
    return text


def extract_cloze_items(note_fields: dict, card_ord: int) -> list:
    cloze_parts = []
    for field_name, field_val in note_fields.items():
        if _CLOZE_RE.search(field_val):
            cloze_parts.append(field_val)

    if not cloze_parts:
        return []

    raw = " ".join(cloze_parts)
    target_num = card_ord + 1

    matches = list(_CLOZE_RE.finditer(raw))
    if not matches:
        return []

    has_target = any(int(m.group(1)) == target_num for m in matches)
    if not has_target:
        return []

    sorted_matches = sorted(matches, key=lambda m: m.start(), reverse=True)

    full_raw = raw
    cloze_raw = raw
    keys = []

    for m in sorted_matches:
        cloze_num = int(m.group(1))
        answer = m.group(2)
        hint = m.group(3) or "..."

        full_raw = full_raw[:m.start()] + answer + full_raw[m.end():]

        if cloze_num == target_num:
            cloze_raw = cloze_raw[:m.start()] + answer + cloze_raw[m.end():]
            k = strip_html(answer).strip()
            k = clean_extracted_text(k)
            if k:
                keys.insert(0, k)
        else:
            cloze_raw = cloze_raw[:m.start()] + f"[{hint}]" + cloze_raw[m.end():]

    full_text = clean_extracted_text(strip_html(full_raw))
    cloze_text = clean_extracted_text(strip_html(cloze_raw))

    if not keys or not full_text:
        return []

    combined_key = keys[0] if len(keys) == 1 else " / ".join(keys)

    return [{
        "text": full_text,
        "key": combined_key,
        "cloze": cloze_text,
    }]


def extract_basic_items(note_fields: dict) -> list:
    fields = list(note_fields.values())
    if len(fields) < 2:
        return []

    front = clean_extracted_text(strip_html(fields[0]))
    back = clean_extracted_text(strip_html(fields[1]))

    if not front or not back:
        return []

    key = front
    if len(key) > 60:
        parts = re.split(r"[?:.\n]", key)
        key = parts[-1].strip() if parts[-1].strip() else parts[0].strip()
        if len(key) > 60:
            key = key[:57] + "..."

    return [{"text": back, "key": key, "front": front, "back": back, "cloze": None}]


def items_from_cards(col: Any, card_ids: list) -> list:
    """The items for these cards, one per card, the same fact never twice.

    Two cards of one cloze note share their text but not their key -- the
    deletion each asks for -- and both are wanted.
    """
    items = []
    seen: set = set()
    for card_id in card_ids:
        try:
            card = col.get_card(card_id)
            note = card.note()
            model = card.note_type()
        except Exception:
            continue
        if not note or not model:
            continue
        names = [f["name"] for f in model["flds"]]
        fields = {name: note.fields[i] for i, name in enumerate(names) if i < len(note.fields)}
        new_items = extract_cloze_items(fields, card.ord) if model.get("type", 0) == 1 else extract_basic_items(fields)
        for item in new_items:
            key = (item["text"].lower().strip(), item["key"].lower().strip())
            if key in seen:
                continue
            seen.add(key)
            item["cid"] = int(card_id)
            items.append(item)
    return items


# What the source choice adds to the search: the queue Anki keeps the card
# in, the leech tag or a lapse count, or a failed review in the last day or
# week.
SOURCES = {
    "due": "is:due",
    "new": "is:new",
    "all": "",
    "leeches": "(tag:leech OR prop:lapses>=8)",
    "failed_24h": "rated:1:1",
    "failed_7d": "rated:7:1",
}


def find_items(col: Any, query: str, source: str, max_cards: int, include_hidden: bool = False) -> list:
    """Up to max_cards items from the scope, dealt at random."""
    parts = [f"({query.strip()})" if query.strip() else "", SOURCES.get(source, "")]
    search = " ".join(p for p in parts if p)
    if not include_hidden:
        search = without_hidden(search)
    ids = list(col.find_cards(search)) if search.strip() else []
    random.shuffle(ids)
    # More than asked for are looked at: a note with no usable text is skipped.
    return items_from_cards(col, ids[: max_cards * 3])[:max_cards]
