#!/usr/bin/env python3
"""Pixel-drawing primitives for the LED matrix: a tiny font and LED digits.

Deliberately knows nothing about Spotify or trains - it just puts pixels on a
PIL draw surface, so the commute screen, a digital clock and a weather readout
can all share it.
"""
from __future__ import annotations

from PIL import ImageDraw

# 5-row bitmap font. Width varies per glyph, which is not a nicety: at a fixed
# 3px, N reads as M, and M and W are indistinguishable from N and U. Confirmed
# by rendering them, not by assumption. Only the characters the screens
# actually use are defined; adding a glyph is a one-line change.
GLYPHS: dict[str, list[str]] = {
    "A": ["010", "101", "111", "101", "101"],
    "B": ["110", "101", "110", "101", "110"],
    "C": ["011", "100", "100", "100", "011"],
    "D": ["110", "101", "101", "101", "110"],
    "E": ["111", "100", "110", "100", "111"],
    "H": ["101", "101", "111", "101", "101"],
    "I": ["1", "1", "1", "1", "1"],
    "L": ["100", "100", "100", "100", "111"],
    # N needs a 4th column for its diagonal, or it reads as M or H.
    "N": ["1001", "1101", "1011", "1001", "1001"],
    "O": ["111", "101", "101", "101", "111"],
    # "0" is identical to "O": at 3x5, the only distinguishing feature would be
    # an interior pixel, which reads as an 8. Every time on the commute screen
    # starts with a 0, and they never appear ambiguously, so save the confusion.
    "0": ["111", "101", "101", "101", "111"],
    "P": ["110", "101", "110", "100", "100"],
    "R": ["110", "101", "110", "101", "101"],
    "S": ["011", "100", "010", "001", "110"],
    "T": ["111", "010", "010", "010", "010"],
    # V drops its stems a row earlier than W so the two don't collide.
    "V": ["101", "101", "101", "010", "010"],
    "W": ["10001", "10001", "10101", "11011", "10001"],
    "Y": ["101", "101", "010", "010", "010"],
    "1": ["1", "1", "1", "1", "1"],
    "2": ["111", "001", "111", "100", "111"],
    "3": ["111", "001", "111", "001", "111"],
    "4": ["101", "101", "111", "001", "001"],
    "5": ["111", "100", "111", "001", "111"],
    "6": ["111", "100", "111", "101", "111"],
    "7": ["111", "001", "010", "010", "010"],
    "8": ["111", "101", "111", "101", "111"],
    "9": ["111", "101", "111", "001", "111"],
    " ": ["0", "0", "0", "0", "0"],
}

CHAR_SPACING = 1


def glyph_width(char: str) -> int:
    return len(GLYPHS[char][0])


def text_width(text: str) -> int:
    if not text:
        return 0
    return sum(glyph_width(char) + CHAR_SPACING for char in text) - CHAR_SPACING


def draw_text(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    text: str,
    color: tuple[int, int, int],
) -> int:
    """Draw `text` with its top-left at (x, y); return the x just past it."""
    for char in text:
        glyph = GLYPHS[char]
        for row, bits in enumerate(glyph):
            for col, bit in enumerate(bits):
                if bit == "1":
                    draw.point((x + col, y + row), fill=color)
        x += len(glyph[0]) + CHAR_SPACING
    return x


def draw_text_centred(
    draw: ImageDraw.ImageDraw,
    y: int,
    text: str,
    color: tuple[int, int, int],
    width: int,
) -> None:
    draw_text(draw, (width - text_width(text)) // 2, y, text, color)


# Seven-segment digits, drawn as rectangles rather than a second bitmap font.
# Parametric in size, and the shape reads as a departure board, which is
# exactly the association the commute screen wants.
#
#   aaa
#  f   b
#   ggg
#  e   c
#   ddd
SEGMENTS: dict[str, str] = {
    "0": "abcdef",
    "2": "abged",
    "3": "abgcd",
    "4": "fgbc",
    "5": "afgcd",
    "6": "afgedc",
    "7": "abc",
    "8": "abcdefg",
    "9": "abcfgd",
}


def seven_seg_width(digit: str, w: int, t: int) -> int:
    """Advance width. '1' is a bare stem, so it gets a narrow cell."""
    return t if digit == "1" else w


def draw_seven_seg(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    w: int,
    h: int,
    digit: str,
    color: tuple[int, int, int],
    t: int = 2,
) -> None:
    if digit == "1":
        # A true seven-segment '1' lights segments b and c, which sit at
        # opposite edges of the cell - so "11" reads as four separate posts.
        # One stem is what people actually recognise as a 1.
        draw.rectangle((x, y, x + t - 1, y + h - 1), fill=color)
        return

    mid = y + (h - t) // 2
    bars = {
        "a": (x + t, y, x + w - 1 - t, y + t - 1),
        "b": (x + w - t, y + t, x + w - 1, mid - 1),
        "c": (x + w - t, mid + t, x + w - 1, y + h - 1 - t),
        "d": (x + t, y + h - t, x + w - 1 - t, y + h - 1),
        "e": (x, mid + t, x + t - 1, y + h - 1 - t),
        "f": (x, y + t, x + t - 1, mid - 1),
        "g": (x + t, mid, x + w - 1 - t, mid + t - 1),
    }
    for segment in SEGMENTS[digit]:
        draw.rectangle(bars[segment], fill=color)


def big_number_width(value: int, w: int, t: int, gap: int) -> int:
    digits = str(value)
    return sum(seven_seg_width(d, w, t) for d in digits) + gap * (len(digits) - 1)


def draw_big_number(
    draw: ImageDraw.ImageDraw,
    value: int,
    y: int,
    color: tuple[int, int, int],
    width: int,
    w: int = 11,
    h: int = 17,
    t: int = 2,
    gap: int = 3,
) -> None:
    digits = str(value)
    x = (width - big_number_width(value, w, t, gap)) // 2
    for digit in digits:
        draw_seven_seg(draw, x, y, w, h, digit, color, t)
        x += seven_seg_width(digit, w, t) + gap
