import os
import random
import shutil
import time

from aqt import mw
from aqt.qt import QTimer
from importlib import import_module

# Import the exporter from the main add-on
exporter_module = import_module("search_to_html.exporter")
export_to_html_gui = exporter_module.export_to_html_gui

def generate_folder_name(deck, tags):
    parts = ["Anki_Export"]
    if deck:
        parts.append(deck.replace(" ", "_"))
    if tags:
        parts.extend(tag.replace(" ", "_") for tag in tags)
    return "_".join(parts)

def run_export_test():
    print("🚀 Running headless export test...")

    decks = mw.col.decks.all_names()
    tags = mw.col.tags.all()

    if not decks or not tags:
        print("Not enough decks or tags for testing.")
        return

    deck = random.choice(decks)
    selected_tags = random.sample(tags, min(2, len(tags)))

    print(f"Deck: {deck}")
    print(f"Tags: {selected_tags}")

    base_dir = os.path.expanduser("~/anki_export_test")
    folder_name = generate_folder_name(deck, selected_tags)
    full_path = os.path.join(base_dir, folder_name)
    os.makedirs(full_path, exist_ok=True)

    print(f"Export path: {full_path}")

    try:
        result = export_to_html_gui(
            deck_name=deck,
            tags=selected_tags,
            output_base=full_path,
            progress_callback=lambda c, t: print(f"🔄 Progress: {c}/{t}"),
            stop_flag=lambda: False,
        )

        if result == 0:
            print("No cards matched. Export returned 0.")
        else:
            print(f"Successfully exported {result} cards.")
    except Exception as e:
        print(f"Error during export: {e}")
    finally:
        time.sleep(2)  # Let the OS settle file ops
        shutil.rmtree(base_dir, ignore_errors=True)
        print("Cleaned up test folder.")

# Schedule test after Anki finishes loading
QTimer.singleShot(3000, run_export_test)

