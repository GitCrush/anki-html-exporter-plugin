"""Cutting a piece out of a picture, and shrinking one.

Anki's Python has no imaging library, but Anki itself runs on Qt, whose
QImage does both without the GUI thread. Outside Anki -- the tests -- Pillow
stands in when it is installed; without either, the pictures go as they
are, and the crops are left out.
"""

from __future__ import annotations

import io
from pathlib import Path

from .occlusion import Box

# The vision model sees the whole picture at this size at most; enough to
# read a figure, few enough tokens to be cheap.
MAX_SIDE = 1024
# A crop is padded by this fraction of its own size on every side, so the
# label and its surroundings are both in it.
PAD = 0.6
MIN_CROP = 160


def _crop_pixels(box: Box, width: int, height: int) -> tuple[int, int, int, int]:
    pad_x = max(box.width * width * PAD, (MIN_CROP - box.width * width) / 2, 0)
    pad_y = max(box.height * height * PAD, (MIN_CROP - box.height * height) / 2, 0)
    left = int(max(0, box.left * width - pad_x))
    top = int(max(0, box.top * height - pad_y))
    right = int(min(width, (box.left + box.width) * width + pad_x))
    bottom = int(min(height, (box.top + box.height) * height + pad_y))
    return left, top, max(1, right - left), max(1, bottom - top)


def crop_png(path: Path, box: Box) -> bytes | None:
    """The picture under and around the box, as PNG; None if nothing can draw."""
    try:
        from aqt.qt import QBuffer, QIODevice, QImage, QRect

        image = QImage(str(path))
        if image.isNull():
            return None
        left, top, width, height = _crop_pixels(box, image.width(), image.height())
        piece = image.copy(QRect(left, top, width, height))
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        piece.save(buffer, "PNG")
        return bytes(buffer.data())
    except Exception:
        pass
    try:
        from PIL import Image

        with Image.open(path) as image:
            left, top, width, height = _crop_pixels(box, image.width, image.height)
            piece = image.convert("RGB").crop((left, top, left + width, top + height))
            out = io.BytesIO()
            piece.save(out, "PNG")
            return out.getvalue()
    except Exception:
        return None


def whole_png(path: Path) -> tuple[bytes, str] | None:
    """The picture, shrunk to MAX_SIDE, and its media type; the file itself if nothing can draw."""
    try:
        from aqt.qt import QBuffer, QIODevice, QImage, Qt

        image = QImage(str(path))
        if image.isNull():
            return None
        if max(image.width(), image.height()) > MAX_SIDE:
            image = image.scaled(MAX_SIDE, MAX_SIDE, Qt.AspectRatioMode.KeepAspectRatio,
                                 Qt.TransformationMode.SmoothTransformation)
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        image.save(buffer, "PNG")
        return bytes(buffer.data()), "image/png"
    except Exception:
        pass
    try:
        from PIL import Image

        with Image.open(path) as image:
            image = image.convert("RGB")
            image.thumbnail((MAX_SIDE, MAX_SIDE))
            out = io.BytesIO()
            image.save(out, "PNG")
            return out.getvalue(), "image/png"
    except Exception:
        pass
    suffix = path.suffix.lower()
    mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "gif": "image/gif", "webp": "image/webp"}.get(suffix.lstrip("."))
    if mime is None:
        return None
    try:
        return path.read_bytes(), mime
    except OSError:
        return None
