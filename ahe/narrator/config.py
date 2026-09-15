"""The narrator's settings: the ``narrator`` entry of the add-on's config.

The OpenAI key is stored there too. Anki keeps add-on config in the
collection folder's ``addons21/<package>/meta.json``, readable by anyone with
the account, which is where the key would otherwise be typed into the
add-on's config dialog anyway. The environment overrides it: a shell that
already has ``OPENAI_API_KEY`` set needs nothing written down.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .. import config as addon_config

# OpenAI's voices. cedar, echo, onyx and ash are the male ones; cedar and
# marin are the two OpenAI recommends for gpt-4o-mini-tts.
VOICES = ["cedar", "echo", "onyx", "ash", "marin", "alloy", "ballad", "coral", "fable", "nova", "sage", "shimmer", "verse"]
# Seconds per card the page offers. The narration is planned for these; how
# long the audio actually comes out is measured afterwards.
DURATIONS = [10, 20, 30, 45, 60, 90, 120]

# How the cards of a scope are put in order. "browser" is the sort the user
# set in Anki's own browser; "learning" is the order the reviewer would
# present them in -- cards in learning first, then reviews by due date, then
# new cards by position.
ORDERS = {
    "browser": "Browser order",
    "learning": "Learning order",
    "created": "Creation order",
    "random": "Random",
}

DEFAULTS: dict[str, Any] = {
    "openai_api_key": "",
    "order": "browser",
    "text_model": "gpt-5.4",
    "tts_model": "gpt-4o-mini-tts",
    # Speech to text, for talking about a card
    "stt_model": "gpt-4o-mini-transcribe",
    "voice": "cedar",
    # Planned seconds of narration per card
    "seconds": 30,
    # A pause after each card before the next one, in seconds
    "gap": 1.0,
    # The card comes up with its front, as in Anki; the back is revealed
    # "auto", this many seconds into the narration, or on "wait" only on a
    # key or a click. The narration itself tells the whole card either way.
    "reveal": "auto",
    "reveal_gap": 2.0,
    # Free text the model is told about how to narrate ("like a lecturer",
    # "for a child"). Empty means the built-in style.
    "style": "",
    # Spoken language; empty means the language of the card
    "language": "",
    # Also narrate suspended and buried cards
    "include_hidden": False,
    # On an image occlusion card, let the text model see the picture: it
    # reads the label under the mask and the narration is about that.
    "vision": True,
    # Playback speed of the narration, on the page and in the exports. The
    # audio is stretched with its pitch kept; the voice is never asked to
    # hurry, which it does badly.
    "speed": 1.0,
    # Stop spending once today's narrations have cost this much, in US
    # dollars; 0 means no limit.
    "daily_budget": 0.0,
    # Words the voice gets wrong, and what to hand it instead: {"Aα": "A-Alpha"}.
    # Applied to what is spoken, not to what is shown.
    "pronunciation": {},
    # Also answer on the local network, so a phone can open the page (the key
    # in the address is still required)
    "share": False,
    # Run gui_hooks.card_will_show, the hook the reviewer uses, so other
    # add-ons can post-process the cards. Off: those hooks are written for
    # the reviewer and may inject controls that only work inside Anki.
    "apply_gui_hooks": False,
    "last_search": "",
    # The ffmpeg the exports run; a name on PATH or a full path
    "ffmpeg": "ffmpeg",
    # What OpenAI charges, in US dollars: chat models per million tokens in
    # and out, speech per minute of audio (gpt-4o-mini-tts: $0.60 per million
    # characters in plus $12 per million audio tokens out, about 1.5 cents a
    # minute) or per million characters (tts-1). A model not listed is
    # matched by prefix; the usage display says when nothing matched.
    "prices": {
        "gpt-5.4": {"input": 2.50, "output": 15.00},
        "gpt-5.4-mini": {"input": 0.75, "output": 4.50},
        "gpt-5": {"input": 1.25, "output": 10.00},
        "gpt-5-mini": {"input": 0.25, "output": 2.00},
        "gpt-4.1": {"input": 2.00, "output": 8.00},
        "gpt-4o": {"input": 2.50, "output": 10.00},
        "gpt-4o-mini": {"input": 0.15, "output": 0.60},
        "gpt-4o-mini-tts": {"per_minute": 0.015},
        "gpt-4o-mini-transcribe": {"per_minute": 0.003},
        "gpt-4o-transcribe": {"per_minute": 0.006},
        "whisper-1": {"per_minute": 0.006},
        "tts-1": {"per_1m_chars": 15.00},
        "tts-1-hd": {"per_1m_chars": 30.00},
    },
}

# Only these may be changed from the page.
PAGE_KEYS = {
    "openai_api_key", "order", "text_model", "tts_model", "stt_model", "voice", "seconds", "gap",
    "style", "language", "include_hidden", "vision", "last_search", "speed", "daily_budget",
    "pronunciation", "share", "reveal", "reveal_gap",
}


def get_config() -> dict[str, Any]:
    """The narrator's settings, defaults filled in, the key from the environment if set."""
    return with_defaults(addon_config.get_config().get("narrator"))


def with_defaults(stored: Any) -> dict[str, Any]:
    config = dict(DEFAULTS)
    if isinstance(stored, dict):
        config.update({k: v for k, v in stored.items() if k in DEFAULTS and k != "prices"})
        # The price list is merged, so a new default reaches a stored config.
        if isinstance(stored.get("prices"), dict):
            config["prices"] = {**DEFAULTS["prices"], **stored["prices"]}
    if os.environ.get("OPENAI_API_KEY"):
        config["openai_api_key"] = os.environ["OPENAI_API_KEY"]
    return config


def save_config(config: dict[str, Any]) -> None:
    whole = addon_config.get_config()
    whole["narrator"] = {k: v for k, v in config.items() if k in DEFAULTS}
    addon_config.save_config(whole)


def apply_changes(config: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    """The config with the page's changes in, typed like the defaults."""
    updated = dict(config)
    for key, value in changes.items():
        if key not in PAGE_KEYS or value is None:
            continue
        # The page never shows the key it has, so an empty field means "keep".
        if key == "openai_api_key" and not str(value).strip():
            continue
        default = DEFAULTS[key]
        if isinstance(default, dict):
            if not isinstance(value, dict):
                continue
            if key == "pronunciation":
                value = {str(k).strip(): str(v).strip() for k, v in value.items() if str(k).strip()}
        elif isinstance(default, bool):
            value = bool(value)
        elif isinstance(default, int):
            value = int(value)
        elif isinstance(default, float):
            value = float(value)
        else:
            value = str(value).strip()
        if key == "order" and value not in ORDERS:
            continue
        if key == "speed":
            value = min(3.0, max(0.5, value))
        if key == "daily_budget":
            value = max(0.0, value)
        if key == "reveal" and value not in ("auto", "wait"):
            continue
        if key == "reveal_gap":
            value = min(10.0, max(0.0, value))
        updated[key] = value
    return updated


def public(config: dict[str, Any]) -> dict[str, Any]:
    """The settings as the page may see them: the key only as a fact."""
    data = {k: v for k, v in config.items() if k not in ("openai_api_key", "prices")}
    key = config.get("openai_api_key", "")
    data["has_key"] = bool(key)
    data["key_hint"] = ("…" + key[-4:]) if len(key) >= 8 else ""
    data["voices"] = VOICES
    data["durations"] = DURATIONS
    data["orders"] = ORDERS
    return data


def user_files_dir() -> Path:
    """Where the cache lives: the folder Anki keeps across add-on updates."""
    override = os.environ.get("NARRATOR_DATA")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent.parent / "user_files"
