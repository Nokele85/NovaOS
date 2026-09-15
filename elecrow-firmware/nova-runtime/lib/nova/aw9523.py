# AW9523 port expander: panel power, backlight, RGB lamp, camera flash, mute.
#
# A register-for-register port of rook_elecrow/board_io.h, which ran on this
# board. Order matters: panel power (P1_0) before the display init, or the
# screen stays dark and the fault looks like SPI.
from . import board as B

REG_OUTPUT1 = 0x03
REG_CONFIG0 = 0x04
REG_CONFIG1 = 0x05
REG_ID = 0x10
REG_CTL = 0x11
REG_LED_EN0 = 0x12
REG_LED_EN1 = 0x13
CHIP_ID = 0x23


def _dim_p0(pin):
    return 0x24 + pin


def _dim_p1(pin):
    return 0x20 + pin


class AW9523:
    def __init__(self, i2c):
        self.i2c = i2c
        self._out1 = 0x01

    def _w(self, reg, value):
        self.i2c.writeto_mem(B.AW9523_ADDR, reg, bytes([value & 0xFF]))

    def _r(self, reg):
        return self.i2c.readfrom_mem(B.AW9523_ADDR, reg, 1)[0]

    def begin(self):
        """True when the chip answered with its id and the panel has power."""
        try:
            if self._r(REG_ID) != CHIP_ID:
                return False
        except OSError:
            return False
        self._w(REG_CTL, 0x10)                       # push-pull on port 0
        # ISEL 0 = LED mode: RGB and backlight dim, everything else GPIO
        self._w(REG_LED_EN0, 0xFF & ~((1 << B.AW_RGB_G) | (1 << B.AW_RGB_R)
                                      | (1 << B.AW_RGB_B)))
        self._w(REG_LED_EN1, 0xFF & ~(1 << (B.AW_BACKLIGHT - 8)))
        self._w(REG_CONFIG0, 0x8F)                   # P0 4/5/6 outputs
        # OUTPUT1 deliberately NOT 0xFF: bit 1 is the camera flash. Start muted
        # (bit 4 HIGH) - the speaker module unmutes for exactly its playback.
        self._out1 = 0x01 | (1 << (B.AW_MUTE - 8))
        self._w(REG_OUTPUT1, self._out1)
        self._w(REG_CONFIG1, 0xE4)                   # P1 0/1/3/4 outputs
        return True

    def backlight(self, percent):
        percent = max(0, min(100, int(percent)))
        self._w(_dim_p1(B.AW_BACKLIGHT - 8), 0 if percent == 0 else percent * 2 + 55)

    def rgb(self, r, g, b):
        self._w(_dim_p0(B.AW_RGB_R), r)
        self._w(_dim_p0(B.AW_RGB_G), g)
        self._w(_dim_p0(B.AW_RGB_B), b)

    def _bit(self, pin, on):
        mask = 1 << (pin - 8)
        self._out1 = (self._out1 | mask) if on else (self._out1 & ~mask)
        self._w(REG_OUTPUT1, self._out1)

    def flash(self, on):
        self._bit(B.AW_FLASH, on)

    def mute(self, muted):
        self._bit(B.AW_MUTE, muted)
