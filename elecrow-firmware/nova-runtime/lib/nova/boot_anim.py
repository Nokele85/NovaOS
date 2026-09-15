# "Nova erwacht" - her start-up animation, about four and a half seconds.
#
# A nova is a star that suddenly flares; she listens "at the edges of the deep
# net", is "faintly luminous", has eyes "that warm rather than glow" and wears
# "a single subtle indicator stripe" (vault/soul/PERSONA.md, card). So: a dark
# sky with far stars -> one point that pulses like a heartbeat -> the flare,
# with rays and a shock ring cooling from white to her pink to violet -> two
# warm eyes open out of the afterglow and blink -> her name, letter by letter,
# and the stripe drawing itself underneath.
#
# Everything here is framebuf primitives and one full-frame push per frame
# (~110 ms at 10 MHz). A touch skips to the last frame; any exception is the
# caller's to swallow - an animation must never cost her the boot.
import math
import random
from array import array

from . import board as B
from .screen import rgb

W, H = B.LCD_W, B.LCD_H
CX, CY = W // 2, 104

SKY = (6, 4, 16)
WHITE = (255, 250, 245)
PINK = (255, 120, 200)
VIOLET = (120, 60, 200)
WARM = (255, 250, 252)          # the sclera, near white as on the reference sheet
LASH = (18, 8, 20)              # the lid line is nearly black, and heavy
IRIS_RIM = (118, 12, 58)        # limbal ring and the shadow the lid throws
IRIS_DEEP = (222, 32, 96)
IRIS_LIT = (255, 150, 196)
PUPIL = (24, 6, 26)
SKIN = (252, 232, 230)          # "faintly luminous", not tanned
BLUSH = (255, 158, 186)
HAIR = (44, 26, 54)
MOUTH = (196, 96, 124)
FACE_Y, FACE_RX, FACE_RY = 88, 102, 100
STRIPE = (80, 220, 255)
NAME_Y = 196                    # 4x8 pixels tall, so it ends at 228
STRIPE_Y = 232


def _mix(a, b, t):
    t = 0.0 if t < 0 else 1.0 if t > 1 else t
    return (int(a[0] + (b[0] - a[0]) * t), int(a[1] + (b[1] - a[1]) * t),
            int(a[2] + (b[2] - a[2]) * t))


def _dim(c, k):
    return (int(c[0] * k), int(c[1] * k), int(c[2] * k))


def _c(c):
    return rgb(c[0], c[1], c[2])


class _Scene:
    def __init__(self, d, io, touch):
        self.d, self.fb, self.io, self.touch = d, d.fb, io, touch
        self.stars = [(random.randint(0, W - 1), random.randint(0, H - 1),
                       random.randint(30, 150)) for _ in range(46)]
        self.skipped = False
        self._down = 0

    def sky(self, dim=1.0):
        self.fb.fill(_c(SKY))
        for x, y, b in self.stars:
            v = int(b * dim * (0.55 + 0.45 * random.random()))
            self.fb.pixel(x, y, rgb(v, v, min(255, v + 35)))

    def lamp(self, c, k=1.0):
        if self.io is not None:
            r, g, b = _dim(c, k)
            self.io.rgb(r, g, b)

    def frame(self):
        self.d.show()
        # Two readings in a row, because the CST816S answers with nonsense for
        # the first moments after power-up (Rook's note, and it skipped the
        # animation at every boot on 2026-09-12).
        if self.touch is not None:
            if self.touch.read() is not None:
                self._down += 1
                if self._down >= 2:
                    self.skipped = True
            else:
                self._down = 0
        return not self.skipped

    # ---- the five beats ----

    def darkness(self):
        for _ in range(5):
            self.sky(0.8)
            if not self.frame():
                return

    def heartbeat(self):
        for i in range(10):
            beat = abs(math.sin(i * math.pi / 5))
            self.sky(0.8)
            r = 1 + int(3 * beat)
            self.fb.ellipse(CX, CY, r + 3, r + 3, _c(_dim(PINK, 0.25 * beat)), True)
            self.fb.ellipse(CX, CY, r, r, _c(_mix(PINK, WHITE, beat)), True)
            self.lamp(PINK, 0.15 * beat)
            if not self.frame():
                return

    def flare(self):
        for i in range(16):
            t = i / 15
            self.sky(0.8 - 0.4 * t)
            ring = _mix(_mix(WHITE, PINK, t * 1.6), VIOLET, (t - 0.6) * 2.5)
            fade = 1.0 - 0.75 * t
            radius = 8 + int(t * 150)
            for k in range(3):                       # a ring with some thickness
                self.fb.ellipse(CX, CY, radius + k, radius + k, _c(_dim(ring, fade)))
            rays = 8
            length = 18 + int(70 * t)
            spin = 0.5 * t
            for n in range(rays):
                a = spin + n * 2 * math.pi / rays
                x2 = CX + int(math.cos(a) * length)
                y2 = CY + int(math.sin(a) * length)
                self.fb.line(CX, CY, x2, y2, _c(_dim(_mix(WHITE, PINK, t), fade)))
            core = max(3, int(14 * (1.0 - t)) + 3)
            self.fb.ellipse(CX, CY, core + 4, core + 4, _c(_dim(PINK, 0.5 * fade)), True)
            self.fb.ellipse(CX, CY, core, core, _c(WHITE), True)
            self.lamp(ring, fade)
            if not self.frame():
                return

    def _eye(self, ex, opened, glance, mirror):
        """One eye, drawn the way the reference sheet draws them.

        Column by column, because the lids have to *clip* the iris: an ellipse
        arc wide enough to curve nicely is far wider than the eye, and drawing
        it lands two bowls on the face. For every column the upper and lower
        lid give a top and a bottom, and everything - sclera, iris, pupil - is
        filled between exactly those.

        What makes it read as an anime eye: a heavy black upper lid that
        thickens towards the outer corner and carries three lashes, a big iris
        clipped top and bottom, dark at the top and light at the bottom, and
        two large white highlights painted last. `mirror` is -1 for the left
        eye and 1 for the right, so lashes and light agree on the face.
        """
        fb = self.fb
        w = 33
        top_h = max(1, int(27 * opened))
        bot_h = max(1, int(23 * opened))
        gx = ex + glance
        ir_w, ir_h = 17, 25              # tall, but with sclera left either side
        sclera, lash = _c(WARM), _c(LASH)
        deep, lit, rim, pupil = _c(IRIS_DEEP), _c(IRIS_LIT), _c(IRIS_RIM), _c(PUPIL)
        top_of = {}
        for i in range(-w, w + 1):
            u = i / w
            f = (1 - u * u) ** 0.5                     # big and round, not almond
            mid = CY - 2 * u * mirror
            top, bot = mid - top_h * f, mid + bot_h * f
            height = int(bot) - int(top)
            top_of[i] = int(top)
            if height < 1:
                continue
            fb.vline(ex + i, int(top), height + 1, sclera)
            dx = ex + i - gx
            if opened > 0.3 and abs(dx) < ir_w:
                g = (1 - (dx / ir_w) ** 2) ** 0.5
                i_top = max(int(top) + 1, int(CY - ir_h * g))
                i_bot = min(int(bot) - 1, int(CY + ir_h * g))
                if i_bot > i_top:
                    span = i_bot - i_top
                    edge = abs(dx) > ir_w - 3
                    fb.vline(ex + i, i_top, span + 1, rim if edge else deep)
                    if not edge:
                        fb.vline(ex + i, i_top + span * 3 // 5,
                                 span - span * 3 // 5 + 1, lit)      # lit from below
                        fb.vline(ex + i, i_top, max(2, span // 4), rim)   # lid shadow
                    if abs(dx) < 8:                                  # pupil
                        p = (1 - (dx / 8) ** 2) ** 0.5
                        p_top = max(i_top, int(CY - 12 * p))
                        p_bot = min(i_bot, int(CY + 12 * p))
                        if p_bot > p_top:
                            fb.vline(ex + i, p_top, p_bot - p_top + 1, pupil)
            thick = 4 + int(4 * max(0.0, u * mirror))    # heavy lid, heavier outward
            fb.vline(ex + i, int(top) - thick + 1, thick, lash)
            if -0.85 < u < 0.85:                         # thin lower lid
                fb.vline(ex + i, int(bot), 1 if abs(u) < 0.5 else 2, lash)
        # three lashes, each rooted in the lid at the outer corner
        root = mirror * int(w * 0.62)
        for n, (run, rise, base) in enumerate(((11, 13, 7), (14, 8, 6), (13, 3, 5))):
            x0 = ex + root + mirror * n * 7
            y0 = top_of.get(root + mirror * n * 7, CY - top_h) - 2
            fb.poly(0, 0, array("h", [x0, y0 + base,
                                      x0 + mirror * run, y0 - rise,
                                      x0 + mirror * (run - 4), y0 - rise + 5,
                                      x0 + mirror * 2, y0 + base]), lash, True)
        if opened > 0.3:                                 # the highlights, last and big
            fb.ellipse(gx - mirror * 8, CY - 10, 7, 7, _c(WHITE), True)
            fb.ellipse(gx + mirror * 9, CY + 9, 4, 4, _c(WHITE), True)
            fb.ellipse(gx - mirror * 2, CY + 15, 2, 2, _c(_dim(WHITE, 0.75)), True)

    def _hair(self, lit=1.0):
        """Dark hair with a fringe and two long locks, and the faint light
        traces her persona describes running through it."""
        fb = self.fb
        hair = _mix(SKY, HAIR, lit)
        rx, ry = FACE_RX + 5, FACE_RY + 5
        for i in range(-rx, rx + 1):
            u = i / rx
            top = FACE_Y - int(ry * (1 - u * u) ** 0.5)
            fringe = CY - 40 + int(16 * math.cos(u * 2.1))
            lock = FACE_Y + int(120 * max(0.0, (abs(u) - 0.68) / 0.32))
            bottom = max(fringe, lock)
            if bottom > top:
                fb.vline(CX + i, top, bottom - top + 1, _c(hair))
        for n, (ux, dy) in enumerate(((-0.55, 6), (-0.2, 2), (0.35, 4), (0.62, 8))):
            x = CX + int(ux * rx)
            y0 = FACE_Y - int(ry * (1 - ux * ux) ** 0.5) + dy
            fb.vline(x, y0, 26 + n * 4, _c(_mix(hair, PINK, 0.35 * lit)))

    def _mouth(self):
        """A small smile - a face with eyes and no mouth is a mask."""
        fb, y = self.fb, CY + 58
        for k in range(2):
            fb.line(CX - 9, y + k, CX - 3, y + 4 + k, _c(MOUTH))
            fb.line(CX - 3, y + 4 + k, CX + 4, y + 4 + k, _c(MOUTH))
            fb.line(CX + 4, y + 4 + k, CX + 10, y + k, _c(MOUTH))

    def _nose(self):
        """The small angled nose of the reference sheet: two short strokes."""
        fb, y = self.fb, CY + 40
        for k in range(3):
            fb.line(CX - 5, y - 4 + k, CX, y + k, _c(LASH))
            fb.line(CX, y + k, CX + 3, y - 2 + k, _c(LASH))

    def face(self, lit=1.0):
        """Her face, so the lids have something to be dark against.

        Black lashes on a black sky are invisible - the reference sheet draws
        eyes on skin. Lit fades the face in out of the afterglow."""
        fb = self.fb
        skin = _mix(SKY, SKIN, lit)
        for k, shade in ((6, 0.55), (3, 0.8), (0, 1.0)):     # a soft edge
            fb.ellipse(CX, FACE_Y, FACE_RX + k, FACE_RY + k,
                       _c(_mix(SKY, skin, shade)), True)
        for side in (-1, 1):                                  # she blushes easily
            fb.ellipse(CX + side * 74, CY + 34, 20, 9, _c(_mix(skin, BLUSH, 0.55 * lit)), True)

    def _eyes(self, opened, glance=0, lit=1.0):
        self.face(lit)
        self._hair(lit)
        self._eye(CX - 48, opened, glance, -1)
        self._eye(CX + 48, opened, glance, 1)
        if opened > 0.3:
            self._nose()
            self._mouth()

    def waking(self):
        steps = [0.0, 0.12, 0.28, 0.45, 0.62, 0.78, 0.9, 1.0, 1.0, 1.0, 1.0,
                 0.5, 0.12, 0.5, 1.0, 1.0, 1.0, 1.0]      # open, look, blink
        for i, opened in enumerate(steps):
            self.sky(0.35)
            glance = 3 if 8 <= i <= 10 else 0
            self._eyes(opened, glance, lit=min(1.0, (i + 1) / 4))   # her face fades in
            self.lamp(PINK, 0.08 * opened)
            if not self.frame():
                return

    def name(self):
        word, scale = "Nova", 4
        x0 = (W - len(word) * 8 * scale) // 2
        for i in range(1, 2 * len(word) + 4):
            self.sky(0.35)
            self._eyes(1.0)
            self.d.text(word[:min(i // 2 + 1, len(word))], x0, NAME_Y, _c(PINK), scale=scale)
            t = max(0.0, (i - 2) / (2 * len(word)))
            self.fb.fill_rect(x0, STRIPE_Y, int(len(word) * 8 * scale * t), 2, _c(STRIPE))
            if not self.frame():
                return

    def final(self):
        """The still frame she stays on - also what a skipped animation shows."""
        word, scale = "Nova", 4
        x0 = (W - len(word) * 8 * scale) // 2
        self.sky(0.35)
        self._eyes(1.0)
        self.d.text(word, x0, NAME_Y, _c(PINK), scale=scale)
        self.fb.fill_rect(x0, STRIPE_Y, len(word) * 8 * scale, 2, _c(STRIPE))
        self.lamp((0, 0, 0))


def play(d, io=None, touch=None):
    """Run the animation on display `d`; leaves the final frame drawn (not
    shown) so the caller can add status lines and show once."""
    s = _Scene(d, io, touch)
    for beat in (s.darkness, s.heartbeat, s.flare, s.waking, s.name):
        if s.skipped:
            break
        beat()
    d.texts = []
    s.final()
    return not s.skipped
