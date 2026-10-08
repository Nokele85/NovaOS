"""A comic page out of her panels (SPEC §7.6b) — the layout, and nothing else.

`show_picture` is one picture. Asked for a comic she had only that: one frame at
a time, each a different man in a different style, with no words in any of them
because an image model cannot letter, and no page that held them together. This
is the missing half, and it is deliberately not a model's job: the camera
renders the panels, and *this* cuts the page, draws the frames and sets her
words in real type — so the text is the text she wrote, in every panel, spelled
the way she spelled it.

Pure functions over bytes. The lab (world/selfies.py) decides what is rendered
and when; nothing here touches a GPU, a clock or a file.
"""
from __future__ import annotations

import io
import random
import re
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont

#: Fewest and most panels on one page. Two is the smallest thing that is a
#: sequence; past six the panels of a phone-sized page are too small to read.
MIN_PANELS, MAX_PANELS = 2, 6
#: Longest caption under one panel. A caption is a line or two, not a paragraph —
#: a page that is mostly type is a story with pictures, and `tell_story` exists.
MAX_TEXT = 220

#: Said before every panel when she names no style of her own: one hand, one
#: page. The "no text" half matters as much as the ink — letters a diffusion
#: model draws are noise, and the page sets the real ones underneath.
COMIC_STYLE = ("black and white ink comic panel, bold brush lines, strong "
               "contrast, hatched shadows, graphic novel art")
#: Kept out of every panel, added to whatever she asked to avoid.
COMIC_AVOID = ("text, letters, words, speech bubbles, captions, signature, "
               "watermark, panel borders, multiple panels, collage")

PAGE_W = 1600
MARGIN = 44
GUTTER = 26
FRAME = 6
INK = (16, 16, 16)
PAPER = (250, 248, 242)

#: What a panel is rendered at, by how many share its row: (width, height). One
#: is a wide establishing shot, two are squares, three are tall strips — all
#: three are shapes the model was trained on, so nothing is stretched.
WIDE, HALF, TALL = (1344, 768), (1024, 1024), (832, 1216)
SHAPES = {1: WIDE, 2: HALF, 3: TALL}

_FONTS = ("DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf", "arialbd.ttf",
          "DejaVuSans.ttf", "arial.ttf")


def arrangements(n: int) -> list[list[int]]:
    """Every way `n` panels can sit in rows of one, two or three.

    Left out: a page that is only wide panels (a column of pictures, not a
    page) and one with more than two of them (it runs off the phone)."""
    found: list[list[int]] = []

    def walk(left: int, so_far: list[int]) -> None:
        if not left:
            found.append(so_far)
            return
        for count in (1, 2, 3):
            if count <= left:
                walk(left - count, so_far + [count])

    walk(n, [])
    kept = [r for r in found if r.count(1) <= 2 and (len(r) == 1 or set(r) != {1})]
    return kept or found


def parse_layout(layout: str, n: int) -> list[int]:
    """Her own page: "1-2-2" is a wide panel over two rows of two.

    Raises ValueError, in words she can act on, when it does not add up."""
    parts = [int(x) for x in re.findall(r"\d+", layout or "")]
    if not parts or any(x not in (1, 2, 3) for x in parts):
        raise ValueError('layout is rows of 1, 2 or 3 panels, like "1-2-2"')
    if sum(parts) != n:
        raise ValueError(f'layout "{layout}" holds {sum(parts)} panels, but the '
                         f"page has {n} — make them match, or leave layout out")
    return parts


def rows(n: int, layout: str = "", seed: int | None = None) -> list[int]:
    """How `n` panels sit on the page: hers when she said, else one of the
    arrangements, picked by `seed` — so two pages are not cut the same way."""
    if n < 1:
        return []
    if (layout or "").strip():
        return parse_layout(layout, n)
    choices = arrangements(n)
    if seed is None:
        return choices[0]
    return random.Random(seed).choice(choices)


def panel_sizes(page: list[int]) -> list[tuple[int, int]]:
    """The size each panel should be rendered at, in reading order."""
    return [SHAPES[count] for count in page for _ in range(count)]


@lru_cache(maxsize=8)
def _font(size: int):
    for name in _FONTS:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)      # always there, if plainer


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, width: int) -> list[str]:
    """Greedy word wrap by measured width; a word wider than the line is cut."""
    lines: list[str] = []
    for para in text.split("\n"):
        line = ""
        for word in para.split():
            while draw.textlength(word, font=font) > width:      # one endless word
                cut = max(1, int(len(word) * width / draw.textlength(word, font=font)) - 1)
                if line:
                    lines.append(line)
                    line = ""
                lines.append(word[:cut])
                word = word[cut:]
            trial = f"{line} {word}".strip()
            if line and draw.textlength(trial, font=font) > width:
                lines.append(line)
                line = word
            else:
                line = trial
        if line:
            lines.append(line)
    return lines


def _fit(data: bytes, width: int, height: int) -> Image.Image:
    """The render, centre-cropped to the panel's shape and scaled to it."""
    img = Image.open(io.BytesIO(data)).convert("RGB")
    want = width / height
    w, h = img.size
    if w / h > want:
        new = int(h * want)
        img = img.crop(((w - new) // 2, 0, (w - new) // 2 + new, h))
    else:
        new = int(w / want)
        img = img.crop((0, (h - new) // 2, w, (h - new) // 2 + new))
    return img.resize((width, height), Image.LANCZOS)


def compose_page(panels: list[tuple[bytes, str]], title: str = "",
                 page: list[int] | None = None) -> bytes:
    """One PNG page from rendered panels and the words under each.

    `panels` is reading order: (image bytes, caption). A caption may be empty —
    a row of silent panels gets no band at all rather than an empty one.
    `page` is the rows (`rows()`); left out, the first arrangement."""
    n = len(panels)
    if not n:
        raise ValueError("a page needs at least one panel")
    probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    cap_font, title_font = _font(32), _font(58)
    pad, line_h = 16, 42
    inner = PAGE_W - 2 * MARGIN

    # measure first: a row is as tall as its tallest caption needs
    plan, i, y = [], 0, MARGIN
    title = " ".join((title or "").split())
    title_lines = _wrap(probe, title, title_font, inner) if title else []
    if title_lines:
        y += len(title_lines) * 70 + 22
    page = page or rows(n)
    if sum(page) != n:
        raise ValueError(f"the layout holds {sum(page)} panels, the page has {n}")
    for count in page:
        width = (inner - (count - 1) * GUTTER) // count
        height = round(width * SHAPES[count][1] / SHAPES[count][0])
        cells = []
        for k in range(count):
            data, text = panels[i]
            lines = _wrap(probe, " ".join((text or "").split()), cap_font,
                          width - 2 * pad - 2 * FRAME)
            cells.append((MARGIN + k * (width + GUTTER), data, lines))
            i += 1
        band = max(len(lines) for _, _, lines in cells)
        band = band * line_h + 2 * pad if band else 0
        plan.append((y, width, height, band, cells))
        y += height + band + GUTTER
    page_h = y - GUTTER + MARGIN

    page = Image.new("RGB", (PAGE_W, page_h), PAPER)
    draw = ImageDraw.Draw(page)
    ty = MARGIN
    for line in title_lines:
        draw.text((MARGIN, ty), line, font=title_font, fill=INK)
        ty += 70
    for top, width, height, band, cells in plan:
        for x, data, lines in cells:
            page.paste(_fit(data, width, height), (x, top))
            if band:
                draw.rectangle((x, top + height, x + width, top + height + band),
                               fill=(255, 255, 255))
                draw.line((x, top + height, x + width, top + height),
                          fill=INK, width=FRAME // 2)
                ly = top + height + pad
                for line in lines:
                    draw.text((x + pad + FRAME, ly), line, font=cap_font, fill=INK)
                    ly += line_h
            draw.rectangle((x, top, x + width, top + height + band),
                           outline=INK, width=FRAME)
    out = io.BytesIO()
    page.save(out, format="PNG", optimize=True)
    return out.getvalue()
