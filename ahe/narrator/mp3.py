"""How long an MP3 plays, read off its frames.

Anki's Python has no audio library, and the length of a narration is needed
on the server side -- for the page's timing now, for the audiobook and video
exports later. MPEG audio is a sequence of frames each holding a fixed number
of samples at a fixed rate, so walking the headers and adding them up gives
the duration exactly, for constant and variable bit rates alike.
"""

from __future__ import annotations

from pathlib import Path

_BITRATES = {
    # (MPEG version 1 or 2, layer) -> kbps by bitrate index 1..14
    (1, 1): [32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448],
    (1, 2): [32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384],
    (1, 3): [32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320],
    (2, 1): [32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256],
    (2, 2): [8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
    (2, 3): [8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
}
_SAMPLE_RATES = {1: [44100, 48000, 32000], 2: [22050, 24000, 16000], 25: [11025, 12000, 8000]}


def duration(path: Path) -> float:
    try:
        data = path.read_bytes()
    except OSError:
        return 0.0
    pos = _skip_id3(data)
    seconds = 0.0
    frames = 0
    while pos + 4 <= len(data):
        header = int.from_bytes(data[pos : pos + 4], "big")
        frame = _frame(header)
        if frame is None:
            pos += 1  # not a frame boundary; resync
            continue
        length, samples, rate = frame
        if pos + 4 <= len(data) and frames == 0 and _is_info_frame(data, pos, length):
            pass  # a Xing/Info header carries no audio
        else:
            seconds += samples / rate
        frames += 1
        pos += length
    return round(seconds, 2)


def sample_rate(path: Path) -> int:
    """The rate of the first audio frame, or 0 if none is found."""
    try:
        data = path.read_bytes()
    except OSError:
        return 0
    pos = _skip_id3(data)
    while pos + 4 <= len(data):
        frame = _frame(int.from_bytes(data[pos : pos + 4], "big"))
        if frame is not None:
            return frame[2]
        pos += 1
    return 0


def _skip_id3(data: bytes) -> int:
    if data[:3] != b"ID3" or len(data) < 10:
        return 0
    size = 0
    for byte in data[6:10]:
        size = (size << 7) | (byte & 0x7F)
    return 10 + size


def _frame(header: int) -> tuple[int, int, int] | None:
    """(frame length in bytes, samples, sample rate) for a header, if valid."""
    if header & 0xFFE00000 != 0xFFE00000:
        return None
    version_bits = (header >> 19) & 3
    layer_bits = (header >> 17) & 3
    bitrate_index = (header >> 12) & 15
    rate_index = (header >> 10) & 3
    padding = (header >> 9) & 1
    if version_bits == 1 or layer_bits == 0 or bitrate_index in (0, 15) or rate_index == 3:
        return None
    version = {3: 1, 2: 2, 0: 25}[version_bits]
    layer = 4 - layer_bits  # 1, 2 or 3
    table_version = 1 if version == 1 else 2
    kbps = _BITRATES[(table_version, layer)][bitrate_index - 1]
    rate = _SAMPLE_RATES[version][rate_index]
    if layer == 1:
        samples = 384
        length = (12 * kbps * 1000 // rate + padding) * 4
    else:
        samples = 1152 if (layer == 2 or version == 1) else 576
        per_slot = 144 if (layer == 2 or version == 1) else 72
        length = per_slot * kbps * 1000 // rate + padding
    if length < 4:
        return None
    return length, samples, rate


def _is_info_frame(data: bytes, pos: int, length: int) -> bool:
    body = data[pos + 4 : pos + length]
    return b"Xing" in body or b"Info" in body
