# ST7789 240x284 over SPI, drawn through one RAM framebuffer.
#
# Apps draw into `fb` (a framebuf.FrameBuffer) and call `show()`. The buffer
# is also what the host reads back for a screenshot, so what Nova says she drew
# can be checked against what the panel was sent. Colours are stored
# byte-swapped (`rgb()`), i.e. in the big-endian order the panel reads - no
# per-frame conversion.
import struct
import time

import framebuf
from machine import Pin, SPI

from . import board as B

W = B.LCD_W
H = B.LCD_H
TEXT_LOG_MAX = 24


def rgb(r, g, b):
    c = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | ((b & 0xFF) >> 3)
    return ((c & 0xFF) << 8) | (c >> 8)


BLACK = rgb(0, 0, 0)
WHITE = rgb(255, 255, 255)
RED = rgb(255, 0, 0)
GREEN = rgb(0, 255, 0)
BLUE = rgb(0, 0, 255)


class Display:
    def __init__(self, spi=None):
        # SPI(1), not SPI(2): on MicroPython 1.29 on this board merely creating
        # SPI(2) hangs the chip into an interrupt-watchdog reset, at any clock
        # and on any pins (measured 2026-09-11). The TF card, which would have
        # wanted the other controller, runs on SoftSPI instead (nova/sd.py).
        # miso=None is not optional: left out, MicroPython puts MISO on the
        # controller's default GPIO 13 - which is this panel's MOSI - turning
        # the data line into an input. The backlight comes on, no picture does
        # (seen 2026-09-11).
        self.spi = spi or SPI(B.LCD_SPI_ID, baudrate=B.LCD_SPI_HZ, polarity=0, phase=0,
                              sck=Pin(B.LCD_SCK), mosi=Pin(B.LCD_MOSI), miso=None)
        self.dc = Pin(B.LCD_DC, Pin.OUT, value=0)
        self.cs = Pin(B.LCD_CS, Pin.OUT, value=1)
        self.buf = bytearray(W * H * 2)
        self.fb = framebuf.FrameBuffer(self.buf, W, H, framebuf.RGB565)
        # what text was drawn where - the host's screenshot summary reads it,
        # word for word, which beats a vision model reading 8-pixel glyphs
        self.texts = []
        self.frames = 0
        self._init()

    def _cmd(self, c, data=None):
        self.cs(0)
        self.dc(0)
        self.spi.write(bytes([c]))
        if data:
            self.dc(1)
            self.spi.write(data)
        self.cs(1)

    def _init(self):
        self._cmd(0x01)                        # software reset
        time.sleep_ms(150)
        self._cmd(0x11)                        # sleep out
        time.sleep_ms(120)
        self._cmd(0x3A, b"\x55")               # 16-bit colour
        self._cmd(0x36, bytes([0x08 if B.LCD_BGR else 0x00]))
        self._cmd(0x21 if B.LCD_INVERT else 0x20)
        self._cmd(0x13)                        # normal mode
        self._cmd(0x29)                        # display on
        time.sleep_ms(20)

    def show(self):
        x0, y0 = B.LCD_X_OFFSET, B.LCD_Y_OFFSET
        self._cmd(0x2A, struct.pack(">HH", x0, x0 + W - 1))
        self._cmd(0x2B, struct.pack(">HH", y0, y0 + H - 1))
        self.cs(0)
        self.dc(0)
        self.spi.write(b"\x2C")
        self.dc(1)
        self.spi.write(self.buf)
        self.cs(1)
        self.frames += 1

    # ---- conveniences; apps may use self.fb directly for everything else ----

    def fill(self, color=BLACK):
        self.fb.fill(color)
        self.texts = []

    def text(self, s, x, y, color=WHITE, scale=1):
        s = str(s)
        self.texts.append((s, x, y, scale))
        del self.texts[:-TEXT_LOG_MAX]
        if scale <= 1:
            self.fb.text(s, x, y, color)
            return
        # 8x8 font, scaled by drawing each lit pixel as a block
        tmp = bytearray(8 * 8 * 2)
        glyph = framebuf.FrameBuffer(tmp, 8, 8, framebuf.RGB565)
        for i, ch in enumerate(s):
            glyph.fill(0)
            glyph.text(ch, 0, 0, 1)
            ox = x + i * 8 * scale
            for gy in range(8):
                for gx in range(8):
                    if glyph.pixel(gx, gy):
                        self.fb.fill_rect(ox + gx * scale, y + gy * scale,
                                          scale, scale, color)

    def snapshot(self, step=1):
        """Raw big-endian RGB565, every `step`-th pixel - for the host."""
        if step <= 1:
            return bytes(self.buf)
        out = bytearray((W // step) * (H // step) * 2)
        o = 0
        for y in range(0, H - H % step, step):
            row = y * W * 2
            for x in range(0, W - W % step, step):
                i = row + x * 2
                out[o] = self.buf[i]
                out[o + 1] = self.buf[i + 1]
                o += 2
        return bytes(out)
