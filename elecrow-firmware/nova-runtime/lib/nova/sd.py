# The TF card, mounted at /sd.
#
# The card sits on its own SPI bus (SCK 7 / MISO 6 / MOSI 8), and the
# schematic has no chip-select net: Elecrow's own code passes CS = -1. The
# card is therefore selected permanently, and every GPIO of this module is
# already taken - so the driver gets a chip-select that does nothing rather
# than a pin that belongs to something else.
#
# Driver: micropython-lib's `sdcard.py`, installed with the runtime. Its
# absence is reported, not raised: Nova's apps then live in flash (/apps).
import os

from machine import Pin, SoftSPI

from . import board as B

MOUNT = "/sd"


class _NoChipSelect:
    OUT = 1

    def init(self, *args, **kwargs):
        pass

    def __call__(self, value=None):
        return 0


class _Retrying:
    """The card as a block device, each block access tried up to three times.

    Without a chip-select line the card never sees a transaction end, so one
    garbled response leaves it out of step for the next command: measured
    2026-09-11, random reads were clean, a directory walk hit `CMD17` answering
    non-zero (EIO). Sixteen idle clocks between tries let it finish whatever
    it was still sending. `errors` counts every retry, so a card that keeps
    needing them shows up in `nova.info()` instead of in a corrupted file.
    """
    TRIES = 3

    def __init__(self, card):
        self.card = card
        self.errors = 0

    def _again(self, fn, block, buf, *rest):
        for attempt in range(self.TRIES):
            try:
                return fn(block, buf, *rest)
            except OSError:
                self.errors += 1
                if attempt == self.TRIES - 1:
                    raise
                self.card.spi.write(b"\xff" * 16)

    def readblocks(self, block, buf, *rest):
        return self._again(self.card.readblocks, block, buf, *rest)

    def writeblocks(self, block, buf, *rest):
        return self._again(self.card.writeblocks, block, buf, *rest)

    def ioctl(self, op, arg):
        return self.card.ioctl(op, arg)


_status = "not_tried"
_card = None


def retries():
    return _card.errors if _card is not None else 0


def status():
    return _status


def card():
    """The block device behind /sd (the owner's format step needs it)."""
    return _card


def mount():
    global _status, _card
    try:
        os.stat(MOUNT)
        _status = "mounted"
        return _status
    except OSError:
        pass
    try:
        import sdcard
    except ImportError:
        _status = "driver_missing"
        return _status
    try:
        # SoftSPI: the display holds hardware SPI(1), and SPI(2) hangs this
        # chip on MicroPython 1.29 (nova/display.py). App files are small;
        # a bit-banged bus is slower, not unreliable.
        spi = SoftSPI(baudrate=B.SD_HZ, polarity=0, phase=0,
                      sck=Pin(B.SD_SCK), mosi=Pin(B.SD_MOSI), miso=Pin(B.SD_MISO))
        _card = _Retrying(sdcard.SDCard(spi, _NoChipSelect(), baudrate=B.SD_HZ))
        try:
            os.mount(os.VfsFat(_card), MOUNT)
            _status = "mounted"
        except OSError as e:     # a card is there, but no FAT on it
            _status = "unformatted: %s" % e
    except Exception as e:       # no card, unformatted card, wrong wiring
        _status = "failed: %s" % e
    return _status
