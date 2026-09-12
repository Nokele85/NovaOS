"""Scene boundaries in a finished story (yurios/world/stories.py).

Live, on 2026-09-11, seven of nine scenes of a finished story were cut off by
the per-call token ceiling and stitched together mid-word; the end-of-story
check never saw it because it only reads the last sentence. These pin the two
halves of the fix: a scene is asked for under the ceiling, and whatever still
comes back truncated is trimmed to its last complete sentence."""
from __future__ import annotations

import asyncio
import json
import re
import wave

from yurios.world.qwenvoice import QwenVoice
from yurios.world.stories import (SCENE_MAX_TOKENS, StoryLab, _has_complete_sentence,
                                  _trim_to_sentence)


def _voice() -> QwenVoice:
    """A voice these tests never reach: the TTS itself is faked out below."""
    return QwenVoice(speech_url="http://unused", supervisor_url="http://unused",
                     speaker="sohee")


class TruncatingUtility:
    """Every scene comes back cut mid-word, the way a hit ceiling returns it."""

    def __init__(self):
        self.calls: list[int] = []

    async def complete(self, messages, **kwargs):
        # Scene calls only: a state note is asked for with a small budget on
        # purpose, and counting it here would hide the ceiling this pins.
        if "Schreibe die nächste Szene" in messages[-1]["content"]                 or "abschließenden Abschnitt" in messages[-1]["content"]:
            self.calls.append(kwargs.get("max_tokens"))
        if "Antworte nur mit JSON" in messages[-1]["content"]:
            return json.dumps({"title": "Der Leuchtturm", "characters": ["Nova", "Benni"],
                               "scenes": [{"purpose": "Aufbruch", "turn": "Sturm"}]})
        words = " ".join(["Nova und Benni gingen weiter zum Leuchtturm."] * 60)
        return words + " Dann bestell"


def test_a_german_closing_quote_ends_a_sentence():
    """„…“ closes with U+201C; the live story d09e2c5a0a lost its last quote
    mark and gained the fallback line because this read as unfinished."""
    assert _has_complete_sentence("„Danke, dass du bleibst.“")
    assert _has_complete_sentence("»Ja.«")
    assert not _has_complete_sentence("„Danke, dass du")


async def test_two_story_requests_never_share_the_tts_at_once(tmp_path):
    """One job's closing /unload must not pull the voice from under another."""
    active, overlaps, posts = [0], [0], []
    lab = StoryLab(utility=TruncatingUtility(), post=lambda *a, **kw: posts.append(kw),
                   root=tmp_path, voice=_voice(), min_words=10, scenes=1)

    async def fake_tts(text, target):
        active[0] += 1
        overlaps[0] = max(overlaps[0], active[0])
        await asyncio.sleep(0.01)
        target.write_bytes(b"")
        active[0] -= 1
        return target

    async def fake_mp3(path):
        return path.with_suffix(".mp3")

    lab._tts = fake_tts
    lab._telegram_audio = fake_mp3
    lab.start({"theme": "eins"})
    lab.start({"theme": "zwei"})
    while lab._tasks:
        await asyncio.sleep(0.005)
    assert overlaps[0] == 1
    assert len(posts) == 2


def test_trim_keeps_the_last_whole_sentence_and_its_closing_quote():
    assert _trim_to_sentence("Sie lachte. Dann bestell") == "Sie lachte."
    assert _trim_to_sentence("„Ja.“ Er ging z") == "„Ja.“"
    assert _trim_to_sentence("ganz ohne Satzende") == "ganz ohne Satzende"
    assert _trim_to_sentence("Fertig.") == "Fertig."


async def test_no_scene_of_a_finished_story_ends_mid_sentence(tmp_path):
    utility = TruncatingUtility()
    posts = []
    lab = StoryLab(utility=utility, post=lambda *a, **kw: posts.append((a, kw)),
                   root=tmp_path, voice=_voice(), min_words=10, scenes=3)

    async def fake_tts(text, target):
        with wave.open(str(target), "wb") as out:
            out.setnchannels(1); out.setsampwidth(2); out.setframerate(24000)
            out.writeframes(b"\0\0" * 100)
        return target

    async def fake_mp3(path):
        return path.with_suffix(".mp3")

    lab._tts = fake_tts
    lab._telegram_audio = fake_mp3
    lab.start({"theme": "Leuchtturm", "_channel": "telegram"})
    while lab._tasks:
        await asyncio.sleep(0)

    story = next(tmp_path.glob("*/story.md")).read_text(encoding="utf-8")
    paragraphs = [p for p in story.split("\n\n")[1:] if p.strip()]
    assert paragraphs and all(_has_complete_sentence(p) for p in paragraphs)
    assert not re.search(r"bestell\b", story)
    # every scene and the finale were asked for with room to finish
    assert utility.calls and all(t == SCENE_MAX_TOKENS for t in utility.calls)
    assert posts and posts[-1][1].get("audio_path", "").endswith(".mp3")
