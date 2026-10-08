# Music she writes herself, played on the board's speaker in the background.
#
#   from nova import music
#   music.play("E4 G4 A4:2 - C5:0.5 B4:0.5 A4:4", tempo=80, loop=True, minutes=10)
#   music.stop()
#
# play() returns at once; the piece plays on until it ends, until stop(), or -
# when it loops - until `minutes` are up. One piece at a time: a new play()
# ends the one before it. The score notation is in score.py.
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

_now = {"playing": False, "title": "", "loop": False, "loops": 0, "notes": 0,
        "error": None}
_stop = False
_out = None
_feeder = None
_SILENCE = bytes(960)                   # 30 ms


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


def stop(wait_ms=600):
    """End the piece that is playing. True once the speaker is silent."""
    global _stop
    _stop = True
    start = time.ticks_ms()
    while _now["playing"] and time.ticks_diff(time.ticks_ms(), start) < wait_ms:
        time.sleep_ms(20)               # the speaker's next request ends it
    if _now["playing"]:
        _finish()                       # ...and if none came, end it here
    return not _now["playing"]


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
    """The piece as the slices of sound the speaker is handed, one at a time."""
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
        if not loop:
            return
        if until and time.ticks_diff(time.ticks_ms(), until) >= 0:
            return


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


def play(score, tempo=90, volume=QUIET, loop=False, minutes=0, title="",
         wave="soft", background=True):
    """Play a melody she wrote. `score` is notes with lengths in beats,
    "E4 G4 A4:2 - C5:0.5"; `tempo` is beats per minute (30..240); `volume` is
    0..0.25 and quiet (0.06) when left out; `loop` repeats it, for `minutes`
    (0 = until stop()); `wave` is "soft", "sine" or "square". Returns status()
    at once - the piece plays on. `background=False` waits until it has ended."""
    global _stop, _out, _feeder
    from machine import I2S, Pin

    import nova
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
    until = time.ticks_add(time.ticks_ms(), int(minutes * 60000)) if minutes else 0
    _now.update({"playing": True, "title": str(title), "loop": bool(loop),
                 "loops": 0, "notes": len(notes), "error": None})
    _stop = False
    _feeder = _slices(notes, tempo, cache, loop, until)
    try:
        _out = I2S(0, sck=Pin(B.SPK_BCK), ws=Pin(B.SPK_WS), sd=Pin(B.SPK_DATA),
                   mode=I2S.TX, bits=16, format=I2S.MONO, rate=RATE, ibuf=8192)
        _out.irq(_feed)
        nova.io().mute(False)
        _out.write(_SILENCE)                # the first slice starts the asking
    except Exception:
        _finish()
        raise
    if not background:
        while _now["playing"]:
            time.sleep_ms(20)
    return status()
