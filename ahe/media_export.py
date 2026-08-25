"""Collecting the media an export references.

The old implementation pulled every file through AnkiConnect's
``retrieveMediaFile`` and threw away anything that did not look like an image.
Running inside Anki we can simply copy from ``col.media.dir()``, which is both
much faster and keeps audio, video, fonts and SVG intact.
"""

from __future__ import annotations

import base64
import mimetypes
import os
import re
import shutil
import urllib.parse
from html import unescape as _unescape
from pathlib import Path

MEDIA_SUBDIR = "media"

# What counts as "not a file in collection.media" and is therefore left to the
# browser rather than looked up locally.
_REMOTE_RE = re.compile(r"(?i)^(https?|ftp|data|mailto):")

# What may actually be fetched. Deliberately narrower than _REMOTE_RE: an
# ftp: reference is still passed through untouched, it is just never retrieved.
_FETCHABLE_RE = re.compile(r"(?i)^https?://")


# Anki's own reference patterns (MediaManager.regexps) plus the element types
# a hand written template may use that Anki itself never emits.
_REFERENCE_RES = [
    re.compile(r"(?i)(\[sound:(?P<fname>[^]]+)\])"),
    re.compile(
        r"(?i)(<(?:img|audio|video|source|track|embed|input|script)\b[^>]* src="
        r"(?P<str>[\"'])(?P<fname>[^>]+?)(?P=str)[^>]*>)"
    ),
    re.compile(
        r"(?i)(<(?:img|audio|video|source|track|embed|input|script)\b[^>]* src="
        r"(?!['\"])(?P<fname>[^ >]+)[^>]*?>)"
    ),
    re.compile(r"(?i)(<object\b[^>]* data=(?P<str>[\"'])(?P<fname>[^>]+?)(?P=str)[^>]*>)"),
    re.compile(r"(?i)(<object\b[^>]* data=(?!['\"])(?P<fname>[^ >]+)[^>]*?>)"),
    re.compile(r"(?i)(<(?:a|link)\b[^>]* href=(?P<str>[\"'])(?P<fname>[^>]+?)(?P=str)[^>]*>)"),
]

# The answer of a cloze deletion is markup, and Anki keeps it escaped inside
# an attribute -- so an image used as the answer is a media reference the
# patterns above cannot see.
_DATA_CLOZE_RE = re.compile(r"""(?i)(?P<lead>\bdata-cloze=)(?P<q>["'])(?P<value>[^"']*)(?P=q)""")

_CSS_URL_RE = re.compile(r"""(?i)url\(\s*(?P<q>["']?)(?P<fname>[^)"']+)(?P=q)\s*\)""")

_SOUND_TAG_RE = re.compile(r"(?i)\[sound:(?P<fname>[^]]+)\]")

# Inline scripts only: a <script src="_helper.js"> points at a media file and
# has to be rewritten like any other reference.
_SCRIPT_RE = re.compile(r"(?is)<script\b(?![^>]*\bsrc\s*=)[^>]*>.*?</script\s*>")

_AUDIO_EXTS = {".mp3", ".ogg", ".oga", ".wav", ".m4a", ".flac", ".opus", ".aac", ".weba"}
_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".mpg", ".mpeg", ".m4v", ".ogv"}


def is_remote(url: str) -> bool:
    return bool(_REMOTE_RE.match(url.strip()))


def media_kind(filename: str) -> str:
    """``"audio"``, ``"video"`` or ``"file"`` for a media filename."""
    ext = os.path.splitext(filename)[1].lower()
    if ext in _AUDIO_EXTS:
        return "audio"
    if ext in _VIDEO_EXTS:
        return "video"
    return "file"


class MediaCollector:
    """Resolves media references and copies (or inlines) the files."""

    def __init__(
        self,
        media_dir: str,
        dest_dir: Path | None,
        *,
        inline: bool = False,
        download_remote: bool = False,
        link_prefix: str | None = None,
    ) -> None:
        self.media_dir = media_dir
        self.dest_dir = dest_dir
        self.inline = inline
        self.download_remote = download_remote
        # Set by the live view, which has a server to hand and therefore needs
        # no copy: the file stays in collection.media and is served from there.
        self.link_prefix = link_prefix

        self._urls: dict[str, str] = {}
        # built on first use, and only when remote media is actually wanted
        self._client = None
        self.missing: set[str] = set()
        self.copied_bytes = 0

    @property
    def copied(self) -> int:
        return sum(1 for url in self._urls.values() if url)

    # Resolving a single reference
    ##################################################################

    def url_for(self, reference: str) -> str:
        """Map a media reference onto its location in the export.

        Unknown or remote references are handed back unchanged so the export
        degrades the same way the reviewer does.
        """
        reference = reference.strip()
        if not reference or reference.startswith("#"):
            return reference
        if is_remote(reference):
            return self._remote_url(reference)
        if reference.startswith(f"{MEDIA_SUBDIR}/") or (
            self.link_prefix and reference.startswith(self.link_prefix)
        ):
            # Already rewritten by us -- rewrite_html walks over the players it
            # just inserted, and those must not be treated as missing media.
            return reference

        cached = self._urls.get(reference)
        if cached is not None:
            return cached or reference

        source = self._source_path(reference)
        if source is None:
            if _is_flat_name(reference):
                self.missing.add(reference)
            self._urls[reference] = ""
            return reference

        try:
            url = self._store(source)
        except OSError:
            self.missing.add(reference)
            self._urls[reference] = ""
            return reference

        self._urls[reference] = url
        return url

    def _source_path(self, reference: str) -> Path | None:
        """Locate ``reference`` inside the collection's media folder."""
        candidates = [reference]
        unquoted = urllib.parse.unquote(reference)
        if unquoted != reference:
            candidates.append(unquoted)

        for candidate in candidates:
            name = candidate.split("?", 1)[0].split("#", 1)[0]
            # media filenames are flat; anything else is not ours to serve
            if not name or "/" in name or "\\" in name or name.startswith("."):
                continue
            path = Path(self.media_dir) / name
            if path.is_file():
                return path
        return None

    def _store(self, source: Path) -> str:
        if self.link_prefix is not None:
            return self.link_prefix + urllib.parse.quote(source.name)

        data_size = source.stat().st_size
        if self.inline:
            mime = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
            encoded = base64.b64encode(source.read_bytes()).decode("ascii")
            self.copied_bytes += data_size
            return f"data:{mime};base64,{encoded}"

        assert self.dest_dir is not None
        self.dest_dir.mkdir(parents=True, exist_ok=True)
        target = self.dest_dir / source.name
        if not target.exists() or target.stat().st_size != data_size:
            shutil.copyfile(source, target)
            self.copied_bytes += data_size
        return f"{MEDIA_SUBDIR}/{urllib.parse.quote(source.name)}"

    def _http(self):
        """Anki's own HTTP client, reused across the export.

        Downloading a picture a note points at is what the editor does when you
        paste a URL, so this goes through the same client: same user agent,
        same TLS handling -- including the ``ANKI_NOVERIFYSSL`` escape hatch
        that people on school and company networks depend on -- and one session
        for the whole export rather than a connection per file.
        """
        if self._client is None:
            from anki.httpclient import HttpClient

            self._client = HttpClient()
            self._client.timeout = 20
        return self._client

    def _remote_url(self, url: str) -> str:
        """Fetch a card's http(s) reference, if the user asked for that."""
        if not self.download_remote or not _FETCHABLE_RE.match(url):
            return url

        cached = self._urls.get(url)
        if cached is not None:
            return cached or url

        try:
            client = self._http()
            with client.get(url) as response:
                payload = client.stream_content(response)
        except Exception:
            self._urls[url] = ""
            return url

        name = os.path.basename(urllib.parse.urlparse(url).path) or "remote"
        name = re.sub(r"[^A-Za-z0-9._-]", "_", name)[:120]
        if self.inline:
            mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
            stored = f"data:{mime};base64,{base64.b64encode(payload).decode('ascii')}"
        else:
            assert self.dest_dir is not None
            self.dest_dir.mkdir(parents=True, exist_ok=True)
            target = self.dest_dir / f"remote_{abs(hash(url)) % (10**10)}_{name}"
            target.write_bytes(payload)
            stored = f"{MEDIA_SUBDIR}/{urllib.parse.quote(target.name)}"
        self.copied_bytes += len(payload)
        self._urls[url] = stored
        return stored

    # Rewriting documents
    ##################################################################

    def rewrite_html(self, html: str) -> str:
        if not html:
            return html
        # Script bodies are skipped: what looks like a reference in there is
        # usually a template literal (`src="${url}"`), and rewriting or
        # reporting those as missing media is just noise.
        chunks = []
        position = 0
        for match in _SCRIPT_RE.finditer(html):
            chunks.append(self._rewrite_markup(html[position : match.start()]))
            chunks.append(match.group(0))
            position = match.end()
        chunks.append(self._rewrite_markup(html[position:]))
        return "".join(chunks)

    def _rewrite_markup(self, html: str, nested: bool = False) -> str:
        if not html:
            return html
        html = self.sound_tags_to_players(html)
        for pattern in _REFERENCE_RES:
            html = pattern.sub(self._replace_reference, html)
        html = _CSS_URL_RE.sub(self._replace_css_url, html)
        return html if nested else _DATA_CLOZE_RE.sub(self._replace_data_cloze, html)

    def _replace_data_cloze(self, match: re.Match) -> str:
        """A cloze whose answer is a picture.

        Anki writes that answer into ``data-cloze`` as escaped markup, where it
        waits until the reader clicks the deletion open. In the reviewer the
        bare filename in it resolves against the media folder; in an export it
        has to be rewritten like any other reference, or the answer arrives as
        a broken image.
        """
        value = match.group("value")
        if "&" not in value:
            return match.group(0)
        inner = _unescape(value)
        rewritten = self._rewrite_markup(inner, nested=True)
        if rewritten == inner:
            return match.group(0)
        quote = match.group("q")
        return f"{match.group('lead')}{quote}{_attr(rewritten)}{quote}"

    def rewrite_css(self, css: str) -> str:
        if not css:
            return css
        return _CSS_URL_RE.sub(self._replace_css_url, css)

    def _replace_reference(self, match: re.Match) -> str:
        whole = match.group(0)
        try:
            reference = match.group("fname")
        except IndexError:
            return whole
        replacement = self.url_for(reference)
        if replacement == reference:
            return whole
        # Substitute in place; the filename may legitimately occur more than
        # once in the tag (eg. an alt attribute), so anchor on the last one.
        index = whole.rfind(reference)
        if index == -1:
            return whole
        return whole[:index] + replacement + whole[index + len(reference) :]

    def _replace_css_url(self, match: re.Match) -> str:
        reference = match.group("fname")
        replacement = self.url_for(reference)
        if replacement == reference:
            return match.group(0)
        quote = match.group("q") or '"'
        return f"url({quote}{replacement}{quote})"

    def sound_tags_to_players(self, html: str) -> str:
        """Turn raw ``[sound:...]`` tags into playable HTML5 elements.

        Rendered card sides never contain these -- the backend has already
        replaced them with AV references -- but raw field values do.
        """

        def repl(match: re.Match) -> str:
            return self.player_html(match.group("fname"))

        return _SOUND_TAG_RE.sub(repl, html)

    def player_html(self, filename: str) -> str:
        url = self.url_for(filename)
        kind = media_kind(filename)
        escaped = _attr(url)
        if kind == "video":
            return (
                f'<video class="ahe-av" controls preload="none" src="{escaped}">'
                f"</video>"
            )
        if kind == "audio":
            return (
                f'<audio class="ahe-av" controls preload="none" src="{escaped}">'
                f"</audio>"
            )
        return f'<a class="ahe-av ahe-av-file" href="{escaped}">{_text(filename)}</a>'


def _is_flat_name(reference: str) -> bool:
    """True for something that could be a media filename at all."""
    name = reference.split("?", 1)[0].split("#", 1)[0]
    return bool(name) and "/" not in name and "\\" not in name


def _attr(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _text(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
