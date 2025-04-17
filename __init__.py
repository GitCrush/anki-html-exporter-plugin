from aqt import mw
from aqt.qt import *
from aqt.gui_hooks import browser_will_show_context_menu
from .tag_input_widget import TagInputWidget
from .exporter import export_to_html_gui
import os
import traceback
from PyQt6.QtWidgets import QMessageBox

def check_anki_connect_installed():
    if "2055492159" not in mw.addonManager.allAddons():
        QMessageBox.critical(
            mw,
            "AnkiConnect Missing",
            "AnkiConnect is required for this add-on to work. Please install it from the AnkiWeb Add-ons menu."
        )
        return False
    return True


class ExportWorker(QThread):
    progress = pyqtSignal(int)
    finished = pyqtSignal(int)
    error = pyqtSignal(str)

    def __init__(self, deck, tags, output_path, stop_check):
        super().__init__()
        self.deck = deck
        self.tags = tags
        self.output_path = output_path
        self.stop_check = stop_check

    def run(self):
        try:
            result = export_to_html_gui(
                deck_name=self.deck,
                tags=self.tags,
                output_base=self.output_path,
                progress_callback=lambda c, t: self.progress.emit(int((c / t) * 100)) if t else 0,
                stop_flag=self.stop_check,
            )
            self.finished.emit(result)
        except Exception as e:
            traceback.print_exc()
            self.error.emit(str(e))


def generate_folder_name(deck, tags):
    name_parts = ["Anki_Export"]
    if deck:
        name_parts.append(deck.replace(" ", "_"))
    if tags:
        name_parts.extend(tag.replace(" ", "_") for tag in tags)
    return "_".join(name_parts)


def show_export_dialog():
    if not check_anki_connect_installed():
        return  # Exit silently if missing
    dialog = QDialog(mw, Qt.WindowType.Window)
    dialog.setWindowTitle("Export to HTML")
    dialog.setMinimumWidth(500)
    layout = QVBoxLayout()

    # Deck selection
    deck_label = QLabel("Select deck:")
    deck_dropdown = QComboBox()
    deck_names = mw.col.decks.all_names()
    deck_dropdown.addItem("All Decks")
    deck_dropdown.addItems(deck_names)
    layout.addWidget(deck_label)
    layout.addWidget(deck_dropdown)

    # Tag input
    tag_label = QLabel("Enter tags:")
    all_tags = mw.col.tags.all()
    tag_input_widget = TagInputWidget(all_tags)
    layout.addWidget(tag_label)
    layout.addWidget(tag_input_widget)

    # Folder selection
    path_label = QLabel("Save to destination:")
    folder_input = QLineEdit()
    browse_button = QPushButton("Browse")
    path_layout = QHBoxLayout()
    path_layout.addWidget(folder_input)
    path_layout.addWidget(browse_button)
    layout.addWidget(path_label)
    layout.addLayout(path_layout)

    def browse():
        folder = QFileDialog.getExistingDirectory(dialog, "Select Export Folder")
        if folder:
            folder_input.setText(folder)

    browse_button.clicked.connect(browse)

    # Card count
    card_count_label = QLabel("Matching cards: 0")
    layout.addWidget(card_count_label)

    # Progress bar + stop
    progress_label = QLabel("Progress:")
    progress_bar = QProgressBar()
    stop_button = QPushButton("Stop")
    stop_button.setEnabled(False)
    stop_button.setFixedWidth(100)
    progress_layout = QHBoxLayout()
    progress_layout.addWidget(progress_bar)
    progress_layout.addWidget(stop_button)
    layout.addWidget(progress_label)
    layout.addLayout(progress_layout)

    # Control buttons
    button_layout = QHBoxLayout()
    export_button = QPushButton("Export")
    cancel_button = QPushButton("Cancel")
    clear_tag_btn = QPushButton("Clear Tags")
    clear_tag_btn.setFixedWidth(100)
    button_layout.addWidget(export_button)
    button_layout.addWidget(clear_tag_btn)
    button_layout.addWidget(cancel_button)
    layout.addLayout(button_layout)

    dialog.setLayout(layout)

    # Internal state
    stop_requested = False
    worker = None

    def update_card_count():
        deck = deck_dropdown.currentText()
        if deck == "All Decks":
            deck = None
        tags = tag_input_widget.get_tags()
        query_parts = []
        if deck:
            query_parts.append(f'deck:"{deck}"')
        if tags:
            for tag in tags:
                query_parts.append(f'tag:"{tag}"')
        query = " ".join(query_parts)
        try:
            count = len(mw.col.find_cards(query)) if query else 0
            card_count_label.setText(f"Matching cards: {count}")
        except Exception as e:
            card_count_label.setText("Matching cards: error")
            print("Query error:", e)

    # Connections
    cancel_button.clicked.connect(lambda: on_cancel())
    clear_tag_btn.clicked.connect(tag_input_widget.clear_tags)
    deck_dropdown.currentIndexChanged.connect(update_card_count)
    tag_input_widget.tagChanged.connect(update_card_count)
    browse_button.clicked.connect(update_card_count)

    def on_stop():
        nonlocal stop_requested
        stop_requested = True
        stop_button.setEnabled(False)
        export_button.setEnabled(True)
        export_button.setText("Export")

    stop_button.clicked.connect(on_stop)

    def run_export():
        nonlocal stop_requested, worker
        stop_requested = False
        stop_button.setEnabled(True)
        export_button.setEnabled(False)
        export_button.setText("Exporting...")

        deck = deck_dropdown.currentText()
        if deck == "All Decks":
            deck = None

        tags = tag_input_widget.get_tags()
        base_folder = folder_input.text().strip()

        if not base_folder:
            QMessageBox.warning(dialog, "Missing Folder", "Please select a folder.")
            reset_export_ui()
            return

        if not deck and not tags:
            QMessageBox.warning(dialog, "Missing Filters", "Please provide a deck or tags.")
            reset_export_ui()
            return

        folder_name = generate_folder_name(deck, tags)
        output_path = os.path.join(base_folder, folder_name)
        os.makedirs(output_path, exist_ok=True)

        def stop_check():
            return stop_requested

        worker = ExportWorker(deck, tags, output_path, stop_check)
        worker.progress.connect(progress_bar.setValue)
        worker.finished.connect(on_export_finished)
        worker.error.connect(on_export_error)
        worker.start()

    def on_export_finished(result):
        stop_button.setEnabled(False)
        export_button.setEnabled(True)
        export_button.setText("Export")
        if stop_requested:
            print("Export cancelled by user.")
            return
        if result == 0:
            QMessageBox.information(dialog, "No Cards", "No matching cards found.")
            return
        QMessageBox.information(dialog, "Export Complete", f"{result} cards exported.")

    def on_export_error(msg):
        stop_button.setEnabled(False)
        export_button.setEnabled(True)
        export_button.setText("Export")
        QMessageBox.critical(dialog, "Error", f"Export failed: {msg}")

    def on_cancel():
        nonlocal stop_requested
        stop_requested = True
        stop_button.setEnabled(False)
        export_button.setEnabled(True)
        export_button.setText("Export")
        if worker and worker.isRunning():
            print("Cancel requested. Waiting for export thread to stop...")

            def close_dialog_when_done():
                print("Export thread ended. Dialog remains open.")

            worker.finished.connect(close_dialog_when_done)
        else:
            print("Cancel pressed before worker was running.")

    def reset_export_ui():
        stop_button.setEnabled(False)
        export_button.setEnabled(True)
        export_button.setText("Export")

    export_button.clicked.connect(run_export)
    update_card_count()
    dialog.show()


def add_menu_entry():
    action = QAction("Export to HTML (Deck/Tag)", mw)
    action.triggered.connect(show_export_dialog)
    mw.form.menuTools.addAction(action)


add_menu_entry()

