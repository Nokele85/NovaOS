# The 1 W speaker behind the NS4168 amplifier (standard I2S, 16-bit mono).
#
# The amplifier is muted except while something plays (AW9523 P1_4 HIGH =
# muted); a sound that ends - or fails - always leaves it muted again.
# Status: wiring from Rook's board.h; playback on this runtime UNVERIFIED.
import math
import struct

from machine import I2S, Pin

from . import board as B


def _open(rate):
    return I2S(0, sck=Pin(B.SPK_BCK), ws=Pin(B.SPK_WS), sd=Pin(B.SPK_DATA),
               mode=I2S.TX, bits=16, format=I2S.MONO, rate=rate, ibuf=8192)


def tone(io, freq=880, ms=200, volume=0.3, rate=16000):
    n = int(rate * ms / 1000)
    amp = int(32767 * max(0.0, min(1.0, volume)))
    buf = bytearray(n * 2)
    for i in range(n):
        struct.pack_into("<h", buf, i * 2, int(amp * math.sin(2 * math.pi * freq * i / rate)))
    out = _open(rate)
    try:
        io.mute(False)
        out.write(buf)
    finally:
        io.mute(True)
        out.deinit()


def play_wav(io, path):
    """16-bit PCM mono WAV; returns the number of audio bytes written."""
    with open(path, "rb") as f:
        head = f.read(44)
        if head[0:4] != b"RIFF" or head[8:12] != b"WAVE":
            raise ValueError("not a WAV file")
        channels, rate = struct.unpack("<HI", head[22:28])
        bits = struct.unpack("<H", head[34:36])[0]
        if channels != 1 or bits != 16:
            raise ValueError("need 16-bit mono, got %d ch / %d bit" % (channels, bits))
        out = _open(rate)
        written = 0
        try:
            io.mute(False)
            chunk = bytearray(2048)
            while True:
                n = f.readinto(chunk)
                if not n:
                    break
                out.write(chunk[:n])
                written += n
        finally:
            io.mute(True)
            out.deinit()
    return written
