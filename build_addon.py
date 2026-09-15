#!/usr/bin/env python3
"""Build the distributable .ankiaddon files.

Produces two archives in ``dist/``:

``…-ankiweb.ankiaddon``   for the upload form on AnkiWeb, which assigns the
                          name and package itself and therefore wants no
                          manifest.json
``….ankiaddon``           for handing the file to someone directly or for
                          installing it locally; this one needs the manifest

Both are validated before they are written: no stray bytecode, no development
leftovers, the version in the manifest and in ``ahe/__init__`` agreeing, and
every file the code loads at runtime actually present.

Run with:  python3 build_addon.py
           python3 build_addon.py --tailwind    (regenerate the panel's CSS)
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"

# The navigation panel is written in Tailwind utilities. The generated CSS is
# committed so that building the add-on needs nothing but Python; the stamp is
# what keeps that copy honest.
TAILWIND_DIR = ROOT / "tailwind"
TAILWIND_INPUT = TAILWIND_DIR / "input.css"
TAILWIND_OUTPUT = ROOT / "ahe" / "web" / "tailwind.css"
TAILWIND_STAMP = TAILWIND_DIR / "stamp.txt"
# Everything the class scanner reads, and therefore everything that can make
# the generated file stale.
TAILWIND_SOURCES = [
    TAILWIND_INPUT,
    ROOT / "ahe" / "web" / "shell.js",
    ROOT / "ahe" / "writer.py",
    ROOT / "ahe" / "hypnagog" / "web" / "index.html",
]

# Everything that belongs in the add-on, in the order it is written.
PAYLOAD = [
    "__init__.py",
    "config.json",
    "config.md",
    "ahe/__init__.py",
    "ahe/assets.py",
    "ahe/config.py",
    "ahe/dialog.py",
    "ahe/export.py",
    "ahe/live.py",
    "ahe/live_server.py",
    "ahe/media_export.py",
    "ahe/qr.py",
    "ahe/renderer.py",
    "ahe/tag_input_widget.py",
    "ahe/writer.py",
    "ahe/web/frame.css",
    "ahe/web/frame.js",
    "ahe/web/shell.css",
    "ahe/web/shell.js",
    "ahe/web/shim.js",
    "ahe/web/tailwind.css",
    "ahe/hypnagog/__init__.py",
    "ahe/hypnagog/extract.py",
    "ahe/hypnagog/routes.py",
    "ahe/hypnagog/service.py",
    "ahe/hypnagog/web/app.js",
    "ahe/hypnagog/web/hypnagog.js",
    "ahe/hypnagog/web/index.html",
    "ahe/narrator/__init__.py",
    "ahe/narrator/capture.py",
    "ahe/narrator/chat.py",
    "ahe/narrator/config.py",
    "ahe/narrator/imaging.py",
    "ahe/narrator/mp3.py",
    "ahe/narrator/narrate.py",
    "ahe/narrator/occlusion.py",
    "ahe/narrator/openai_api.py",
    "ahe/narrator/outputs.py",
    "ahe/narrator/routes.py",
    "ahe/narrator/service.py",
    "ahe/narrator/text.py",
    "ahe/narrator/tls.py",
    "ahe/narrator/usage.py",
    "ahe/narrator/view.py",
    "ahe/narrator/web/app.css",
    "ahe/narrator/web/app.js",
    "ahe/narrator/web/body.html",
]

MANIFEST = "manifest.json"

# Read by ExportWriter at export time; a missing one would only surface when a
# user runs an export, so check it here instead.
RUNTIME_WEB_ASSETS = [
    "frame.css",
    "frame.js",
    "shell.css",
    "shell.js",
    "shim.js",
    "tailwind.css",
]


def fail(message: str) -> None:
    print(f"  ✗ {message}")
    sys.exit(1)


def declared_version() -> str:
    """The version in ahe/__init__.py, read without importing aqt."""
    tree = ast.parse((ROOT / "ahe" / "__init__.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__version__":
                    return ast.literal_eval(node.value)
    fail("ahe/__init__.py defines no __version__")
    raise SystemExit(1)  # unreachable, keeps type checkers happy


def tailwind_stamp() -> str:
    """A fingerprint of everything the Tailwind build reads."""
    digest = hashlib.sha256()
    for path in TAILWIND_SOURCES:
        digest.update(path.read_bytes())
    return digest.hexdigest()


def run_tailwind() -> None:
    """Regenerate ahe/web/tailwind.css. Needs Node; nothing else does."""
    print("tailwind…")
    if not shutil.which("npm"):
        fail("npm is not on PATH — needed only for this step")
    if not (TAILWIND_DIR / "node_modules").is_dir():
        print("  installing the Tailwind CLI…")
        subprocess.run(
            ["npm", "install", "--no-audit", "--no-fund"],
            cwd=TAILWIND_DIR,
            check=True,
        )
    subprocess.run(["npm", "run", "--silent", "build"], cwd=TAILWIND_DIR, check=True)
    TAILWIND_STAMP.write_text(tailwind_stamp() + "\n", encoding="utf-8")
    size = TAILWIND_OUTPUT.stat().st_size
    print(f"  ✓ ahe/web/tailwind.css ({size / 1024:.1f} KB)")


def check_tailwind() -> None:
    """Refuse to ship a stylesheet that no longer matches the markup.

    A class added to the panel without rebuilding would simply have no rule --
    the panel would look broken, and only in the built add-on.
    """
    if not TAILWIND_OUTPUT.is_file():
        fail("ahe/web/tailwind.css is missing — run: python3 build_addon.py --tailwind")
    if not TAILWIND_STAMP.is_file():
        fail("tailwind/stamp.txt is missing — run: python3 build_addon.py --tailwind")
    if TAILWIND_STAMP.read_text(encoding="utf-8").strip() != tailwind_stamp():
        fail(
            "ahe/web/tailwind.css is stale: the panel's markup changed since it "
            "was generated — run: python3 build_addon.py --tailwind"
        )


def check() -> str:
    print("checking…")

    missing = [name for name in PAYLOAD + [MANIFEST] if not (ROOT / name).is_file()]
    if missing:
        fail(f"missing from the working tree: {', '.join(missing)}")

    manifest = json.loads((ROOT / MANIFEST).read_text(encoding="utf-8"))
    for key in ("name", "package", "version", "min_point_version"):
        if key not in manifest:
            fail(f"{MANIFEST} has no {key!r}")

    version = declared_version()
    if manifest["version"] != version:
        fail(
            f"version mismatch: {MANIFEST} says {manifest['version']!r}, "
            f"ahe/__init__.py says {version!r}"
        )

    for name in RUNTIME_WEB_ASSETS:
        if f"ahe/web/{name}" not in PAYLOAD:
            fail(f"ahe/web/{name} is loaded at runtime but not in the payload")
    for name in ("app.css", "app.js", "body.html"):
        if f"ahe/narrator/web/{name}" not in PAYLOAD:
            fail(f"ahe/narrator/web/{name} is loaded at runtime but not in the payload")
    # Every module and web file of the packages, so a new one cannot be forgotten
    for package in ("narrator", "hypnagog"):
        for module in sorted((ROOT / "ahe" / package).glob("*.py")):
            if f"ahe/{package}/{module.name}" not in PAYLOAD:
                fail(f"ahe/{package}/{module.name} is not in the payload")
        for asset in sorted((ROOT / "ahe" / package / "web").iterdir()):
            if asset.is_file() and not asset.name.endswith("_stable") and f"ahe/{package}/web/{asset.name}" not in PAYLOAD:
                fail(f"ahe/{package}/web/{asset.name} is not in the payload")

    # config.json is the defaults file Anki shows; it has to parse and to cover
    # the keys the add-on reads back.
    config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    defaults_src = (ROOT / "ahe" / "config.py").read_text(encoding="utf-8")
    for key in re.findall(r'^\s{4}"([a-z_]+)":', defaults_src, re.M):
        if key not in config:
            fail(f"config.json is missing the default for {key!r}")
    hypnagog_src = (ROOT / "ahe" / "hypnagog" / "routes.py").read_text(encoding="utf-8")
    for key in re.findall(r'^\s{4}"([a-z_0-9]+)":', hypnagog_src.split("DEFAULTS: dict")[1].split("STATIC")[0], re.M):
        if key not in config.get("hypnagog", {}):
            fail(f"config.json is missing the hypnagog default for {key!r}")
    narrator_src = (ROOT / "ahe" / "narrator" / "config.py").read_text(encoding="utf-8")
    for key in re.findall(r'^\s{4}"([a-z_]+)":', narrator_src.split("DEFAULTS: dict")[1].split("PAGE_KEYS")[0], re.M):
        if key not in config.get("narrator", {}):
            fail(f"config.json is missing the narrator default for {key!r}")

    if any(part == "__pycache__" for name in PAYLOAD for part in Path(name).parts):
        fail("payload contains bytecode")

    check_tailwind()

    print(f"  ✓ {len(PAYLOAD)} files, version {version}")
    return version


def build(version: str) -> None:
    DIST.mkdir(exist_ok=True)
    variants = [
        (f"anki-html-exporter-{version}-ankiweb.ankiaddon", False, "AnkiWeb upload"),
        (f"anki-html-exporter-{version}.ankiaddon", True, "direct install"),
    ]

    print("building…")
    for filename, with_manifest, purpose in variants:
        target = DIST / filename
        names = ([MANIFEST] if with_manifest else []) + PAYLOAD
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
            for name in names:
                # A fixed timestamp keeps rebuilds byte-identical.
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                archive.writestr(info, (ROOT / name).read_bytes())

        with zipfile.ZipFile(target) as archive:
            broken = archive.testzip()
            if broken:
                fail(f"{filename}: corrupt entry {broken}")
            entries = archive.namelist()

        if any(part == "__pycache__" for e in entries for part in Path(e).parts):
            fail(f"{filename} contains bytecode")
        if any(
            e.startswith(("backup/", "export_test_runner/", "tests/", "dist/"))
            for e in entries
        ):
            fail(f"{filename} contains development leftovers")

        size = target.stat().st_size
        print(f"  ✓ dist/{filename}  ({size / 1024:.0f} KB, {len(entries)} files) — {purpose}")


if __name__ == "__main__":
    if "--tailwind" in sys.argv[1:]:
        run_tailwind()
    build(check())
    print("\ndone. Upload the -ankiweb file at https://ankiweb.net/shared/mine")
