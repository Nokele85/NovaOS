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
#: page. Deliberately not "comic panel" — the first real page (2026-10-08) took
#: that word literally and drew panels inside the panels, half of them in ink
#: and half in colour. One scene, one look, and what the camera is good at.
COMIC_STYLE = ("a single scene, graphic novel illustration, clean ink "
               "outlines, cinematic light, muted colours, highly detailed")
#: Kept out of every panel, added to whatever she asked to avoid.
COMIC_AVOID = ("text, letters, words, speech bubbles, captions, signature, "
               "watermark, panel borders, multiple panels, split image, inset, "
               "collage, picture in picture")

#: How often one panel may be drawn again after the check refused it, and how
#: many redraws a whole page may spend. A redraw is a minute of GPU; a page that
#: cannot be got right in these is sent with its doubts named (§7.6b).
PANEL_REDRAWS, PAGE_REDRAWS = 2, 4

#: What the camera is told about how many people are in a panel — the one thing
#: a text-to-image model gets wrong most, and the thing a reader forgives least.
#: "Figures", not "people": the second of her cast was a dog, and "exactly two
#: people" is how a dog gets drawn as a man.
_COUNT = {0: "No people and no animals anywhere in this picture, the scene is empty",
          1: "Exactly one figure in this picture, nobody else",
          2: "Exactly two figures in this picture, nobody else",
          3: "Exactly three figures in this picture, nobody else"}
_NOT = {0: "people, person, human, figure, silhouette, character, animal",
        1: "second figure, second person, extra person, crowd, duplicate",
        2: "third figure, third person, crowd, duplicate",
        3: "crowd"}

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
    if parts.count(1) > 2:
        raise ValueError(f'layout "{layout}" has {parts.count(1)} wide panels; a '
                         "page holds two at most — put the others in rows of 2 "
                         'or 3, like "1-2-1", or leave layout out')
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


#: The name she is in the cast under. Her look is the camera's to know — the
#: same locked identity a selfie is rendered from — so a comic can have her in
#: it, recognisably her, without her describing herself differently each time.
SELF = "me"


def with_her(named: dict[str, str], panels: list[dict], name: str,
             identity: str) -> tuple[dict[str, str], list[dict]]:
    """Her cast and panels with `me` resolved to who she is (§7.6b).

    `me` becomes her own name, and its look becomes her identity followed by
    whatever she added for this page (what she is wearing, say). Panels that
    list `me` list her name instead. A page she is not in comes back as it was."""
    name = (name or "").strip() or "the heroine"
    mine = [k for k in named if k.casefold() in (SELF, name.casefold())]
    asked = any(str(w).strip().casefold() in (SELF, name.casefold())
                for p in panels for w in (p.get("who") or []) if isinstance(p.get("who"), list))
    if not mine and not asked:
        return named, panels
    extra = " ".join(named[k] for k in mine if named[k])
    out = {k: v for k, v in named.items() if k not in mine}
    out = {name: ". ".join(x.strip().rstrip(".") for x in (identity, extra) if x and x.strip()),
           **out}
    fixed = []
    for panel in panels:
        who = panel.get("who")
        if isinstance(who, list):
            panel = {**panel, "who": [name if str(w).strip().casefold() in (SELF, name.casefold())
                                      else w for w in who]}
        fixed.append(panel)
    return out, fixed


_LABEL = re.compile(r"(?:^|(?<=[.;])\s+)([^\s.:;,][^.:;,]{0,38}):\s+")
_ARTICLE = re.compile(r"^(?:der|die|das|den|ein|eine|the|a|an)\s+", re.I)


_MONO = re.compile(r"black.{0,5}white|monochrome|no colou?r|schwarz.?wei|ohne farbe|"
                   r"keine farbe|s/w\b", re.I)
#: Said last, and kept out, when the page's look is black and white: a picture
#: that names "an orange and green cushion" is otherwise painted in orange and
#: green, whatever the style line in front of it said.
MONO_LAST = "Strictly monochrome: black ink on white only, no colour anywhere"
MONO_NOT = "colour, color, colourful, coloured, tinted, orange, red, green, blue, yellow"


def cast_of(cast) -> tuple[dict[str, str], str]:
    """Her cast as (named figures, plain description).

    `{"Fährmann": "old, long coat", …}` is the form that lets a panel hold only
    the figures that are in it. A plain string is the older form: one line that
    goes into every panel, which is what drew both men into all four panels of
    the first page."""
    if isinstance(cast, dict):
        named = {_ARTICLE.sub("", str(k).strip()): " ".join(str(v or "").split())
                 for k, v in cast.items() if str(k).strip()}
        return named, ""
    plain = " ".join(str(cast or "").split())
    # "Der Fährmann: hager, … . me: schlank, …" — the object, written as a
    # sentence. Read as what it means rather than poured into every panel.
    labels = list(_LABEL.finditer(plain))
    if labels and labels[0].start() == 0:
        named = {}
        for m, nxt in zip(labels, labels[1:] + [None]):
            look = plain[m.end():nxt.start() if nxt else len(plain)].strip(" .;")
            named[_ARTICLE.sub("", m.group(1)).strip()] = look
        return named, ""
    return {}, plain


def figures_in(panel: dict, named: dict[str, str]) -> list[str] | None:
    """Who is in this panel: her `who` when she gave one, else the figures the
    picture names. `None` is "not known" — nothing named, nothing listed — and
    must not be read as "nobody": a panel that says only "he turns his head" has
    someone in it."""
    if not named:
        return None
    who = panel.get("who")
    if isinstance(who, list):
        return [n for n in named if any(n.casefold() == str(w).strip().casefold()
                                        for w in who)]
    said = str(panel.get("picture") or "").casefold()
    # `me` is never found by mention: the two letters are inside "home" and "time"
    found = [n for n in named if n.casefold() != SELF and n.casefold() in said]
    return found or None


def panel_prompt(style: str, panel: dict, named: dict[str, str],
                 plain: str) -> tuple[str, str]:
    """(what the camera is asked for, what it is told to leave out) for one panel.

    Only the figures that are in the panel are described, each under its name,
    and the head-count is said in words — so a wide shot of an empty boat is not
    handed two men to put somewhere."""
    parts = [style, str(panel.get("picture") or "")]
    leave_out = ""
    figures = figures_in(panel, named)
    if figures is not None:
        parts += [f"{name}: {named[name]}" for name in figures if named[name]]
        if len(figures) in _COUNT:
            parts.append(_COUNT[len(figures)])
            leave_out = _NOT[len(figures)]
    elif plain:
        parts.append(plain)
    if _MONO.search(style or ""):
        parts.append(MONO_LAST)
        leave_out = ", ".join(x for x in (leave_out, MONO_NOT) if x)
    return ". ".join(p.strip().rstrip(".") for p in parts if p and p.strip()) + ".", leave_out


def check_question(style: str, panel: dict, named: dict[str, str]) -> str:
    """What the reader with eyes is asked about one drawn panel (§7.6b)."""
    figures = figures_in(panel, named)
    if figures is None:
        people = ""
    elif not figures:
        people = " Nobody should be visible in it."
    else:
        people = (f" The figures visible in it should be exactly {len(figures)}: "
                  + ", ".join(f"{n} ({named[n]})" if named[n] else n for n in figures) + ".")
    return (
        "This image was drawn as one panel of a comic. It should show: "
        f"«{panel.get('picture', '')}».{people} The look asked for: {style}.\n"
        "Does the image clearly contradict that — a wrong number of people, "
        "something essential missing, or the opposite of what was asked (a boat "
        "that should be empty with someone in it; colour where black and white "
        "was asked)? Ignore small details, exact poses and drawing quality.\n"
        "Answer with ONE line: `OK`, or `WRONG: ` and what contradicts it in a "
        "few words.")


def passes(verdict: str) -> bool:
    """Did the check let the panel through? Anything that is not a plain
    refusal does: a reader that could not answer must not cost a redraw."""
    return not str(verdict or "").strip().upper().lstrip("`*\"' ").startswith("WRONG")


def english_request(lines: list[str]) -> str:
    """What the model is asked so the camera gets English (§7.6b)."""
    import json
    return ("Translate each string of this JSON array into plain, concrete English "
            "for an image generator. Keep names as they are, keep every detail, add "
            "nothing. Strings already in English stay as they are. Answer with the "
            "JSON array only, same length, same order.\n\n"
            + json.dumps(lines, ensure_ascii=False))


def english_answer(answer: str, lines: list[str]) -> list[str]:
    """The translated lines, or the originals when the answer is not usable."""
    import json
    try:
        found = json.loads(answer[answer.index("["):answer.rindex("]") + 1])
    except (ValueError, TypeError):
        return lines
    if (not isinstance(found, list) or len(found) != len(lines)
            or not all(isinstance(x, str) and (x.strip() or not src.strip())
                       for x, src in zip(found, lines))):
        return lines
    return [x.strip() for x in found]


def as_data_url(data: bytes, side: int = 768) -> str:
    """A panel small enough to show a model: a JPEG data URL."""
    import base64
    img = Image.open(io.BytesIO(data)).convert("RGB")
    img.thumbnail((side, side))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode()


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

    canvas = Image.new("RGB", (PAGE_W, page_h), PAPER)
    draw = ImageDraw.Draw(canvas)
    ty = MARGIN
    for line in title_lines:
        draw.text((MARGIN, ty), line, font=title_font, fill=INK)
        ty += 70
    for top, width, height, band, cells in plan:
        for x, data, lines in cells:
            canvas.paste(_fit(data, width, height), (x, top))
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
    canvas.save(out, format="PNG", optimize=True)
    return out.getvalue()
