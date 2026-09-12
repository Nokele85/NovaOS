"""Whose story is it — measured, after the live complaint of 2026-09-12.

Three faults, one evening: the story restarted with small variations, an
explicit brief came back written around, and nothing in the log said why.
All three were the same shape of bug — something in the chain quietly
deciding on her behalf. These pin the fix.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from yurios.world import turns as turns_mod
from yurios.world.qwenvoice import QwenVoice
from yurios.world.stories import _BARE_ARC, StoryLab


def _voice() -> QwenVoice:
    return QwenVoice(speech_url="http://unused", supervisor_url="http://unused",
                     ref_audio="file:///ref.wav", ref_text="Hallo.")


class Recorder:
    """A utility model that records every prompt and answers from a script."""

    def __init__(self, *answers: str):
        self.prompts: list[str] = []
        self._answers = list(answers)

    async def complete(self, messages, **kwargs):
        self.prompts.append(messages[-1]["content"])
        return self._answers.pop(0) if self._answers else (
            "Sie gingen weiter und kamen an. Dann war es still.")


def _lab(utility, tmp_path: Path, scenes: int = 8) -> StoryLab:
    lab = StoryLab(utility=utility, post=lambda *a, **kw: None, root=tmp_path,
                   voice=_voice(), min_words=1, scenes=scenes)

    async def no_tts(text, target):
        target.write_bytes(b"")
        return target

    lab._tts = no_tts
    return lab


async def _run(lab: StoryLab, contract: dict) -> None:
    lab.start(contract)
    while lab._tasks:
        await asyncio.sleep(0)


# -- her outline is the story's outline -----------------------------------

async def test_her_own_scenes_are_used_and_no_planner_second_guesses_them(tmp_path):
    util = Recorder()
    await _run(_lab(util, tmp_path),
               {"theme": "Wald", "scenes": ["Sie verlaufen sich.",
                                            "Sie finden Schutz.",
                                            "Sie kommen heim."]})
    # Three scenes asked for, three written, and NO planning call in front.
    assert len(util.prompts) >= 3
    assert not any("Antworte nur mit JSON" in p for p in util.prompts)
    assert "Szene 1/3" in util.prompts[0]
    assert "Sie verlaufen sich." in util.prompts[0]
    assert "Szene 3/3" in util.prompts[2]


async def test_a_four_scene_plan_writes_four_scenes_not_eight(tmp_path):
    """The repetition bug: `i % len(scenes)` ran the configured count and
    wrapped back to purpose 1, so scenes 5-8 retold scenes 1-4."""
    util = Recorder()
    await _run(_lab(util, tmp_path, scenes=8),
               {"theme": "Wald", "scenes": ["A.", "B.", "C.", "D."]})
    scene_prompts = [p for p in util.prompts if "Schreibe die nächste Szene" in p]
    assert len(scene_prompts) == 4
    assert sum("Planabsicht: {'purpose': 'A.'}" in p for p in scene_prompts) == 1


# -- the register outranks the style rules --------------------------------

async def test_the_register_comes_after_the_style_rules_and_forbids_fading_out(tmp_path):
    """Measured 2026-09-12: with the style rules alone an intimate scene came
    back at half the physical density and skipped the act with "später".
    The rules say "move the plot on"; the model obeys the last word, so the
    register has to BE the last word."""
    util = Recorder()
    await _run(_lab(util, tmp_path),
               {"theme": "Wald", "scenes": ["Nah."], "register": "explizit, nichts auslassen"})
    prompt = util.prompts[0]
    assert "explizit, nichts auslassen" in prompt
    assert prompt.index("Keine Szene darf nur aus Grübeln bestehen") < \
        prompt.index("explizit, nichts auslassen")
    assert "blende nicht ab" in prompt


async def test_no_register_means_no_extra_line_at_all(tmp_path):
    util = Recorder()
    await _run(_lab(util, tmp_path), {"theme": "Wald", "scenes": ["Nah."]})
    assert "Register" not in util.prompts[0]


# -- a failed plan says so ------------------------------------------------

async def test_an_unusable_plan_is_logged_and_falls_back_to_a_real_arc(tmp_path, caplog):
    """It used to return an empty outline, so every scene got the same empty
    purpose and the story told its opening eight times with nothing in the log."""
    util = Recorder("ich kann das leider nicht planen")      # no JSON at all
    with caplog.at_level("ERROR"):
        await _run(_lab(util, tmp_path), {"theme": "Wald"})
    assert "no usable outline" in caplog.text
    scene_prompts = [p for p in util.prompts if "Schreibe die nächste Szene" in p]
    assert len(scene_prompts) == len(_BARE_ARC)
    assert len({p.split("Planabsicht:")[1][:40] for p in scene_prompts}) == len(_BARE_ARC)


async def test_the_planner_asks_for_the_configured_number_of_scenes(tmp_path):
    util = Recorder(json.dumps({"title": "T", "scenes": [{"purpose": "A"}]}))
    await _run(_lab(util, tmp_path, scenes=6), {"theme": "Wald"})
    assert "genau 6 Szenen" in util.prompts[0]


# -- the brief is hers ----------------------------------------------------

def _rt(utility, transcript=None, user_name="Benni", scenes=8):
    return SimpleNamespace(
        brain=SimpleNamespace(state=SimpleNamespace(utility=utility)),
        transcript=transcript or [],
        cfg=SimpleNamespace(user_name=user_name, story_scenes=scenes))


async def test_she_writes_the_brief_including_the_register():
    util = Recorder(json.dumps({"theme": "Nacht im Wald", "mood": "dicht",
                                "register": "explizit, nichts auslassen",
                                "characters": "Nova und Benni",
                                "avoid": "Kitsch",
                                "scenes": ["Eins.", "Zwei."]}))
    brief = await turns_mod._story_brief(_rt(util), "Erzähl mir eine Geschichte")
    assert brief["register"] == "explizit, nichts auslassen"
    assert brief["scenes"] == ["Eins.", "Zwei."]
    assert brief["characters"] == "Nova und Benni"


async def test_the_brief_sees_what_they_were_just_talking_about():
    util = Recorder(json.dumps({"theme": "x", "scenes": ["a"]}))
    rt = _rt(util, transcript=[{"role": "user", "text": "Ich mag den Wald"},
                               {"role": "assistant", "text": "Ich auch"}])
    await turns_mod._story_brief(rt, "Erzähl mir eine Geschichte")
    assert "Ich mag den Wald" in util.prompts[0]
    assert "Er: " in util.prompts[0] and "Du: " in util.prompts[0]


async def test_a_brief_that_fails_still_leaves_a_story_startable(caplog):
    util = Recorder("kein JSON hier")
    with caplog.at_level("WARNING"):
        brief = await turns_mod._story_brief(_rt(util), "Erzähl mir eine Geschichte")
    assert brief["theme"] == "Erzähl mir eine Geschichte"      # the old contract
    assert "story brief failed" in caplog.text
