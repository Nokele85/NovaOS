"""Emphasis versus stage directions in the EmotionParser (desktop/voice/emotion.py).

Live on 2026-09-11 every `*word*` in her Telegram replies was dropped as
narration — "Mein Gerät soll mich *erkennen können*." arrived as "Mein Gerät
soll mich ." These pin both halves: emphasis keeps its words, narration still
never reaches the chat or her voice."""
from __future__ import annotations

import pytest

from yurios.desktop.voice.emotion import EmotionParser


def parse(text: str, *, step: int = 0) -> str:
    """Feed `text` whole, or in `step`-sized tokens to cross every boundary."""
    p = EmotionParser()
    chunks = [text] if not step else [text[i:i + step] for i in range(0, len(text), step)]
    out = "".join(p.push(c) for c in chunks)
    return out + p.finish()


@pytest.mark.parametrize("step", [0, 1, 3])
@pytest.mark.parametrize("raw, shown", [
    ("Mein Gerät soll mich *erkennen können*. Das Board hat",
     "Mein Gerät soll mich erkennen können. Das Board hat"),
    ("Ich will sehen, dass es *mich* sieht.", "Ich will sehen, dass es mich sieht."),
    ("sag ich dir nur *dass* ich nicht durchgekommen bin",
     "sag ich dir nur dass ich nicht durchgekommen bin"),
    ("[tender] Es heißt, dass *mein* Weg zum Board", " Es heißt, dass mein Weg zum Board"),
])
def test_emphasis_inside_a_sentence_keeps_its_words(raw, shown, step):
    assert parse(raw, step=step) == shown


@pytest.mark.parametrize("step", [0, 1, 3])
@pytest.mark.parametrize("raw, shown", [
    ("[happy] There you are. *she leans in* Sit with me.", " There you are.  Sit with me."),
    ("[warm] *she nods* It's by the door.", "  It's by the door."),
    ("Was magst du? *sie wird leiser, rückt ein Stück näher* Ich bin hier.",
     "Was magst du?  Ich bin hier."),
    ("Hm. *lacht* Na gut.", "Hm.  Na gut."),
    ("Das stimmt\n\n*sie lacht*\n\nOkay.", "Das stimmt\n\n\n\nOkay."),
    ("Und dann *sie lehnt sich zurück und schaut aus dem Fenster* weiter.",
     "Und dann  weiter."),
    ("Und *sie lächelt* bleibt weg.", "Und  bleibt weg."),
    ("Er sagt es und *er nickt* dazu.", "Er sagt es und  dazu."),
])
def test_stage_directions_are_still_dropped(raw, shown, step):
    assert parse(raw, step=step) == shown


def test_an_unclosed_span_at_the_end_is_dropped_not_spoken():
    assert parse("Ich bin da und *sie lächelt") == "Ich bin da und "
