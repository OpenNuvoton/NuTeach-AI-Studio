"""16x16 one-bit glyphs for the label characters outside ASCII.

An LCD-less board draws the predicted label into the camera frame (`numl_overlay.c`), and the
font inside that file only covers printable ASCII: a Chinese class name arrived there as UTF-8
bytes and came out as one `?` per byte. The firmware cannot carry a whole CJK font (the three
boards have 2 MiB of flash), so the Studio draws just the characters this project's labels use,
when it builds the firmware, and writes them into `Model/Labels.cpp` next to the labels
themselves.

The pixels come from a font that is already on the PC (nothing is bundled). A character none of
those fonts has gets no glyph, and the firmware draws a single `?` for it.

Pillow is imported inside the functions that draw, like every other heavy import under
`nuteach/mcu/`.
"""
from __future__ import annotations

import logging
import os
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)

#: Glyph cell height, and the widest a glyph may be. `NUML_OVERLAY_GLYPH_H` in numl_overlay.h.
CELL = 16

#: Fonts that come with Windows 10/11, in the order they are asked; one that is not installed
#: is skipped. Microsoft JhengHei comes first: of the fonts tried on 2026-10-02 it gave the most
#: legible one-bit Traditional Chinese at this size. The rest cover what it lacks
#: (Simplified-only forms, kana, hangul).
FONT_FILES = ("msjh.ttc", "mingliu.ttc", "msyh.ttc", "simsun.ttc", "msgothic.ttc", "malgun.ttf")

# Tall characters that fill a CJK font's design box. Where a font puts them decides which 16
# rows of its drawing become the cell; a font that has none of them is not used.
_REFERENCE = "國鬱龜"

# A code point no font maps: drawing it shows what a font draws for a character it lacks.
_UNMAPPED = "\U0010ffff"

# Characters that take no room of their own: combining marks, variation selectors, joiners.
_ZERO_WIDTH_CATEGORIES = frozenset({"Mn", "Me", "Cf"})

# Not characters a font is asked for: controls, line and paragraph separators, surrogates,
# private use, unassigned. The firmware draws `?` for them, as it does for an ASCII control.
_NEVER_DRAWN_CATEGORIES = frozenset({"Cc", "Zl", "Zp", "Cs", "Co", "Cn"})

# The drawing canvas: the pen starts one cell in from the left and the baseline sits two cells
# down, so a glyph that reaches outside its cell is still drawn whole, and the cell can be moved
# over it before it is cut out (see _cell_start).
_PEN_X = CELL
_BASELINE_Y = 2 * CELL
_CANVAS = (3 * CELL, 3 * CELL)

# What Pillow and FreeType raise for a font, or one glyph of it, they cannot use.
_FONT_ERRORS = (OSError, ValueError)


def _cell_start(start: int, size: int, ink_low: int, ink_high: int) -> int:
    """Where a cell of `size` pixels starts so that the ink `[ink_low, ink_high)` is inside it.

    `start` is where the font's metrics put the cell. Hundreds of glyphs reach a pixel or two
    outside it at this size (a stroke left of the pen, an accent above a capital): the cell then
    moves by that much, which shows less than a missing stroke. Ink that cannot fit at all stays
    where it is and is cut.
    """
    if ink_high - ink_low > size:
        return start
    if ink_low < start:
        return ink_low
    if ink_high > start + size:
        return ink_high - size
    return start


@dataclass(frozen=True)
class Glyph:
    code_point: int
    width: int  # pixels the pen advances, 0..CELL; 0 draws nothing
    rows: tuple[int, ...]  # CELL values, bit 15 = leftmost pixel


def wide_characters(labels: Iterable[str]) -> list[str]:
    """The characters outside ASCII that `labels` use, each once, in code point order."""
    return sorted({ch for label in labels for ch in str(label) if ord(ch) >= 0x80})


def fonts_dir() -> Path:
    root = os.environ.get("WINDIR") or os.environ.get("SystemRoot") or r"C:\Windows"
    return Path(root) / "Fonts"


class _Face:
    """One font at `CELL` pixels: draws a character into a cell, or says it has no such glyph."""

    def __init__(self, path: Path) -> None:
        from PIL import ImageFont

        if not path.is_file():
            raise OSError(f"{path.name} is not installed")
        self.name = path.name
        self._font = ImageFont.truetype(
            str(path), CELL, layout_engine=ImageFont.Layout.BASIC
        )
        # Pillow looks a file it cannot open up by name in the system font folder. This class
        # is asked for one file and must not answer with another.
        opened = os.path.normcase(os.path.abspath(str(self._font.path)))
        if opened != os.path.normcase(os.path.abspath(path)):
            raise OSError(f"{path.name} could not be opened")
        self._missing = self._draw(_UNMAPPED).tobytes()
        boxes = [
            box
            for box in (self._draw(ch).getbbox() for ch in _REFERENCE if self.has(ch))
            if box is not None
        ]
        if not boxes:
            raise OSError(f"{path.name} has none of the reference characters")
        top = min(box[1] for box in boxes)
        height = max(box[3] for box in boxes) - top
        # Centre the reference characters in the cell; an odd spare row goes below them.
        self._crop_y = top - max(0, (CELL - height) // 2)

    def _draw(self, ch: str) -> Any:
        from PIL import Image, ImageDraw

        canvas = Image.new("1", _CANVAS, 0)
        draw = ImageDraw.Draw(canvas)
        draw.fontmode = "1"  # one bit per pixel, no anti-aliasing
        draw.text((_PEN_X, _BASELINE_Y), ch, font=self._font, fill=1, anchor="ls")
        return canvas

    def has(self, ch: str) -> bool:
        return self._draw(ch).tobytes() != self._missing

    def advance(self, ch: str) -> int:
        return max(1, min(CELL, round(self._font.getlength(ch))))

    def glyph(self, ch: str) -> Glyph | None:
        if not self.has(ch):
            return None
        width = self.advance(ch)
        drawing = self._draw(ch)
        ink = drawing.getbbox()
        if ink is None:
            return Glyph(ord(ch), width, (0,) * CELL)
        left = _cell_start(_PEN_X, width, ink[0], ink[2])
        top = _cell_start(self._crop_y, CELL, ink[1], ink[3])
        pixels = drawing.load()
        rows = tuple(
            sum(0x8000 >> x for x in range(width) if pixels[left + x, top + y])
            for y in range(CELL)
        )
        return Glyph(ord(ch), width, rows)


def _load_faces() -> list[_Face]:
    faces: list[_Face] = []
    folder = fonts_dir()
    for name in FONT_FILES:
        try:
            faces.append(_Face(folder / name))
        except _FONT_ERRORS:
            # Not installed, not readable, or not a font Pillow can open: the next one is asked.
            LOGGER.debug("label font not usable: %s", name, exc_info=True)
    return faces


def _drawn(faces: list[_Face], ch: str, blank: bool) -> Glyph | None:
    """`ch` from the first face that can draw it; a face that fails on it is passed over."""
    for face in faces:
        try:
            if blank:
                return Glyph(ord(ch), face.advance(ch), (0,) * CELL)
            glyph = face.glyph(ch)
        except _FONT_ERRORS:
            # A damaged glyph in an otherwise usable font ("invalid outline"). A label that
            # shows `?` is better than firmware that cannot be built.
            LOGGER.debug("%s cannot draw U+%04X", face.name, ord(ch), exc_info=True)
            continue
        if glyph is not None:
            return glyph
    return None


def render(labels: Iterable[str]) -> tuple[tuple[Glyph, ...], tuple[str, ...]]:
    """Draw every character outside ASCII that `labels` use.

    Returns `(glyphs, missing)`: the glyphs in ascending code point order (the firmware
    bisects), and the characters that got none because no font on this PC has them.
    """
    characters = wide_characters(labels)
    if not characters:
        return (), ()
    faces = _load_faces()
    glyphs: list[Glyph] = []
    missing: list[str] = []
    for ch in characters:
        category = unicodedata.category(ch)
        if category in _ZERO_WIDTH_CATEGORIES:
            glyphs.append(Glyph(ord(ch), 0, (0,) * CELL))
            continue
        glyph = None
        if category not in _NEVER_DRAWN_CATEGORIES:
            # A space is blank in every font, and so is what some fonts draw for a character
            # they lack; only its width is worth asking for.
            glyph = _drawn(faces, ch, blank=category == "Zs")
        if glyph is None:
            missing.append(ch)
        else:
            glyphs.append(glyph)
    if missing:
        LOGGER.warning(
            "no font in %s draws %s; the board will show '?' there",
            fonts_dir(),
            " ".join(f"U+{ord(ch):04X}" for ch in missing),
        )
    return tuple(glyphs), tuple(missing)


def _code_point_list(characters: Iterable[str]) -> str:
    return " ".join(f"U+{ord(ch):04X}" for ch in characters)


def cpp_text(glyphs: tuple[Glyph, ...], missing: tuple[str, ...] = ()) -> str:
    """The glyph table as C++ source, to follow the label strings in `Labels.cpp`.

    Empty when there is nothing to say, so the `Labels.cpp` of a project whose labels are all
    ASCII stays what it always was. The four names are declared in `numl_overlay.h`, which also
    describes the layout -- as weak references, so a project without a table still links.
    """
    lines: list[str] = []
    if missing:
        lines += [
            "",
            f"/* No font on the PC that built this firmware has {_code_point_list(missing)};",
            " * a label that uses one of them is drawn with '?' in its place. */",
        ]
    if not glyphs:
        return "\n".join(lines) + "\n" if lines else ""
    lines += [
        "",
        "/* Glyphs for the label characters outside ASCII, drawn by the Studio when it built this",
        " * firmware. Firmware that draws a label as pixels (numl_overlay.c) looks them up by code",
        " * point. Code points ascend; a width is the pixels to advance (0 draws nothing); each",
        f" * glyph has {CELL} rows, bit 15 = leftmost pixel. */",
        "#include <stdint.h>",
        "",
        'extern "C" {',
        "",
        f"extern const uint32_t g_u32LabelGlyphCount = {len(glyphs)}u;",
        "",
        "extern const uint32_t g_au32LabelGlyphCodePoints[] =",
        "{",
    ]
    lines += [f"    0x{glyph.code_point:04X}u," for glyph in glyphs]
    lines += ["};", "", "extern const uint8_t g_au8LabelGlyphWidths[] =", "{"]
    lines += [f"    {glyph.width}u," for glyph in glyphs]
    lines += ["};", "", "extern const uint16_t g_au16LabelGlyphRows[] =", "{"]
    for glyph in glyphs:
        shown = f" {chr(glyph.code_point)}" if glyph.width else ""
        lines.append(f"    /* U+{glyph.code_point:04X}{shown} */")
        for start in range(0, CELL, 8):
            chunk = ", ".join(f"0x{row:04X}" for row in glyph.rows[start : start + 8])
            lines.append(f"    {chunk},")
    lines += ["};", "", '} /* extern "C" */', ""]
    return "\n".join(lines)
