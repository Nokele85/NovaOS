import asyncio
import json
import wave
from pathlib import Path

from yurios.world.stories import StoryLab


from yurios.world.qwenvoice import QwenVoice


def _voice() -> QwenVoice:
    """Never reached: these tests fake the TTS call itself."""
    return QwenVoice(speech_url="http://unused", supervisor_url="http://unused",
                     ref_audio="file:///ref.wav", ref_text="Hallo.")


class Utility:
    def __init__(self):
        self.calls = 0

    async def complete(self, messages, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return json.dumps({"title": "Der leuchtende Wald", "characters": ["Mara"],
                               "scenes": [{"purpose": "Aufbruch", "turn": "Fund"}]})
        return "Mara ging weiter. Sie fand den Weg nach Hause und schloss die Tür hinter sich."


async def test_story_job_writes_complete_artifacts_and_posts_audio(tmp_path):
    posts = []
    lab = StoryLab(utility=Utility(), post=lambda *a, **kw: posts.append((a, kw)),
                   root=tmp_path, voice=_voice(), min_words=10, scenes=2)

    async def fake_tts(text, target):
        with wave.open(str(target), "wb") as out:
            out.setnchannels(1); out.setsampwidth(2); out.setframerate(24000)
            out.writeframes(b"\0\0" * 100)
        return target

    lab._tts = fake_tts
    lab.start({"theme": "Wald", "mood": "ruhig", "_channel": "telegram"})
    while lab._tasks:
        await asyncio.sleep(0)
    assert (next(tmp_path.iterdir()) / "story.md").is_file()
    assert posts and posts[0][1]["audio_path"].endswith("story.mp3")


async def test_story_job_does_not_publish_below_minimum_word_contract(tmp_path):
    posts = []
    lab = StoryLab(utility=Utility(), post=lambda *a, **kw: posts.append((a, kw)),
                   root=tmp_path, voice=_voice(), min_words=500, scenes=2)

    async def fake_tts(text, target):
        raise AssertionError("short story must not reach TTS")

    lab._tts = fake_tts
    lab.start({"theme": "Wald", "mood": "ruhig", "_corr_id": "corr-story"})
    while lab._tasks:
        await asyncio.sleep(0)

    assert posts and posts[0][0][1].startswith("Die Geschichte konnte")
    assert not list(tmp_path.glob("*/story.md"))


async def test_story_event_keeps_origin_correlation_id(tmp_path):
    posts = []
    lab = StoryLab(utility=Utility(), post=lambda *a, **kw: posts.append((a, kw)),
                   root=tmp_path, voice=_voice(), min_words=10, scenes=2)

    async def fake_tts(text, target):
        with wave.open(str(target), "wb") as out:
            out.setnchannels(1); out.setsampwidth(2); out.setframerate(24000)
            out.writeframes(b"\0\0" * 100)
        return target

    async def fake_mp3(path: Path):
        return path.with_suffix(".mp3")

    lab._tts = fake_tts
    lab._telegram_audio = fake_mp3
    lab.start({"theme": "Wald", "mood": "ruhig", "_corr_id": "corr-story"})
    while lab._tasks:
        await asyncio.sleep(0)

    assert posts[0][1]["corr_id"] == "corr-story"
