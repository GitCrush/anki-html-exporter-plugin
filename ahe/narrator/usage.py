"""What the narrations have cost: tokens, characters, minutes -- and money.

OpenAI reports token counts for the chat call; the speech endpoint reports
nothing, so speech is counted by the characters sent and the minutes of audio
that came back. Prices live in the config so they can follow OpenAI's; the
figure shown is an estimate, not a bill.

Three counts are kept: this session (since the server started), today, and
all time. The last two persist in ``user_files/usage.json``.
"""

from __future__ import annotations

import datetime
import json
import threading
from pathlib import Path
from typing import Any

COUNTERS = ["narrations", "input_tokens", "output_tokens", "speech_chars", "speech_seconds",
            "listened_seconds", "cost"]


def _empty() -> dict[str, float]:
    return {key: 0 for key in COUNTERS}


def _price(prices: dict, model: str) -> dict:
    """The price entry for a model: exact, else the longest prefix listed."""
    if model in prices:
        return prices[model]
    best = ""
    for key in prices:
        if model.startswith(key) and len(key) > len(best):
            best = key
    return prices.get(best, {})


def chat_cost(prices: dict, model: str, input_tokens: int, output_tokens: int) -> float | None:
    entry = _price(prices, model)
    if "input" not in entry or "output" not in entry:
        return None
    return (input_tokens * entry["input"] + output_tokens * entry["output"]) / 1_000_000


def speech_cost(prices: dict, model: str, chars: int, seconds: float) -> float | None:
    entry = _price(prices, model)
    if "per_minute" in entry:
        return seconds / 60 * entry["per_minute"] + chars * entry.get("per_1m_chars", 0) / 1_000_000
    if "per_1m_chars" in entry:
        return chars * entry["per_1m_chars"] / 1_000_000
    return None


class Ledger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._session = _empty()
        self._unpriced: set[str] = set()
        self._data: dict[str, Any] = {"total": _empty(), "days": {}}
        try:
            stored = json.loads(path.read_text("utf-8"))
            if isinstance(stored, dict):
                self._data["total"].update(stored.get("total", {}))
                self._data["days"] = dict(stored.get("days", {}))
        except (OSError, ValueError):
            pass

    def _today(self) -> dict[str, float]:
        day = datetime.date.today().isoformat()
        days = self._data["days"]
        if day not in days:
            # Only the last few weeks are worth keeping by day.
            for old in sorted(days)[:-30]:
                del days[old]
            days[day] = _empty()
        return days[day]

    def add(self, counts: dict[str, float], cost: float | None, model: str) -> None:
        with self._lock:
            if cost is None:
                self._unpriced.add(model)
            for bucket in (self._session, self._today(), self._data["total"]):
                for key, value in counts.items():
                    bucket[key] = bucket.get(key, 0) + value
                if cost is not None:
                    bucket["cost"] = bucket.get("cost", 0) + cost
            self._save()

    def add_chat(self, prices: dict, model: str, input_tokens: int, output_tokens: int) -> None:
        self.add(
            {"input_tokens": input_tokens, "output_tokens": output_tokens},
            chat_cost(prices, model, input_tokens, output_tokens), model,
        )

    def add_speech(self, prices: dict, model: str, chars: int, seconds: float) -> None:
        self.add(
            {"narrations": 1, "speech_chars": chars, "speech_seconds": seconds},
            speech_cost(prices, model, chars, seconds), model,
        )

    def add_transcription(self, prices: dict, model: str, seconds: float) -> None:
        """A recording understood: counted by its length, priced per minute."""
        self.add({"listened_seconds": seconds}, speech_cost(prices, model, 0, seconds), model)

    def reset(self) -> None:
        with self._lock:
            self._session = _empty()
            self._data = {"total": _empty(), "days": {}}
            self._save()

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "session": dict(self._session),
                "today": dict(self._today()),
                "total": dict(self._data["total"]),
                "unpriced": sorted(self._unpriced),
            }

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._data, indent=1), "utf-8")
        except OSError:
            pass
