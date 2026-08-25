"""Add-on configuration, stored through Anki's own config mechanism."""

from __future__ import annotations

from typing import Any

ADDON_PACKAGE = __name__.split(".")[0]

# Deck, note type, card template and the card id already sit in every card's
# header row, so repeating them in the details strip only adds noise.
DEFAULT_EXCLUDED_SPECIAL = ["Deck", "Subdeck", "Note type", "Card", "Card ID"]

DEFAULTS: dict[str, Any] = {
    "output_dir": "",
    "order": "deck",
    "single_file": False,
    "embed_mathjax": True,
    "download_remote_media": False,
    "apply_gui_hooks": False,
    "view_mode": "auto",
    "interactive": True,
    "study": False,
    "show_meta": False,
    "show_special": False,
    "show_fields": False,
    "dark": False,
    "excluded_fields": [],
    "excluded_special": list(DEFAULT_EXCLUDED_SPECIAL),
    "last_search": "",
}


def get_config() -> dict[str, Any]:
    config = dict(DEFAULTS)
    try:
        import aqt

        stored = aqt.mw.addonManager.getConfig(ADDON_PACKAGE)
    except Exception:
        stored = None
    if isinstance(stored, dict):
        config.update({k: v for k, v in stored.items() if k in DEFAULTS})
    return config


def save_config(config: dict[str, Any]) -> None:
    try:
        import aqt

        aqt.mw.addonManager.writeConfig(ADDON_PACKAGE, config)
    except Exception:
        pass
