"""Her board apps are checked against what the board really has (tools/elecrow_api.py).

Found live on 2026-10-07: `warm_pad` called `nova.speaker.tone(220, 1200)` and
`nova.display.clear()` for a library where the first needs the io chip and an
import, and the second is a function with no `clear`. It would have slept until
the full hour and died on the tone. Offline: the library is read as source.
"""
from __future__ import annotations

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


def test_the_reference_shows_her_the_music_module():
    card = elecrow_api.api_card(API, "music")
    assert "play(score" in card and "stop(" in card
