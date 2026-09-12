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


async def _antwort(text: str) -> str:
    """One canned completion, awaited the way the real provider is."""
    return text


def _voice() -> QwenVoice:
    return QwenVoice(speech_url="http://unused", supervisor_url="http://unused",
                     speaker="sohee")


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

    async def no_tts(sections, target):
        target.write_bytes(b"")
        return target

    async def no_mp3(path):
        return path.with_suffix(".mp3")

    lab._tts = no_tts
    lab._telegram_audio = no_mp3
    return lab


async def _run(lab: StoryLab, contract: dict) -> None:
    """Run one job to completion and insist it actually succeeded.

    `_run` swallows every exception and posts an apology instead, so a test
    that only inspects prompts passes just as happily when the job died
    halfway. The posted line is what tells the two apart."""
    posts: list[str] = []
    lab.post = lambda role, text, **kw: posts.append(text)
    lab.start(contract)
    while lab._tasks:
        await asyncio.sleep(0)
    assert posts, "the job posted nothing at all"
    assert "nicht fertig" not in posts[-1], f"the story job failed: {posts[-1]}"


# -- her outline is the story's outline -----------------------------------

async def test_her_own_scenes_are_used_and_no_planner_second_guesses_them(tmp_path):
    util = Recorder()
    await _run(_lab(util, tmp_path),
               {"theme": "Wald", "scenes": ["Sie verlaufen sich.",
                                            "Sie finden Schutz.",
                                            "Sie kommen heim."]})
    # Three scenes asked for, three written, and NO planning call in front.
    szenen = [p for p in util.prompts if "Schreibe die nächste Szene" in p]
    assert len(szenen) == 3
    assert not any("Antworte nur mit JSON" in p for p in util.prompts)
    assert "Szene 1/3" in szenen[0]
    assert "Sie verlaufen sich." in szenen[0]
    assert "Szene 3/3" in szenen[2]


async def test_a_four_scene_plan_writes_four_scenes_not_eight(tmp_path):
    """The repetition bug: `i % len(scenes)` ran the configured count and
    wrapped back to purpose 1, so scenes 5-8 retold scenes 1-4."""
    util = Recorder()
    await _run(_lab(util, tmp_path, scenes=8),
               {"theme": "Wald", "scenes": ["A.", "B.", "C.", "D."]})
    scene_prompts = [p for p in util.prompts if "Schreibe die nächste Szene" in p]
    assert len(scene_prompts) == 4
    assert sum("Planabsicht: A." in p for p in scene_prompts) == 1
    assert not any("{'purpose'" in p for p in scene_prompts)   # Text, kein dict


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


# -- her register reaches the voice, scene by scene -----------------------

async def test_each_scene_is_spoken_with_its_own_register(tmp_path):
    """The whole point of CustomVoice: scene 3 may sound unlike scene 1.
    Base ignored instructions, so 23 minutes arrived in one tone."""
    gesagt: list[tuple[str, str]] = []
    util = Recorder()
    lab = _lab(util, tmp_path)

    async def fake_on_card(sections, target):
        for text, instruct in sections:
            gesagt.append((text[:20], instruct))
        target.write_bytes(b"")
        return target

    lab._tts_on_card = fake_on_card
    lab._tts = StoryLab._tts.__get__(lab)      # das echte, damit _tts_on_card laeuft
    await _run(lab, {"theme": "Wald",
                     "scenes": ["Leicht.", "Dunkel.", "Nah."],
                     "scene_moods": ["fröhlich und leicht",
                                     "gedämpft und spannungsvoll",
                                     "zärtlich und leise"]})
    register = [m for _, m in gesagt]
    assert register[:3] == ["fröhlich und leicht", "gedämpft und spannungsvoll",
                            "zärtlich und leise"]
    # the finale she did not plan is spoken in her plain register, not the last
    # scene's — a closing paragraph whispered because scene 3 was is a bug
    assert register[3:] == [""] * len(register[3:])


async def test_a_story_without_scene_moods_speaks_plainly_throughout(tmp_path):
    gesagt: list[str] = []
    lab = _lab(Recorder(), tmp_path)

    async def fake_on_card(sections, target):
        gesagt.extend(i for _, i in sections)
        target.write_bytes(b"")
        return target

    lab._tts_on_card = fake_on_card
    lab._tts = StoryLab._tts.__get__(lab)      # das echte, damit _tts_on_card laeuft
    await _run(lab, {"theme": "Wald", "scenes": ["Eins.", "Zwei."]})
    assert gesagt and all(i == "" for i in gesagt)


# -- the story carries state, not a tail ----------------------------------

async def test_each_scene_is_handed_state_rather_than_the_previous_tail(tmp_path):
    """The forest was re-introduced 24 times in one live story because the
    hand-off was raw text. A scene now inherits ORT/FIGUREN/GESCHEHEN/OFFEN."""
    util = Recorder(
        "Szene eins, im Wald.",
        "ORT: Waldrand\nFIGUREN: beide, sie friert\nGESCHEHEN: Weg verloren\nOFFEN: der Rückweg",
        "Szene zwei.",
        "ORT: Hütte\nFIGUREN: beide, ruhiger\nGESCHEHEN: Unterschlupf\nOFFEN: der Rückweg")
    await _run(_lab(util, tmp_path), {"theme": "Wald", "scenes": ["A.", "B."]})
    zweite = [p for p in util.prompts if "Szene 2/2" in p]
    assert zweite, "die zweite Szene wurde nie geschrieben"
    assert "ORT: Waldrand" in zweite[0]
    assert "OFFEN: der Rückweg" in zweite[0]


async def test_a_state_note_that_fails_falls_back_to_the_tail_and_says_so(tmp_path, caplog):
    util = Recorder("Sie gingen zum Leuchtturm und blieben stehen.",
                    "völliger Unsinn ohne Struktur")     # keine Akte
    with caplog.at_level("WARNING"):
        await _run(_lab(util, tmp_path), {"theme": "Wald", "scenes": ["A.", "B."]})
    assert "falling back" in caplog.text
    zweite = [p for p in util.prompts if "Szene 2/2" in p]
    assert "Leuchtturm" in zweite[0]          # der alte Weg, aber er trägt


async def test_the_state_note_is_asked_without_her_persona(tmp_path):
    """A record of facts is not a performance; pasting the persona in front of
    it only spends tokens."""
    util = Recorder("Eine Szene.", "ORT: hier\nFIGUREN: beide\nGESCHEHEN: x\nOFFEN: y")
    lab = _lab(util, tmp_path)
    lab.character_context = lambda: SimpleNamespace(
        personality="VERBINDLICHE PERSONA", backbone="", scenario="", drives=[])
    await _run(lab, {"theme": "Wald", "scenes": ["A.", "B."]})
    akte = [p for p in util.prompts if "Zustandsakte" in p]
    assert akte and "VERBINDLICHE PERSONA" not in akte[0]


async def test_the_finale_is_shown_the_whole_arc_and_what_is_still_open(tmp_path):
    """It is told to close every open thread; before this it only ever saw the
    last 3200 characters and could not know which threads those were."""
    akte = ("ORT: Dach\nFIGUREN: beide\nGESCHEHEN: sie steigen hoch\nOFFEN: sein Geständnis")
    util = Recorder("Szene eins.", akte, "Szene zwei.", akte)
    await _run(_lab(util, tmp_path),
               {"theme": "Dach", "scenes": ["Aufstieg aufs Dach.", "Das Geständnis."]})
    finale = [p for p in util.prompts if "abschließenden Abschnitt" in p]
    assert finale, "kein Finale-Durchgang"
    assert "Aufstieg aufs Dach." in finale[0]        # der geplante Bogen
    assert "OFFEN: sein Geständnis" in finale[0]     # und das Offene


# -- a scene knows where to stop, and who everyone is ---------------------

async def test_a_scene_is_told_where_to_stop(tmp_path):
    """Live, 2026-09-12: scene 1 was asked only for "the path narrows" and
    wrote all the way into finding and entering the shelter — scenes 5 and 6's
    material — so those had to discover it a second time."""
    util = Recorder()
    await _run(_lab(util, tmp_path),
               {"theme": "Wald", "scenes": ["Der Weg wird schmaler.",
                                            "Sie finden den Unterstand.",
                                            "Sie gehen heim."]})
    szenen = [p for p in util.prompts if "Schreibe die nächste Szene" in p]
    assert "NÄCHSTE Szene ist: Sie finden den Unterstand." in szenen[0]
    assert "Erzähle sie NICHT mit" in szenen[0]
    # the last scene has nothing after it to trespass on
    assert "NÄCHSTE Szene" not in szenen[-1]


async def test_every_scene_is_told_not_to_switch_pronouns(tmp_path):
    """Benni was "er" in scene 1 and "sie" in scene 3 of the live story."""
    util = Recorder()
    await _run(_lab(util, tmp_path), {"theme": "Wald", "scenes": ["A.", "B."]})
    szenen = [p for p in util.prompts if "Schreibe die nächste Szene" in p]
    assert szenen and all("Pronomen" in p and "wechsle sie niemals" in p
                          for p in szenen)


async def test_the_state_record_tracks_time_and_weather(tmp_path):
    """The rain stopped in scene 3 and started again in scene 5, because the
    record had no slot for when it is and what the weather does."""
    util = Recorder()
    await _run(_lab(util, tmp_path), {"theme": "Wald", "scenes": ["A.", "B."]})
    akte = [p for p in util.prompts if "Zustandsakte" in p]
    assert akte
    for feld in ("ORT:", "ZEIT:", "FIGUREN:", "GESCHEHEN:", "OFFEN:"):
        assert feld in akte[0], feld
    assert "Pronomen" in akte[0]      # carried forward, scene to scene


async def test_her_title_is_used_and_never_her_reasoning(tmp_path):
    """`theme` held her note about the brief, and `theme` was the title — so
    "die frühere Bitte um heftigen Sex" would have ridden along as the caption
    of the recording on his phone."""
    posts: list[dict] = []
    lab = _lab(Recorder(), tmp_path)
    lab.post = lambda role, text, **kw: posts.append({"text": text})
    lab.start({"theme": "Notiz an mich: er wollte etwas Privates",
               "title": "Die Nacht im Unterstand",
               "scenes": ["A.", "B."]})
    while lab._tasks:
        await asyncio.sleep(0)
    assert posts and "Die Nacht im Unterstand" in posts[-1]["text"]
    assert "Notiz an mich" not in posts[-1]["text"]


async def test_without_a_title_the_theme_still_names_the_story(tmp_path):
    posts: list[dict] = []
    lab = _lab(Recorder(), tmp_path)
    lab.post = lambda role, text, **kw: posts.append({"text": text})
    lab.start({"theme": "Der Leuchtturm", "scenes": ["A.", "B."]})
    while lab._tasks:
        await asyncio.sleep(0)
    assert posts and "Der Leuchtturm" in posts[-1]["text"]


# -- the rules pass: narrow, and never destructive ------------------------

async def test_the_pass_is_told_it_corrects_and_does_not_rewrite(tmp_path):
    """It straightened 8 of 8 passages and grew the story by 12%, because the
    instruction was polite instead of operational."""
    util = Recorder("OK", "OK")
    lab = _lab(util, tmp_path)
    await lab._straighten(["Anfang.", "Zweiter."], "ERZÄHLPERSON: Ich-Form")
    p = util.prompts[-1]
    assert "Kein Satz kommt hinzu" in p
    assert "keiner fällt weg" in p and "keiner wird umgestellt" in p
    assert "schöner gemacht" in p
    assert "nicht dein" in p and "Auftrag" in p


async def test_a_passage_that_obeys_is_left_exactly_as_written(tmp_path):
    """A pass that rewrites what is already fine is how good prose gets worn
    down. An obedient passage costs one short "OK" and nothing changes."""
    lab = _lab(Recorder(), tmp_path)
    teile = ["Erster Absatz.", "Zweiter Absatz, völlig in Ordnung."]
    lab._complete = lambda *a, **kw: _antwort("OK")
    n = await lab._straighten(teile, "ERZÄHLPERSON: Du-Form")
    assert n == 0
    assert teile == ["Erster Absatz.", "Zweiter Absatz, völlig in Ordnung."]


async def test_a_correction_that_is_really_a_summary_is_refused(tmp_path):
    """The dangerous failure: the model answers with a tidy précis instead of
    a corrected passage, and a quarter of the story quietly disappears."""
    lab = _lab(Recorder(), tmp_path)
    lang = " ".join(["Ein Satz mit vielen Wörtern darin."] * 30)
    teile = ["Anfang.", lang]
    lab._complete = lambda *a, **kw: _antwort("Sie gingen weiter.")
    with_log = await lab._straighten(teile, "ERZÄHLPERSON: Du-Form")
    assert with_log == 0
    assert teile[1] == lang          # unangetastet


async def test_a_correction_that_grows_the_passage_is_refused(tmp_path):
    """The other direction, and the one the live run hit: 8 of 8 passages
    "corrected" and the story 12% longer. Fixing a pronoun does not add prose."""
    lab = _lab(Recorder(), tmp_path)
    original = " ".join(["Ein Satz mit Wörtern."] * 20)
    aufgeblaeht = original + " " + " ".join(["Und noch ein ganz neuer Satz."] * 12)
    teile = ["Anfang.", original]
    lab._complete = lambda *a, **kw: _antwort(aufgeblaeht)
    assert await lab._straighten(teile, "ERZÄHLPERSON: Du-Form") == 0
    assert teile[1] == original


async def test_a_real_correction_is_taken(tmp_path):
    lab = _lab(Recorder(), tmp_path)
    original = " ".join(["Ich ging durch den nassen Wald weiter."] * 20)
    korrigiert = " ".join(["Du gingst durch den nassen Wald weiter."] * 20)
    teile = ["Anfang.", original]
    lab._complete = lambda *a, **kw: _antwort(korrigiert)
    assert await lab._straighten(teile, "ERZÄHLPERSON: Du-Form") == 1
    assert teile[1].startswith("Du gingst")


async def test_a_failing_rules_pass_never_costs_the_story(tmp_path):
    """It runs after the story is finished. Nothing it does may lose it."""
    lab = _lab(Recorder(), tmp_path)
    teile = ["Anfang.", "Zweiter Absatz."]

    async def kaputt(*a, **kw):
        raise RuntimeError("provider weg")

    lab._complete = kaputt
    assert await lab._straighten(teile, "regeln") == 0
    assert teile == ["Anfang.", "Zweiter Absatz."]


async def test_the_rules_come_from_the_opening_not_from_a_template(tmp_path):
    """Whatever scene 1 established is the law — so the pass cannot impose a
    shape the story never had."""
    util = Recorder()
    lab = _lab(util, tmp_path)
    await lab._house_rules("Du gingst durch den Wald, und Benni ging vor.")
    assert any("Du gingst durch den Wald" in p for p in util.prompts)
    assert any("ERZÄHLPERSON" in p for p in util.prompts)


async def test_a_checker_thinking_out_loud_never_reaches_the_story(tmp_path):
    """Live, 2026-09-12: two passages were published beginning "Ich prüfe den
    Abschnitt gegen die Regeln. Verstöße: 1. Wetter…" — and would have been
    read out loud. The length window waved them through; prose about the rules
    is never a corrected passage."""
    lab = _lab(Recorder(), tmp_path)
    original = " ".join(["Sie ging weiter durch den Wald."] * 20)
    meta = ("Ich prüfe den Abschnitt gegen die Regeln. Verstöße: 1. Wetter. "
            "Korrigierter Abschnitt: " + original)
    teile = ["Anfang.", original]
    lab._complete = lambda *a, **kw: _antwort(meta)
    assert await lab._straighten(teile, "ERZÄHLPERSON: Ich-Form") == 0
    assert teile[1] == original


async def test_the_rules_never_turn_the_plot_into_law(tmp_path):
    """Weather, time of day, tense and names are story. Derived as rules they
    made the checker correct a dawn scene into saying "es wird dunkel"."""
    util = Recorder()
    lab = _lab(util, tmp_path)
    await lab._house_rules("Es war Abend und trocken, und Benni ging vor.")
    p = util.prompts[-1]
    assert "ERZÄHLPERSON" in p and "ANREDE" in p and "ZEITFORM" in p
    assert "Die Hauptfiguren stehen fest: Nova und" in p
    assert "keine weiteren Regeln ab" in p
    assert "Wetter" in p and "gehören zur Handlung" in p


async def test_a_pronoun_swapped_without_its_verb_is_refused(tmp_path):
    """The pass produced "sagtest er" while it was forbidden to change more
    than single words — my own constraint caused it. It may fix the verb now,
    and a passage that still comes back broken is kept as it was."""
    lab = _lab(Recorder(), tmp_path)
    original = " ".join(['„Komm mit", sagtest du leise, und gingst voran.'] * 12)
    kaputt = " ".join(['„Komm mit", sagtest er leise, und gingst er voran.'] * 12)
    teile = ["Anfang.", original]
    lab._complete = lambda *a, **kw: _antwort(kaputt)
    assert await lab._straighten(teile, "ANREDE: er") == 0
    assert teile[1] == original


async def test_the_pass_may_change_the_verb_that_the_pronoun_requires(tmp_path):
    """The instruction has to permit what the job needs, or it produces the
    very fault it was added to remove."""
    util = Recorder("OK")
    lab = _lab(util, tmp_path)
    await lab._straighten(["Anfang.", "Zweiter."], "ANREDE: er")
    p = util.prompts[-1]
    assert "auch die Verbformen" in p
    assert "sagtest er" in p               # the counter-example, by name
    assert "Erzählperson, Anrede und" in p


async def test_the_house_rules_name_address_and_tense(tmp_path):
    util = Recorder()
    lab = _lab(util, tmp_path)
    await lab._house_rules("Du gingst durch den Wald.")
    p = util.prompts[-1]
    assert "ANREDE:" in p and "ZEITFORM:" in p
