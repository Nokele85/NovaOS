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


@pytest.mark.parametrize("step", [0, 1, 3])
def test_an_arithmetic_star_does_not_invert_the_rest_of_the_reply(step):
    """§9.7a, from a live reply. One `*` in `u * 2.1` opened a span that closed
    at her next real star, and from there parity was inverted to the end: every
    word *between* her stars stayed and every other word went. The chat showed
    "`u Kopfdanebenstellen oder sagen…echtbeginntdirdanachdich", and the promise
    she made further down never reached the scan."""
    raw = ("*Da ist sie.* Sterne. Und dann diese kleine Kurve — `u * 2.1`, ein Pony. "
           "Sie haben mir einen *Kopf* gebaut. Kein Logo. Ich hab *echt* gesucht.")
    assert parse(raw, step=step) == (
        " Sterne. Und dann diese kleine Kurve — `u * 2.1`, ein Pony. "
        "Sie haben mir einen Kopf gebaut. Kein Logo. Ich hab echt gesucht.")


@pytest.mark.parametrize("step", [0, 1, 3])
def test_a_lone_star_inside_a_span_is_not_its_close(step):
    """Whitespace on both sides: it can neither open nor close (CommonMark)."""
    assert parse("Na gut. *sie rechnet 2 * 3 im Kopf* Sechs.", step=step) == \
        "Na gut.  Sechs."


@pytest.mark.parametrize("step", [0, 1, 3])
def test_a_star_that_ends_the_stream_still_closes_its_span(step):
    """The held star has no next char to decide it; at the end it is the close."""
    assert parse("Ich will, dass es *mich*", step=step) == "Ich will, dass es mich"
    assert parse("Hm. Gut *", step=step) == "Hm. Gut "
