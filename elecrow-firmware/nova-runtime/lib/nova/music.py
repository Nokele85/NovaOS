# Music she writes herself, played on the board's speaker in the background.
#
#   from nova import music
#   music.play("E4 G4 A4:2 - C5:0.5 B4:0.5 A4:4", tempo=80, loop=True, minutes=10)
#   music.pad("A2+E3+C4:8 F2+C3+A3:8 C3+G3+E4:8 G2+D3+B3:8", tempo=40)
#   music.stop()
#
# play() is a melody: one voice, note after note. pad() is a carpet of sound:
# slow chords of up to four voices, each with a faint octave above it that
# drifts against it, swelling in and fading out. Both return at once; the piece
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

from . import board as B
from . import score as S

RATE = 16000
WAVES = ("soft", "sine", "square")
#: Background music is company, not an alarm. QUIET is the level when she names
#: none, and LOUDEST is the most a piece may ask for.
QUIET, LOUDEST = 0.06, 0.25

#: A pad is rendered at half the rate (it has no bright edges to lose) in
#: half-second slices, each kept at PAD_LEVELS loudnesses so a chord can swell
#: and fade without a sample being touched while it plays.
PAD_RATE = 8000
PAD_SLICE = 4000
PAD_LEVELS = 5
MAX_PAD_CHORDS = 6

_now = {"playing": False, "title": "", "loop": False, "loops": 0, "notes": 0,
        "error": None}
_stop = False
_out = None
_feeder = None
_SILENCE = bytes(960)                   # 30 ms at RATE


def status():
    """What is playing right now: {"playing", "title", "loop", "loops", "notes", "error"}."""
    return dict(_now)


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
    try:
        out.write(next(_feeder))
    except StopIteration:
        _finish()
    except Exception as e:                  # never die without a word
        _now["error"] = repr(e)
        _finish()


def _start(feeder, rate):
    global _stop, _out, _feeder
    from machine import I2S, Pin

    import nova
    _stop = False
    _feeder = feeder
    try:
        _out = I2S(0, sck=Pin(B.SPK_BCK), ws=Pin(B.SPK_WS), sd=Pin(B.SPK_DATA),
                   mode=I2S.TX, bits=16, format=I2S.MONO, rate=rate, ibuf=8192)
        _out.irq(_feed)
        nova.io().mute(False)
        _out.write(_SILENCE)                # the first slice starts the asking
    except Exception:
        _finish()
        raise


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
    return buf


def _slices(notes, tempo, cache, loop, until):
    """The melody as the slices of sound the speaker is handed, one at a time."""
    beat = 60.0 / tempo
    quiet = memoryview(_SILENCE)
    while True:
        for freq, beats in notes:
            left = int(RATE * beats * beat) * 2
            if freq:
                tone = cache[freq]
                gap = min(640, left // 8)           # a breath between notes
                for _ in range((left - gap) // len(tone)):
                    yield tone
                    left -= len(tone)
            while left > 0:                         # the rest of the note is silence:
                n = min(left, len(_SILENCE)) & ~1   # never cut a period in the middle
                if not n:
                    break
                yield quiet[:n]
                left -= n
        _now["loops"] += 1
        if _over(loop, until):
            return


def play(score, tempo=90, volume=QUIET, loop=False, minutes=0, title="",
         wave="soft", background=True):
    """Play a melody she wrote. `score` is notes with lengths in beats,
    "E4 G4 A4:2 - C5:0.5"; `tempo` is beats per minute (30..240); `volume` is
    0..0.25 and quiet (0.06) when left out; `loop` repeats it, for `minutes`
    (0 = until stop()); `wave` is "soft", "sine" or "square". Returns status()
    at once - the piece plays on. `background=False` waits until it has ended."""
    notes = S.parse(score)
    if not 30 <= tempo <= 240:
        raise ValueError("tempo is beats per minute, 30 to 240")
    if wave not in WAVES:
        raise ValueError("wave is one of: " + ", ".join(WAVES))
    stop()
    amp = int(32767 * max(0.0, min(LOUDEST, volume)))
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

def _pad_levels(voices, amp):
    # Half a second of the chord, every voice set to a whole number of cycles in
    # it so the slice follows itself without a seam. That rounding is at most a
    # hertz, and it is what makes the voices lean against each other; the octave
    # above each one is a cycle sharp on purpose, a shimmer two seconds long.
    n = PAD_SLICE
    parts = []
    for f in voices:
        parts.append((int(f * n / PAD_RATE + 0.5), 1.0))
        parts.append((int(2 * f * n / PAD_RATE + 0.5) + 1, 0.2))
    norm = sum(w for _, w in parts)
    steps = [(2 * math.pi * c / n, amp * w / norm) for c, w in parts]
    sin = math.sin
    full = bytearray(n * 2)
    for i in range(n):
        v = 0.0
        for step, gain in steps:
            v += gain * sin(step * i)
        struct.pack_into("<h", full, i * 2, int(v))
    levels = []
    for k in range(1, PAD_LEVELS):
        part = bytearray(n * 2)
        for i in range(0, n * 2, 2):
            struct.pack_into("<h", part, i,
                             struct.unpack_from("<h", full, i)[0] * k // PAD_LEVELS)
        levels.append(part)
    levels.append(full)
    return levels


def _pad_slices(chords, tempo, bank, loop, until):
    beat = 60.0 / tempo
    quiet = bytes(PAD_SLICE * 2)
    while True:
        for voices, beats in chords:
            n = max(1, int(beats * beat * PAD_RATE / PAD_SLICE + 0.5))
            if not voices:
                for _ in range(n):
                    yield quiet
                continue
            levels = bank[voices]
            for i in range(n):                  # swell in, hold, fade out
                yield levels[min(PAD_LEVELS, i + 1, n - i) - 1]
        _now["loops"] += 1
        if _over(loop, until):
            return


def pad(score, tempo=40, volume=QUIET, loop=True, minutes=10, title=""):
    """Play a carpet of sound she wrote: slow chords that swell in and fade out.
    `score` is chords with lengths in beats, voices joined by +,
    "A2+E3+C4:8 F2+C3+A3:8 -:2"; up to 4 voices in a chord and 6 different
    chords in a piece; 4 beats when no length is given. `tempo` is beats per
    minute (20..120) - slow is the point. `volume` is 0..0.25, quiet when left
    out. `loop` repeats it for `minutes` (0 = until stop()). It takes a few
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
    amp = int(32767 * max(0.0, min(LOUDEST, volume)))
    bank = {voices: _pad_levels(voices, amp) for voices in distinct}
    _now.update({"playing": True, "title": str(title), "loop": bool(loop),
                 "loops": 0, "notes": len(chords), "error": None})
    _start(_pad_slices(chords, tempo, bank, loop, _until(minutes)), PAD_RATE)
    return status()
