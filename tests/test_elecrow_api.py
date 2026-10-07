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
