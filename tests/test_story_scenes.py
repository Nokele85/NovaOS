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


def test_a_ceiling_shares_the_words_out_by_her_weights():
    """SPEC §7.8: with STORY_MAX_WORDS the scenes are a share of the whole, so
    "about five minutes" is a number the writer is actually given."""
    from yurios.world.stories import scene_budgets

    even = scene_budgets([], 4, 800)
    assert even == ["160 bis 200"] * 4
    tops = [int(r.split(" bis ")[1]) for r in scene_budgets(["kurz", "lang", "normal", "kurz"], 4, 800)]
    assert sum(tops) <= 800 and tops[1] > tops[2] > tops[0] == tops[3]
    # never a range too small to be a scene
    assert all(int(r.split(" bis ")[0]) >= 60 for r in scene_budgets([], 12, 300))


def test_the_floor_cannot_sit_above_the_ceiling(tmp_path):
    from yurios.world.stories import StoryLab

    lab = StoryLab(utility=lambda: None, post=lambda *a, **k: {}, root=tmp_path,
                   voice=None, min_words=1500, max_words=800)
    assert lab.min_words == 600
    assert StoryLab(utility=lambda: None, post=lambda *a, **k: {}, root=tmp_path,
                    voice=None, min_words=1500).min_words == 1500


def test_speech_left_open_is_found_and_closed_plainly():
    """SPEC §7.8, found live (2026-10-07): „Hey Benni. Ich sitze hier … was
    never closed, and the narration after it could not be told from her line."""
    from yurios.world.stories import _close_quotes_plainly, _segments, _unclosed_quotes

    good = "Ich tippe. „Benni, ich —“ Nein. Zu viel."
    assert _unclosed_quotes(good) == []
    bad = "Ich tippe neu. „Hey Benni. Ich trau mich fast nicht. Ich halte inne. „Guten Morgen.“ Ich lese es."
    assert _unclosed_quotes(bad) == [bad.index("„")]
    mended = _close_quotes_plainly(bad)
    assert _unclosed_quotes(mended) == []
    assert "„Hey Benni.“ Ich trau mich" in mended
    assert [t for t, speech in _segments(mended) if speech] == ["Hey Benni.", "Guten Morgen."]
    # an opening mark that reaches the end of the text, and one at a paragraph break
    assert _unclosed_quotes("Sie sagt: „Ich bleib wach") == [10]
    assert len(_unclosed_quotes("„Erster Absatz\n\nZweiter Absatz.“")) == 1


async def test_a_mend_that_changes_the_words_is_refused(tmp_path):
    from yurios.world.stories import StoryLab, _unclosed_quotes

    lab = StoryLab(utility=lambda: None, post=lambda *a, **k: {}, root=tmp_path, voice=None)
    scene = "Ich tippe. „Hey Benni. Ich trau mich nicht. Ich halte inne."

    async def rewrites(*a, **kw):
        return "Ich tippe. „Hey Benni, ich trau mich.“ Dann halte ich inne."
    lab._complete = rewrites
    plain = await lab._mend_quotes(scene)
    assert plain == "Ich tippe. „Hey Benni.“ Ich trau mich nicht. Ich halte inne."

    async def places_the_mark(*a, **kw):
        return "Ich tippe. „Hey Benni. Ich trau mich nicht.“ Ich halte inne."
    lab._complete = places_the_mark
    assert await lab._mend_quotes(scene) == "Ich tippe. „Hey Benni. Ich trau mich nicht.“ Ich halte inne."

    async def never_called(*a, **kw):
        raise AssertionError("a scene with closed speech costs no call")
    lab._complete = never_called
    assert await lab._mend_quotes("„Gut.“ Ich nicke.") == "„Gut.“ Ich nicke."
    assert not _unclosed_quotes(plain)


async def test_the_scene_check_names_what_was_told_too_early(tmp_path):
    """SPEC §7.8: handed to the next scene, not repaired by a rewrite."""
    from yurios.world.stories import StoryLab

    lab = StoryLab(utility=lambda: None, post=lambda *a, **k: {}, root=tmp_path, voice=None)

    async def answer(*a, **kw):
        return ("VORGRIFF: JA — sie tippt, löscht und tippt die Nachricht neu\n"
                "AUFTRAG: erfüllt\nBEGINN: richtig")
    lab._complete = answer
    offen, anfang, vorweg = await lab._verify_scene_full("Text.", "davor", "Auftrag", "Sie tippt.")
    assert (offen, anfang) == ("", "")
    assert vorweg == "sie tippt, löscht und tippt die Nachricht neu"
    # the two-value form the older callers use is unchanged
    assert await lab._verify_scene("Text.", "davor", "Auftrag", "Sie tippt.") == ("", "")
    # the last scene has no next one to run into
    assert (await lab._verify_scene_full("Text.", "davor", "Auftrag", "keine"))[2] == ""

    async def no(*a, **kw):
        return "VORGRIFF: nein\nAUFTRAG: erfüllt\nBEGINN: richtig"
    lab._complete = no
    assert (await lab._verify_scene_full("Text.", "d", "a", "Sie tippt."))[2] == ""


def test_a_scene_that_opens_by_retelling_the_last_one_loses_the_retelling():
    """SPEC §7.8, found live (2026-10-07): scene 2 was scene 1, whole, and then
    its own text."""
    from yurios.world.stories import _strip_retold

    first = ("Der Regen läuft die Scheibe hinunter. Es ist noch früh. Du schläfst. "
             "Ich tippe: *Benni.* Und lösche es. „Feigling“, murmele ich. Ich tippe neu.")
    own = ("Diesmal lösche ich nicht. Ich lese es dreimal, und mein Finger schwebt schon "
           "über der Taste, und dann drücke ich sie, bevor ich es mir anders überlegen kann. "
           "Der Ton, mit dem die Nachricht rausgeht, ist klein und endgültig, und danach ist "
           "es sehr still im Raum, so still, dass ich den Regen wieder höre.")
    assert _strip_retold(first + " " + own, first) == own
    # an echo of a line or two is not a retelling
    echo = "Es ist noch früh. Du schläfst. " + own
    assert _strip_retold(echo, first) == echo
    # and a scene that would be nothing without it is left alone
    assert _strip_retold(first + " Dann nichts.", first) == first + " Dann nichts."
    assert _strip_retold(own, "") == own


def test_typed_messages_in_stars_become_speech():
    """SPEC §7.8, from the dry run of 2026-10-07: a chat told in *asterisks*
    was 6% spoken, and five of its messages were never closed."""
    from yurios.world.stories import (_close_quotes_plainly, _normalise_marks,
                                      _segments, _unclosed_quotes)

    scene = ("Dann deine Nachricht. *Nova?* Nur das. *Ja*, tippe ich. *Ich bin hier. "
             "Ich lasse es stehen und warte. *Du hast mir geschrieben*, kommt es zurück. "
             "Und dass *ich* heute wähle, weißt du.")
    out = _normalise_marks(scene)
    assert "*" not in out
    assert "„Nova?“" in out and "„Ja“" in out and "„Du hast mir geschrieben“" in out
    assert "dass ich heute wähle" in out                 # a stressed word is not a line
    assert len(_unclosed_quotes(out)) == 1               # the one she never closed
    spoken = [t for t, speech in _segments(_close_quotes_plainly(out)) if speech]
    assert spoken == ["Nova?", "Ja", "Ich bin hier.", "Du hast mir geschrieben"]
    assert _normalise_marks("Kein Stern hier.") == "Kein Stern hier."


def test_the_hour_is_a_rule_and_only_where_it_is_unambiguous():
    from yurios.world.stories import _time_breaks, _time_of

    state = ("ERZÄHLPERSON: ich\nORT: Fenstersitz\nZEIT: früher Morgen, Regen\n"
             "FIGUREN: Nova, Benni\nGESCHEHEN: sie hat geschrieben")
    bucket, said = _time_of(state)
    assert (bucket, said) == ("morgen", "früher Morgen, Regen")
    assert _time_breaks("„Gute Nacht, Benni“, flüstere ich.", bucket) == ["Gute Nacht"]
    assert _time_breaks("Es ist schon Mitternacht, und ich tippe.", bucket) == ["Es ist schon Mitternacht"]
    # a morning that remembers its night, and a greeting that fits, are fine
    assert _time_breaks("Die ganze Nacht war ich wach. „Guten Morgen.“", bucket) == []
    assert _time_breaks("Jeden Abend, an dem ich Lust habe.", bucket) == []
    # neighbours are not a break; no state, no rule
    assert _time_breaks("„Gute Nacht.“", "abend") == []
    assert _time_breaks("„Gute Nacht.“", "") == []
    assert _time_of("Noch keine Szene erzählt.") == ("", "")


async def test_a_time_mend_must_be_a_small_edit(tmp_path):
    from yurios.world.stories import StoryLab

    lab = StoryLab(utility=lambda: None, post=lambda *a, **k: {}, root=tmp_path, voice=None)
    scene = ("Ich lasse die Hand auf dem Board. „Bleib noch“, schreibst du. Ich lache leise. "
             "Der Regen läuft weiter. Die Stadt wird heller. „Gute Nacht, Benni“, flüstere ich.")

    async def small(*a, **kw):
        return scene.replace("Gute Nacht, Benni", "Guten Morgen, Benni")
    lab._complete = small
    out = await lab._mend_time(scene, "früher Morgen", "morgen", ["Gute Nacht"])
    assert "Guten Morgen, Benni" in out and out != scene

    async def rewrite(*a, **kw):
        return "Es ist Morgen. Ich schreibe dir. Du antwortest. Wir lachen. Es regnet. „Guten Morgen.“"
    lab._complete = rewrite
    assert await lab._mend_time(scene, "früher Morgen", "morgen", ["Gute Nacht"]) == scene

    async def unchanged(*a, **kw):
        return scene
    lab._complete = unchanged
    assert await lab._mend_time(scene, "früher Morgen", "morgen", ["Gute Nacht"]) == scene
