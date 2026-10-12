"""Her board apps are checked against what the board really has (tools/elecrow_api.py).

Found live on 2026-10-07: `warm_pad` called `nova.speaker.tone(220, 1200)` and
`nova.display.clear()` for a library where the first needs the io chip and an
import, and the second is a function with no `clear`. It would have slept until
the full hour and died on the tone. Offline: the library is read as source.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import elecrow_api  # noqa: E402

LIB = ROOT / "elecrow-firmware" / "nova-runtime" / "lib" / "nova"
API = elecrow_api.load_api(LIB)

WARM_PAD = '''import time, nova

SENTENCE = "Nova war hier, Benni."
time.sleep(5)
nova.speaker.tone(220, 1200)
try:
    nova.display.clear()
    nova.display.text(SENTENCE, 0, 0)
    nova.display.show()
except Exception as e:
    print("display error:", e)
'''

CORRECT = '''import time, nova
from nova import speaker
speaker.tone(nova.io(), 220, 1200)
d = nova.display()
d.fill()
d.text("Nova war hier.", 8, 8)
d.show()
nova.log("knocked")
'''


def test_the_app_she_really_wrote_is_refused_with_the_right_names():
    said = "\n".join(elecrow_api.check_app({"main.py": WARM_PAD}, API))
    assert "the first argument is the io chip, not 220" in said
    assert "tone(io, freq=880, ms=200" in said
    assert "`nova.display` is a function, not an object" in said
    assert "add `from nova import speaker`" in said


def test_a_correct_app_passes_and_so_does_the_runtime_itself():
    assert elecrow_api.check_app({"main.py": CORRECT}, API) == []
    for shipped in (LIB.parent.parent).glob("*.py"):
        assert elecrow_api.check_app(
            {shipped.name: shipped.read_text(encoding="utf-8")}, API) == [], shipped.name


def test_circuitpython_and_desktop_modules_are_named_for_what_they_are():
    said = "\n".join(elecrow_api.check_app(
        {"main.py": "import board, displayio\nimport numpy as np\n"}, API))
    assert "`import board` — that is CircuitPython" in said
    assert "`import displayio`" in said and "numpy" in said
    # her own module of that name is hers
    assert elecrow_api.check_app({"main.py": "import board\n", "board.py": "X = 1\n"}, API) == []


def test_a_method_the_screen_does_not_have_and_an_argument_too_many():
    said = "\n".join(elecrow_api.check_app({"main.py": (
        "import nova\nd = nova.display()\nd.clear()\nd.text('a', 0, 0, 1, 2, 3)\n"
        "nova.display().blit()\nnova.vibrate()\n")}, API))
    assert "Display has no `clear()`; it has: show, fill, text, snapshot" in said
    assert "d.text takes at most 5 arguments" in said
    assert "Display has no `blit()`" in said
    assert "nova has no `vibrate`" in said


def test_a_syntax_error_is_one_problem_and_not_a_crash():
    said = elecrow_api.check_app({"main.py": "def f(:\n  pass\n"}, API)
    assert len(said) == 1 and "syntax error" in said[0]
    assert elecrow_api.check_app({"notes.txt": "def f(:"}, API) == []


def test_the_reference_is_built_from_the_source_and_shows_a_correct_example():
    card = elecrow_api.api_card(API)
    assert "nova.speaker.tone(io, freq=880, ms=200, volume=0.3, rate=16000)" in card
    assert "class Display — get it with `nova.display()`" in card
    assert "speaker.tone(nova.io(), 220, 1200)" in card
    # the example the card teaches is one the check accepts
    example = card.split("```python\n", 1)[1].split("```", 1)[0]
    assert elecrow_api.check_app({"main.py": example}, API) == []
    only = elecrow_api.api_card(API, "speaker")
    assert "nova.speaker.tone" in only and "nova.net.connect" not in only


# --- her music on the board (docs/tools.md → device_play_music) ----------------

def _score():
    import importlib.util
    path = LIB / "score.py"
    spec = importlib.util.spec_from_file_location("nova_score_t", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_score_is_read_the_way_she_writes_it():
    score = _score()
    notes = score.parse("A4 C4:2 - | F#3:0.5 Bb4,H4")
    assert [round(f, 1) for f, _ in notes] == [440.0, 261.6, 0.0, 185.0, 466.2, 493.9]
    assert [b for _, b in notes] == [1.0, 2.0, 1.0, 0.5, 1.0, 1.0]
    assert score.seconds(notes, 60) == 6.5
    for bad, why in (("X4", "not a note"), ("C9", "octave"), ("C4:long", "length"),
                     ("C4:0", "between 0 and 16"), ("", "empty")):
        try:
            score.parse(bad)
        except ValueError as exc:
            assert why in str(exc), (bad, str(exc))
        else:
            raise AssertionError(f"{bad!r} was accepted")


def test_the_music_tool_refuses_what_the_board_would_and_stays_quiet():
    import elecrow_mcp_server as server
    refusal, code = server.music_call("E4 G4 X9", 90, 0.06, True, 10, "", "soft")
    assert code == "" and refusal["status"] == "bad_score" and "X9" in refusal["error"]
    assert "Nothing was played" in refusal["message"]
    assert server.music_call("E4", 500, 0.06, True, 10, "", "soft")[0]["status"] == "bad_tempo"
    assert server.music_call("E4", 90, 0.06, True, 10, "", "organ")[0]["status"] == "bad_wave"

    facts, code = server.music_call("E4 G4 A4:2", 60, 0.9, True, 9999, "Regen", "soft")
    assert facts == {"notes": 3, "volume": 0.25, "minutes": 120.0, "seconds_per_pass": 4.0}
    assert "volume=0.25" in code and "minutes=120.0" in code and "title='Regen'" in code
    compile(code, "<music>", "exec")
    # played once, there is no clock to run out
    assert server.music_call("E4", 60, 0.06, False, 10, "", "sine")[0]["minutes"] == 0.0
    # no level named: the knob on the board is left where he put it
    facts, code = server.music_call("E4", 60, 0, True, 10, "", "soft")
    assert facts["volume"] is None and "volume=None" in code
    assert "volume=None" in server.pad_call("C3+G3", 40, 0, True, 10, "")[1]
    card = elecrow_api.api_card(API, "music")
    assert "volume(" in card and "knob(" in card


def test_the_reference_shows_her_the_music_module():
    card = elecrow_api.api_card(API, "music")
    assert "play(score" in card and "stop(" in card


def test_a_carpet_of_sound_is_read_and_bounded():
    score = _score()
    chords = score.parse_chords("A2+E3+C4:8 - F2+C3")
    assert [len(v) for v, _ in chords] == [3, 0, 2] and [b for _, b in chords] == [8.0, 4.0, 4.0]
    assert round(chords[0][0][0], 1) == 110.0
    for bad, why in (("C2+E2+G2+B2+D3", "1 to 4 voices"), ("C2+X", "not a note"), ("", "empty")):
        try:
            score.parse_chords(bad)
        except ValueError as exc:
            assert why in str(exc), (bad, str(exc))
        else:
            raise AssertionError(f"{bad!r} was accepted")

    import elecrow_mcp_server as server
    facts, code = server.pad_call("A2+E3+C4:8 F2+C3+A3:8", 60, 0.9, True, 5, "Traum")
    assert facts == {"chords": 2, "volume": 0.25, "minutes": 5.0, "seconds_per_pass": 16.0}
    assert "music.pad(" in code and "title='Traum'" in code
    compile(code, "<pad>", "exec")
    assert server.pad_call("C2+Q", 40, 0.06, True, 10, "")[0]["status"] == "bad_score"
    many = " ".join(f"{n}2+{n}3" for n in "CDEFGAB")
    assert "6 at most" in server.pad_call(many, 40, 0.06, True, 10, "")[0]["error"]
    assert server.pad_call("C2", 10, 0.06, True, 10, "")[0]["status"] == "bad_tempo"
    assert "pad(score" in elecrow_api.api_card(API, "music")


def test_a_module_the_board_does_not_have_is_named_before_it_is_written():
    """Found live (2026-10-09): `import audio` inside a try, and the conclusion
    that the firmware had no sound."""
    problems = elecrow_api.check_app({"main.py": (
        "import time, math, struct\nfrom machine import I2S\n"
        "try:\n    import audio\nexcept ImportError:\n    audio = None\n"
        "import helper\nimport numpy\n"), "helper.py": "X = 1\n"}, API)
    assert len(problems) == 2, problems
    said = " | ".join(problems)
    assert "no module `audio`" in said and "music.pad" in said
    assert "numpy" in said and "helper" not in said and "struct" not in said


def test_plucked_strings_are_a_call_like_the_others():
    import elecrow_mcp_server as server
    facts, code = server.harp_call("A3+E4+C5:8 F3+C4+A4:8", 60, 0, True, 5, "Regen")
    assert facts == {"chords": 2, "volume": None, "minutes": 5.0, "seconds_per_pass": 16.0}
    assert "music.harp(" in code and "volume=None" in code
    compile(code, "<harp>", "exec")
    assert server.harp_call("A3+Q", 60, 0, True, 5, "")[0]["status"] == "bad_score"
    assert server.harp_call("A3", 500, 0, True, 5, "")[0]["status"] == "bad_tempo"
    # a melody is plucked too, unless she asks for another wave
    assert "wave='pluck'" in server.music_call("E4 G4", 80, 0, False, 0, "", "pluck")[1]
    assert "harp(score" in elecrow_api.api_card(API, "music")


def test_a_bowed_string_is_a_call_like_the_others():
    import elecrow_mcp_server as server
    facts, code = server.erhu_call("E4:2 G4 A4:3 -", 60, 0, True, 5, "Sehnsucht")
    assert facts == {"notes": 4, "volume": None, "minutes": 5.0, "seconds_per_pass": 7.0}
    assert "music.erhu(" in code and "title='Sehnsucht'" in code
    compile(code, "<erhu>", "exec")
    assert server.erhu_call("E4 X", 60, 0, True, 5, "")[0]["status"] == "bad_score"
    assert "erhu(score" in elecrow_api.api_card(API, "music")


def test_recorded_instruments_are_a_call_like_the_others():
    import elecrow_mcp_server as server
    facts, code = server.keys_call("A3+E4+C5:8 F3+C4+A4:8", "choir", "", 60, 0, True, 5, "Atem")
    assert facts == {"chords": 2, "instrument": "choir", "volume": None, "minutes": 5.0,
                     "seconds_per_pass": 16.0}
    assert "music.keys(" in code and "instrument='choir'" in code
    compile(code, "<keys>", "exec")
    bad = server.keys_call("A3", "erhu", "", 60, 0, True, 5, "")[0]
    assert bad["status"] == "bad_instrument" and "warmpad" in bad["error"]
    assert server.keys_call("A3", "harp", "sideways", 60, 0, True, 5, "")[0]["status"] == "bad_style"
    # several instruments at once, joined by +
    facts, code = server.keys_call("A3+E4", " warmpad + harp ", "", 50, 0, True, 5, "")
    assert facts["instrument"] == "warmpad+harp" and "instrument='warmpad+harp'" in code
    assert server.keys_call("A3", "warmpad+kazoo", "", 50, 0, True, 5, "")[0]["status"] == "bad_instrument"
    many = server.keys_call("A3", "harp+vibes+choir+halo", "", 50, 0, True, 5, "")[0]
    assert many["status"] == "bad_instrument"
    assert "keys(score" in elecrow_api.api_card(API, "music")


def test_an_instrument_pack_is_cut_from_a_soundfont(tmp_path):
    """tools/elecrow_samples.py, on a SoundFont small enough to build here."""
    import struct

    import elecrow_samples as cut

    def chunk(tag, body):
        return tag + struct.pack("<I", len(body)) + body + (b"\0" if len(body) & 1 else b"")

    def name(text):
        return text.encode().ljust(20, b"\0")

    wave = b"".join(struct.pack("<h", int(9000 * ((i % 100) / 50 - 1))) for i in range(22050))
    shdr = (struct.pack("<20sIIIIIBbHH", name("A4"), 0, 22050, 20000, 22000, 22050, 69, 0, 0, 1)
            + struct.pack("<20sIIIIIBbHH", name("EOS"), 0, 0, 0, 0, 0, 0, 0, 0, 0))
    igen = struct.pack("<HH", 43, 0x7F00) + struct.pack("<HH", 54, 1) + struct.pack("<HH", 53, 0)
    pdta = (chunk(b"phdr", struct.pack("<20sHHHIII", name("Warm"), 89, 0, 0, 0, 0, 0)
                  + struct.pack("<20sHHHIII", name("EOP"), 0, 0, 1, 0, 0, 0))
            + chunk(b"pbag", struct.pack("<HH", 0, 0) + struct.pack("<HH", 1, 0))
            + chunk(b"pgen", struct.pack("<HH", 41, 0) + struct.pack("<HH", 0, 0))
            + chunk(b"inst", struct.pack("<20sH", name("Warm"), 0) + struct.pack("<20sH", name("EOI"), 1))
            + chunk(b"ibag", struct.pack("<HH", 0, 0) + struct.pack("<HH", 3, 0))
            + chunk(b"igen", igen + struct.pack("<HH", 0, 0))
            + chunk(b"shdr", shdr))
    body = b"sfbk" + chunk(b"LIST", b"sdta" + chunk(b"smpl", wave)) + chunk(b"LIST", b"pdta" + pdta)
    path = tmp_path / "tiny.sf2"
    path.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)

    data = cut.pack(cut.load(path), "warmpad")
    head, pcm = data.split(b"\n", 1)
    (zone,) = json.loads(head)["zones"]
    assert zone["hz"] == 440.0 and len(pcm) == zone["n"] * 2
    assert zone["loop"] and zone["loop"][1] == zone["n"]       # it ends where its loop does


def test_the_air_around_the_music_is_a_call_of_its_own():
    import re

    import elecrow_mcp_server as server
    facts, code = server.ambience_call(" Rain + fire ", 40, 30)
    assert facts == {"scene": "rain+fire", "level": 40, "minutes": 30.0}
    assert "music.ambience('rain+fire', level=40" in code
    compile(code, "<ambience>", "exec")
    assert server.ambience_call("off", 50, 0)[0] == {"scene": "off"}
    bad = server.ambience_call("rain+traffic", 50, 0)[0]
    assert bad["status"] == "bad_scene" and "crickets" in bad["error"] and "forest" in bad["error"]
    assert server.ambience_call("storm", 500, 9999)[0] == {"scene": "storm", "level": 100,
                                                           "minutes": 480.0}
    # the names the tool offers are the names the board has
    source = (LIB / "music.py").read_text(encoding="utf-8")
    airs = source[source.index("AIRS = {"):source.index("PLACES = {")]
    places = source[source.index("PLACES = {"):source.index("MAX_AIRS")]
    assert set(re.findall(r'^    "(\w+)": \(', airs, re.M)) == set(server.AMBIENCES)
    assert set(re.findall(r'"(\w+)": "', places)) == set(server.PLACES)
    for mix in re.findall(r': "([\w+]+)"', places):
        assert set(mix.split("+")) <= set(server.AMBIENCES), mix
    assert "ambience(scene" in elecrow_api.api_card(API, "music")


def test_a_tune_is_played_note_for_note_over_the_chords():
    import elecrow_mcp_server as server
    facts, code = server.keys_call("C4+E4+G4:4 F3+A3+C4:4", "strings", "", 60, 0, True, 5, "Lied",
                                   melody="E4 G4 C5:2 A4 G4 E4:2", lead="musicbox")
    assert facts["melody_notes"] == 6 and facts["melody_seconds"] == facts["seconds_per_pass"] == 8.0
    assert "warning" not in facts
    assert "melody='E4 G4 C5:2 A4 G4 E4:2'" in code and "lead='musicbox'" in code
    compile(code, "<tune>", "exec")
    # a tune alone, and one that would drift against its chords
    alone, code = server.keys_call("", "vibes", "", 60, 0, False, 0, "", melody="C4 D4 E4")
    assert alone["chords"] == 0 and alone["melody_notes"] == 3 and "music.keys(''" in code
    drift = server.keys_call("C4+E4:8", "harp", "", 60, 0, True, 5, "", melody="C4 D4 E4")[0]
    assert drift["accompaniment_adjusted"] is True
    assert drift["seconds_per_pass"] == drift["melody_seconds"] == 3.0
    assert server.keys_call("C4", "harp", "", 60, 0, True, 5, "", melody="C4 Q")[0]["status"] == "bad_melody"
    assert server.keys_call("C4", "harp", "", 60, 0, True, 5, "", melody="C4", lead="kazoo")[0]["status"] == "bad_instrument"
