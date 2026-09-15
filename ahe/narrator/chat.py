"""Talking about the card on screen.

The listener asks -- by voice, transcribed, or typed -- and a model answers
from the card: what it says, why, how it connects. The answer is read aloud
by the narration's voice. One conversation per card, kept for the session,
so a card can be returned to.
"""

from __future__ import annotations

import threading
from typing import Any

from ..renderer import RenderedCard
from .narrate import Narrator, _clean, split_language, spoken
from .text import card_text

# Turns kept per card; older ones fall off the front
MAX_TURNS = 20

TUTOR_PROMPT = """You are a tutor talking with a learner about one flashcard, which they can see and have just heard narrated. Your answers are spoken aloud to them.

Rules:
- Answer the question asked, from the card first. You may add what a good tutor would add -- the why, the mechanism, an example, how it connects -- but keep the card's content as the ground and say when you go beyond it. Do not invent facts.
- Speak the language the learner speaks, unless told otherwise.
- Short: two to five sentences, as much as the question needs. Plain spoken prose: no headings, no bullet points, no markdown, no lists read out as lists.
- Write for the ear: expand abbreviations, keep formulas sayable.
- Do not describe the card's layout, and do not repeat the narration back."""


class Tutor:
    def __init__(self, narrator: Narrator) -> None:
        self.narrator = narrator
        self._lock = threading.Lock()
        self._talks: dict[int, list[dict[str, str]]] = {}

    def history(self, card_id: int) -> list[dict[str, str]]:
        with self._lock:
            return list(self._talks.get(card_id, []))

    def forget(self, card_id: int | None = None) -> None:
        with self._lock:
            if card_id is None:
                self._talks.clear()
            else:
                self._talks.pop(card_id, None)

    def ask(
        self, card: RenderedCard, narration: str, message: str, config: dict, voice: str
    ) -> dict[str, Any]:
        """The learner's message answered: text, spoken, and the talk so far."""
        message = message.strip()
        if not message:
            raise ValueError("nothing was said")
        content = card_text(card, self.narrator._seen_cached(card, config))
        system = TUTOR_PROMPT
        if config.get("language"):
            system += f"\nAnswer in {config['language']}."
        if config.get("style"):
            system += f"\nStyle: {config['style']}"
        system += "\n\nCARD:\n" + content["prompt"]
        if narration:
            system += "\n\nWHAT THE NARRATION SAID:\n" + spoken(narration)

        with self._lock:
            talk = self._talks.setdefault(card.card_id, [])
            turns = list(talk)
        messages = [{"role": "system", "content": system}] + turns + [{"role": "user", "content": message}]

        self.narrator._check_budget(config)
        answer, tokens_in, tokens_out = self.narrator.client(config).chat(config["text_model"], messages)
        self.narrator.ledger.add_chat(config.get("prices", {}), config["text_model"], tokens_in, tokens_out)
        # Not asked for here, but a model used to the narration's format may
        # add the language line all the same; it is not to be spoken.
        language, answer = split_language(answer.strip())
        answer = _clean(answer)

        with self._lock:
            talk = self._talks.setdefault(card.card_id, [])
            talk.append({"role": "user", "content": message})
            talk.append({"role": "assistant", "content": answer})
            del talk[:-MAX_TURNS]
            history = list(talk)

        audio, duration, language = self.narrator.speak_text(answer, config, voice, language)
        return {"text": answer, "audio": audio, "duration": duration, "language": language, "history": history}
