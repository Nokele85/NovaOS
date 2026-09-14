"""Her storytelling voice: one card, borrowed and given back.

XTTS narrated at RTF 4.4 and, worse, stayed resident: after the stories of
2026-09-11 the LAN card was still holding 7.2 GB nobody had asked for. Qwen3-TTS
narrates the same text at RTF ~0.48 and hands the card back through a second
service (`/an`, `/zustand`, `/aus`), which is what these pin."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from yurios.world import qwenvoice
from yurios.world.qwenvoice import SPEAK_MAX_WORDS, QwenVoice, passages


def _voice(**kw) -> QwenVoice:
    return QwenVoice(speech_url="http://speak", supervisor_url="http://power",
                     speaker="sohee", **kw)


# -- splitting ------------------------------------------------------------

def test_a_paragraph_that_fits_is_spoken_in_one_breath():
    text = "Erster Absatz mit zwei Sätzen. Der zweite Satz.\n\nZweiter Absatz."
    assert passages(text) == ["Erster Absatz mit zwei Sätzen. Der zweite Satz.",
                              "Zweiter Absatz."]


def test_a_long_paragraph_is_split_between_sentences_never_inside_one():
    # The old path cut every 90 words regardless of where that fell, so a
    # clause was read as if it were a whole sentence and landed flat.
    sentence = " ".join(["Wort"] * 40) + "."
    parts = passages("\n\n".join([" ".join([sentence] * 10)]), max_words=100)
    assert len(parts) > 1
    for part in parts:
        assert part.endswith(".")
        assert part.count(".") <= 3          # whole sentences, not fragments
    assert " ".join(parts).count("Wort") == 400   # nothing lost


def test_a_single_sentence_over_the_ceiling_is_still_one_passage():
    """Better one long request than a sentence sawn in half."""
    assert len(passages("Ein " + "sehr " * 400 + "langer Satz.")) == 1


def test_the_ceiling_stays_clear_of_the_model_s_own_limit():
    # ~2048 tokens is about 2.7 minutes; German narration runs ~2.5 words/s.
    assert SPEAK_MAX_WORDS / 2.5 < 0.75 * 162


# -- the card -------------------------------------------------------------

class _Client:
    def __init__(self, states, *, speech=b"\0\0"):
        self.calls: list[str] = []
        self._states = list(states)
        self._speech = speech

    async def post(self, url, **kw):
        self.calls.append(url.rsplit("/", 1)[-1])
        if url.endswith("/v1/audio/speech"):
            return SimpleNamespace(content=self._speech, raise_for_status=lambda: None)
        state = self._states.pop(0) if self._states else {}
        return SimpleNamespace(content=b"{}", json=lambda: state,
                               raise_for_status=lambda: None)

    async def get(self, url, **kw):
        self.calls.append(url.rsplit("/", 1)[-1])
        state = self._states.pop(0) if self._states else {"bereit": True}
        return SimpleNamespace(content=b"{}", json=lambda: state,
                               raise_for_status=lambda: None)


def test_a_model_that_is_already_resident_is_not_waited_for():
    client = _Client([{"bereit": True, "vram_mib": 8245}])
    asyncio.run(_voice().power_on(client))
    assert client.calls == ["an"]


def test_loading_is_waited_out_by_asking_the_supervisor(monkeypatch):
    monkeypatch.setattr(qwenvoice, "LOAD_POLL_S", 0)
    client = _Client([{"status": "startet", "bereit": False},
                      {"bereit": False, "vorgang": "startet"},
                      {"bereit": True, "vram_mib": 8245, "seit_s": 111.0}])
    asyncio.run(_voice().power_on(client))
    assert client.calls == ["an", "zustand", "zustand"]


def test_a_load_that_fails_is_an_error_not_a_silent_pass(monkeypatch):
    monkeypatch.setattr(qwenvoice, "LOAD_POLL_S", 0)
    client = _Client([{"bereit": False}, {"bereit": False, "vorgang": "fehler",
                                          "anzeige": "Fehler"}])
    with pytest.raises(RuntimeError):
        asyncio.run(_voice().power_on(client))


def test_unloading_never_raises_because_the_story_is_already_written():
    class Broken:
        async def post(self, url, **kw):
            raise httpx.ConnectError("supervisor gone")

    asyncio.run(_voice().power_off(Broken()))    # logs, does not raise


def test_the_supervisor_token_travels_but_the_speech_call_is_plain():
    voice = _voice(token="geheim")
    assert voice._headers == {"x-companion-token": "geheim"}
    assert _voice()._headers == {}


def test_the_speaker_rides_on_every_call_and_the_register_only_when_given():
    """CustomVoice, not Base: the speaker is the identity and `instructions` is
    the per-scene register. Base ignored instructions, which is why 23 minutes
    of story arrived in one flat tone (measured 2026-09-12)."""
    plain = _voice()._body("Ein Satz.")
    assert plain["voice"] == "sohee"
    assert plain["language"] == "German"
    assert plain["response_format"] == "pcm"
    assert "instructions" not in plain          # no register = her plain voice
    assert plain["task_type"] == "CustomVoice"     # the server validates this
    assert "ref_audio" not in plain
    # Not streamed on purpose: the model plans prosody over a whole request.
    assert "stream" not in plain

    tense = _voice()._body("Ein Satz.", "gedämpft und spannungsvoll")
    assert tense["instructions"] == "gedämpft und spannungsvoll"
    assert tense["voice"] == "sohee"


def test_a_line_of_dialogue_may_name_another_of_the_model_s_speakers():
    """The director casts a line; the loaded model already holds every voice."""
    line = _voice()._body("Rein.", "low and firm", "dylan")
    assert line["voice"] == "dylan"
    assert line["instructions"] == "low and firm"
    assert _voice()._body("Rein.", "", "")["voice"] == "sohee"
