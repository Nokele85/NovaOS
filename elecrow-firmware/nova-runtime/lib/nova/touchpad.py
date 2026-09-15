# CST816S touch controller on the shared I2C bus (address 0x15).
#
# Rook's lesson (touch.h): the chip only answers reliably after the first
# touch; a missing ACK while it sleeps is not a fault. And one press reports
# "a finger" many times while it lands - `pressed()` debounces that.
import time

from . import board as B


class Touch:
    def __init__(self, i2c, debounce_ms=250):
        self.i2c = i2c
        self.debounce_ms = debounce_ms
        self._down = False
        self._last = 0

    def read(self):
        """(x, y) in display coordinates while a finger is down, else None."""
        try:
            d = self.i2c.readfrom_mem(B.TOUCH_ADDR, 0x02, 5)
        except OSError:
            return None
        if d[0] == 0:
            return None
        x = ((d[1] & 0x0F) << 8) | d[2]
        y = ((d[3] & 0x0F) << 8) | d[4]
        if B.TOUCH_ROTATED:
            x, y = B.LCD_W - 1 - x, B.LCD_H - 1 - y
        return (max(0, min(B.LCD_W - 1, x)), max(0, min(B.LCD_H - 1, y)))

    def pressed(self):
        """The point of a *new* press, once per touch; None otherwise."""
        point = self.read()
        now = time.ticks_ms()
        new = None
        if point and not self._down and time.ticks_diff(now, self._last) > self.debounce_ms:
            new = point
            self._last = now
        self._down = point is not None
        return new
