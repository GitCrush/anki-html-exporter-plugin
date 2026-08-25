#!/usr/bin/env python3
"""Building the export dialog outside Anki, to measure it and look at it.

A Qt dialog's size is decided by the widest thing in it, and which thing that
is cannot be reasoned out from the source with any confidence -- a group box
title that cannot wrap, two list widgets side by side, a checkbox label that
carries its own explanation. The way to know is to build it and ask.

Anki is not needed for that. The dialog wants ``aqt`` for the Qt names it
re-exports, a handful of utility functions and a main window to be a child of;
all of that is stood up here, with a collection stub that answers the few
questions the dialog asks while it is being built. Qt itself runs offscreen.

    python3 tests/dialog_probe.py                 measure, and write PNGs to /tmp
    python3 tests/dialog_probe.py /tmp/before     ... under that name
    python3 tests/dialog_probe.py /tmp/x 900 700  ... at that size

Prints the default size, the narrowest the window can be made, and what each
tab needs on its own -- the largest of those is what actually sets the floor.
"""

from __future__ import annotations

import os
import re
import sys
import types
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Anki: two names the renderer imports and never reaches on this path.
_anki = types.ModuleType("anki")
_sound = types.ModuleType("anki.sound")
_sound.AV_REF_RE = re.compile(r"\[anki:(?P<param>[^\]]+)\]")


class _SoundOrVideoTag:
    pass


_sound.SoundOrVideoTag = _SoundOrVideoTag
sys.modules.setdefault("anki", _anki)
sys.modules.setdefault("anki.sound", _sound)

# aqt.qt is Anki's own re-export of PyQt6, so re-export it the same way.
_qt = types.ModuleType("aqt.qt")
for _module in ("QtWidgets", "QtCore", "QtGui"):
    _imported = __import__("PyQt6." + _module, fromlist=["*"])
    for _name in dir(_imported):
        if not _name.startswith("_"):
            setattr(_qt, _name, getattr(_imported, _name))


class _Entry:
    def __init__(self, name: str, ident: int = 1) -> None:
        self.name, self.id = name, ident


class _Decks:
    def all_names_and_ids(self):
        return [
            _Entry(name)
            for name in (
                "Standard",
                "Ankiphil - Vorklinik::Physiologie",
                "Ankiphil - Vorklinik::Anatomie::Histologie",
            )
        ]


class _Tags:
    def all(self):
        return ["##Ankiphil_Vorklinik_v5.1", "marked", "leech"]


class _Models:
    def all_names_and_ids(self):
        return [_Entry("Basic", 1), _Entry("Cloze", 2)]

    def get(self, _mid):
        return {"name": "Basic", "flds": [{"name": "Vorderseite"}, {"name": "Rückseite"}]}


class _Col:
    decks, tags, models = _Decks(), _Tags(), _Models()

    def find_cards(self, *_args, **_kwargs):
        return list(range(1434))


class _AddonManager:
    def getConfig(self, _package):  # noqa: N802 (Anki's name)
        return None

    def writeConfig(self, *_args):  # noqa: N802
        pass


class _Profile:
    name = "User 1"


_aqt = types.ModuleType("aqt")
_aqt.mw = None  # a real QWidget, once there is a QApplication
_aqt.qt = _qt

_hooks = types.ModuleType("aqt.gui_hooks")
for _hook in ("profile_will_close", "browser_will_show_context_menu", "browser_menus_did_init"):
    setattr(_hooks, _hook, [])
_aqt.gui_hooks = _hooks

_utils = types.ModuleType("aqt.utils")
_utils.openFolder = _utils.showInfo = _utils.showWarning = lambda *a, **k: None
_utils.tooltip = lambda *a, **k: None

_operations = types.ModuleType("aqt.operations")


class _QueryOp:
    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def with_progress(self, *_args, **_kwargs):
        return self

    def run_in_background(self) -> None:
        pass


_operations.QueryOp = _QueryOp

for _name, _module in (
    ("aqt", _aqt),
    ("aqt.qt", _qt),
    ("aqt.utils", _utils),
    ("aqt.operations", _operations),
    ("aqt.gui_hooks", _hooks),
):
    sys.modules[_name] = _module


def main() -> int:
    from PyQt6.QtWidgets import QApplication, QWidget

    app = QApplication([])

    class _MainWindow(QWidget):
        col, pm, addonManager = _Col(), _Profile(), _AddonManager()

    _aqt.mw = _MainWindow()

    from ahe.dialog import ExportDialog

    dialog = ExportDialog()
    dialog.show()
    app.processEvents()

    print(f"  default size    {dialog.width()} x {dialog.height()}")
    print(f"  narrowest       {dialog.minimumSizeHint().width()} px")
    for index in range(dialog.tabs.count()):
        page = dialog.tabs.widget(index)
        name = dialog.tabs.tabText(index)
        print(f"    {name:<10} needs {page.minimumSizeHint().width()} px")

    stem = sys.argv[1] if len(sys.argv) > 1 else "/tmp/ahe-dialog"
    if len(sys.argv) > 3:
        dialog.resize(int(sys.argv[2]), int(sys.argv[3]))
    for index in range(dialog.tabs.count()):
        dialog.tabs.setCurrentIndex(index)
        app.processEvents()
        target = f"{stem}-{dialog.tabs.tabText(index).lower()}.png"
        dialog.grab().save(target)
        print(f"  wrote {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
