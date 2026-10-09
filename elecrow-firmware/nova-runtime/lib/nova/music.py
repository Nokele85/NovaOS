# Music she writes herself, played on the board's speaker in the background.
#
#   from nova import music
#   music.play("E4 G4 A4:2 - C5:0.5 B4:0.5 A4:4", tempo=80, loop=True, minutes=10)
#   music.pad("A2+E3+C4:8 F2+C3+A3:8 C3+G3+E4:8 G2+D3+B3:8", tempo=40)
#   music.stop()
#
# play() is a melody: one voice, each note struck and dying away. pad() is a
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
import struct
import time
from array import array

import micropython

from . import board as B
from . import score as S

RATE = 16000
WAVES = ("soft", "sine", "square")
#: Loudness is a knob on the board, not a property of a piece: 0..100 % in
#: tens, kept across pieces and power cuts, stepped by the board's button (ten
#: up per press, and from 100 round to 0 - sound off). Every piece is rendered
#: at FULL, what 100 % means, and turned down on its way to the speaker.
FULL = 0.4
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
PAD_VOICE = ((1, 0, 1.0), (1, 1, 0.5), (2, 0, 0.3), (3, 0, 0.1))
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
    "error", "volume"} - volume is the knob, in percent."""
    now = dict(_now)
    now["volume"] = volume()
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
    level = volume()
    volume(0 if level >= 100 else level + 10)


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
    """End the piece that is playing. True once the speaker is silent."""
    global _stop
    _stop = True
    start = time.ticks_ms()
    while _now["playing"] and time.ticks_diff(time.ticks_ms(), start) < wait_ms:
        time.sleep_ms(20)               # the speaker's next request ends it
    if _now["playing"]:
        _finish()                       # ...and if none came, end it here
    return not _now["playing"]


def _feed(out):
    # Called by the speaker when it has taken the last slice.
    if _stop or out is not _out:
        if out is _out:
            _finish()
        return
    global _turn
    try:
        data = next(_feeder)
        level = _vol if _vol is not None else FIRST_VOLUME
        if level < 100:                         # the knob, on the way out
            size = len(data)
            quieter = memoryview(_mix[_turn])[:size]
            _turn = (_turn + 1) % len(_mix)
            _scale(data, quieter, size // 2, level)
            data = quieter
        out.write(data)
    except StopIteration:
        _finish()
    except Exception as e:                  # never die without a word
        _now["error"] = repr(e)
        _finish()


def _start(feeder, rate):
    global _stop, _out, _feeder
    from machine import I2S, Pin

    import nova
    global _mix
    _stop = False
    _feeder = feeder
    if _mix is None:
        _mix = [bytearray(PAD_SLICE * 2) for _ in range(3)]
    try:
        knob()
    except Exception:                       # no button is no reason for no music
        pass
    try:
        _out = I2S(0, sck=Pin(B.SPK_BCK), ws=Pin(B.SPK_WS), sd=Pin(B.SPK_DATA),
                   mode=I2S.TX, bits=16, format=I2S.MONO, rate=rate, ibuf=8192)
        _out.irq(_feed)
        nova.io().mute(False)
        _out.write(_SILENCE)                # the first slice starts the asking
    except Exception:
        _finish()
        raise


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
    b = ptr16(buf)
    t = ptr16(tab)
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
    o = ptr16(out)
    pa = ptr16(a)
    pb = ptr16(b)
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
    s = ptr16(src)
    d = ptr16(dst)
    i = 0
    while i < n:
        v = int(s[i])
        if v >= 32768:
            v -= 65536
        d[i] = ((v * num) // 100) & 0xFFFF
        i += 1


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


def play(score, tempo=90, volume=None, loop=False, minutes=0, title="",
         wave="soft", background=True):
    """Play a melody she wrote. `score` is notes with lengths in beats,
    "E4 G4 A4:2 - C5:0.5"; `tempo` is beats per minute (30..240); `volume` is
    best left out - loudness is the knob (see volume()), and a number here
    (0..0.25) sets that knob; `loop` repeats it, for `minutes`
    (0 = until stop()); `wave` is "soft", "sine" or "square". Returns status()
    at once - the piece plays on. `background=False` waits until it has ended."""
    notes = S.parse(score)
    if not 30 <= tempo <= 240:
        raise ValueError("tempo is beats per minute, 30 to 240")
    if wave not in WAVES:
        raise ValueError("wave is one of: " + ", ".join(WAVES))
    stop()
    amp = _level(volume)
    cache = {}
    for freq, _ in notes:
        if freq and freq not in cache:
            cache[freq] = _cycle(freq, amp, wave)
    _now.update({"playing": True, "title": str(title), "loop": bool(loop),
                 "loops": 0, "notes": len(notes), "error": None})
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
            if 0 < cycles * PAD_RATE // n < 5000:   # nothing shrill, nothing the rate folds
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
