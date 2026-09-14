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
        # The per-scene check runs between every pair of scenes. Answering it
        # cleanly here keeps it out of the scripted answers, so a test can go
        # on saying what the SCENES come back as.
        if "Du prüfst EINE Szene" in messages[-1]["content"]:
            return "AUFTRAG: erfüllt\nBEGINN: richtig"
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
        for text, instruct, _speaker in sections:
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
        gesagt.extend(i for _, i, _ in sections)
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
    last 3200 characters and could not know which threads those were. It now
    runs only after extension scenes, so this story is made too short."""
    akte = ("ORT: Dach\nFIGUREN: beide\nGESCHEHEN: sie steigen hoch\nOFFEN: sein Geständnis")
    util = Recorder("Szene eins.", akte, "Szene zwei.", akte)
    lab = _lab(util, tmp_path)
    lab.min_words = 20
    await _run(lab, {"theme": "Dach", "scenes": ["Aufstieg aufs Dach.", "Das Geständnis."]})
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

def test_sentences_never_lose_a_word():
    """Splitting is the pass's foundation: a sentence lost here is prose that
    disappears from the story."""
    from yurios.world.stories import _sentences
    text = 'Sie blieb stehen. „Hörst du das?“ Ganz leise … Er nickte und ging'
    assert " ".join(_sentences(text)) == text
    assert len(_sentences(text)) == 4


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

async def test_she_plans_what_they_talk_about_in_each_scene(tmp_path):
    """Turning reported speech around afterwards moved the spoken share from
    10% to 15% and stopped there. A story written with little speech in it
    cannot be talked into having more — so the talking is planned."""
    util = Recorder()
    await _run(_lab(util, tmp_path),
               {"theme": "Heimweg", "scenes": ["Der Weg.", "Die Wohnung."],
                "scene_talk": ["ob sie den letzten Bus noch kriegen",
                               "warum er den ganzen Abend so still war"]})
    szenen = [p for p in util.prompts if "Schreibe die nächste Szene" in p]
    assert "sprechen die beiden über: ob sie den letzten Bus noch kriegen" in szenen[0]
    assert "warum er den ganzen Abend so still war" in szenen[1]
    assert all("als gesprochene Rede aus, nicht als Bericht" in p for p in szenen)


async def test_a_scene_without_a_planned_conversation_is_not_given_an_empty_one(tmp_path):
    util = Recorder()
    await _run(_lab(util, tmp_path), {"theme": "Wald", "scenes": ["A.", "B."]})
    szenen = [p for p in util.prompts if "Schreibe die nächste Szene" in p]
    assert szenen and not any("sprechen die beiden über" in p for p in szenen)


async def test_the_scenes_are_written_as_a_radio_play(tmp_path):
    """Three attempts to talk a narrator into more speech failed — 10%, 15%,
    13%, 14% spoken over four runs of the same request. This is not a tweak to
    the same form but a different one: in a radio play the speech carries the
    story, and the finished thing is a recording anyway. The prose was always
    written for the page and then read out."""
    util = Recorder()
    await _run(_lab(util, tmp_path), {"theme": "Heimweg", "scenes": ["A.", "B."]})
    szenen = [p for p in util.prompts if "Schreibe die nächste Szene eines Hörspiels" in p]
    assert len(szenen) == 2
    assert all("lebt von gesprochener Rede" in p for p in szenen)
    assert all("nur so viel, wie das Ohr braucht" in p for p in szenen)


async def test_scenes_are_written_at_the_temperature_writing_wants(tmp_path):
    """Measured across three settings: 0.75 gives 13% spoken, 1.2 gives 12.1%,
    1.5 gives 19.7% — and at 1.5 the writing stops following its own plan
    (9.3% of sentences mix the address, and an outline demanding an explicit
    scene comes back at a fifth of the usual density). The dial tips rather
    than trades, so it stays where the story is best."""
    gesehen: list[float] = []

    class Temperaturen(Recorder):
        async def complete(self, messages, **kwargs):
            gesehen.append(kwargs.get("temperature"))
            self.prompts.append(messages[-1]["content"])
            return "Eine Szene, die weitergeht. Und endet."

    util = Temperaturen()
    lab = _lab(util, tmp_path)
    await _run(lab, {"theme": "Wald", "scenes": ["A.", "B."]})
    szenen = [tp for tp, pr in zip(gesehen, util.prompts)
              if "Schreibe die nächste Szene" in pr]
    andere = [tp for tp, pr in zip(gesehen, util.prompts)
              if "Zustandsakte" in pr or "woran sich der Rest" in pr]
    assert szenen and all(tp == 0.75 for tp in szenen)
    # Planen, Akte und Prüfen wollen richtig sein, nicht überraschend
    assert andere and all(tp == 0.75 for tp in andere)


# -- the second pass, whose only job is that they talk --------------------

async def test_bodies_are_not_assumed_silent(tmp_path):
    """Measured on a finished story: the physical passages are 51% of the text
    and carry 15% speech, while the two conversational ones reach 32%. That
    block is where the missing dialogue is — and people do talk."""
    util = Recorder()
    await _run(_lab(util, tmp_path),
               {"theme": "Wald", "scenes": ["A.", "B."], "register": "explizit"})
    szenen = [p for p in util.prompts if "Schreibe die nächste Szene" in p]
    assert all("Auch dabei wird gesprochen" in p for p in szenen)
    assert all("Stumme Körper sind unwahrscheinlicher" in p for p in szenen)


# -- what a computer finds better than a reader ---------------------------

def test_a_thing_in_two_neighbouring_passages_is_a_suspect():
    """Live, 2026-09-12: the key turned in passage 2 and again in 3, the
    jackets fell in 3 and again in 4 — and the reviewer named 37 other
    sentences and missed every one. Counting is what a computer is for."""
    from yurios.world.stories import _echoes
    teile = ["Wir gingen los.",
             "Ich drehte den Schlüssel im Schloss.",
             "Der Schlüssel drehte sich zweimal, dann gab das Schloss nach.",
             "Die Jacke fiel zu Boden.",
             "Ihre Jacke landete neben meiner."]
    gefunden = " ".join(_echoes(teile))
    assert "schlüssel in Absatz 2 und 3" in gefunden
    assert "schloss in Absatz 2 und 3" in gefunden
    assert "jacke in Absatz 4 und 5" in gefunden


def test_a_thing_running_through_the_whole_story_is_a_motif_not_a_repeat():
    """Ranking by frequency finds hands and breath, which is the opposite of
    the question: a thing in nine passages belongs there."""
    from yurios.world.stories import _echoes
    teile = [f"Seine Hand lag auf meiner Schulter, Absatz {i}." for i in range(1, 7)]
    assert _echoes(teile) == []


# -- a scene is checked before it counts ----------------------------------

async def test_a_scene_that_did_its_job_is_not_touched(tmp_path):
    lab = _lab(Recorder(), tmp_path)
    lab._complete = lambda *a, **kw: _antwort("AUFTRAG: erfüllt\nBEGINN: richtig")
    assert await lab._verify_scene(
        "Text.", "davor", "Auftrag", "danach") == ("", "")


async def test_a_scene_that_went_somewhere_else_is_named(tmp_path):
    """Passage 2 walked into the stairwell it was not due in, and passage 3
    then unlocked the same door a second time. The end-of-story review named
    37 other sentences and missed both — it reads for contradictions and
    cannot know where a scene was supposed to be."""
    lab = _lab(Recorder(), tmp_path)
    lab._complete = lambda *a, **kw: _antwort(
        "AUFTRAG: verfehlt kein Gehweg, ihr seid schon im Hauseingang\n"
        "BEGINN: FALSCH die Szene setzt erst im Hauseingang ein")
    offen, anfang = await lab._verify_scene(
        "Text.", "davor", "Gehweg", "Haustür")
    assert "kein Gehweg" in offen
    assert "Hauseingang" in anfang


async def test_leading_to_the_next_scene_is_allowed_and_said_so(tmp_path):
    """Asked whether a scene *touches* the next one's material, the check fired
    on 6 to 7 of 8 scenes in every story measured — which is not a finding,
    it is the handover storytelling is made of."""
    util = Recorder("AUFTRAG: erfüllt\nBEGINN: richtig")
    lab = _lab(util, tmp_path)
    await lab._verify_scene("Text.", "davor", "Auftrag", "danach")
    p = util.prompts[-1]
    assert "darf zur Tür führen" in p
    assert "FALSCH nur dann, wenn der Ort oder die Lage" in p


async def test_a_failed_check_lets_the_scene_stand(tmp_path):
    lab = _lab(Recorder(), tmp_path)

    async def kaputt(*a, **kw):
        raise RuntimeError("kein Modell")

    lab._complete = kaputt
    assert await lab._verify_scene("Text.", "d", "a", "n") == ("", "")


async def test_a_repair_that_loses_half_the_scene_is_refused(tmp_path):
    """A repair that satisfies the brief and drops half the prose is not one."""
    antworten = []

    class Streng(Recorder):
        async def complete(self, messages, **kwargs):
            self.prompts.append(messages[-1]["content"])
            if "Du prüfst EINE Szene" in messages[-1]["content"]:
                return ("AUFTRAG: erfüllt\n"
                        "BEGINN: FALSCH sie setzt erst im Flur ein")
            antworten.append(1)
            # erste Szene lang, die Reparatur viel zu kurz
            return "Kurz." if len(antworten) > 1 else " ".join(["Wort"] * 60) + "."

    lab = _lab(Streng(), tmp_path, scenes=1)
    await _run(lab, {"theme": "Wald", "scenes": ["A."]})
    md = next(tmp_path.glob("*/story.md")).read_text(encoding="utf-8")
    assert "Kurz." not in md.split("\n\n")[1]
    assert len(antworten) >= 2  # die Reparatur wurde wirklich versucht


# -- a scene is told where it starts, not only what it must cover ---------

def test_the_ground_a_scene_opens_on_is_read_off_its_own_line():
    """Five of eight scenes opened on a later beat of their own brief — on the
    pavement while he was already unlocking the door. The place is the leading
    chunks of the outline line, up to where it starts naming people."""
    from yurios.world.stories import _opening
    assert _opening("Gehweg und Straße, beide, wir laufen schnell — Ungeduld.")         == "Gehweg und Straße"
    # two scenes in the same flat are told apart by the second chunk
    assert _opening("Wohnung, Bett, beide, er fickt mich.") == "Wohnung, Bett"
    assert _opening("Wohnung, Flur und Wand, beide, Kuss.") == "Wohnung, Flur und Wand"
    # a line that names only people anchors nothing, and says so
    assert _opening("beide, irgendwo") == ""
    assert _opening("") == ""


def test_the_seam_is_the_last_words_the_listener_heard():
    """The state note says what changed; it does not say what was just said.
    Handing over the actual last sentences is what lets the next scene
    continue instead of setting up again."""
    from yurios.world.stories import _last_sentences
    naht = _last_sentences("Er sucht den Schlüssel. Sie friert. „Komm schon.“")
    assert naht == "Sie friert. „Komm schon.“"
    assert _last_sentences("") == ""


# -- a story makes sense from one scene to the next ------------------------

async def test_a_scene_is_written_with_the_whole_story_so_far(tmp_path):
    """325edf7838: scenes saw a 120-word note and two sentences, so a figure
    nobody had introduced was "still screaming" in scene 4 and a threat at the
    end of scene 6 was gone in scene 7. The next scene sees the text itself."""
    util = Recorder("Erste Szene mit einem Hund namens Micky.")
    await _run(_lab(util, tmp_path), {"theme": "Park", "scenes": ["Im Park.", "Am Teich."]})
    zweite = [p for p in util.prompts if "Szene 2/2" in p]
    assert zweite, "keine zweite Szene geschrieben"
    assert "Die Geschichte bis hierher, vollständig" in zweite[0]
    assert "Erste Szene mit einem Hund namens Micky." in zweite[0]


async def test_a_scene_is_not_made_to_arrive_where_the_last_one_already_did(tmp_path):
    """325edf7838 ended scene 5 at a car park and scene 6 described the same car
    park again, because it was told the first paragraph must play there."""
    util = Recorder()
    await _run(_lab(util, tmp_path), {"theme": "x", "scenes": [
        "Parkplatz, beide, sie steigen aus.", "Supermarkt, beide, sie kaufen ein."]})
    erste = [p for p in util.prompts if "Szene 1/2" in p][0]
    zweite = [p for p in util.prompts if "Szene 2/2" in p][0]
    assert "Der erste Absatz spielt dort." in erste
    assert "gehört an diesen Ort: Supermarkt" in zweite
    assert "beschreibst den Ort nicht noch einmal" in zweite
    assert "setzt hier ein" not in zweite


async def test_a_story_that_ends_in_its_last_scene_gets_no_second_ending(tmp_path):
    """The last scene is told to close every thread; a finale after it anyway
    gave every story two endings and 325edf7838 three."""
    util = Recorder("Szene eins.", "ORT: a", "Szene zwei.", "ORT: b")
    await _run(_lab(util, tmp_path), {"theme": "Dach", "scenes": ["A.", "B."]})
    assert not [p for p in util.prompts if "abschließenden Abschnitt" in p]
    letzte = [p for p in util.prompts if "Szene 2/2" in p]
    assert letzte and "Diese Szene ist die letzte" in letzte[0]


# -- the form is set before the first word ----------------------------------

async def test_the_form_is_set_before_the_first_word(tmp_path):
    """Read off scene 1, the form was left to chance: in the A/B run of
    2026-09-14 scene 1 told the story from Benni's side with Nova in the third
    person, and every correction afterwards made it worse."""
    util = Recorder()
    await _run(_lab(util, tmp_path), {"theme": "Wald", "scenes": ["A.", "B."]})
    szenen = [p for p in util.prompts if "Schreibe die nächste Szene" in p]
    assert len(szenen) == 2
    assert all("Verbindliche Form, die in jedem Satz gilt" in p for p in szenen)
    assert all("Ich-Erzählerin ist Nova" in p for p in szenen)
    assert all("Benni wird durchgehend mit „du“ angesprochen" in p for p in szenen)
    assert all("genau eine Erzählstimme" in p for p in szenen)


async def test_her_own_form_wins_over_the_house_form(tmp_path):
    util = Recorder()
    await _run(_lab(util, tmp_path), {"theme": "Wald", "scenes": ["A.", "B."],
                                      "form": "Personaler Erzähler aus Bennis Sicht, Präteritum"})
    szenen = [p for p in util.prompts if "Schreibe die nächste Szene" in p]
    assert all("Personaler Erzähler aus Bennis Sicht, Präteritum" in p for p in szenen)
    assert not any("Ich-Erzählerin ist Nova" in p for p in szenen)


async def test_she_names_the_form_in_her_brief():
    util = Recorder(json.dumps({"theme": "x", "scenes": ["a"],
                                "form": "Ich-Erzählerin Nova, Benni ist du, Präsens"}))
    brief = await turns_mod._story_brief(_rt(util), "Erzähl mir eine Geschichte")
    assert brief["form"] == "Ich-Erzählerin Nova, Benni ist du, Präsens"
    assert "\"form\"" in util.prompts[0]


# -- one reading by an editor ------------------------------------------------

def _lektor_antwort(*funde):
    return json.dumps({"funde": [
        {"satz": s, "kategorie": k, "befund": "b", "korrektur": c} for s, k, c in funde]})


async def _lektorat(tmp_path, parts, *funde, rules="FORM", register="REGISTER"):
    lab = _lab(Recorder(), tmp_path)
    gesehen: dict = {}

    async def fake(prompt, **kwargs):
        gesehen["prompt"], gesehen["kwargs"] = prompt, kwargs
        return _lektor_antwort(*funde)

    lab._complete = fake
    ergebnis = await lab._lektorat(parts, rules, register)
    return parts, ergebnis, gesehen


async def test_the_editor_reads_the_whole_story_once_and_thinks(tmp_path):
    parts = ["Eins. Zwei.", "Drei."]
    _, _, gesehen = await _lektorat(tmp_path, parts)
    p, kw = gesehen["prompt"], gesehen["kwargs"]
    assert "[Absatz 1]" in p and "[Absatz 2]" in p
    assert "1: Eins." in p and "3: Drei." in p
    assert "Verbindliche Form: FORM" in p and "REGISTER" in p
    assert "Deutliches abschwächen" in p
    assert kw["thinking"] is True
    assert kw["response_format"] == {"type": "json_object"}


async def test_a_correction_inside_one_sentence_is_applied_and_nothing_else_moves(tmp_path):
    """An axe that should be the hatchet from before — measured, and right."""
    parts = ["Ich nahm das Beil. Dann ging ich.", "Ich hielt die Axt auf den Knien. Es regnete."]
    parts, (geaendert, _), _ = await _lektorat(
        tmp_path, parts, (3, "figuren", "Ich hielt das Beil auf den Knien."))
    assert geaendert == 1
    assert parts[0] == "Ich nahm das Beil. Dann ging ich."
    assert parts[1] == "Ich hielt das Beil auf den Knien. Es regnete."


async def test_a_perspective_finding_is_never_patched_a_sentence_at_a_time(tmp_path):
    """Applied sentence by sentence it left every passage half "ich", half "Nova"."""
    parts = ["Nova wischte sich die Stirn. Du sahst zu."]
    parts, (geaendert, hinweise), _ = await _lektorat(
        tmp_path, parts, (1, "perspektive", "Ich wischte mir die Stirn."))
    assert geaendert == 0 and hinweise == 1
    assert parts[0] == "Nova wischte sich die Stirn. Du sahst zu."


async def test_a_sentence_is_struck_only_if_it_really_repeats_one_before_it(tmp_path):
    parts = ["Yara sagte eine Nummer ins Gerät, Datum, Uhrzeit. "
             "Yara sagte die Nummer ins Gerät, Datum und Uhrzeit. "
             "Dann klingelte das Telefon im Wagen."]
    parts, (geaendert, hinweise), _ = await _lektorat(
        tmp_path, parts, (2, "uebergang", "STREICHEN"), (3, "uebergang", "STREICHEN"))
    assert geaendert == 1 and hinweise == 1
    assert parts[0] == ("Yara sagte eine Nummer ins Gerät, Datum, Uhrzeit. "
                        "Dann klingelte das Telefon im Wagen.")


async def test_a_correction_that_balloons_or_argues_keeps_the_sentence(tmp_path):
    parts = ["Er ging. Sie blieb."]
    lang = "Er ging langsam und zögernd durch den dunklen, feuchten, endlos langen Flur hinaus."
    parts, (geaendert, hinweise), _ = await _lektorat(
        tmp_path, parts, (1, "sprache", lang),
        (2, "rede", "Korrigierter Abschnitt: Sie blieb."))
    assert geaendert == 0 and hinweise == 2
    assert parts[0] == "Er ging. Sie blieb."


async def test_the_editor_is_handed_the_suspects(tmp_path):
    parts = ["Ich drehte den Schlüssel.", "Der Schlüssel drehte sich noch einmal."]
    _, _, gesehen = await _lektorat(tmp_path, parts)
    assert "schlüssel in Absatz 1 und 2" in gesehen["prompt"]


async def test_a_story_is_edited_once_and_never_by_the_old_passes(tmp_path):
    class MitLektor(Recorder):
        async def complete(self, messages, **kwargs):
            if "Du bist Lektorin" in messages[-1]["content"]:
                self.prompts.append(messages[-1]["content"])
                return '{"funde": []}'
            return await super().complete(messages, **kwargs)

    util = MitLektor()
    await _run(_lab(util, tmp_path), {"theme": "Wald", "scenes": ["A.", "B."]})
    assert sum("Du bist Lektorin" in p for p in util.prompts) == 1
    assert not any("Satz für Satz nummeriert.\n\nVerbindliche Form" in p and "Lektorin" not in p
                   for p in util.prompts)
    assert not any("mehr gesprochene Rede" in p for p in util.prompts)


async def test_a_failing_editor_never_costs_the_story(tmp_path, caplog):
    """Recorder answers the editor with prose, not JSON — the story still ships."""
    with caplog.at_level("WARNING"):
        await _run(_lab(Recorder(), tmp_path), {"theme": "Wald", "scenes": ["A.", "B."]})
    assert "the lektorat failed; publishing as written" in caplog.text


# -- a full stop is not always the end of a sentence ------------------------

def test_an_abbreviation_does_not_end_a_sentence():
    """Ten of thirty-six findings of the first Lektor run were "Dr." split off
    from "Halbach"."""
    from yurios.world.stories import _sentences
    assert _sentences("Dr. Halbach kam. Sie sagte: „Ja.“") == ["Dr. Halbach kam.", "Sie sagte: „Ja.“"]
    assert _sentences("Das ist z. B. ein Beil. Und dann?") == ["Das ist z. B. ein Beil.", "Und dann?"]


def test_an_ordinal_joins_its_month_but_a_number_can_still_end_a_sentence():
    from yurios.world.stories import _sentences
    assert _sentences("Am 3. Mai kam er. Dann ging er.") == ["Am 3. Mai kam er.", "Dann ging er."]
    assert _sentences("Er zählte bis 43. Dann ging er.") == ["Er zählte bis 43.", "Dann ging er."]


def test_a_spoken_passage_is_not_split_after_an_abbreviation():
    from yurios.world.qwenvoice import rejoin_abbreviations
    assert rejoin_abbreviations(["Da stand Dr.", "Halbach im Regen."]) == ["Da stand Dr. Halbach im Regen."]


async def test_an_editor_that_answers_empty_is_asked_once_more(tmp_path):
    """DeepSeek's JSON mode "may occasionally return empty content"; the first
    live run of the editor hit it on one of two stories."""
    lab = _lab(Recorder(), tmp_path)
    antworten = ["", _lektor_antwort((1, "sprache", "Er ging schnell."))]
    aufrufe = []

    async def fake(prompt, **kwargs):
        aufrufe.append(prompt)
        return antworten.pop(0)

    lab._complete = fake
    parts = ["Er ging eben schnell."]
    geaendert, _ = await lab._lektorat(parts, "FORM", "REGISTER")
    assert len(aufrufe) == 2
    assert geaendert == 1 and parts == ["Er ging schnell."]


def test_an_empty_findings_list_is_an_answer_and_a_missing_one_is_not():
    from yurios.world.stories import _findings
    assert _findings('{"funde": []}') == []
    assert _findings('{"findings": [{"satz": 1}]}') == [{"satz": 1}]
    assert _findings("") is None
    assert _findings('{"notes": "nichts"}') is None
    assert _findings("kein json") is None


# -- the director casts every line, and never touches a letter ---------------

SZENE = ("Marek steht am Spalt. „Neu“, sagt er. „Keiner war drin.“ "
         "„Beruhigend“, sage ich. „Rein“, sagst du.")


def _regie(*zeilen, analyse=None) -> str:
    return json.dumps({"analyse": analyse or {"lage": "Am Höhleneingang."},
                       "lines": [{"i": i, "speaker": s, "warum": "…", "instruction": a}
                                 for i, (s, a) in enumerate(zeilen)]})


def test_a_scene_is_split_into_narration_and_speech_without_losing_a_word():
    from yurios.world.stories import _segments
    teile = _segments(SZENE)
    assert teile == [("Marek steht am Spalt.", False), ("Neu", True), (", sagt er.", False),
                     ("Keiner war drin.", True), ("Beruhigend", True), (", sage ich.", False),
                     ("Rein", True), (", sagst du.", False)]
    # what is spoken is exactly what the story says, minus the quotation marks
    assert "".join(t for t, _ in teile).replace(" ", "") == \
        SZENE.replace("„", "").replace("“", "").replace(" ", "")


async def test_the_director_sees_the_whole_story_and_understands_before_it_directs(tmp_path):
    lab = _lab(Recorder(), tmp_path)
    gesehen: dict = {}

    async def fake(prompt, **kwargs):
        gesehen["prompt"], gesehen["kwargs"] = prompt, kwargs
        return _regie(*[("narrator", "low")] * 8)

    lab._complete = fake
    await lab._direct("s", ["Davor war Wald.", SZENE], ["", ""], "FORM", "Marek, Bergführer")
    p, kw = gesehen["prompt"], gesehen["kwargs"]
    assert "Davor war Wald." in p and "Form of the story: FORM" in p
    assert "Marek, Bergführer" in p and "STEP 1 — UNDERSTAND" in p
    assert "[1] Neu" in p and "never mention accent" in p
    assert kw["thinking"] is True and kw["response_format"] == {"type": "json_object"}


async def test_every_line_is_spoken_by_its_cast_voice_with_its_own_instruction(tmp_path):
    """Nova and the narration in her voice, the user in his, anyone else by gender."""
    lab = StoryLab(utility=Recorder(), post=lambda *a, **k: None, root=tmp_path,
                   voice=_voice(), voices={"user": "dylan", "man": "ryan"})

    async def fake(prompt, **kwargs):
        return _regie(("narrator", "low, even"), ("man", "flat, clipped"),
                      ("narrator", "quick"), ("man", "deadpan"), ("nova", "dry, sardonic"),
                      ("narrator", "quick"), ("user", "low and firm"), ("narrator", "soft"))

    lab._complete = fake
    gesprochen = await lab._direct("s", [SZENE], ["gedämpft"], "FORM", "", tmp_path)
    assert gesprochen[0] == ("Marek steht am Spalt.", "low, even", "")
    assert gesprochen[1][2] == "ryan"
    # the accent note is the code's, never the director's
    assert gesprochen[1][1] == "flat, clipped Clear standard German pronunciation, no English accent."
    assert gesprochen[4] == ("Beruhigend", "dry, sardonic", "")
    assert gesprochen[6] == ("Rein", "low and firm", "dylan")
    assert json.loads((tmp_path / "regie.json").read_text(encoding="utf-8"))[0]["analyse"]


async def test_narration_is_never_given_to_a_character(tmp_path):
    lab = _lab(Recorder(), tmp_path)

    async def fake(prompt, **kwargs):
        return _regie(*[("man", "loud")] * 8)

    lab._complete = fake
    gesprochen = await lab._direct("s", [SZENE], [""], "FORM", "")
    assert gesprochen[0][2] == "" and gesprochen[2][2] == ""      # narration: her voice
    assert gesprochen[1][2] == "ryan"                             # speech: as cast


async def test_a_scene_without_direction_is_spoken_as_before_and_the_story_ships(tmp_path, caplog):
    """Recorder answers the director with prose — the scene keeps her voice and mood."""
    gesagt: list = []
    lab = _lab(Recorder(), tmp_path)

    async def fake_on_card(sections, target):
        gesagt.extend(sections)
        target.write_bytes(b"")
        return target

    lab._tts_on_card = fake_on_card
    lab._tts = StoryLab._tts.__get__(lab)
    with caplog.at_level("WARNING"):
        await _run(lab, {"theme": "Wald", "scenes": ["A."], "scene_moods": ["leise"]})
    assert "no direction; spoken in her own voice" in caplog.text
    assert gesagt[0][1:] == ("leise", "")


async def test_a_director_that_hangs_is_given_up_on(tmp_path, monkeypatch):
    """2026-09-14: queued requests got keep-alives for 900 s and never started."""
    import yurios.world.stories as stories
    monkeypatch.setattr(stories, "DIRECTOR_TIMEOUT_S", 0.01)
    lab = _lab(Recorder(), tmp_path)

    async def haengt(prompt, **kwargs):
        await asyncio.sleep(10)

    lab._complete = haengt
    gesprochen = await lab._direct("s", [SZENE], ["gedämpft"], "FORM", "")
    assert gesprochen == [(SZENE, "gedämpft", "")]


def test_speech_is_found_whether_it_closes_german_or_straight():
    """15f1e38754 closed 66 of 67 quotes with a straight mark: „Siehst du das?", flüstere ich."""
    from yurios.world.stories import _segments
    assert _segments('„Siehst du das?", flüstere ich. „Ja“, sagst du.') == [
        ("Siehst du das?", True), (", flüstere ich.", False),
        ("Ja", True), (", sagst du.", False)]


# -- the author revises on the editor's notes, and the editor checks --------

def _pruefer(*antworten):
    """A `_complete` stand-in: scripted answers in order, prompts recorded."""
    gesehen: list[tuple[str, dict]] = []
    rest = list(antworten)

    async def fake(prompt, **kwargs):
        gesehen.append((prompt, kwargs))
        return rest.pop(0)
    return fake, gesehen


async def test_a_plot_note_is_handed_on_with_the_sentences_it_concerns(tmp_path):
    lab = _lab(Recorder(), tmp_path)
    lab._complete = lambda prompt, **kw: _antwort(json.dumps({"funde": [
        {"satz": 1, "kategorie": "handlung", "befund": "Angekündigt, aber Satz 3 löst es nicht ein.",
         "korrektur": ""},
        {"satz": 2, "kategorie": "perspektive", "befund": "Head-Hopping.", "korrektur": ""}]}))
    offen: list = []
    await lab._lektorat(["Ich nehme dich gleich von hinten. Du lachst. Dann setze ich mich auf dich."],
                        "FORM", "REG", offen)
    assert len(offen) == 1                                   # perspective is never revised
    assert offen[0]["saetze"] == ["Ich nehme dich gleich von hinten.", "Dann setze ich mich auf dich."]


async def test_a_revision_the_editor_confirms_is_kept(tmp_path):
    lab = _lab(Recorder(), tmp_path)
    fake, gesehen = _pruefer(
        json.dumps({"ersetzungen": [{"satz": 1, "neu": "Ich setze mich gleich auf dich."}]}),
        "BEHOBEN: ja, passt jetzt\nNEUER FEHLER: nein")
    lab._complete = fake
    parts = ["Ich nehme dich gleich von hinten. Du lachst. Dann setze ich mich auf dich."]
    offen = [{"kategorie": "handlung", "befund": "Ankündigung verpufft.",
              "saetze": ["Ich nehme dich gleich von hinten."], "streichen": False}]
    assert await lab._revise("s", parts, offen, "FORM", "REG") == (1, 0)
    assert parts == ["Ich setze mich gleich auf dich. Du lachst. Dann setze ich mich auf dich."]
    autorin, pruefung = gesehen
    assert "Du bist die Autorin" in autorin[0] and "NUR diese Sätze ändern: 1, 2" in autorin[0]
    assert autorin[1]["reasoning_effort"] == "low" and autorin[1].get("persona", True)
    assert "Du bist Lektorin" in pruefung[0] and pruefung[1]["persona"] is False


async def test_a_revision_the_editor_refuses_leaves_the_text_and_the_note(tmp_path):
    lab = _lab(Recorder(), tmp_path)
    lab._complete, _ = _pruefer(
        json.dumps({"ersetzungen": [{"satz": 1, "neu": "Ich gehe."}]}),
        "BEHOBEN: ja\nNEUER FEHLER: ja, jetzt fehlt der Grund")
    parts = ["Ich bleibe. Du gehst."]
    offen = [{"kategorie": "handlung", "befund": "x", "saetze": ["Ich bleibe."], "streichen": False}]
    assert await lab._revise("s", parts, offen, "F", "R") == (0, 1)
    assert parts == ["Ich bleibe. Du gehst."]


async def test_a_revision_may_only_touch_the_sentences_of_its_note(tmp_path):
    lab = _lab(Recorder(), tmp_path)
    lab._complete, gesehen = _pruefer(
        json.dumps({"ersetzungen": [{"satz": 5, "neu": "Ganz woanders."}]}))
    parts = ["Eins. Zwei. Drei. Vier. Fünf."]
    offen = [{"kategorie": "handlung", "befund": "x", "saetze": ["Eins."], "streichen": False}]
    assert await lab._revise("s", parts, offen, "F", "R") == (0, 1)
    assert parts == ["Eins. Zwei. Drei. Vier. Fünf."] and len(gesehen) == 1   # no check needed


async def test_a_far_repeat_may_be_struck_but_nothing_else_may_vanish(tmp_path):
    lab = _lab(Recorder(), tmp_path)
    satz = "Yara sagte eine Nummer ins Gerät, Datum, Uhrzeit."
    lab._complete, _ = _pruefer(
        json.dumps({"ersetzungen": [{"satz": 4, "neu": ""}]}),
        "BEHOBEN: ja\nNEUER FEHLER: nein")
    parts = [f"{satz} Es regnete. Wir fuhren los.", "Yara sagte die Nummer ins Gerät, Datum und Uhrzeit. Dann hielt der Wagen."]
    offen = [{"kategorie": "uebergang", "befund": "Wiederholt Satz 1.",
              "saetze": ["Yara sagte die Nummer ins Gerät, Datum und Uhrzeit.", satz], "streichen": True}]
    assert await lab._revise("s", parts, offen, "F", "R") == (1, 0)
    assert parts[1] == "Dann hielt der Wagen."


async def test_a_revision_that_fails_never_costs_the_story(tmp_path, caplog):
    lab = _lab(Recorder(), tmp_path)

    async def kaputt(prompt, **kwargs):
        raise RuntimeError("route down")

    lab._complete = kaputt
    parts = ["Ich bleibe. Du gehst."]
    offen = [{"kategorie": "handlung", "befund": "x", "saetze": ["Ich bleibe."], "streichen": False}]
    with caplog.at_level("WARNING"):
        assert await lab._revise("s", parts, offen, "F", "R") == (0, 1)
    assert parts == ["Ich bleibe. Du gehst."] and "revision failed" in caplog.text


async def test_with_the_director_off_nothing_is_asked(tmp_path):
    lab = StoryLab(utility=Recorder(), post=lambda *a, **k: None, root=tmp_path,
                   voice=_voice(), director=False)

    async def nie(prompt, **kwargs):
        raise AssertionError("the director was asked")

    lab._complete = nie
    assert await lab._direct("s", [SZENE], ["x"], "FORM", "") == [(SZENE, "x", "")]
