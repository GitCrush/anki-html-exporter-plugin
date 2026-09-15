"""Where the mask is on an image occlusion card.

Two kinds of note keep their masks in two places. Anki's own image occlusion
writes every shape into the card as a ``data-shape`` element with its
position as fractions of the picture, the ones asked on this card carrying
``class="cloze"``. Image Occlusion Enhanced, the add-on, keeps an SVG file
per card in the media folder, with the shape asked marked ``qshape`` and
pixel coordinates against the SVG's own size. Both come out here as the
same thing: the picture, and the box the hidden label sits in, in fractions.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

from ..renderer import RenderedCard

MAX_SHAPES = 3

_IMG_RE = re.compile(r'<img\b[^>]*\bsrc="([^"]+)"', re.IGNORECASE)
_NATIVE_SHAPE_RE = re.compile(r'<div\b[^>]*\bclass="cloze"[^>]*\bdata-shape="([a-z]+)"[^>]*>', re.IGNORECASE)
_ATTR_RE = re.compile(r'\bdata-([a-z]+)="([^"]*)"')
_SVG_SIZE_RE = re.compile(r'<svg\b[^>]*\bwidth="([\d.]+)"[^>]*\bheight="([\d.]+)"')
_SVG_SHAPE_RE = re.compile(r'<(rect|ellipse|polygon)\b([^>]*)\bclass="qshape"[^>]*>')
_SVG_ATTR_RE = re.compile(r'\b([a-z]+)="([^"]*)"')


@dataclass
class Box:
    """A rectangle in fractions of the picture: 0..1 from the top left."""
    left: float
    top: float
    width: float
    height: float

    def clamp(self) -> "Box":
        left = min(max(self.left, 0.0), 1.0)
        top = min(max(self.top, 0.0), 1.0)
        return Box(left, top, min(max(self.width, 0.0), 1.0 - left), min(max(self.height, 0.0), 1.0 - top))


@dataclass
class Occlusion:
    image: Path
    boxes: list[Box] = field(default_factory=list)
    kind: str = ""


def find(card: RenderedCard, media_dir: str | Path | None) -> Occlusion | None:
    """The picture and the hidden boxes of this card, or None if it has none."""
    if not media_dir:
        return None
    media = Path(media_dir)
    return _native(card, media) or _enhanced(card, media)


def _media_path(media: Path, src: str) -> Path | None:
    name = urllib.parse.unquote(src.split("/")[-1].split("?")[0])
    if not name or name.startswith("."):
        return None
    path = media / name
    return path if path.is_file() else None


def _native(card: RenderedCard, media: Path) -> Occlusion | None:
    html = card.question
    container = html.find('id="image-occlusion-container"')
    if container < 0:
        return None
    image = None
    for match in _IMG_RE.finditer(html[container:]):
        image = _media_path(media, match.group(1))
        if image:
            break
    if image is None:
        return None
    boxes = []
    for match in _NATIVE_SHAPE_RE.finditer(html):
        data = dict(_ATTR_RE.findall(match.group(0)))
        kind = match.group(1)
        try:
            if kind == "polygon":
                points = [tuple(float(v) for v in pair.split(",")) for pair in data.get("points", "").split() if "," in pair]
                if not points:
                    continue
                xs, ys = [p[0] for p in points], [p[1] for p in points]
                boxes.append(Box(min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)))
            elif kind == "ellipse":
                rx, ry = float(data.get("rx", 0)), float(data.get("ry", 0))
                boxes.append(Box(float(data.get("left", 0)), float(data.get("top", 0)), 2 * rx, 2 * ry))
            elif kind == "rect":
                boxes.append(Box(float(data.get("left", 0)), float(data.get("top", 0)),
                                 float(data.get("width", 0)), float(data.get("height", 0))))
        except ValueError:
            continue
    boxes = [box.clamp() for box in boxes if box.width > 0 and box.height > 0][:MAX_SHAPES]
    return Occlusion(image, boxes, "anki") if boxes else None


def _enhanced(card: RenderedCard, media: Path) -> Occlusion | None:
    """Image Occlusion Enhanced: the base picture and this card's question mask."""
    mask = image = None
    for match in _IMG_RE.finditer(card.question):
        path = _media_path(media, match.group(1))
        if path is None:
            continue
        if path.suffix.lower() == ".svg" and re.search(r"-(oa|ao)-\d+-Q\.svg$", path.name, re.IGNORECASE):
            mask = path
        elif image is None:
            image = path
    if mask is None or image is None:
        return None
    try:
        svg = mask.read_text("utf-8", "replace")
    except OSError:
        return None
    size = _SVG_SIZE_RE.search(svg)
    if not size:
        return None
    width, height = float(size.group(1)), float(size.group(2))
    if width <= 0 or height <= 0:
        return None
    boxes = []
    for kind, attrs in _SVG_SHAPE_RE.findall(svg):
        data = dict(_SVG_ATTR_RE.findall(attrs))
        try:
            if kind == "rect":
                box = Box(float(data["x"]) / width, float(data["y"]) / height,
                          float(data["width"]) / width, float(data["height"]) / height)
            elif kind == "ellipse":
                cx, cy, rx, ry = (float(data[k]) for k in ("cx", "cy", "rx", "ry"))
                box = Box((cx - rx) / width, (cy - ry) / height, 2 * rx / width, 2 * ry / height)
            else:
                points = [tuple(float(v) for v in pair.split(",")) for pair in data.get("points", "").split() if "," in pair]
                if not points:
                    continue
                xs, ys = [p[0] for p in points], [p[1] for p in points]
                box = Box(min(xs) / width, min(ys) / height, (max(xs) - min(xs)) / width, (max(ys) - min(ys)) / height)
        except (KeyError, ValueError):
            continue
        boxes.append(box.clamp())
    boxes = [box for box in boxes if box.width > 0 and box.height > 0][:MAX_SHAPES]
    return Occlusion(image, boxes, "enhanced") if boxes else None
