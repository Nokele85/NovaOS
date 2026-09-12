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
    await lab._straighten(["Anfang.", "Zweiter."], "ERZÄHLPERSON: Ich-Form", {1: {0: "x"}})
    p = util.prompts[-1]
    # Sentence by sentence, and only the numbers it names come back.
    assert "Satz für Satz" in p
    assert "<Nummer>: <der angeglichene Satz>" in p
    assert "NICHT dein" in p and "Auftrag" in p
    assert "nicht gefällt, ist kein Grund" in p


async def test_a_passage_that_obeys_is_left_exactly_as_written(tmp_path):
    """Nothing the model does not name may change. Not by instruction — we
    simply never replace a sentence it did not return."""
    lab = _lab(Recorder(), tmp_path)
    teile = ["Erster Absatz.", "Zweiter Absatz. Er ist völlig in Ordnung."]
    lab._complete = lambda *a, **kw: _antwort("OK")
    geaendert, gesamt = await lab._straighten(teile, "ANREDE: er", {1: {0: "x", 1: "y"}})
    assert (geaendert, gesamt) == (0, 2)
    assert teile[1] == "Zweiter Absatz. Er ist völlig in Ordnung."


async def test_only_the_named_sentence_changes_and_the_rest_is_untouched(tmp_path):
    """The whole point of going sentence by sentence: a passage used to come
    back whole, which let the model rewrite everything it touched — 8 of 8
    passages and the story 12% longer, twice."""
    lab = _lab(Recorder(), tmp_path)
    teile = ["Anfang.",
             "Du gingst voran. Der Wald war still. Du bliebst stehen."]
    lab._complete = lambda *a, **kw: _antwort("1: Er ging voran.")
    geaendert, gesamt = await lab._straighten(teile, "ANREDE: er", {1: {0: "x", 1: "y"}})
    assert (geaendert, gesamt) == (1, 2)      # zwei geprüft, einer geändert
    assert teile[1] == "Er ging voran. Der Wald war still. Du bliebst stehen."


async def test_a_refused_sentence_costs_that_sentence_and_not_the_passage(tmp_path):
    lab = _lab(Recorder(), tmp_path)
    teile = ["Anfang.", "Du gingst voran. Der Wald war still."]
    # erster Satz kaputt (Pronomen ohne Verb), zweiter sauber
    lab._complete = lambda *a, **kw: _antwort(
        "1: Gingst er voran.\n2: Der Wald ist still.")
    geaendert, _ = await lab._straighten(teile, "ZEITFORM: Präsens", {1: {0: "x", 1: "y"}})
    assert geaendert == 1
    assert teile[1] == "Du gingst voran. Der Wald ist still."


async def test_a_sentence_answered_with_reasoning_is_kept_as_written(tmp_path):
    lab = _lab(Recorder(), tmp_path)
    teile = ["Anfang.", "Du gingst voran. Der Wald war still."]
    lab._complete = lambda *a, **kw: _antwort(
        "1: Verstöße: Anrede falsch. Korrigierter Abschnitt: Er ging voran.")
    geaendert, _ = await lab._straighten(teile, "ANREDE: er", {1: {0: "x", 1: "y"}})
    assert geaendert == 0
    assert teile[1] == "Du gingst voran. Der Wald war still."


async def test_an_answer_that_is_not_numbered_changes_nothing(tmp_path):
    """A line we cannot read is a line we must not apply."""
    lab = _lab(Recorder(), tmp_path)
    teile = ["Anfang.", "Du gingst voran. Der Wald war still."]
    lab._complete = lambda *a, **kw: _antwort(
        "Gerne! Hier ist die überarbeitete Fassung: Er ging voran, still.")
    assert await lab._straighten(teile, "ANREDE: er", {1: {0: "x", 1: "y"}}) == (0, 2)
    assert teile[1] == "Du gingst voran. Der Wald war still."


async def test_a_failing_rules_pass_never_costs_the_story(tmp_path):
    """It runs after the story is finished. Nothing it does may lose it."""
    lab = _lab(Recorder(), tmp_path)
    teile = ["Anfang.", "Zweiter Absatz."]

    async def kaputt(*a, **kw):
        raise RuntimeError("provider weg")

    lab._complete = kaputt
    assert await lab._straighten(teile, "regeln", {1: {0: "x"}}) == (0, 1)
    assert teile == ["Anfang.", "Zweiter Absatz."]


async def test_the_rules_come_from_the_opening_not_from_a_template(tmp_path):
    """Whatever scene 1 established is the law — so the pass cannot impose a
    shape the story never had."""
    util = Recorder()
    lab = _lab(util, tmp_path)
    await lab._house_rules("Du gingst durch den Wald, und Benni ging vor.")
    assert any("Du gingst durch den Wald" in p for p in util.prompts)
    assert any("ERZÄHLPERSON" in p for p in util.prompts)


async def test_the_house_rules_name_address_and_tense(tmp_path):
    util = Recorder()
    lab = _lab(util, tmp_path)
    await lab._house_rules("Du gingst durch den Wald.")
    p = util.prompts[-1]
    assert "ANREDE:" in p and "ZEITFORM:" in p


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


def test_sentences_never_lose_a_word():
    """Splitting is the pass's foundation: a sentence lost here is prose that
    disappears from the story."""
    from yurios.world.stories import _sentences
    text = 'Sie blieb stehen. „Hörst du das?“ Ganz leise … Er nickte und ging'
    assert " ".join(_sentences(text)) == text
    assert len(_sentences(text)) == 4


async def test_an_ok_for_a_single_sentence_is_not_a_correction(tmp_path):
    """"12: OK" means that sentence is fine. Read as a correction it replaces
    the sentence with the word OK, and against a one-word sentence the length
    window lets it through — it reached a published story on 2026-09-12 and
    would have been read out loud."""
    lab = _lab(Recorder(), tmp_path)
    teile = ["Anfang.", "Du gingst voran. Zusammen."]
    lab._complete = lambda *a, **kw: _antwort("1: Er ging voran.\n2: OK")
    geaendert, _ = await lab._straighten(teile, "ANREDE: er", {1: {0: "x", 1: "y"}})
    assert geaendert == 1
    assert teile[1] == "Er ging voran. Zusammen."


def test_no_control_character_ever_hides_in_a_pattern():
    """A guard that cannot match is worse than no guard: it reports safety.

    Writing these regexes through shell heredocs turned "\b" into a literal
    backspace (0x08) five times over. The module imported, ruff passed, mypy
    passed, every test passed — and the patterns silently matched nothing."""
    from pathlib import Path
    import yurios.world.stories as mod
    quelle = Path(mod.__file__).read_bytes()
    schlimm = {b"\x08": "backspace", b"\x0c": "formfeed", b"\x0b": "vertical tab",
               b"\x07": "bell", b"\x1b": "escape"}
    gefunden = {name for byte, name in schlimm.items() if byte in quelle}
    assert not gefunden, f"control characters in stories.py: {gefunden}"


def test_every_guard_actually_matches_what_it_is_for():
    """The patterns, exercised. Each of these is a fault that reached a
    published story on 2026-09-12."""
    from yurios.world.stories import _META, _BROKEN_VERB, _mixed_address
    assert _META.search("Verstöße: 1. Wetter: Es regnet")
    assert _META.search("Korrigierter Abschnitt: Du drückst meine Hände")
    assert not _META.search("Sie ging durch den Wald und blieb stehen.")
    assert _BROKEN_VERB.search('„Komm mit", sagtest er leise')
    assert not _BROKEN_VERB.search("Du sagtest es so, wie man etwas sagt")
    assert _mixed_address(["Du gingst.", "Du bliebst.", "Er sah mich an.",
                           "Du lachtest."]) == 1
    assert _mixed_address(["Du gingst.", "Du bliebst, aber wieder.",
                           "Du lachtest."]) == 0


async def test_every_scene_after_the_first_is_told_the_form_scene_one_set(tmp_path):
    """Measured over five runs: no correction pass, however it was cut, beat
    not breaking the form in the first place. Scene 1 decides it; every scene
    after it is handed that decision verbatim."""
    util = Recorder(
        "Ich ging voran, und du bliebst dicht hinter mir.",      # Szene 1
        "ERZÄHLPERSON: Ich-Form, ich ist Nova\nANREDE: du\nZEITFORM: Vergangenheit",
        "Zweite Szene.")
    await _run(_lab(util, tmp_path), {"theme": "Wald", "scenes": ["A.", "B."]})
    szenen = [p for p in util.prompts if "Schreibe die nächste Szene" in p]
    assert len(szenen) == 2
    # The opening cannot be told a form that does not exist yet.
    assert "Verbindliche Form" not in szenen[0]
    assert "Verbindliche Form" in szenen[1]
    assert "ANREDE: du" in szenen[1]
    assert "ZEITFORM: Vergangenheit" in szenen[1]


async def test_a_form_that_cannot_be_read_never_stops_the_story(tmp_path, caplog):
    """It is a help, not a precondition. If scene 1 yields no form, the later
    scenes simply decide for themselves, as they always did."""
    class Bockig(Recorder):
        async def complete(self, messages, **kwargs):
            self.prompts.append(messages[-1]["content"])
            if "woran sich der Rest halten muss" in messages[-1]["content"]:
                raise RuntimeError("kein Modell")
            return "Eine Szene, die weitergeht und endet."

    with caplog.at_level("WARNING"):
        await _run(_lab(Bockig(), tmp_path), {"theme": "Wald", "scenes": ["A.", "B."]})
    assert "could not read its own form" in caplog.text


# -- the card is handed back even when the process is killed --------------

async def test_a_killed_story_still_gives_the_card_back(tmp_path):
    """A finished story unloads in a `finally`; a cancelled one never reaches
    it. Every story interrupted by a restart on 2026-09-12 left 8.4 GB on the
    card until some later story happened to finish."""
    aus: list[str] = []
    lab = _lab(Recorder(), tmp_path)
    lab._voice_on = True

    async def frei(client):
        aus.append("aus")

    lab.voice.power_off = frei
    lab._tasks.add(asyncio.create_task(asyncio.sleep(60)))
    await lab.close()
    assert aus == ["aus"]
    assert not lab._voice_on
    assert not [t for t in lab._tasks if not t.done()]


async def test_shutting_down_with_no_story_running_touches_nothing(tmp_path):
    """Unloading a voice this process never loaded would take the card from
    whoever else is using it."""
    aus: list[str] = []
    lab = _lab(Recorder(), tmp_path)

    async def frei(client):
        aus.append("aus")

    lab.voice.power_off = frei
    await lab.close()
    assert aus == []


async def test_a_finished_story_leaves_the_flag_down(tmp_path):
    """Otherwise shutdown would unload a voice that is already off — harmless,
    but it would report a release that did not happen."""
    lab = _lab(Recorder(), tmp_path)
    lab._tts = None            # nicht genutzt; wir prüfen nur die Buchführung
    assert lab._voice_on is False


# -- weight goes where she puts it, and people talk -----------------------

async def test_she_decides_how_much_room_each_scene_gets(tmp_path):
    """One live story gave 1898 words to walking through the forest and 699 to
    what had been asked for — because every scene was asked for the same 350
    to 450 words, so length followed scene count instead of importance."""
    util = Recorder()
    await _run(_lab(util, tmp_path),
               {"theme": "Wald", "scenes": ["Hinweg.", "Ankunft.", "Darum ging es."],
                "scene_lengths": ["kurz", "normal", "lang"]})
    szenen = [p for p in util.prompts if "Schreibe die nächste Szene" in p]
    assert "Umfang: 180 bis 250" in szenen[0]
    assert "Umfang: 350 bis 450" in szenen[1]
    assert "Umfang: 600 bis 800" in szenen[2]


async def test_an_unknown_length_falls_back_instead_of_refusing(tmp_path):
    util = Recorder()
    await _run(_lab(util, tmp_path),
               {"theme": "Wald", "scenes": ["A.", "B."],
                "scene_lengths": ["episch", ""]})
    szenen = [p for p in util.prompts if "Schreibe die nächste Szene" in p]
    assert all("Umfang: 350 bis 450" in p for p in szenen)


async def test_every_scene_is_asked_for_real_dialogue(tmp_path):
    """The prompt asked for "action, dialogue and a visible change" all along
    and the spoken share still fell from 22.6% to 12.9% between two stories.
    A share that is not named is not asked for."""
    util = Recorder()
    await _run(_lab(util, tmp_path), {"theme": "Wald", "scenes": ["A.", "B."]})
    szenen = [p for p in util.prompts if "Schreibe die nächste Szene" in p]
    assert szenen and all("ein Drittel der Szene ist gesprochener Dialog" in p
                          for p in szenen)


def test_the_spoken_share_is_measured_not_hoped_for():
    from yurios.world.stories import _dialogue_share
    assert _dialogue_share("Er ging weiter.") == 0.0
    viel = '„Komm mit“, sagte sie. „Ich bin müde“, sagte er. „Trotzdem.“'
    assert _dialogue_share(viel) > 0.4      # die Redezeichen selbst zählen nicht mit


# -- one thinking pass finds, the cheap pass repairs ----------------------

async def test_the_review_reads_the_whole_story_and_thinks(tmp_path):
    """These faults span the story: a clause repeated five times and a shirt
    taken off twice are invisible to a checker that only sees one passage.
    Measured on a real story — 24 913 reasoning tokens, 110 s, 1.6 cents —
    it found every fault a careful reading found, plus one it had missed."""
    gesehen: dict = {}

    class Denker(Recorder):
        async def complete(self, messages, **kwargs):
            gesehen.update(kwargs)
            self.prompts.append(messages[-1]["content"])
            return "3: Anrede weicht ab\n5: Formulierung wiederholt sich"

    lab = _lab(Denker(), tmp_path)
    funde = await lab._review(["Eins. Zwei.", "Drei. Vier.", "Fünf. Sechs."],
                              "ANREDE: du")
    assert gesehen.get("thinking") is True
    assert gesehen.get("max_tokens") >= 32_000       # es denkt über 24k hinaus
    # Satz 3 liegt in Absatz 1 an Stelle 0, Satz 5 in Absatz 2 an Stelle 0
    assert funde == {1: {0: "Anrede weicht ab"},
                     2: {0: "Formulierung wiederholt sich"}}


async def test_the_opening_is_the_law_and_never_a_defendant(tmp_path):
    """The form is read off scene 1, so correcting scene 1 against it would be
    circular — and would move the ground everything else stands on."""
    lab = _lab(Recorder("1: angeblich falsch\n2: auch"), tmp_path)
    funde = await lab._review(["Eins. Zwei.", "Drei."], "ANREDE: du")
    assert funde == {}


async def test_a_failed_review_corrects_nothing_rather_than_everything(tmp_path):
    class Kaputt(Recorder):
        async def complete(self, messages, **kwargs):
            raise RuntimeError("kein Modell")

    lab = _lab(Kaputt(), tmp_path)
    assert await lab._review(["Eins.", "Zwei."], "ANREDE: du") == {}


async def test_only_the_reviewed_passages_are_touched_at_all(tmp_path):
    """It used to send every passage and get 94% of the sentences back. A
    passage nobody complained about is not sent, and costs nothing."""
    util = Recorder("OK")
    lab = _lab(util, tmp_path)
    teile = ["Anfang.", "Zweiter Absatz.", "Dritter Absatz.", "Vierter Absatz."]
    await lab._straighten(teile, "ANREDE: er", {2: {0: "Anrede"}})
    assert len(util.prompts) == 1                     # nur ein Absatz, nicht drei
    assert "Dritter Absatz." in util.prompts[0]
    assert "Zweiter Absatz." not in util.prompts[0]


async def test_a_sentence_nobody_asked_about_is_not_replaced(tmp_path):
    """It sees the whole passage for context, so it may name a neighbour it
    would rather fix. It does not get to decide that."""
    lab = _lab(Recorder(), tmp_path)
    teile = ["Anfang.", "Erster Satz. Zweiter Satz. Dritter Satz."]
    lab._complete = lambda *a, **kw: _antwort("1: Ungefragt.\n2: Gefragt.")
    geaendert, _ = await lab._straighten(teile, "ANREDE: er", {1: {1: "nur der"}})
    assert geaendert == 1
    assert teile[1] == "Erster Satz. Gefragt. Dritter Satz."


async def test_the_review_is_told_how_little_is_spoken_and_asked_to_fix_it(tmp_path):
    """"More dialogue" is not a fault you can point at — but "this sentence
    reports what was said instead of showing it" is, and it is repairable one
    sentence at a time. The share fell to 10% while the prompt was already
    asking for a third."""
    util = Recorder("OK")
    lab = _lab(util, tmp_path)
    await lab._review(["Er ging.", "Ich sagte ihm, dass ich müde war."], "ANREDE: du")
    p = util.prompts[-1]
    assert "BERICHTET über etwas Gesagtes" in p
    assert "ein Drittel wäre richtig" in p
    assert "% der Geschichte" in p          # der gemessene Ist-Wert steht drin
    assert "Beschreibung, Handlung und innere Sätze bleiben" in p


async def test_the_repair_may_turn_a_report_into_spoken_words(tmp_path):
    util = Recorder("OK")
    lab = _lab(util, tmp_path)
    await lab._straighten(["Anfang.", "Zweiter."], "ANREDE: du", {1: {0: "Bericht"}})
    p = util.prompts[-1]
    assert "in gesprochene Rede verwandeln" in p
    assert "Ich bin müde" in p              # das Beispiel, beim Namen genannt


async def test_she_plans_what_stays_unsaid_and_one_thing_to_sense(tmp_path):
    """Planning the topic of a conversation did nothing — 10%, 15%, 13% spoken
    over three runs. The documented remedy is the opposite: give the writer
    what goes unsaid, and the spoken lines have to circle it. The sensory
    anchor closes the other exit, which is summarising a scene instead of
    rendering it."""
    util = Recorder()
    await _run(_lab(util, tmp_path),
               {"theme": "Heimweg", "scenes": ["Der Weg.", "Die Wohnung."],
                "scene_subtext": ["er will fragen, ob sie bleibt, traut sich nicht",
                                  "sie weiß es längst und tut, als wüsste sie es nicht"],
                "scene_sense": ["nasser Stoff an der Haut", "kalter Schlüssel"]})
    szenen = [p for p in util.prompts if "Schreibe die nächste Szene" in p]
    assert "Unausgesprochen zwischen ihnen: er will fragen" in szenen[0]
    assert "darum herumreden" in szenen[0]
    assert "Sinnesanker, der in dieser Szene vorkommt: nasser Stoff" in szenen[0]
    assert "kalter Schlüssel" in szenen[1]


async def test_nothing_unsaid_is_invented_when_she_planned_none(tmp_path):
    util = Recorder()
    await _run(_lab(util, tmp_path), {"theme": "Wald", "scenes": ["A.", "B."]})
    szenen = [p for p in util.prompts if "Schreibe die nächste Szene" in p]
    assert szenen and not any("Unausgesprochen" in p or "Sinnesanker" in p
                              for p in szenen)
