"""The export dialog."""

from __future__ import annotations

import os
import re
from datetime import date
from pathlib import Path
from typing import Any, Sequence

import aqt
from aqt import mw
from aqt.operations import QueryOp
from aqt.qt import *
from aqt.utils import openFolder, showInfo, showWarning, tooltip

from . import __version__
from . import config as addon_config
from . import live
from .hypnagog import service as hypnagog_service
from .narrator import service as narrator_service
from . import qr
from .export import ORDERS, ExportAborted, ExportRequest, build_search, run_export
from .renderer import FLAG_NAMES, SPECIAL_NAMES
from .tag_input_widget import TagInputWidget
from .writer import ExportOptions

ALL_DECKS = "— all decks —"

VIEW_MODES = [
    ("auto", "Auto — answer side, plus the question when the sides differ"),
    ("qa", "Question + answer, separately"),
    ("a", "Answer only, without the repeated question"),
    ("q", "Question only"),
]

SPECIAL_FIELD_NAMES = SPECIAL_NAMES


class ExportDialog(QDialog):
    def __init__(self, card_ids: Sequence[int] | None = None, parent=None) -> None:
        super().__init__(parent or mw, Qt.WindowType.Window)
        self.card_ids = list(card_ids) if card_ids else None
        self.config = addon_config.get_config()
        self._field_names: list[str] = []
        self._field_list_populated = False

        self.setWindowTitle(f"Export to HTML — v{__version__}")
        self.resize(700, 640)
        self.setSizeGripEnabled(True)

        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._selection_tab(), "Selection")
        self.tabs.addTab(self._content_tab(), "Content")
        self.tabs.addTab(self._output_tab(), "Output")
        layout.addWidget(self.tabs)

        self.count_label = QLabel("Matching cards: –")
        layout.addWidget(self.count_label)

        self.live_box = self._live_box()
        layout.addWidget(self.live_box)

        buttons = QDialogButtonBox()
        self.export_button = buttons.addButton(
            "Export", QDialogButtonBox.ButtonRole.AcceptRole
        )
        self.live_button = buttons.addButton(
            "Browse live", QDialogButtonBox.ButtonRole.ActionRole
        )
        self.live_button.setToolTip(
            "Open these cards in your browser without writing anything. Decks, "
            "tags and the search stay changeable in the page."
        )
        self.live_button.clicked.connect(self._browse_live)
        self.narrator_button = buttons.addButton(
            "Narrate", QDialogButtonBox.ButtonRole.ActionRole
        )
        self.narrator_button.setToolTip(
            "Open these cards as a narrated slide show: each card read aloud by "
            "a language model's telling of it. Needs an OpenAI key, entered on "
            "the page."
        )
        self.narrator_button.clicked.connect(self._narrate)
        self.hypnagog_button = buttons.addButton(
            "Hypnagog", QDialogButtonBox.ButtonRole.ActionRole
        )
        self.hypnagog_button.setToolTip(
            "Open these cards as a rapid full-screen presentation: each fact "
            "flashed, shown with its cloze revealed piece by piece, and faded."
        )
        self.hypnagog_button.clicked.connect(self._hypnagog)
        buttons.addButton(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        self.export_button.clicked.connect(self.start_export)
        layout.addWidget(buttons)

        self._refresh_live()

        self._count_timer = QTimer(self)
        self._count_timer.setSingleShot(True)
        self._count_timer.setInterval(300)
        self._count_timer.timeout.connect(self.refresh_selection)

        self.refresh_selection()

    # Live view
    ##################################################################

    def _live_box(self) -> QGroupBox:
        """Shown only while something is being served.

        Everything the live view needs to be operated is here rather than in a
        window of its own: it is a second way of looking at the cards this
        dialog selects, not a second thing to find.
        """
        box = QGroupBox("Live view")
        outer = QVBoxLayout(box)

        self.live_address = QLineEdit()
        self.live_address.setReadOnly(True)
        row = QHBoxLayout()
        row.addWidget(self.live_address, 1)
        copy = QPushButton("Copy")
        copy.clicked.connect(self._copy_live_address)
        row.addWidget(copy)
        self.live_qr_button = QPushButton("QR")
        self.live_qr_button.setCheckable(True)
        self.live_qr_button.setToolTip("Show the address as a QR code")
        # A code for 127.0.0.1 would send the other device to itself.
        self.live_qr_button.setEnabled(False)
        self.live_qr_button.toggled.connect(self._toggle_qr)
        row.addWidget(self.live_qr_button)
        stop = QPushButton("Stop")
        stop.setToolTip("Stop serving. Tabs already open stop working.")
        stop.clicked.connect(self._stop_live)
        row.addWidget(stop)
        outer.addLayout(row)

        # Always black on white, whatever the interface theme: an inverted code
        # is not something every reader will take.
        self.live_qr = QLabel()
        self.live_qr.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.live_qr.setVisible(False)
        outer.addWidget(self.live_qr)

        self.live_qr_note = QLabel()
        self.live_qr_note.setWordWrap(True)
        self.live_qr_note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.live_qr_note.setStyleSheet("color: palette(mid);")
        self.live_qr_note.setVisible(False)
        outer.addWidget(self.live_qr_note)

        self.live_share = QCheckBox("Also reachable from this network")
        self.live_share.setToolTip(
            "Serves the page to other devices on your network. Anyone with the "
            "address can read the whole collection."
        )
        self.live_share.toggled.connect(self._toggle_live_share)
        outer.addWidget(self.live_share)

        # Which address a guest can reach depends on the network they are on,
        # so the choice is theirs rather than a guess of ours.
        self.live_share_address = QComboBox()
        self.live_share_address.setVisible(False)
        self.live_share_address.currentTextChanged.connect(lambda _t: self._refresh_qr())
        outer.addWidget(self.live_share_address)

        self.live_note = QLabel()
        self.live_note.setWordWrap(True)
        self.live_note.setStyleSheet("color: palette(mid);")
        self.live_note.setVisible(False)
        outer.addWidget(self.live_note)

        box.setVisible(False)
        return box

    def _live_options(self) -> ExportOptions:
        options = self._collect_options(None)
        profile = mw.pm.name if mw.pm else "Anki"
        options.title = f"{profile} — live"
        return options

    def _browse_live(self) -> None:
        try:
            live.start(
                self._live_options(),
                share=self.live_share.isChecked(),
                include_hidden=self.include_hidden.isChecked(),
            )
        except OSError as error:
            showWarning(f"Could not start the live view: {error}", parent=self)
            return
        self._refresh_live()
        live.open_in_browser(self._live_search())

    def _serve_page(self, opener) -> None:
        """Another page of the live server, on the cards this dialog shows."""
        try:
            if not live.is_running():
                live.start(
                    self._live_options(),
                    share=self.live_share.isChecked(),
                    include_hidden=self.include_hidden.isChecked(),
                )
        except OSError as error:
            showWarning(f"Could not start the live view: {error}", parent=self)
            return
        self._refresh_live()
        opener(self._live_search())

    def _narrate(self) -> None:
        self._serve_page(narrator_service.open_in_browser)

    def _hypnagog(self) -> None:
        self._serve_page(hypnagog_service.open_in_browser)

    def _live_search(self) -> str:
        """The scope the live view opens on.

        Cards picked in the browser are named one by one; anything else is the
        search this dialog already shows.
        """
        if self._use_browser_selection():
            ids = list(self.card_ids or [])
            return " OR ".join(f"cid:{cid}" for cid in ids) if ids else ""
        return self.current_search(plain=True)

    def _stop_live(self) -> None:
        live.stop()
        self._refresh_live()

    def _toggle_live_share(self, on: bool) -> None:
        """Switch the interface the view is served on. Nothing else.

        Not a reason to open a browser: the tab that is already open goes on
        working, because the replacement server keeps the key and the port.
        """
        if not live.is_running():
            return
        try:
            live.start(self._live_options(), share=on)
        except OSError as error:
            showWarning(
                f"Could not open the live view to the network: {error}", parent=self
            )
            self.live_share.blockSignals(True)
            self.live_share.setChecked(not on)
            self.live_share.blockSignals(False)
            return
        self._refresh_live()
        tooltip(
            "Reachable from this network" if on else "This machine only",
            parent=self,
        )

    def _live_address(self) -> str:
        """The address to hand out: the chosen network one, else this machine."""
        if self.live_share_address.isVisible() and self.live_share_address.currentText():
            return self.live_share_address.currentText()
        return self.live_address.text()

    def _toggle_qr(self, on: bool) -> None:
        self.live_qr.setVisible(on)
        self.live_qr_note.setVisible(on)
        if on:
            self._refresh_qr()

    def _refresh_qr(self) -> None:
        if not self.live_qr_button.isChecked():
            return
        address = self._live_address()
        if not address:
            self.live_qr.clear()
            return
        try:
            pixmap = _qr_pixmap(address)
        except Exception as error:
            self.live_qr.clear()
            self.live_qr_note.setText(f"No code for this address: {error}")
            return
        self.live_qr.setPixmap(pixmap)
        self.live_qr_note.setText("")

    def _copy_live_address(self) -> None:
        text = self._live_address()
        if text:
            QGuiApplication.clipboard().setText(text)
            tooltip("Address copied", parent=self)

    def _refresh_live(self) -> None:
        running = live.is_running()
        self.live_box.setVisible(running)
        self.live_button.setText("Browse live again" if running else "Browse live")
        if not running:
            return

        search = self._live_search()
        self.live_address.setText(live.url(search) or "")
        shared = live.share_urls(search)
        self.live_share_address.clear()
        self.live_share_address.addItems(shared)
        self.live_share_address.setVisible(bool(shared))
        self.live_note.setVisible(bool(shared))
        # Several addresses only ever mean one thing worth saying.
        self.live_note.setText(
            "If the first address does not answer, try the next." if shared else ""
        )
        # Without the network share there is no address another device could
        # reach, so there is no code worth showing.
        self.live_qr_button.setEnabled(bool(shared))
        self.live_qr_button.setToolTip(
            "Show the address as a QR code"
            if shared
            else "Needs the network share: a code for this machine's own "
            "address would send the other device to itself."
        )
        if not shared and self.live_qr_button.isChecked():
            self.live_qr_button.setChecked(False)
        self._refresh_qr()

    # Tabs
    ##################################################################

    def _selection_tab(self) -> QWidget:
        widget = QWidget()
        form = QVBoxLayout(widget)

        if self.card_ids is not None:
            banner = QLabel(
                f"Exporting the {len(self.card_ids)} card(s) selected in the browser. "
                "Clear the checkbox to use the filters below instead."
            )
            banner.setWordWrap(True)
            self.use_selection = QCheckBox("Use browser selection")
            self.use_selection.setChecked(True)
            self.use_selection.toggled.connect(self._on_filter_changed)
            form.addWidget(banner)
            form.addWidget(self.use_selection)
        else:
            self.use_selection = None

        form.addWidget(QLabel("Deck:"))
        self.deck_box = QComboBox()
        self.deck_box.addItem(ALL_DECKS)
        self.deck_box.addItems(
            sorted(entry.name for entry in mw.col.decks.all_names_and_ids())
        )
        self.deck_box.currentIndexChanged.connect(self._on_filter_changed)
        form.addWidget(self.deck_box)

        form.addWidget(QLabel("Tags (combined with AND):"))
        self.tag_input = TagInputWidget(mw.col.tags.all())
        self.tag_input.tagChanged.connect(self._on_filter_changed)
        form.addWidget(self.tag_input)

        clear_tags = QPushButton("Clear tags")
        clear_tags.clicked.connect(self.tag_input.clear_tags)
        clear_tags.setFixedWidth(120)
        form.addWidget(clear_tags)

        form.addWidget(QLabel("Additional Anki search (optional):"))
        self.search_input = QLineEdit(self.config.get("last_search", ""))
        self.search_input.setPlaceholderText(
            'e.g.  is:due  -tag:leech  "note:Basic"  added:30'
        )
        self.search_input.textChanged.connect(self._on_filter_changed)
        form.addWidget(self.search_input)

        self.include_hidden = QCheckBox("Include suspended and buried cards")
        self.include_hidden.setChecked(bool(self.config.get("include_hidden", False)))
        self.include_hidden.toggled.connect(self._on_filter_changed)
        form.addWidget(self.include_hidden)

        self.query_preview = QLabel("")
        self.query_preview.setWordWrap(True)
        self.query_preview.setStyleSheet("color: palette(mid);")
        form.addWidget(self.query_preview)

        form.addStretch(1)
        return widget

    def _content_tab(self) -> QWidget:
        widget = QWidget()
        outer = QVBoxLayout(widget)

        view_box = QGroupBox("Visible by default")
        view_box.setToolTip(
            "Where the export opens. Every one of these can also be switched "
            "inside it."
        )
        view = QVBoxLayout(view_box)

        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("Card sides:"))
        self.view_mode = QComboBox()
        for key, label in VIEW_MODES:
            self.view_mode.addItem(label, key)
        index = self.view_mode.findData(self.config.get("view_mode", "auto"))
        self.view_mode.setCurrentIndex(index if index >= 0 else 0)
        self.view_mode.setToolTip(
            "Auto shows the answer side, and adds the question only for cards "
            "whose answer does not already contain it — which is most of the "
            "time, since standard templates repeat the front and cloze answers "
            "are the same text revealed."
        )
        mode_row.addWidget(self.view_mode, 1)
        view.addLayout(mode_row)

        self.interactive = QCheckBox("Reveal on click")
        self.interactive.setToolTip(
            "Anki's rendering already carries the answer of a cloze deletion "
            "and the shapes of an image occlusion, so in the export they can "
            "be lifted by clicking them."
        )
        self.study = QCheckBox("Start in study mode")
        self.study.setToolTip(
            "Only the front of each card is on screen; the answer appears on "
            "'Show answer' or the space bar."
        )

        self.show_meta = QCheckBox("Header row")
        self.show_meta.setToolTip("Deck, note type and tags above each card.")
        self.show_special = QCheckBox("Details strip")
        self.show_special.setToolTip(
            "Note ID, created, modified, state, due, interval, reviews, flag."
        )
        self.show_fields = QCheckBox("Note fields")
        self.dark_mode = QCheckBox("Night mode")
        for checkbox, key in (
            (self.interactive, "interactive"),
            (self.study, "study"),
            (self.show_meta, "show_meta"),
            (self.show_special, "show_special"),
            (self.show_fields, "show_fields"),
            (self.dark_mode, "dark"),
        ):
            checkbox.setChecked(bool(self.config.get(key)))
            view.addWidget(checkbox)
        outer.addWidget(view_box)

        lists = QHBoxLayout()

        fields_box = QGroupBox("Note fields to include")
        fields_layout = QVBoxLayout(fields_box)
        # A whole-collection selection can pull in a few hundred field names,
        # so the list needs a way to narrow it down.
        self.field_filter = QLineEdit()
        self.field_filter.setPlaceholderText("Filter field names…")
        self.field_filter.textChanged.connect(self._filter_field_list)
        fields_layout.addWidget(self.field_filter)
        self.field_list = QListWidget()
        # A list asks for about 256 px on its own, and there are two of them
        # side by side -- which decided how narrow this window could be.
        self.field_list.setMinimumWidth(150)
        self.field_list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        fields_layout.addWidget(self.field_list)
        fields_buttons = QHBoxLayout()
        for label, checked in (("All", True), ("None", False)):
            button = QPushButton(label)
            button.clicked.connect(
                lambda _=False, w=self.field_list, c=checked: _set_all(w, c)
            )
            fields_buttons.addWidget(button)
        fields_layout.addLayout(fields_buttons)
        lists.addWidget(fields_box)

        special_box = QGroupBox("Special fields to include")
        special_layout = QVBoxLayout(special_box)
        self.special_list = QListWidget()
        self.special_list.setMinimumWidth(150)
        self.special_list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        excluded = set(self.config.get("excluded_special", []))
        for name in SPECIAL_FIELD_NAMES:
            item = QListWidgetItem(name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Unchecked if name in excluded else Qt.CheckState.Checked
            )
            self.special_list.addItem(item)
        special_layout.addWidget(self.special_list)
        special_buttons = QHBoxLayout()
        for label, checked in (("All", True), ("None", False)):
            button = QPushButton(label)
            button.clicked.connect(
                lambda _=False, w=self.special_list, c=checked: _set_all(w, c)
            )
            special_buttons.addWidget(button)
        special_layout.addLayout(special_buttons)
        lists.addWidget(special_box)

        outer.addLayout(lists)
        return widget

    def _output_tab(self) -> QWidget:
        widget = QWidget()
        form = QFormLayout(widget)

        self.folder_input = QLineEdit(self.config.get("output_dir", ""))
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        row = QHBoxLayout()
        row.addWidget(self.folder_input)
        row.addWidget(browse)
        container = QWidget()
        container.setLayout(row)
        form.addRow("Destination folder:", container)

        self.name_input = QLineEdit()
        form.addRow("Export name:", self.name_input)

        self.order_box = QComboBox()
        for key, label in ORDERS.items():
            self.order_box.addItem(label, key)
        index = self.order_box.findData(self.config.get("order", "deck"))
        if index >= 0:
            self.order_box.setCurrentIndex(index)
        form.addRow("Sort cards by:", self.order_box)

        self.single_file = QCheckBox("Single self-contained .html file")
        self.single_file.setChecked(bool(self.config.get("single_file")))
        self.single_file.setToolTip(
            "Media is embedded in the file. Easier to pass around, but large "
            "and slower to open, and MathJax loses its web fonts."
        )
        form.addRow("", self.single_file)

        self.embed_mathjax = QCheckBox("Bundle MathJax when needed")
        self.embed_mathjax.setToolTip(
            "About 1.7 MB, copied in only when a card uses \\(…\\) or \\[…\\]."
        )
        self.embed_mathjax.setChecked(bool(self.config.get("embed_mathjax", True)))
        form.addRow("", self.embed_mathjax)

        self.download_remote = QCheckBox("Download media from URLs")
        self.download_remote.setChecked(
            bool(self.config.get("download_remote_media"))
        )
        self.download_remote.setToolTip(
            "Cards can point at pictures on the web instead of carrying them; "
            "those load in the reviewer but would be missing from an offline "
            "export. This keeps a copy.\n\n"
            "The URLs come from the notes, and in a shared deck they were "
            "written by someone else — Anki will contact every host they "
            "chose, which tells that host your IP address."
        )
        form.addRow("", self.download_remote)

        remote_note = QLabel(
            "Contacts those hosts, which reveals your IP to them."
        )
        remote_note.setWordWrap(True)
        remote_note.setStyleSheet("color: palette(mid);")
        form.addRow("", remote_note)

        self.apply_gui_hooks = QCheckBox("Let add-ons post-process cards")
        self.apply_gui_hooks.setChecked(bool(self.config.get("apply_gui_hooks")))
        self.apply_gui_hooks.setToolTip(
            "Runs the same hook the reviewer runs, so add-ons that inject markup "
            "into cards also affect the export. Off by default: those hooks are "
            "written for the reviewer and may inject buttons that only work "
            "inside Anki."
        )
        form.addRow("", self.apply_gui_hooks)

        self.open_after = QCheckBox("Open the folder when finished")
        self.open_after.setChecked(True)
        form.addRow("", self.open_after)

        return widget

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Select export folder", self.folder_input.text() or os.path.expanduser("~")
        )
        if folder:
            self.folder_input.setText(folder)

    # Selection handling
    ##################################################################

    def _on_filter_changed(self, *_args) -> None:
        self._count_timer.start()

    def _use_browser_selection(self) -> bool:
        return self.use_selection is not None and self.use_selection.isChecked()

    def current_search(self, plain: bool = False) -> str:
        """The search the filter widgets add up to.

        ``plain`` leaves suspended and buried cards in regardless of the
        checkbox -- for the live view, whose server applies that rule itself
        so it also holds once the reader changes the scope in the page.
        """
        deck = self.deck_box.currentText()
        return build_search(
            None if deck == ALL_DECKS else deck,
            self.tag_input.get_tags(),
            self.search_input.text(),
            include_hidden=plain or self.include_hidden.isChecked(),
        )

    def current_card_ids(self) -> list[int]:
        if self._use_browser_selection():
            return list(self.card_ids or [])
        search = self.current_search()
        if not search.strip():
            return []
        try:
            return list(mw.col.find_cards(search))
        except Exception as exc:
            self.query_preview.setText(f"Invalid search: {exc}")
            return []

    def refresh_selection(self) -> None:
        if self._use_browser_selection():
            self.query_preview.setText("Using the cards selected in the browser.")
        else:
            search = self.current_search()
            self.query_preview.setText(search or "No filter set.")

        card_ids = self.current_card_ids()
        self.count_label.setText(f"Matching cards: {len(card_ids)}")
        self._refresh_field_list(card_ids)
        if not self.name_input.text():
            self.name_input.setText(self._suggested_name())

    def _refresh_field_list(self, card_ids: Sequence[int]) -> None:
        names = _field_names_for(card_ids)
        if names == self._field_names and self._field_list_populated:
            return

        excluded = set(self.config.get("excluded_fields", [])) | set(
            _unchecked_names(self.field_list)
        )
        self._field_names = names
        self._field_list_populated = True
        self.field_list.clear()

        if not names:
            # An empty box reads as broken; say why it is empty.
            hint = QListWidgetItem(
                "Fields appear once the selection matches cards."
            )
            hint.setFlags(Qt.ItemFlag.NoItemFlags)
            self.field_list.addItem(hint)
            return

        for name in names:
            item = QListWidgetItem(name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Unchecked if name in excluded else Qt.CheckState.Checked
            )
            self.field_list.addItem(item)
        self._filter_field_list(self.field_filter.text())

    def _filter_field_list(self, needle: str) -> None:
        needle = needle.strip().lower()
        for index in range(self.field_list.count()):
            item = self.field_list.item(index)
            item.setHidden(bool(needle) and needle not in item.text().lower())

    def _suggested_name(self) -> str:
        parts = ["Anki_Export"]
        if self._use_browser_selection():
            parts.append("Selection")
        else:
            deck = self.deck_box.currentText()
            if deck != ALL_DECKS:
                parts.append(deck.replace("::", "-"))
            parts.extend(self.tag_input.get_tags())
        parts.append(date.today().isoformat())
        name = "_".join(parts)
        return re.sub(r'[<>:"/\\|?*\s]+', "_", name)[:120]

    # Export
    ##################################################################

    def _collect_options(self, output_dir: Path | None) -> ExportOptions:
        return ExportOptions(
            output_dir=output_dir,
            title=self.name_input.text().strip() or "Anki Export",
            single_file=self.single_file.isChecked(),
            embed_mathjax=self.embed_mathjax.isChecked(),
            download_remote=self.download_remote.isChecked(),
            apply_gui_hooks=self.apply_gui_hooks.isChecked(),
            view_mode=self.view_mode.currentData(),
            interactive=self.interactive.isChecked(),
            study=self.study.isChecked(),
            show_meta=self.show_meta.isChecked(),
            show_special=self.show_special.isChecked(),
            show_fields=self.show_fields.isChecked(),
            dark=self.dark_mode.isChecked(),
            included_fields=_checked_names(self.field_list),
            included_special=_checked_names(self.special_list),
        )

    def _save_config(self) -> None:
        self.config.update(
            {
                "output_dir": self.folder_input.text().strip(),
                "order": self.order_box.currentData(),
                "single_file": self.single_file.isChecked(),
                "embed_mathjax": self.embed_mathjax.isChecked(),
                "download_remote_media": self.download_remote.isChecked(),
                "apply_gui_hooks": self.apply_gui_hooks.isChecked(),
                "view_mode": self.view_mode.currentData(),
                "interactive": self.interactive.isChecked(),
                "study": self.study.isChecked(),
                "show_meta": self.show_meta.isChecked(),
                "show_special": self.show_special.isChecked(),
                "show_fields": self.show_fields.isChecked(),
                "dark": self.dark_mode.isChecked(),
                "excluded_fields": _unchecked_names(self.field_list),
                "excluded_special": _unchecked_names(self.special_list),
                "last_search": self.search_input.text(),
                "include_hidden": self.include_hidden.isChecked(),
            }
        )
        addon_config.save_config(self.config)

    def start_export(self) -> None:
        base = self.folder_input.text().strip()
        if not base:
            showWarning("Please choose a destination folder.", parent=self)
            self.tabs.setCurrentIndex(2)
            return

        card_ids = self.current_card_ids()
        if not card_ids:
            showWarning("No cards match the current selection.", parent=self)
            self.tabs.setCurrentIndex(0)
            return

        output_dir = Path(base) / (self.name_input.text().strip() or "Anki_Export")
        if output_dir.exists() and any(output_dir.iterdir()):
            answer = QMessageBox.question(
                self,
                "Folder not empty",
                f"{output_dir} already exists.\n\n"
                "The previous export (index.html, css/, js/, media/) will be replaced. "
                "Continue?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        self._save_config()

        # find_cards has already run on the main thread, so hand the ids over
        # rather than letting the background op query again.
        request = ExportRequest(
            options=self._collect_options(output_dir),
            search="" if self._use_browser_selection() else self.current_search(),
            card_ids=card_ids,
            order=self.order_box.currentData(),
        )

        self.export_button.setEnabled(False)

        def op(col: Any):
            def progress(done: int, total: int) -> bool:
                if mw.progress.want_cancel():
                    return False
                mw.taskman.run_on_main(
                    lambda: mw.progress.update(
                        label=f"Rendering card {done} of {total}…",
                        value=done,
                        max=total,
                    )
                )
                return True

            return run_export(col, request, progress)

        def on_success(result) -> None:
            self.export_button.setEnabled(True)
            self._report(result, request)

        def on_failure(exc: Exception) -> None:
            self.export_button.setEnabled(True)
            if isinstance(exc, ExportAborted):
                tooltip("Export cancelled.", parent=self)
                return
            showWarning(f"Export failed:\n\n{exc}", parent=self)

        QueryOp(parent=self, op=op, success=on_success).failure(on_failure).with_progress(
            "Exporting cards…"
        ).run_in_background()

    def _report(self, result, request: ExportRequest) -> None:
        lines = [
            f"{result.card_count} cards written to:",
            str(result.path),
            "",
            f"Media files: {result.media_count} ({result.media_bytes / 1024 / 1024:.1f} MB)",
        ]
        if result.mathjax_embedded:
            lines.append("MathJax bundled for LaTeX/MathJax cards.")
        if result.missing_media:
            preview = ", ".join(result.missing_media[:5])
            lines.append(
                f"Missing from the media folder: {len(result.missing_media)} "
                f"file(s) — {preview}…"
            )
        if request.errors:
            lines.append("")
            lines.append(f"{len(request.errors)} card(s) could not be rendered:")
            lines.extend(request.errors[:5])

        showInfo("\n".join(lines), parent=self, title="Export complete")
        if self.open_after.isChecked():
            openFolder(str(result.path.parent))


# Helpers
######################################################################


def _qr_pixmap(text: str, module: int = 5, quiet: int = 4) -> QPixmap:
    """The address as a QR code, drawn a module at a time and then scaled.

    Nearest-neighbour on the way up, so the modules stay squares with hard
    edges; a smoothed code is a code a camera has to work at.
    """
    matrix = qr.encode(text)
    side = len(matrix) + 2 * quiet
    image = QImage(side, side, QImage.Format.Format_RGB32)
    image.fill(0xFFFFFFFF)
    for row, cells in enumerate(matrix):
        for col, dark in enumerate(cells):
            if dark:
                image.setPixel(col + quiet, row + quiet, 0xFF000000)
    return QPixmap.fromImage(
        image.scaled(
            side * module,
            side * module,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation,
        )
    )


def _set_all(widget: QListWidget, checked: bool) -> None:
    """Check or uncheck every visible row, so All/None respects the filter."""
    state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
    for index in range(widget.count()):
        item = widget.item(index)
        if not item.isHidden():
            item.setCheckState(state)


def _checked_names(widget: QListWidget) -> set[str]:
    return {
        widget.item(index).text()
        for index in range(widget.count())
        if widget.item(index).checkState() == Qt.CheckState.Checked
    }


def _unchecked_names(widget: QListWidget) -> list[str]:
    return [
        widget.item(index).text()
        for index in range(widget.count())
        if widget.item(index).checkState() == Qt.CheckState.Unchecked
    ]


MID_QUERY_LIMIT = 20_000


def _field_names_for(card_ids: Sequence[int]) -> list[str]:
    """Field names of every note type involved in the selection."""
    if not card_ids:
        return []
    from anki.utils import ids2str

    try:
        if len(card_ids) > MID_QUERY_LIMIT:
            # Inlining hundreds of thousands of ids is not worth it; for a
            # selection that large, offering every note type's fields is a
            # harmless superset (unused names simply never show up).
            mids = mw.col.db.list("select distinct mid from notes")
        else:
            mids = mw.col.db.list(
                "select distinct mid from notes where id in "
                "(select nid from cards where id in %s)" % ids2str(card_ids)
            )
    except Exception:
        return []

    names: dict[str, None] = {}
    for mid in mids:
        notetype = mw.col.models.get(mid)
        if not notetype:
            continue
        for fld in notetype["flds"]:
            names.setdefault(fld["name"], None)
    return list(names)


def show_export_dialog(card_ids: Sequence[int] | None = None, parent=None) -> None:
    dialog = ExportDialog(card_ids=card_ids, parent=parent)
    dialog.show()
