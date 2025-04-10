import os
import re
import base64
import imghdr
import html
import urllib.parse
import requests
from aqt import mw

ANKI_CONNECT_URL = "http://localhost:8765"

def anki_request(action, params=None):
    payload = {
        "action": action,
        "version": 6,
        "params": params or {}
    }
    try:
        response = requests.post(ANKI_CONNECT_URL, json=payload)
        response.raise_for_status()
        result = response.json()
        if "error" in result and result["error"]:
            print("AnkiConnect error:", result["error"])
            return []
        return result["result"]
    except Exception as e:
        print("Exception in anki_request:", e)
        return []

def extract_media_filenames(html_content):
    return re.findall(r'src=["\']([^"\']+)["\']', html_content)

def is_external_url(url):
    return url.startswith("http://") or url.startswith("https://")

def download_media_file(filename, media_dir):
    if is_external_url(filename):
        return None
    media_data = anki_request("retrieveMediaFile", {"filename": filename})
    if media_data:
        binary_data = base64.b64decode(media_data)
        image_format = imghdr.what(None, binary_data)
        if image_format:
            os.makedirs(media_dir, exist_ok=True)
            path = os.path.join(media_dir, filename)
            with open(path, "wb") as f:
                f.write(binary_data)
            return f"media/{filename}"
    return None

def get_card_info(card_ids):
    return anki_request("cardsInfo", {"cards": card_ids})

def build_query(deck_name, tags):
    parts = []
    if deck_name:
        parts.append(f'deck:"{deck_name}"')
    if tags:
        parts.extend([f'tag:{tag}' for tag in tags])
    return " ".join(parts)

def export_to_html_gui(deck_name=None, tags=None, note_ids=None, output_base=None, progress_callback=None, stop_flag=None):
    print("Starting export_to_html_gui()")

    if note_ids:
        print(f"Using {len(note_ids)} selected notes")
        query = f"nid:{' OR nid:'.join(map(str, note_ids))}"
        card_ids = anki_request("findCards", {"query": query})
    else:
        if not deck_name and not tags:
            raise ValueError("Please provide a deck or tags.")
        query = build_query(deck_name, tags)
        card_ids = anki_request("findCards", {"query": query})

    if not card_ids:
        print("No cards found.")
        return 0

    cards = get_card_info(card_ids)
    total = len(cards)

    media_folder = os.path.join(output_base, "media")
    css_folder = os.path.join(output_base, "css")

    os.makedirs(media_folder, exist_ok=True)
    os.makedirs(css_folder, exist_ok=True)

    html_file = os.path.join(output_base, "index.html")
    css_file = os.path.join(css_folder, "styles.css")

    with open(css_file, "w", encoding="utf-8") as f:
        f.write("""
        body { font-family: Arial, sans-serif; background: #121212; color: #ffffff; display: flex; flex-direction: column; align-items: center; padding: 20px; }
        .card { border: 1px solid #444; padding: 20px; margin: 10px; border-radius: 8px; background: #1e1e1e; width: 90%; max-width: 900px; text-align: center; position: relative; }
        .card-id { font-size: 12px; color: #aaa; text-decoration: none; position: absolute; top: 5px; right: 10px; }
        .tags { font-size: 12px; color: #aaa; margin-top: 10px; border-top: 1px solid #444; padding-top: 5px; }
        img { max-width: 100%; height: auto; display: block; margin: 10px auto; }
        .extra-info-button { background-color: #333; color: #fff; border: none; padding: 5px 10px; cursor: pointer; margin-top: 5px; border-radius: 5px; text-decoration: none; display: inline-block; }
        .extra-info-button:hover { background-color: #555; }
        """)

    with open(html_file, "w", encoding="utf-8") as out:
        out.write("<html><head><meta charset='utf-8'><title>Exported Cards</title>")
        out.write("<meta name='viewport' content='width=device-width, initial-scale=1'>")
        out.write("<link rel='stylesheet' type='text/css' href='css/styles.css'>")
        out.write("<script>")
        out.write("""
        function openExtraInfo(content, isImage, isURL) {
            let newWindow = window.open("", "_blank", "width=800,height=600");
            newWindow.document.write(`
                <html>
                <head>
                    <meta name='viewport' content='width=device-width, initial-scale=1'>
                    <title>Extra Info</title>
                    <style>
                        body { background:#ffffff; color:#000; font-family: Arial, sans-serif; padding: 20px; margin: 0; }
                        img, iframe { max-width: 100%; max-height: 90vh; height: auto; width: auto; display: block; margin: auto; }
                    </style>
                </head>
                <body>`);
            if (isURL) {
                newWindow.document.write(`<iframe src='${content}'></iframe>`);
            } else if (isImage) {
                newWindow.document.write(`<img src='${content}'>`);
            } else {
                newWindow.document.write(decodeURIComponent(content));
            }
            newWindow.document.write(`</body></html>`);
            newWindow.document.close();
        }
        """)
        out.write("</script></head><body>")

        for i, card in enumerate(cards):
            if stop_flag and stop_flag():
                return 0

            answer = card.get("answer", "")
            answer = re.sub(r'<div id="tags-container".*?>.*?</div>', '', answer, flags=re.DOTALL | re.IGNORECASE)
            fields = card.get("fields", {})
            tags = card.get("tags", [])

            for media_file in extract_media_filenames(answer):
                local_path = download_media_file(media_file, media_folder)
                if local_path:
                    answer = answer.replace(media_file, local_path)
                elif is_external_url(media_file):
                    button = f"<button class='extra-info-button' onclick=\"openExtraInfo('{media_file}', false, true)\">External Media</button>"
                    answer += button

            out.write("<div class='card'>")
            out.write(f"<a href='#{card['cardId']}' class='card-id' id='{card['cardId']}'>Card ID: {card['cardId']}</a>")
            out.write(f"<div>{answer}</div>")

            for field_name, content in fields.items():
                val = content.get("value", "").strip()
                if not val or val in answer or field_name.lower() in ["front", "question"]:
                    continue
                for media_file in extract_media_filenames(val):
                    local_path = download_media_file(media_file, media_folder)
                    if local_path:
                        val = val.replace(media_file, local_path)
                    elif is_external_url(media_file):
                        val = val.replace(media_file, media_file)

                val_encoded = urllib.parse.quote(val)
                out.write(f"<button class='extra-info-button' onclick=\"openExtraInfo(this.dataset.content, false, false)\" data-content='{val_encoded}'>{html.escape(field_name)}</button>")

            if tags:
                out.write(f"<p class='tags'>Tags: {', '.join(tags)}</p>")
            out.write("</div>")

            if progress_callback:
                progress_callback(i + 1, total)

        out.write("</body></html>")

    print(f"Export complete: {html_file}")
    return total

