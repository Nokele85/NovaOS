# Music she writes herself, played on the board's speaker in the background.
#
#   from nova import music
#   music.play("E4 G4 A4:2 - C5:0.5 B4:0.5 A4:4", tempo=80, loop=True, minutes=10)
#   music.pad("A2+E3+C4:8 F2+C3+A3:8 C3+G3+E4:8 G2+D3+B3:8", tempo=40)
#   music.harp("A3+E4+C5:8 F3+C4+A4:8 C4+G4+E5:8 G3+D4+B4:8", tempo=60)
#   music.stop()
#
# harp() plays chords as plucked strings, broken into notes a different way
# each time. play() is a melody: one voice, each note struck and dying away. pad() is a
# carpet of sound: slow chords of up to four voices, each voice with a body of
# overtones that beat slowly against each other, one chord melting into the next. Both return at once; the piece
# plays on until it ends, until stop(), or - when it loops - until `minutes` are
# up. One piece at a time: a new one ends the one before it. The notation is in
# score.py.
#
# No thread. The speaker asks for the next slice of sound when it has room
# (I2S.irq), and the answer is given between whatever else the board is doing.
# The first version ran in a thread of its own, and for as long as it played
# the board answered nothing on its cable - not even the call that would have
# stopped it.
#
# While a piece plays, the speaker is taken: speaker.tone() and play_wav() would
# fail on a busy I2S. stop() first.
import math
import random
import struct
import time
from array import array

import micropython

from . import board as B
from . import score as S

RATE = 16000
WAVES = ("pluck", "soft", "sine", "square")
#: A plucked string is not added up out of sine waves: a burst of noise goes
#: round a loop as long as the note's period and is smoothed a little each
#: time (Karplus-Strong). It starts bright and dies away the way a string
#: does, and no two plucks are the same - which is what the sine voices, all
#: of them a held whistle, could never be.
SLICE = 2000                # samples the mixer hands over at a time (125 ms)
TAIL = 64000                # how long one plucked note rings (4 s)
#: Each pluck is heard again, fainter, a little later and later still - the
#: room the board does not have. (slices of delay, gain out of 256)
ECHOES = ((3, 80), (7, 34))
STRING_LOW, STRING_HIGH = 196.0, 2000.0     # what the little speaker carries
MAX_STRINGS = 14
#: Loudness is a knob on the board, not a property of a piece: 0..100 % in
#: tens, kept across pieces and power cuts, stepped by the board's button (ten
#: up per press, and from 100 round to 0 - sound off). Every piece is rendered
#: at FULL, what 100 % means, and turned down on its way to the speaker.
FULL = 0.3
VOLUME_FILE = "/nova_volume"
FIRST_VOLUME = 20
#: A piece may still name a level, 0..LOUDEST as before; it sets the knob.
QUIET, LOUDEST = 0.06, 0.25

#: A pad is rendered in one-second slices that follow themselves without a
#: seam; chords melt into each other over PAD_FADE of them. At the full rate:
#: at half of it the speaker added a hiss of its own above every voice.
PAD_RATE = 16000
PAD_SLICE = 16000
PAD_FADE = 3
MAX_PAD_CHORDS = 6
#: How a voice is built, as (multiple of its pitch, cycles off per second,
#: weight). A bare sine held for ten seconds is a whistle in the ear - the
#: first pad sounded like tinnitus. Overtones give it a body (and let a small
#: speaker carry notes too low for it), and the copies that sit a cycle off
#: beat slowly against the rest, so the sound moves while nothing changes.
PAD_VOICE = ((1, 0, 1.0), (2, 0, 0.5), (2, 1, 0.14), (3, 0, 0.26), (4, 0, 0.13),
             (4, 1, 0.05), (5, 0, 0.06))
#: Only two upper parts have a copy a cycle off, and a faint one: up there a
#: hertz is a few cents, a shimmer; on the fundamental it was a wobble that
#: shook the whole sound.
#: Below SPEAKER_FLOOR the little speaker does not sing, it shakes: its cone
#: and the case rattle, and the notes above are smeared by it. Parts under it
#: are thinned out steeply; the ear puts a low note back from its overtones.
SPEAKER_FLOOR = 280
SPEAKER_CEILING = 3600
#: A melody note is struck and dies away rather than being held like a beep.
DECAY = (100, 80, 63, 49, 38, 29, 22)

_now = {"playing": False, "title": "", "loop": False, "loops": 0, "notes": 0,
        "error": None}
_vol = None
_btn = None
_pressed = 0
_mix = None
_turn = 0
_stop = False
_out = None
_feeder = None
_SILENCE = bytes(960)                   # 30 ms at RATE


def status():
    """What is playing right now: {"playing", "title", "loop", "loops", "notes",
    "error", "volume", "ambience"} - volume is the knob, in percent; ambience
    is the air that is running beside the piece, or None."""
    now = dict(_now)
    now["volume"] = volume()
    now["ambience"] = _amb[0] if _amb is not None else None
    return now


def volume(percent=None):
    """The loudness knob, 0..100 in tens. volume() reads it; volume(40) sets it.
    It holds for every piece until it is changed - by this, or by the button on
    the board: each press is ten more, and from 100 it goes round to 0."""
    global _vol
    if _vol is None:
        try:
            with open(VOLUME_FILE) as f:
                _vol = max(0, min(100, int(f.read().strip()) // 10 * 10))
        except (OSError, ValueError):
            _vol = FIRST_VOLUME
    if percent is not None:
        _vol = max(0, min(100, int(percent) // 10 * 10))
        try:
            with open(VOLUME_FILE, "w") as f:
                f.write(str(_vol))
        except OSError:
            pass
    return _vol


def _press(_pin):
    global _pressed
    now = time.ticks_ms()
    if time.ticks_diff(now, _pressed) < 250:        # one press, not its bounce
        return
    _pressed = now
    try:                                # a press also ends a doze
        from . import power
        if power.poke_from_press():
            return                      # ...and that press only woke her
    except Exception:
        pass
    level = volume()
    volume(0 if level >= 100 else level + 10)
    try:                                # drawn outside the interrupt
        micropython.schedule(_show_volume, 0)
    except RuntimeError:
        pass


def _show_volume(_arg=None):
    # A strip along the bottom of the screen: the level in words and as a bar.
    # It stays until whatever owns the screen draws again.
    try:
        import nova

        from . import screen as SC
        d = nova.display()
        top = SC.H - 58
        d.fb.fill_rect(0, top, SC.W, 58, SC.BLACK)
        d.fb.hline(0, top, SC.W, SC.WHITE)
        d.text("Lautst. %d%%" % _vol if _vol else "Ton aus", 8, top + 8, SC.WHITE, 2)
        d.fb.rect(8, top + 32, SC.W - 16, 16, SC.WHITE)
        if _vol:
            d.fb.fill_rect(10, top + 34, (SC.W - 20) * _vol // 100, 12, SC.GREEN)
        d.show()
    except Exception:
        pass


def knob():
    """Wire the board's button to the volume. Done at start-up and before
    every piece; doing it twice changes nothing."""
    global _btn
    if _btn is None:
        from machine import Pin
        _btn = Pin(B.BUTTON, Pin.IN, Pin.PULL_UP)
        _btn.irq(_press, Pin.IRQ_FALLING)
    return volume()


def _level(volume_arg):
    # a piece that names a level sets the knob; one that does not leaves it
    if volume_arg:
        volume(max(10, int(max(0.0, min(LOUDEST, volume_arg)) / LOUDEST * 100 + 5)))
    return int(32767 * FULL)


def _finish():
    global _out, _feeder
    out, _out, _feeder = _out, None, None
    try:
        import nova
        nova.io().mute(True)
    except Exception:
        pass
    if out is not None:
        try:
            out.deinit()
        except Exception:
            pass
    _now["playing"] = False


def stop(wait_ms=900):
    """End the piece that is playing. The ambience, if there is one, goes on -
    ambience("off") ends that. True once the piece is silent."""
    global _stop, _feeder
    if _feeder is None and not _now["playing"]:
        return True
    _stop = True
    start = time.ticks_ms()
    while _now["playing"] and time.ticks_diff(time.ticks_ms(), start) < wait_ms:
        time.sleep_ms(20)               # the speaker's next request ends it
    if _now["playing"] or _feeder is not None:
        _feeder = None                  # ...and if none came, end it here
        _now["playing"] = False
        if _amb is None:
            _finish()
    _stop = False
    return True


def _feed(out):
    # Called by the speaker when it has taken the last slice: the next slice of
    # the piece, the ambience laid under it, the knob on the way out.
    global _turn, _feeder, _stop, _amb
    if out is not _out:
        return
    data = None
    if _feeder is not None:
        if _stop:
            _feeder = None
            _now["playing"] = False
            _stop = False
        else:
            try:
                data = next(_feeder)
            except StopIteration:
                _feeder = None
                _now["playing"] = False
            except Exception as e:      # never die without a word
                _now["error"] = repr(e)
                _feeder = None
                _now["playing"] = False
    if data is None:
        if _amb is None and not _fading:
            _finish()
            return
        data = _HUSH                    # no piece: the ambience alone
    try:
        size = len(data)
        level = _vol if _vol is not None else FIRST_VOLUME
        if _amb is not None or _fading or level < 100:
            mixed = memoryview(_mix[_turn])[:size]
            _turn = (_turn + 1) % len(_mix)
            if _amb is not None or _fading:
                mixed[:] = data
                _amb_render(mixed, size // 2)
                if level < 100:
                    _scale(mixed, mixed, size // 2, level)
            else:
                _scale(data, mixed, size // 2, level)
            data = mixed
        out.write(data)
    except Exception as e:
        _now["error"] = repr(e)
        _amb = None
        _fading.clear()
        _finish()


def _open():
    global _out, _mix
    if _mix is None:
        _mix = [bytearray(PAD_SLICE * 2) for _ in range(3)]
    try:
        knob()
    except Exception:                   # no button is no reason for no music
        pass
    if _out is not None:
        return
    from machine import I2S, Pin

    import nova
    try:
        _out = I2S(0, sck=Pin(B.SPK_BCK), ws=Pin(B.SPK_WS), sd=Pin(B.SPK_DATA),
                   mode=I2S.TX, bits=16, format=I2S.MONO, rate=RATE, ibuf=8192)
        _out.irq(_feed)
        nova.io().mute(False)
        _out.write(_SILENCE)            # the first slice starts the asking
    except Exception:
        _finish()
        raise


def _start(feeder, rate):
    global _stop, _feeder
    _stop = False
    _feeder = feeder
    _open()


_TAB = None


def _table():
    global _TAB
    if _TAB is None:
        _TAB = array("h", (int(32767 * math.sin(2 * math.pi * i / 4096) + 0.5)
                           for i in range(4096)))
    return _TAB


@micropython.viper
def _add_sine(buf, n: int, tab, step: int, gain: int):
    # buf[i] += sine * gain / 32768, the sine read from a 4096-step table with a
    # 30-bit phase; `step` is the phase a sample advances.
    b = ptr16(buf)  # noqa: F821 - MicroPython Viper builtin
    t = ptr16(tab)  # noqa: F821 - MicroPython Viper builtin
    phase = 0
    i = 0
    while i < n:
        s = int(t[(phase >> 18) & 4095])
        if s >= 32768:
            s -= 65536
        v = int(b[i])
        if v >= 32768:
            v -= 65536
        b[i] = (v + ((s * gain + 16384) >> 15)) & 0xFFFF
        phase = (phase + step) & 0x3FFFFFFF
        i += 1


@micropython.viper
def _blend(out, n: int, a, ga0: int, ga1: int, b, gb0: int, gb1: int):
    # out = a * ga + b * gb, each gain (0..256) gliding from its 0 to its 1
    # across the slice - one chord rising out of another with no step to hear.
    o = ptr16(out)  # noqa: F821 - MicroPython Viper builtin
    pa = ptr16(a)  # noqa: F821 - MicroPython Viper builtin
    pb = ptr16(b)  # noqa: F821 - MicroPython Viper builtin
    i = 0
    while i < n:
        x = int(pa[i])
        if x >= 32768:
            x -= 65536
        y = int(pb[i])
        if y >= 32768:
            y -= 65536
        ga = ga0 + ((ga1 - ga0) * i) // n
        gb = gb0 + ((gb1 - gb0) * i) // n
        o[i] = ((x * ga + y * gb) >> 8) & 0xFFFF
        i += 1


@micropython.viper
def _scale(src, dst, n: int, num: int):
    s = ptr16(src)  # noqa: F821 - MicroPython Viper builtin
    d = ptr16(dst)  # noqa: F821 - MicroPython Viper builtin
    i = 0
    while i < n:
        v = int(s[i])
        if v >= 32768:
            v -= 65536
        d[i] = ((v * num) // 100) & 0xFFFF
        i += 1


@micropython.viper
def _ks(buf, n: int, period: int, frac: int, keep: int):
    # The string itself: every sample is the one a period ago, read between two
    # neighbours (`frac`/256, which is what tunes it), leaned a quarter on the
    # one before (a gentle smoothing: half-and-half made every note a dry
    # click, an egg slicer) and `keep`/32768 of that (the dying away).
    b = ptr16(buf)  # noqa: F821 - MicroPython Viper builtin
    prev = 0
    i = period + 1
    while i < n:
        d0 = int(b[i - period])
        if d0 >= 32768:
            d0 -= 65536
        d1 = int(b[i - period - 1])
        if d1 >= 32768:
            d1 -= 65536
        x = (d0 * (256 - frac) + d1 * frac) >> 8
        y = ((x * 3 + prev) >> 2) * keep >> 15
        prev = x
        b[i] = y & 0xFFFF
        i += 1


@micropython.viper
def _fade_out(buf, start: int, n: int):
    b = ptr16(buf)  # noqa: F821 - MicroPython Viper builtin
    i = 0
    while i < n:
        v = int(b[start + i])
        if v >= 32768:
            v -= 65536
        b[start + i] = (v * (n - i) // n) & 0xFFFF
        i += 1


@micropython.viper
def _mix_in(out, src, off: int, n: int, gain: int):
    # out += src[off:] * gain / 256, held inside what 16 bits can say
    o = ptr16(out)  # noqa: F821 - MicroPython Viper builtin
    s = ptr16(src)  # noqa: F821 - MicroPython Viper builtin
    i = 0
    while i < n:
        x = int(s[off + i])
        if x >= 32768:
            x -= 65536
        y = int(o[i])
        if y >= 32768:
            y -= 65536
        y += (x * gain) >> 8
        if y > 32767:
            y = 32767
        elif y < -32767:
            y = -32767
        o[i] = y & 0xFFFF
        i += 1


# ---- the air around the music ---------------------------------------------------------

#: Rain, wind, a fire, crickets: none of these is a recording. They are what
#: they are in nature - noise, shaped - and so they are made here, endlessly
#: and never twice the same, out of four small machines:
#:   ("n", high?, smoothing, gain from, gain to, seconds between moods * 10)
#:        coloured noise whose loudness drifts between two levels (wind, surf)
#:   ("t", chance, keep, gain, smoothing)   random ticks that die away (drops,
#:        crackle, thunder: one a second is chance 65)
#:   ("c", hertz, period, pulse, pulses, gain)   a pulsed tone (crickets, frogs)
#:   ("p", from hz, to hz, glide %, ring ms, gain, pause from, pause to (s * 10),
#:        repeats, chime?)   single notes now and then (birds, drips, chimes)
AIRS = {
    "rain": (("n", 1, 60, 200, 230, 40), ("n", 0, 7, 380, 520, 50),
             ("t", 1700, 32113, 520, 120)),
    "drizzle": (("n", 1, 90, 110, 140, 50), ("t", 600, 32000, 380, 150)),
    "downpour": (("n", 1, 40, 500, 580, 40), ("n", 0, 10, 700, 880, 40),
                 ("t", 4200, 32000, 700, 140)),
    "thunder": (("t", 2, 32765, 3600, 3),),
    "wind": (("n", 0, 5, 350, 900, 50), ("n", 0, 18, 90, 300, 40)),
    "gale": (("n", 0, 8, 700, 1600, 35), ("n", 0, 26, 200, 600, 30), ("n", 1, 50, 60, 200, 30)),
    "leaves": (("n", 1, 90, 60, 260, 35), ("n", 0, 40, 50, 180, 40)),
    "fire": (("n", 0, 4, 520, 760, 40), ("t", 150, 32440, 1700, 200),
             ("t", 420, 31800, 320, 255)),
    "embers": (("n", 0, 3, 320, 460, 50), ("t", 50, 32300, 1300, 220)),
    "crickets": (("c", 4186, 11000, 250, 3, 230), ("c", 4699, 17000, 230, 3, 150)),
    "cicadas": (("c", 5274, 16000, 60, 120, 110),),
    "frogs": (("c", 659, 34000, 400, 4, 300), ("c", 523, 52000, 520, 3, 260)),
    "birds": (("p", 2093, 4186, 6, 80, 520, 30, 110, 2, 1), ("p", 1568, 3136, -5, 110, 380, 50, 160, 1, 1)),
    "owl": (("p", 392, 392, -2, 280, 800, 90, 220, 1, 0),),
    "stream": (("n", 0, 30, 380, 700, 8), ("n", 1, 90, 110, 130, 20), ("t", 900, 32300, 340, 60)),
    "waves": (("n", 0, 12, 100, 1300, 70), ("n", 1, 50, 20, 300, 70)),
    "waterfall": (("n", 0, 25, 900, 1040, 40), ("n", 1, 60, 300, 340, 40)),
    "drips": (("p", 1047, 2093, 10, 50, 700, 15, 60, 0, 1),),
    "chimes": (("p", 1047, 2093, 0, 1100, 520, 35, 120, 1, 1),),
    "bells": (("p", 523, 1047, 0, 2000, 600, 80, 240, 0, 1),),
    "hum": (("n", 0, 2, 900, 1100, 60),),
}
#: Names for mixtures that are asked for often. Two or three sounds, no more:
#: a place is a ground (rain, wind, fire) and one thing living in it.
PLACES = {
    "night": "crickets+owl", "forest": "leaves+birds", "storm": "downpour+thunder+gale",
    "campfire": "fire+crickets", "beach": "waves+wind", "jungle": "cicadas+frogs+drizzle",
    "pond": "frogs+stream", "cave": "drips+hum", "garden": "birds+chimes",
    "hearth": "embers+rain", "meadow": "birds+wind", "temple": "bells+wind",
}
MAX_AIRS = 8
#: Every sound with a pitch - a bird, a drip, a chime, a cricket - sings one of
#: these five notes (C D E G A) in some octave. Thrown anywhere, they were a
#: pile of noises beside the music; on one scale they belong to it, whatever
#: white-key chord is sounding.
SCALE = (261.63, 293.66, 329.63, 392.0, 440.0)
#: A scene stays what it is until it is changed: the same rain goes on raining,
#: only never drop for drop the same. When it is changed, the old one sinks
#: away over FADE_S seconds while the new one rises - a place is left, not cut.
FADE_S = 4
_amb = None                     # [scene, layers, until, how much of it is there (0..256)]
_fading = []                    # scenes on their way out
_scratch = None
_HUSH = bytes(SLICE * 2)
_NOTHING = bytes(PAD_SLICE * 2)


def ambiences():
    """Every sound of the air there is: the single ones, then the mixed places."""
    return sorted(AIRS) + sorted(PLACES)


@micropython.viper
def _amb_noise(out, n: int, state):
    # 0 seed, 1 the smoothed value, 2 smoothing, 3 high (1) or low (0), 4 gain
    # now, 5 gain at the slice's end
    o = ptr16(out)  # noqa: F821 - MicroPython Viper builtin
    st = ptr32(state)  # noqa: F821 - MicroPython Viper builtin
    seed = int(st[0])
    lp = int(st[1])
    a = int(st[2])
    high = int(st[3])
    g0 = int(st[4])
    g1 = int(st[5])
    i = 0
    while i < n:
        seed = seed * 214013 + 2531011
        w = ((seed >> 15) & 0xFFFF) - 32768
        lp += ((w - lp) * a) >> 8
        x = lp
        if high:
            x = w - lp
        y = int(o[i])
        if y >= 32768:
            y -= 65536
        y += (x * (g0 + ((g1 - g0) * i) // n)) >> 12
        if y > 32767:
            y = 32767
        elif y < -32767:
            y = -32767
        o[i] = y & 0xFFFF
        i += 1
    st[0] = seed
    st[1] = lp
    st[4] = g1


@micropython.viper
def _amb_ticks(out, n: int, state):
    # 0 seed, 1 how loud the last tick still is, 2 chance of a new one, 3 how
    # much of it is kept each sample, 4 gain, 5 the smoothed value, 6 smoothing
    o = ptr16(out)  # noqa: F821 - MicroPython Viper builtin
    st = ptr32(state)  # noqa: F821 - MicroPython Viper builtin
    seed = int(st[0])
    env = int(st[1])
    chance = int(st[2])
    keep = int(st[3])
    gain = int(st[4])
    lp = int(st[5])
    a = int(st[6])
    i = 0
    while i < n:
        seed = seed * 214013 + 2531011
        if ((seed >> 11) & 0xFFFFF) < chance:
            env = 12000 + ((seed >> 4) & 0x3FFF)
        w = ((seed >> 15) & 0xFFFF) - 32768
        lp += ((w - lp) * a) >> 8
        if env > 4:
            y = int(o[i])
            if y >= 32768:
                y -= 65536
            y += (((lp * env) >> 15) * gain) >> 12
            if y > 32767:
                y = 32767
            elif y < -32767:
                y = -32767
            o[i] = y & 0xFFFF
            env = (env * keep) >> 15
        i += 1
    st[0] = seed
    st[1] = env
    st[5] = lp


@micropython.viper
def _amb_chirp(out, n: int, tab, state):
    # 0 phase, 1 step, 2 where in its period, 3 the period, 4 one pulse,
    # 5 how many pulses, 6 gain
    o = ptr16(out)  # noqa: F821 - MicroPython Viper builtin
    t = ptr16(tab)  # noqa: F821 - MicroPython Viper builtin
    st = ptr32(state)  # noqa: F821 - MicroPython Viper builtin
    phase = int(st[0])
    step = int(st[1])
    at = int(st[2])
    period = int(st[3])
    pulse = int(st[4])
    sounding = pulse * 2 * int(st[5])
    gain = int(st[6])
    i = 0
    while i < n:
        if at < sounding and ((at // pulse) & 1) == 0:
            phase = (phase + step) & 0x3FFFFFFF
            s = int(t[(phase >> 18) & 4095])
            if s >= 32768:
                s -= 65536
            edge = at % pulse
            if edge > pulse - edge:
                edge = pulse - edge
            y = int(o[i])
            if y >= 32768:
                y -= 65536
            y += (((s * edge * 2) // pulse) * gain) >> 12
            if y > 32767:
                y = 32767
            elif y < -32767:
                y = -32767
            o[i] = y & 0xFFFF
        at += 1
        if at >= period:
            at = 0
        i += 1
    st[0] = phase
    st[2] = at


@micropython.viper
def _amb_ping(out, n: int, tab, state):
    # one note ringing out: 0 phase, 1 step, 2 how the step slides, 3 how loud
    # it still is, 4 how much is kept each sample, 5 gain, 6 samples of its
    # soft beginning left
    o = ptr16(out)  # noqa: F821 - MicroPython Viper builtin
    t = ptr16(tab)  # noqa: F821 - MicroPython Viper builtin
    st = ptr32(state)  # noqa: F821 - MicroPython Viper builtin
    phase = int(st[0])
    step = int(st[1])
    slide = int(st[2])
    env = int(st[3])
    keep = int(st[4])
    gain = int(st[5])
    rise = int(st[6])
    i = 0
    while i < n and env > 8:
        phase = (phase + step) & 0x3FFFFFFF
        step += slide
        s = int(t[(phase >> 18) & 4095])
        if s >= 32768:
            s -= 65536
        x = (s * env) >> 15
        if rise > 0:
            x = (x * (160 - rise)) // 160
            rise -= 1
        y = int(o[i])
        if y >= 32768:
            y -= 65536
        y += (x * gain) >> 12
        if y > 32767:
            y = 32767
        elif y < -32767:
            y = -32767
        o[i] = y & 0xFFFF
        env = (env * keep) >> 15
        i += 1
    st[0] = phase
    st[1] = step
    st[3] = env
    st[6] = rise


def _amb_render(buf, n):
    """Lay the ambience into one slice (which may already hold music): the
    scene that is there, rising to its full level, and any that is leaving."""
    global _amb, _scratch
    if _scratch is None:
        _scratch = memoryview(bytearray(PAD_SLICE * 2))
    if _amb is not None and _amb[2] and time.ticks_diff(time.ticks_ms(), _amb[2]) >= 0:
        _fading.append(_amb)                    # its time is up: it fades too
        _amb = None
    step = max(1, n * 256 // (FADE_S * RATE))
    nothing = memoryview(_NOTHING)
    for entry in ([_amb] if _amb is not None else []) + list(_fading):
        if entry is _amb:
            entry[3] = min(256, entry[3] + step)
        else:
            entry[3] -= step
            if entry[3] <= 0:
                _fading.remove(entry)
                continue
        part = _scratch[:n * 2]
        part[:] = nothing[:n * 2]
        _amb_layers(part, n, entry[1])
        _mix_in(buf, part, 0, n, entry[3])


def _amb_layers(buf, n, layers):
    tab = _table()
    for layer in layers:
        kind, st = layer[0], layer[1]
        if kind == 4:
            st.render(buf, n, _mix_in)
        elif kind == 0:
            if layer[2] != layer[3]:            # its loudness drifts to a new mood
                layer[5] -= n
                if layer[5] <= 0:
                    layer[4] = layer[2] + _rnd(layer[3] - layer[2] + 1)
                    layer[5] = layer[6] * (RATE // 20) + _rnd(layer[6] * (RATE // 10))
                st[5] = st[4] + (layer[4] - st[4]) * min(n, 4000) // 40000    # slowly
            _amb_noise(buf, n, st)
        elif kind == 1:
            _amb_ticks(buf, n, st)
        elif kind == 2:
            if st[2] < n:                       # the next call a little sooner or later
                st[3] = layer[2] + _rnd(layer[2] // 3)
            _amb_chirp(buf, n, tab, st)
        else:
            spec = layer[2]
            layer[3] -= n
            if layer[3] <= 0:
                if layer[4] > 0:                # a quick repeat, as birds do
                    layer[4] -= 1
                    layer[3] = RATE // 8 + _rnd(RATE // 10)
                else:
                    layer[4] = _rnd(spec[8] + 1)
                    layer[3] = (spec[6] + _rnd(spec[7] - spec[6] + 1)) * (RATE // 10)
                hz = spec[1] + _rnd(spec[2] - spec[1] + 1)
                if spec[9]:                     # onto the scale, in its own octave
                    note = SCALE[_rnd(len(SCALE))]
                    while note * 2 <= hz:
                        note *= 2
                    hz = note
                ring = spec[4] * (RATE // 1000)
                st[0] = 0
                st[1] = int(hz / RATE * (1 << 30))
                st[2] = st[1] * spec[3] // 100 // ring
                st[3] = 20000 + _rnd(12000)
                st[4] = 32768 - 32768 // ring
                st[6] = 160
            _amb_ping(buf, n, tab, st)


def soundscape(layers, level=35, minutes=60, title="Recorded atmosphere"):
    """SPEC §26.4: mix recorded PCM backgrounds/events independently of music.

    Each layer has files (catalogue names), mode background/event, gain 0..100,
    and event gap_min/gap_max in seconds. Background variants crossfade;
    events have changing variants, gaps and levels. ambience('off') ends it.
    """
    global _amb
    from . import soundscape as recordings
    adjusted = []
    for spec in layers:
        item = dict(spec)
        item["gain"] = int(item.get("gain", 50)) * max(0, min(100, int(level))) // 100
        adjusted.append(item)
    loaded = recordings.load(adjusted)          # validate/load before replacing the scene
    _fading.clear()
    if _amb is not None:
        _fading.append(_amb)
    _amb = [str(title)[:60], [[4, layer] for layer in loaded], _until(minutes), 0]
    _open()
    return status()


def ambience(scene="rain", level=35, minutes=0):
    """The air around the music: rain, wind, a fire, crickets ... made here and
    never twice the same. It runs on its own - with a piece playing over it or
    with nothing at all - and stays the same scene until ambience("off"), another
    scene, or `minutes` are over; a change fades over a few seconds. `scene` is
    one of ambiences(), or several mixed with +, "rain+fire+owl"; places like
    "forest", "night", "storm", "campfire" are mixtures with a name. `level`
    is 0..100, how loud it is beside the music (35 when left out); the knob
    turns both. Returns status()."""
    global _amb
    if not scene or scene in ("off", "none", "stop"):
        if _amb is not None:
            _fading.append(_amb)                # it sinks away, it is not cut
            _amb = None
        return status()
    wanted = []
    for name in str(scene).replace(" ", "").lower().split("+"):
        for part in PLACES.get(name, name).split("+"):
            if part and part not in AIRS:
                raise ValueError("no ambience %r - there is: %s" % (part, ", ".join(ambiences())))
            if part and part not in wanted:
                wanted.append(part)
    if not wanted or len(wanted) > MAX_AIRS:
        raise ValueError("one to %d sounds at once, joined by +" % MAX_AIRS)
    share = max(0, min(100, int(level)))
    layers = []
    for name in wanted:
        for spec in AIRS[name]:
            seed = random.getrandbits(30)
            if spec[0] == "n":
                low, high = spec[3] * share // 50, spec[4] * share // 50
                layers.append([0, array("i", [seed, 0, spec[2], spec[1], low, low]),
                               low, high, low, 0, spec[5]])
            elif spec[0] == "t":
                layers.append([1, array("i", [seed, 0, spec[1], spec[2],
                                              spec[3] * share // 50, 0, spec[4]])])
            elif spec[0] == "c":
                layers.append([2, array("i", [0, int(spec[1] / RATE * (1 << 30)), _rnd(spec[2]),
                                              spec[2], spec[3], spec[4], spec[5] * share // 50]),
                               spec[2]])
            else:
                layers.append([3, array("i", [0, 0, 0, 0, 32000, spec[5] * share // 50, 0]),
                               spec, _rnd(spec[7] * (RATE // 10)) + RATE // 4, 0])
    if _amb is not None:
        _fading.clear()                         # one scene leaving at a time is enough
        _fading.append(_amb)
    _amb = ["+".join(wanted), layers, _until(minutes), 0]
    _open()
    return status()


def _until(minutes):
    return time.ticks_add(time.ticks_ms(), int(minutes * 60000)) if minutes else 0


def _over(loop, until):
    return not loop or (until and time.ticks_diff(time.ticks_ms(), until) >= 0)


# ---- a melody ----------------------------------------------------------------

def _cycle(freq, amp, wave):
    # Whole periods, about 30 ms of them: a slice that starts and ends at zero
    # can follow itself, or silence, without a click at the seam.
    periods = max(1, int(freq * 0.03))
    n = int(RATE * periods / freq + 0.5)
    buf = bytearray(n * 2)
    step = 2 * math.pi * periods / n
    for i in range(n):
        x = step * i
        if wave == "sine":
            v = math.sin(x)
        elif wave == "square":
            v = 0.45 if math.sin(x) >= 0 else -0.45
        else:                               # soft: a fundamental with two overtones
            v = (math.sin(x) + 0.35 * math.sin(2 * x) + 0.15 * math.sin(3 * x)) / 1.5
        struct.pack_into("<h", buf, i * 2, int(amp * v))
    levels = [buf]                          # the same slice, dying away
    for percent in DECAY[1:]:
        quieter = bytearray(n * 2)
        _scale(buf, quieter, n, percent)
        levels.append(quieter)
    return levels


def _slices(notes, tempo, cache, loop, until):
    """The melody as the slices of sound the speaker is handed, one at a time."""
    beat = 60.0 / tempo
    quiet = memoryview(_SILENCE)
    while True:
        for freq, beats in notes:
            left = int(RATE * beats * beat) * 2
            if freq:
                levels = cache[freq]
                size = len(levels[0])
                gap = min(640, left // 8)           # a breath between notes
                count = (left - gap) // size
                for k in range(count):              # struck, then dying away
                    yield levels[k * len(levels) // count]
                    left -= size
            while left > 0:                         # the rest of the note is silence:
                n = min(left, len(_SILENCE)) & ~1   # never cut a period in the middle
                if not n:
                    break
                yield quiet[:n]
                left -= n
        _now["loops"] += 1
        if _over(loop, until):
            return


# ---- plucked strings -------------------------------------------------------------

def _rnd(n):
    return random.getrandbits(16) % n if n > 1 else 0


def _fit(freq):
    # into the range the speaker carries, by octaves
    while freq < STRING_LOW:
        freq *= 2
    while freq > STRING_HIGH:
        freq /= 2
    return freq


def _string(freq):
    wave_len = RATE / freq - 0.25
    period = int(wave_len)
    frac = int((wave_len - period) * 256)
    amp = int(32767 * FULL)
    buf = bytearray(TAIL * 2)
    prev = 0
    soft = 0
    for i in range(period + 1):                 # the pluck: noise, softened twice -
        v = ((random.getrandbits(16) - 32768) * amp) >> 15      # a finger, not a nail
        prev = (v + prev) >> 1
        soft = (prev + soft) >> 1
        struct.pack_into("<h", buf, i * 2, soft * 2)
    keep = int(32768 * math.exp(math.log(0.35) / (4.0 * freq)))
    _ks(buf, TAIL, period, frac, min(32767, keep))
    _fade_out(buf, TAIL - 8000, 8000)
    return buf


def _mixer(steps):
    """Strings ringing over each other: `steps` yields (what is plucked now,
    how many slices until the next step); what is still sounding is added up."""
    outs = [bytearray(SLICE * 2) for _ in range(3)]     # the speaker may still hold one
    silence = bytes(SLICE * 2)
    cache = {}
    ringing = []
    k = 0

    def one():
        nonlocal k, ringing
        out = outs[k]
        k = (k + 1) % 3
        out[:] = silence
        for voice in ringing:
            if voice[1] >= 0:                   # an echo waits its turn below zero
                _mix_in(out, voice[0], voice[1], SLICE, voice[2])
            voice[1] += SLICE
        ringing = [v for v in ringing if v[1] < TAIL]
        return out

    for plucked, slices in steps:
        for freq, gain in plucked:
            key = int(freq * 10)
            if key not in cache:
                if len(cache) >= MAX_STRINGS:
                    cache.clear()
                cache[key] = _string(freq)
            ringing.append([cache[key], 0, gain])
            for wait, share in ECHOES:
                ringing.append([cache[key], -wait * SLICE, gain * share >> 8])
        for _ in range(slices):
            yield one()
    while ringing:                              # let the last notes ring out
        yield one()


def _melody_steps(notes, tempo, loop, until):
    beat = 60.0 / tempo
    while True:
        for freq, beats in notes:
            slices = max(1, int(beats * beat * RATE / SLICE + 0.5))
            yield ([(_fit(freq), 200 + _rnd(40))] if freq else []), slices
        _now["loops"] += 1
        if _over(loop, until):
            return


def _harp_steps(chords, tempo, loop, until, count_passes=True):
    # Each chord is played as broken notes, half a beat apart, and how it is
    # broken is thrown anew every time: rising, falling, there and back, or
    # wandering; some steps stay silent, some notes sparkle an octave up, and
    # no two are plucked equally hard. The same score never sounds twice alike.
    beat = 60.0 / tempo
    step = max(1, int(beat * 0.5 * RATE / SLICE + 0.5))
    while True:
        for voices, beats in chords:
            count = max(1, int(beats * 2 + 0.5))
            if not voices:
                yield [], step * count
                continue
            notes = sorted(_fit(f) for f in voices)
            size = len(notes)
            shape = _rnd(4)
            pos = 0
            for i in range(count):
                plucked = []
                if i == 0:
                    plucked.append((notes[0], 230))
                    if size > 2 and _rnd(3) == 0:
                        plucked.append((notes[-1], 140))
                elif _rnd(100) < 78:
                    if shape == 0:
                        pos = (pos + 1) % size
                    elif shape == 1:
                        pos = (pos - 1) % size
                    elif shape == 2 and size > 1:
                        j = i % (2 * size - 2)
                        pos = j if j < size else 2 * size - 2 - j
                    elif size > 1:
                        pos = (pos + 1 + _rnd(size - 1)) % size
                    freq = notes[pos]
                    if _rnd(6) == 0 and freq * 2 <= STRING_HIGH:
                        freq *= 2
                    plucked.append((freq, 120 + _rnd(110)))
                yield plucked, step
        if count_passes:
            _now["loops"] += 1
        if _over(loop, until):
            return


def harp(score, tempo=60, volume=None, loop=True, minutes=10, title=""):
    """Play chords she wrote as plucked strings, broken into single notes that
    ring over each other - and broken differently every time, so the piece
    never repeats itself exactly. `score` is chords like pad()'s,
    "A3+E4+C5:8 F3+C4+A4:8 -:2", lengths in beats; `tempo` is beats per minute
    (30..160); `loop` repeats it for `minutes` (0 = until stop()). Loudness is
    the knob (see volume()). Returns status() at once - the piece plays on."""
    chords = S.parse_chords(score)
    if not 30 <= tempo <= 160:
        raise ValueError("a harp's tempo is beats per minute, 30 to 160")
    stop()
    _level(volume)
    _now.update({"playing": True, "title": str(title), "loop": bool(loop),
                 "loops": 0, "notes": len(chords), "error": None})
    _start(_mixer(_harp_steps(chords, tempo, loop, _until(minutes))), RATE)
    return status()


# ---- recorded instruments ----------------------------------------------------------

#: Everything above computes its sound. This replays it: a few recorded notes
#: of a real instrument (cut out of a SoundFont by tools/elecrow_samples.py and
#: kept in /sd/inst), each stretched to the pitch wanted - what a digital piano
#: does. STRUCK ones die away after the stroke; the others are held as long as
#: the chord lasts.
STRUCK = ("harp", "vibes", "musicbox", "piano")
KEYS_LOW, KEYS_HIGH = 130.0, 1600.0
MAX_VOICES = 10
MAX_LAYERS = 3
_inst = {}                      # name -> its zones, for the instruments in use


def instruments():
    """The recorded instruments on the board: names for keys(instrument=...)."""
    import os
    found = []
    for folder in ("/sd/inst", "/inst"):
        try:
            found += [f[:-4] for f in os.listdir(folder) if f.endswith(".bin")]
        except OSError:
            pass
    return sorted(set(found))


def _load(name):
    if name in _inst:
        return _inst[name]
    import gc
    import json
    gc.collect()
    for folder in ("/sd/inst", "/inst"):
        try:
            f = open("%s/%s.bin" % (folder, name), "rb")
            break
        except OSError:
            f = None
    if f is None:
        raise ValueError("no instrument %r on the board - there is: %s"
                         % (name, ", ".join(instruments()) or "none"))
    zones = []
    with f:
        head = json.loads(f.readline())
        for zone in head["zones"]:
            pcm = bytearray(zone["n"] * 2)
            f.readinto(pcm)
            loop = zone.get("loop") or (0, 0)
            zones.append((zone["hz"], pcm, loop[0], loop[1], zone["n"]))
    _inst[name] = zones
    return zones


@micropython.viper
def _sample_in(out, n: int, src, state):
    # One recorded note, stretched and added to the slice. state: 0 where in
    # the recording, 1 the fraction between two samples, 2 how far a step is
    # (65536 = as recorded), 3 loudness now, 4 loudness at the slice's end
    # (0..4096), 5/6 the loop, 7 its length, 8 set to 1 when it has run out.
    o = ptr16(out)  # noqa: F821 - MicroPython Viper builtin
    s = ptr16(src)  # noqa: F821 - MicroPython Viper builtin
    st = ptr32(state)  # noqa: F821 - MicroPython Viper builtin
    pos = int(st[0])
    frac = int(st[1])
    inc = int(st[2])
    g0 = int(st[3])
    g1 = int(st[4])
    ls = int(st[5])
    le = int(st[6])
    last = int(st[7]) - 1
    i = 0
    while i < n:
        if pos >= last:
            st[8] = 1
            break
        a = int(s[pos])
        if a >= 32768:
            a -= 65536
        b = int(s[pos + 1])
        if b >= 32768:
            b -= 65536
        x = a + (((b - a) * (frac >> 4)) >> 12)
        y = int(o[i])
        if y >= 32768:
            y -= 65536
        y += (x * (g0 + ((g1 - g0) * i) // n)) >> 12
        if y > 32767:
            y = 32767
        elif y < -32767:
            y = -32767
        o[i] = y & 0xFFFF
        frac += inc
        pos += frac >> 16
        frac &= 0xFFFF
        if le > 0 and pos >= le - 1:
            pos -= le - ls
        i += 1
    st[0] = pos
    st[1] = frac
    st[3] = g1


def _keys_mixer(layers):
    """Recorded notes sounding together, of one instrument or of several at
    once. Each layer is [its steps, its zones, whether it is struck, its share
    of the loudness]; steps yield (notes to start now as (pitch, loudness
    0..255, slices to hold), slices until that layer's next step)."""
    outs = [bytearray(SLICE * 2) for _ in range(3)]
    silence = bytes(SLICE * 2)
    sounding = []                   # [samples, state, slices still held, level, struck]
    waits = [0] * len(layers)
    live = [True] * len(layers)
    k = 0
    while True:
        for n, layer in enumerate(layers):
            if not live[n] or waits[n] > 0:
                continue
            try:
                notes, waits[n] = next(layer[0])
            except StopIteration:
                live[n] = False
                continue
            zones, struck, share = layer[1], layer[2], layer[3]
            for freq, loud, hold in notes:
                while freq < KEYS_LOW:
                    freq *= 2
                while freq > KEYS_HIGH:
                    freq /= 2
                zone = min(zones, key=lambda z: abs(math.log(freq / z[0])))
                level = loud * share // 255
                st = array("i", [0, 0, int(freq / zone[0] * 65536), level if struck else 0, 0,
                                 zone[2], zone[3], zone[4], 0])     # a stroke starts at full
                if len(sounding) >= MAX_VOICES:
                    sounding.pop(0)
                sounding.append([zone[1], st, max(1, hold), level, struck])
        if not any(live):
            for voice in sounding:
                voice[2] = 0
            if not sounding:
                return
        for n in range(len(waits)):
            if waits[n] > 0:
                waits[n] -= 1
        out = outs[k]
        k = (k + 1) % 3
        out[:] = silence
        alive = []
        for voice in sounding:
            pcm, st, struck = voice[0], voice[1], voice[4]
            now = st[3]
            if voice[2] > 0:                    # held: struck notes die away anyway
                voice[2] -= 1
                st[4] = now * 15 // 16 if struck and now else (now or voice[3])
            else:                               # let go
                st[4] = now * (5 if struck else 6) // 8 if now > 24 else 0
            _sample_in(out, SLICE, pcm, st)
            if st[8] == 0 and (st[3] > 0 or voice[2] > 0):
                alive.append(voice)
        sounding = alive
        yield out


def _hold_steps(chords, tempo, loop, until, lead=True):
    # every voice of a chord, held for as long as the chord lasts
    beat = 60.0 / tempo
    while True:
        for voices, beats in chords:
            slices = max(1, int(beats * beat * RATE / SLICE + 0.5))
            yield [(f, 150 + _rnd(60), slices) for f in voices], slices
        if lead:
            _now["loops"] += 1
        if _over(loop, until):
            return


def _broken_steps(chords, tempo, loop, until, hold, lead=True):
    for plucked, slices in _harp_steps(chords, tempo, loop, until, lead):
        yield [(f, gain, hold) for f, gain in plucked], slices


def _tune_steps(notes, tempo, loop, until, lead):
    # a melody, note for note as she wrote it
    beat = 60.0 / tempo
    while True:
        for freq, beats in notes:
            slices = max(1, int(beats * beat * RATE / SLICE + 0.5))
            yield ([(freq, 225 + _rnd(30), slices)] if freq else []), slices
        if lead:
            _now["loops"] += 1
        if _over(loop, until):
            return


def _aligned_steps(factory, slices_per_pass, loop, until, lead):
    # SPEC §26.4: every sampled layer restarts on the same audio-slice boundary.
    while True:
        left = slices_per_pass
        for notes, slices in factory():
            if left <= 0:
                break
            slices = min(slices, left)
            yield notes, slices
            left -= slices
        if left > 0:
            yield [], left
        if lead:
            _now["loops"] += 1
        if _over(loop, until):
            return


def keys(score, instrument="warmpad", style="", tempo=50, volume=None, loop=True,
         minutes=10, title="", melody="", lead=""):
    """Play chords she wrote on a RECORDED instrument - real samples, like a
    digital piano. `instrument` is one of instruments() (warmpad, halo, choir,
    strings, harp, vibes, musicbox ...), or up to three at once joined by +,
    "warmpad+harp": the held ones carry the chords, the struck ones play
    broken notes over them. `score` is chords like pad()'s,
    "A3+E4+C5:8 F3+C4+A4:8 -:2". `style` is "hold" (all voices of a chord
    sounding together, one chord melting into the next) or "broken" (the chord
    as single notes, a different way each time); left out, held instruments
    are held and struck ones (harp, vibes, musicbox, piano) are broken.
    `tempo` is beats per minute (20..160). Loudness is the knob (see volume()).
    `melody` is a tune like play()'s, "E4 G4 A4:2 - C5", played note for note
    on the instrument `lead` over the chords (the score may then be empty: a
    tune alone); left out, `lead` is the first struck instrument named, or
    the musicbox. Returns status() at once - the piece plays on."""
    tune = S.parse(melody) if str(melody).strip() else []
    chords = S.parse_chords(score) if str(score).strip() or not tune else []
    chords = S.fit_accompaniment(chords, tune)
    if not 20 <= tempo <= 160:
        raise ValueError("tempo is beats per minute, 20 to 160")
    names = [n for n in str(instrument).replace(" ", "").split("+") if n]
    if not 1 <= len(names) <= MAX_LAYERS:
        raise ValueError("one to %d instruments, joined by +" % MAX_LAYERS)
    lead = str(lead).strip() or next((n for n in names if n in STRUCK), "musicbox")
    if not chords:
        names = []                          # a tune alone
    if style not in ("", "hold", "broken"):
        raise ValueError('style is "hold" or "broken"')
    stop()
    for name in list(_inst):
        if name not in names and not (tune and name == lead):
            del _inst[name]                 # room for the ones this piece uses
    packs = [_load(name) for name in names]
    _level(volume)
    _now.update({"playing": True, "title": str(title), "loop": bool(loop),
                 "loops": 0, "notes": len(chords) + len(tune), "error": None})
    until = _until(minutes)
    layers = []
    pass_slices = max(1, int(sum(b for _, b in tune) * 60 / tempo * RATE / SLICE + 0.5))
    if tune:                                # the tune leads; what is under it steps back
        steps = _aligned_steps(lambda: _tune_steps(tune, tempo, False, None, False),
                               pass_slices, loop, until, True)
        layers.append([steps, _load(lead),
                       lead in STRUCK, 1250])
    for n, name in enumerate(names):
        struck = name in STRUCK
        how = style if len(names) == 1 and style else ("broken" if struck else "hold")
        first = n == 0 and not tune
        if how == "hold":
            steps = _hold_steps(chords, tempo, loop, until, first)
        else:
            steps = _broken_steps(chords, tempo, loop, until, 12 if struck else 6, first)
        if tune:
            if how == "hold":
                factory = lambda: _hold_steps(chords, tempo, False, None, False)
            else:
                hold = 12 if struck else 6
                factory = lambda hold=hold: _broken_steps(chords, tempo, False, None, hold, False)
            steps = _aligned_steps(factory, pass_slices, loop, until, False)
        share = 1200 if len(names) == 1 else (1000 if struck else 620)
        if tune:
            share = share * 9 // 20
        layers.append([steps, packs[n], struck, share])
    _start(_keys_mixer(layers), RATE)
    return status()


# ---- a bowed string ----------------------------------------------------------------

#: An erhu, as near as this board comes: one bowed string. Its colour is a
#: fixed mix of overtones, nasal and bright in the middle; what makes it sing
#: is drawn sample by sample - the bow biting in, the pitch sliding from note
#: to note instead of jumping, and a vibrato that only sets in once a note is
#: held, never twice at the same speed or depth.
ERHU_LOW, ERHU_HIGH = 260.0, 1200.0
#: The sound box - a small drum of snakeskin - rings at the same places
#: whatever note is bowed: (centre in Hz, width in Hz, strength). A fixed mix
#: of overtones moved these with every note, and that is an organ, not a
#: fiddle.
ERHU_BOX = ((1200.0, 280.0, 2.6), (2900.0, 500.0, 0.8))


def _erhu_wave(freq):
    # One period of the bowed string at this pitch: a sawtooth's overtones
    # (1/n), each weighed by how the box answers at its frequency.
    parts = []
    h = 1
    while h * freq < 5000 and h <= 16:
        hz = h * freq
        weight = 0.14
        for centre, width, strength in ERHU_BOX:
            weight += strength / (1 + ((hz - centre) / width) ** 2)
        parts.append((h, weight / h))
        h += 1
    norm = sum(w for _, w in parts)
    wave = bytearray(4096)
    sine = _table()
    for h, w in parts:
        _add_sine(wave, 2048, sine, ((h << 30) // 2048) & 0x3FFFFFFF, int(30000 * w / norm))
    return wave


@micropython.viper
def _bow(out, n: int, tab, state):
    # state: 0 phase, 1 step, 2 the step it glides to, 3 loudness, 4 the
    # loudness it swells to, 5 vibrato phase, 6 vibrato depth, 7 vibrato speed,
    # 8 the depth it grows to, 9 how slowly the pitch glides, 10 the rosin's noise
    o = ptr16(out)  # noqa: F821 - MicroPython Viper builtin
    t = ptr16(tab)  # noqa: F821 - MicroPython Viper builtin
    st = ptr32(state)  # noqa: F821 - MicroPython Viper builtin
    phase = int(st[0])
    step = int(st[1])
    goal = int(st[2])
    amp = int(st[3])
    want = int(st[4])
    lfo = int(st[5])
    depth = int(st[6])
    speed = int(st[7])
    deep = int(st[8])
    glide = int(st[9])
    seed = int(st[10])
    i = 0
    while i < n:
        step += (goal - step) >> glide
        amp += (want - amp) >> 9
        depth += (deep - depth) >> 13
        lfo = (lfo + speed) & 0xFFFFFF
        tri = (lfo >> 8) - 32768
        if tri < 0:
            tri = 0 - tri
        tri -= 16384
        vib = ((((step >> 12) * tri) >> 12) * depth) >> 6
        phase = (phase + step + vib) & 0x3FFFFFFF
        s = int(t[(phase >> 19) & 2047])
        if s >= 32768:
            s -= 65536
        seed = seed * 214013 + 2531011          # the hair of the bow on the string
        o[i] = (((s * amp) >> 15) + (((((seed >> 16) & 0xFF) - 128) * amp) >> 13)) & 0xFFFF
        i += 1
    st[10] = seed
    st[0] = phase
    st[1] = step
    st[3] = amp
    st[5] = lfo
    st[6] = depth


def _erhu_fit(freq):
    while freq < ERHU_LOW:
        freq *= 2
    while freq > ERHU_HIGH:
        freq /= 2
    return freq


def _erhu_slices(notes, tempo, loop, until):
    waves = {}
    for freq, _ in notes:
        if freq:
            freq = _erhu_fit(freq)
            if int(freq * 10) not in waves:
                waves[int(freq * 10)] = _erhu_wave(freq)
    tab = bytes(4096)
    state = array("i", [0] * 11)
    state[7] = 5767
    state[9] = 10
    state[10] = 12345
    dry = [bytearray(SLICE * 2) for _ in range(8)]
    outs = [bytearray(SLICE * 2) for _ in range(3)]
    made = 0
    k = 0
    beat = 60.0 / tempo
    top = int(32767 * FULL)

    def one():
        nonlocal made, k
        line = dry[made % 8]
        _bow(line, SLICE, tab, state)
        out = outs[k]
        k = (k + 1) % 3
        out[:] = line
        if made >= 3:                           # the room: itself again, fainter
            _mix_in(out, dry[(made - 3) % 8], 0, SLICE, 70)
        if made >= 6:
            _mix_in(out, dry[(made - 6) % 8], 0, SLICE, 30)
        made += 1
        return out

    while True:
        for freq, beats in notes:
            slices = max(1, int(beats * beat * RATE / SLICE + 0.5))
            if not freq:
                state[4] = 0
                for _ in range(slices):
                    yield one()
                continue
            freq = _erhu_fit(freq)
            tab = waves[int(freq * 10)]
            goal = int(freq / RATE * (1 << 30))
            state[2] = goal
            if state[3] < 600 or _rnd(3) == 0:  # slide up into the note
                state[1] = goal - goal // (14 + _rnd(10))
            state[4] = top * (70 + _rnd(31)) // 100
            state[6] = 0
            state[7] = 5900 + _rnd(1300)            # 5.6 to 6.9 times a second and
            state[8] = 1100 + _rnd(800)             # 60 to 100 cents wide: measured
            state[9] = 11 if _rnd(3) == 0 else 10   # now and then a long slide
            for j in range(slices):
                if j == slices - 1 and slices > 2:      # the bow turning
                    state[4] = state[4] * 6 // 10
                yield one()
        _now["loops"] += 1
        if _over(loop, until):
            break
    state[4] = 0
    for _ in range(8):                          # the last note and its echo die away
        yield one()


def erhu(score, tempo=60, volume=None, loop=False, minutes=0, title=""):
    """Play a melody she wrote on a bowed string, like an erhu: notes that
    swell under the bow, slide into each other and tremble when they are held.
    `score` is a melody like play()'s, "E4:2 G4 A4:3 - C5:2"; long notes (2
    beats and more) are what this voice is for. `tempo` is beats per minute
    (30..160); `loop` repeats it for `minutes` (0 = until stop()). Loudness is
    the knob (see volume()). Returns status() at once - the piece plays on."""
    notes = S.parse(score)
    if not 30 <= tempo <= 160:
        raise ValueError("an erhu's tempo is beats per minute, 30 to 160")
    stop()
    _level(volume)
    _now.update({"playing": True, "title": str(title), "loop": bool(loop),
                 "loops": 0, "notes": len(notes), "error": None})
    _start(_erhu_slices(notes, tempo, loop, _until(minutes)), RATE)
    return status()


def play(score, tempo=90, volume=None, loop=False, minutes=0, title="",
         wave="pluck", background=True):
    """Play a melody she wrote. `score` is notes with lengths in beats,
    "E4 G4 A4:2 - C5:0.5"; `tempo` is beats per minute (30..240); `volume` is
    best left out - loudness is the knob (see volume()), and a number here
    (0..0.25) sets that knob; `loop` repeats it, for `minutes`
    (0 = until stop()); `wave` is "pluck" (a plucked string, the default),
    "soft", "sine" or "square". Returns status()
    at once - the piece plays on. `background=False` waits until it has ended."""
    notes = S.parse(score)
    if not 30 <= tempo <= 240:
        raise ValueError("tempo is beats per minute, 30 to 240")
    if wave not in WAVES:
        raise ValueError("wave is one of: " + ", ".join(WAVES))
    stop()
    amp = _level(volume)
    _now.update({"playing": True, "title": str(title), "loop": bool(loop),
                 "loops": 0, "notes": len(notes), "error": None})
    if wave == "pluck":
        _start(_mixer(_melody_steps(notes, tempo, loop, _until(minutes))), RATE)
    else:
        cache = {}
        for freq, _ in notes:
            if freq and freq not in cache:
                cache[freq] = _cycle(freq, amp, wave)
        _start(_slices(notes, tempo, cache, loop, _until(minutes)), RATE)
    if not background:
        while _now["playing"]:
            time.sleep_ms(20)
    return status()


# ---- a carpet of sound ---------------------------------------------------------

def _chord(voices, amp):
    # One second of the chord. Every part is a whole number of cycles in it, so
    # the slice follows itself without a seam.
    n = PAD_SLICE
    parts = []
    for f in voices:
        for multiple, off, weight in PAD_VOICE:
            cycles = int(f * multiple * n / PAD_RATE + 0.5) + off
            hz = cycles * PAD_RATE // n
            if 0 < hz < SPEAKER_CEILING:
                if hz < SPEAKER_FLOOR:
                    weight = weight * hz * hz / (SPEAKER_FLOOR * SPEAKER_FLOOR)
                parts.append((cycles, weight))
    norm = sum(w for _, w in parts) or 1.0
    buf = bytearray(n * 2)
    tab = _table()
    for cycles, weight in parts:
        _add_sine(buf, n, tab, ((cycles << 30) // n) & 0x3FFFFFFF,
                  int(amp * weight / norm))
    return buf


def _pad_slices(chords, tempo, bank, loop, until):
    n = PAD_SLICE
    beat = 60.0 / tempo
    zero = bytes(n * 2)
    outs = [bytearray(n * 2) for _ in range(3)]     # the speaker may still hold one
    k = 0
    prev = zero
    while True:
        for voices, beats in chords:
            cur = bank[voices] if voices else zero
            count = max(1, int(beats * beat * PAD_RATE / n + 0.5))
            fade = min(PAD_FADE, count)
            for i in range(count):
                if i < fade:                        # rising out of the one before
                    g0, g1 = 256 * i // fade, 256 * (i + 1) // fade
                    out = outs[k]
                    k = (k + 1) % 3
                    _blend(out, n, cur, g0, g1, prev, 256 - g0, 256 - g1)
                    yield out
                else:
                    yield cur
            prev = cur
        _now["loops"] += 1
        if _over(loop, until):
            out = outs[k]
            _blend(out, n, zero, 0, 0, prev, 256, 0)    # and the last one fades
            yield out
            return


def pad(score, tempo=40, volume=None, loop=True, minutes=10, title=""):
    """Play a carpet of sound she wrote: slow chords that swell in and fade out.
    `score` is chords with lengths in beats, voices joined by +,
    "A2+E3+C4:8 F2+C3+A3:8 -:2"; up to 4 voices in a chord and 6 different
    chords in a piece; 4 beats when no length is given. `tempo` is beats per
    minute (20..120) - slow is the point. `volume` is best left out: loudness
    is the knob (see volume()). `loop` repeats it for `minutes` (0 = until stop()). It takes a few
    seconds to prepare, then returns status() - the piece plays on."""
    chords = S.parse_chords(score)
    if not 20 <= tempo <= 120:
        raise ValueError("a pad's tempo is beats per minute, 20 to 120")
    distinct = []
    for voices, _ in chords:
        if voices and voices not in distinct:
            distinct.append(voices)
    if len(distinct) > MAX_PAD_CHORDS:
        raise ValueError("%d different chords; a pad holds %d at most"
                         % (len(distinct), MAX_PAD_CHORDS))
    stop()
    amp = _level(volume)
    bank = {voices: _chord(voices, amp) for voices in distinct}
    _now.update({"playing": True, "title": str(title), "loop": bool(loop),
                 "loops": 0, "notes": len(chords), "error": None})
    _start(_pad_slices(chords, tempo, bank, loop, _until(minutes)), PAD_RATE)
    return status()
