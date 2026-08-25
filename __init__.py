"""Anki HTML Exporter.

Exports cards to a browsable HTML page in which every card is rendered by
Anki's own template engine and styled by its own reviewer stylesheet, so the
result matches what the reviewer shows. The same dialog also serves that page
live out of the running collection, without writing anything.

This file only wires up the menu entries; see the ``ahe`` package for the
implementation.
"""

from aqt import gui_hooks, mw
from aqt.qt import QAction

from .ahe.dialog import show_export_dialog


def _on_tools_menu() -> None:
    show_export_dialog()


def _on_browser_context_menu(browser, menu) -> None:
    action = menu.addAction("Export to HTML…")
    action.triggered.connect(lambda: _export_browser_selection(browser))


def _on_browser_menus_did_init(browser) -> None:
    action = QAction("Export to HTML…", browser)
    action.triggered.connect(lambda: _export_browser_selection(browser))
    browser.form.menu_Notes.addAction(action)


def _export_browser_selection(browser) -> None:
    card_ids = browser.selected_cards()
    show_export_dialog(card_ids=card_ids or None, parent=browser)


def _add_tools_entry() -> None:
    action = QAction("Export to HTML…", mw)
    action.triggered.connect(_on_tools_menu)
    mw.form.menuTools.addAction(action)


_add_tools_entry()
gui_hooks.browser_will_show_context_menu.append(_on_browser_context_menu)
gui_hooks.browser_menus_did_init.append(_on_browser_menus_did_init)
