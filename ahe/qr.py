"""A QR code, without a dependency to install.

Anki ships no QR library and an add-on is in no position to require one, so
this is the smallest encoder that covers what it is wanted for here: one
address, in byte mode, at error correction level M, in versions 1 to 10 --
which is up to 271 characters, where a live view's address is around sixty.

Almost nothing here is tabulated. The Reed-Solomon generator polynomials are
built from the field itself, and both the format and the version information
from their BCH codes, so there is no list of magic numbers to mistype. What is
left is one table -- how a version's codewords are divided into blocks -- and
the matrix checks it: the number of modules left over after the function
patterns are placed has to be exactly the number of codewords that table
claims. A wrong entry cannot survive that.
"""

from __future__ import annotations

# The field every QR code is computed in: GF(256) modulo x^8+x^4+x^3+x^2+1.
_PRIMITIVE = 0x11D

_EXP = [0] * 512
_LOG = [0] * 256

_value = 1
for _power in range(255):
    _EXP[_power] = _value
    _LOG[_value] = _power
    _value <<= 1
    if _value & 0x100:
        _value ^= _PRIMITIVE
for _power in range(255, 512):
    _EXP[_power] = _EXP[_power - 255]


def _mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _EXP[_LOG[a] + _LOG[b]]


def _generator(count: int) -> list[int]:
    """The polynomial (x - a^0)(x - a^1)... for ``count`` check symbols."""
    poly = [1]
    for power in range(count):
        product = [0] * (len(poly) + 1)
        for index, coefficient in enumerate(poly):
            product[index] ^= coefficient
            product[index + 1] ^= _mul(coefficient, _EXP[power])
        poly = product
    return poly


def _remainder(data: list[int], count: int) -> list[int]:
    """The Reed-Solomon check codewords for one block."""
    generator = _generator(count)
    working = list(data) + [0] * count
    for index in range(len(data)):
        factor = working[index]
        if not factor:
            continue
        for offset, coefficient in enumerate(generator):
            working[index + offset] ^= _mul(coefficient, factor)
    return working[len(data) :]


# Error correction level M: check codewords per block, then the blocks
# themselves as (how many, data codewords each). Checked against the matrix.
_BLOCKS = {
    1: (10, [(1, 16)]),
    2: (16, [(1, 28)]),
    3: (26, [(1, 44)]),
    4: (18, [(2, 32)]),
    5: (24, [(2, 43)]),
    6: (16, [(4, 27)]),
    7: (18, [(4, 31)]),
    8: (22, [(2, 38), (2, 39)]),
    9: (22, [(3, 36), (2, 37)]),
    10: (26, [(4, 43), (1, 44)]),
}

# Centre coordinates of the alignment patterns, per version.
_ALIGNMENT = {
    1: [],
    2: [6, 18],
    3: [6, 22],
    4: [6, 26],
    5: [6, 30],
    6: [6, 34],
    7: [6, 22, 38],
    8: [6, 24, 42],
    9: [6, 26, 46],
    10: [6, 28, 50],
}

MAX_VERSION = 10


class TooLong(ValueError):
    """The text does not fit in the versions this encoder covers."""


def _data_capacity(version: int) -> int:
    """Data codewords available in a version, across all its blocks."""
    _, groups = _BLOCKS[version]
    return sum(count * words for count, words in groups)


def _pick_version(length: int) -> int:
    for version in range(1, MAX_VERSION + 1):
        header = 4 + (8 if version <= 9 else 16)
        if _data_capacity(version) * 8 >= header + length * 8:
            return version
    raise TooLong(f"{length} bytes is more than this encoder covers")


def _bitstream(payload: bytes, version: int) -> list[int]:
    """Mode, length, data, terminator and padding, as whole codewords."""
    bits: list[int] = []

    def push(value: int, width: int) -> None:
        for shift in range(width - 1, -1, -1):
            bits.append((value >> shift) & 1)

    push(0b0100, 4)  # byte mode
    push(len(payload), 8 if version <= 9 else 16)
    for byte in payload:
        push(byte, 8)

    capacity = _data_capacity(version) * 8
    push(0, min(4, capacity - len(bits)))  # terminator
    while len(bits) % 8:
        bits.append(0)

    codewords = [
        int("".join(str(bit) for bit in bits[index : index + 8]), 2)
        for index in range(0, len(bits), 8)
    ]
    # The two pad codewords the standard names, alternating.
    for index in range(_data_capacity(version) - len(codewords)):
        codewords.append(0xEC if index % 2 == 0 else 0x11)
    return codewords


def _interleave(codewords: list[int], version: int) -> list[int]:
    """Data and check codewords, woven so a burst of damage is spread out."""
    check_count, groups = _BLOCKS[version]

    blocks: list[list[int]] = []
    at = 0
    for count, words in groups:
        for _ in range(count):
            blocks.append(codewords[at : at + words])
            at += words

    checks = [_remainder(block, check_count) for block in blocks]

    result: list[int] = []
    for position in range(max(len(block) for block in blocks)):
        for block in blocks:
            if position < len(block):
                result.append(block[position])
    for position in range(check_count):
        for block in checks:
            result.append(block[position])
    return result


def _skeleton(version: int) -> tuple[list[list[bool]], list[list[bool]]]:
    """The function patterns, and a map of everything they occupy."""
    size = version * 4 + 17
    modules = [[False] * size for _ in range(size)]
    fixed = [[False] * size for _ in range(size)]

    def finder(top: int, left: int) -> None:
        # The ring of light around it is part of the pattern: it is what tells
        # a reader where the finder ends.
        for row in range(-1, 8):
            for col in range(-1, 8):
                r, c = top + row, left + col
                if not (0 <= r < size and 0 <= c < size):
                    continue
                ring = (0 <= row <= 6 and col in (0, 6)) or (
                    0 <= col <= 6 and row in (0, 6)
                )
                core = 2 <= row <= 4 and 2 <= col <= 4
                modules[r][c] = ring or core
                fixed[r][c] = True

    finder(0, 0)
    finder(0, size - 7)
    finder(size - 7, 0)

    centres = _ALIGNMENT[version]
    for row in centres:
        for col in centres:
            # The three corners already hold a finder.
            if (
                (row <= 8 and col <= 8)
                or (row <= 8 and col >= size - 9)
                or (row >= size - 9 and col <= 8)
            ):
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    modules[row + dr][col + dc] = max(abs(dr), abs(dc)) != 1
                    fixed[row + dr][col + dc] = True

    for index in range(size):
        for row, col in ((6, index), (index, 6)):
            if not fixed[row][col]:
                modules[row][col] = index % 2 == 0
                fixed[row][col] = True

    # The one module that is always dark, and the areas the format and version
    # information will be written into once the mask is known.
    modules[size - 8][8] = True
    fixed[size - 8][8] = True

    # Format information: an L around the top-left finder, and a second copy
    # split between the other two finders. Reserving whole rows instead would
    # silently eat data modules -- which is what the codeword count in
    # encode() is there to catch.
    for index in range(9):
        fixed[index][8] = True
        fixed[8][index] = True
    for index in range(size - 8, size):
        fixed[8][index] = True
    for index in range(size - 7, size):
        fixed[index][8] = True

    if version >= 7:
        for index in range(18):
            row, col = index // 3, index % 3
            fixed[row][size - 11 + col] = True
            fixed[size - 11 + col][row] = True

    return modules, fixed


def _place_data(
    modules: list[list[bool]], fixed: list[list[bool]], codewords: list[int]
) -> None:
    """Up one two-module column, down the next, skipping the timing column."""
    size = len(modules)
    bits = [(word >> shift) & 1 for word in codewords for shift in range(7, -1, -1)]
    at = 0
    col = size - 1
    upward = True
    while col > 0:
        if col == 6:  # the vertical timing pattern is not a column of data
            col -= 1
        for step in range(size):
            row = size - 1 - step if upward else step
            for c in (col, col - 1):
                if fixed[row][c]:
                    continue
                modules[row][c] = bool(bits[at]) if at < len(bits) else False
                at += 1
        col -= 2
        upward = not upward


def _masked(row: int, col: int, mask: int) -> bool:
    if mask == 0:
        return (row + col) % 2 == 0
    if mask == 1:
        return row % 2 == 0
    if mask == 2:
        return col % 3 == 0
    if mask == 3:
        return (row + col) % 3 == 0
    if mask == 4:
        return (row // 2 + col // 3) % 2 == 0
    if mask == 5:
        return (row * col) % 2 + (row * col) % 3 == 0
    if mask == 6:
        return ((row * col) % 2 + (row * col) % 3) % 2 == 0
    return ((row + col) % 2 + (row * col) % 3) % 2 == 0


def _bch(value: int, generator: int, width: int) -> int:
    """The remainder that turns a value into its BCH code word."""
    shifted = value << (width - 1)
    while shifted.bit_length() >= width:
        shifted ^= generator << (shifted.bit_length() - width)
    return shifted


def _write_format(modules: list[list[bool]], mask: int) -> None:
    size = len(modules)
    # Level M is 0b00; the mask follows it.
    data = mask
    code = ((data << 10) | _bch(data, 0b10100110111, 11)) ^ 0b101010000010010
    # The first copy runs down column 8 and then left along row 8; the second
    # is split, so that damage to any one finder still leaves a readable copy.
    for index in range(15):
        bit = bool((code >> index) & 1)
        if index < 6:
            modules[index][8] = bit
        elif index == 6:
            modules[7][8] = bit
        elif index == 7:
            modules[8][8] = bit
        elif index == 8:
            modules[8][7] = bit
        else:
            modules[8][14 - index] = bit

        if index < 8:
            modules[8][size - 1 - index] = bit
        else:
            modules[size - 15 + index][8] = bit


def _write_version(modules: list[list[bool]], version: int) -> None:
    if version < 7:
        return
    size = len(modules)
    code = (version << 12) | _bch(version, 0b1111100100101, 13)
    for index in range(18):
        bit = bool((code >> index) & 1)
        row, col = index // 3, index % 3
        modules[row][size - 11 + col] = bit
        modules[size - 11 + col][row] = bit


def _penalty(modules: list[list[bool]]) -> int:
    """How badly a masked matrix reads, by the standard's four measures."""
    size = len(modules)
    score = 0

    # 1: runs of the same colour, and 3: the finder-like sequence, both of
    # which are about a reader mistaking data for structure.
    finder = [True, False, True, True, True, False, True]
    for line in [modules[i] for i in range(size)] + [
        [modules[r][c] for r in range(size)] for c in range(size)
    ]:
        run = 1
        for index in range(1, size):
            if line[index] == line[index - 1]:
                run += 1
            else:
                if run >= 5:
                    score += 3 + (run - 5)
                run = 1
        if run >= 5:
            score += 3 + (run - 5)

        for index in range(size - 6):
            if line[index : index + 7] != finder:
                continue
            before = line[max(0, index - 4) : index]
            after = line[index + 7 : index + 11]
            if len(before) == 4 and not any(before):
                score += 40
            if len(after) == 4 and not any(after):
                score += 40

    # 2: blocks of one colour.
    for row in range(size - 1):
        for col in range(size - 1):
            corner = modules[row][col]
            if (
                modules[row][col + 1] == corner
                and modules[row + 1][col] == corner
                and modules[row + 1][col + 1] == corner
            ):
                score += 3

    # 4: an overall bias towards dark or light.
    dark = sum(1 for row in modules for cell in row if cell)
    percent = dark * 100 // (size * size)
    score += 10 * (abs(percent - 50) // 5)
    return score


def encode(text: str) -> list[list[bool]]:
    """The modules of a QR code for ``text``; True is dark.

    The matrix carries no quiet zone -- whatever draws it has to leave four
    modules of margin, or no reader will find it.
    """
    payload = text.encode("utf-8")
    version = _pick_version(len(payload))
    codewords = _interleave(_bitstream(payload, version), version)

    modules, fixed = _skeleton(version)

    free = sum(1 for row in range(len(fixed)) for col in fixed[row] if not col)
    check_count, groups = _BLOCKS[version]
    expected = sum(count * (words + check_count) for count, words in groups)
    if free // 8 != expected:
        raise AssertionError(
            f"version {version}: the matrix holds {free // 8} codewords, "
            f"the block table says {expected}"
        )

    _place_data(modules, fixed, codewords)

    best: list[list[bool]] | None = None
    best_score = -1
    for mask in range(8):
        candidate = [list(row) for row in modules]
        for row in range(len(candidate)):
            for col in range(len(candidate)):
                if not fixed[row][col] and _masked(row, col, mask):
                    candidate[row][col] = not candidate[row][col]
        _write_format(candidate, mask)
        _write_version(candidate, version)
        score = _penalty(candidate)
        if best is None or score < best_score:
            best, best_score = candidate, score

    assert best is not None
    return best


def as_svg(matrix: list[list[bool]], quiet: int = 4) -> str:
    """The code as an SVG, sized in modules so it is crisp at any size.

    Runs of dark modules become one rectangle each, which keeps a version 10
    code to a few kilobytes instead of three thousand rectangles.
    """
    size = len(matrix) + 2 * quiet
    rects = []
    for row, cells in enumerate(matrix):
        col = 0
        while col < len(cells):
            if not cells[col]:
                col += 1
                continue
            start = col
            while col < len(cells) and cells[col]:
                col += 1
            rects.append(
                f'<rect x="{start + quiet}" y="{row + quiet}"'
                f' width="{col - start}" height="1"/>'
            )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}"'
        ' shape-rendering="crispEdges" role="img" aria-label="QR code">'
        f'<rect width="{size}" height="{size}" fill="#fff"/>'
        f'<g fill="#000">{"".join(rects)}</g></svg>'
    )


def as_text(matrix: list[list[bool]]) -> str:
    """The code as characters, for looking at it without a screen."""
    quiet = "  " * (len(matrix) + 8)
    lines = [quiet, quiet]
    for row in matrix:
        lines.append("    " + "".join("██" if cell else "  " for cell in row) + "    ")
    lines.extend([quiet, quiet])
    return "\n".join(lines)
