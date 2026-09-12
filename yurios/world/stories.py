"""Asynchronous long-form story production: outline -> scenes -> TTS -> Telegram."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import wave
from pathlib import Path
from uuid import uuid4

import httpx

from .qwenvoice import QwenVoice, passages
from .vram import remote_gpu_lock

log = logging.getLogger("world.stories")


def _words(text: str) -> int:
    return len(re.findall(r"\b[\wÄÖÜäöüß'-]+\b", text))


def _has_complete_sentence(text: str) -> bool:
    # “ (U+201C) is the German *closing* quote: „Ja.“ is a finished sentence,
    # and missing it here cut the quote off and appended the fallback ending
    # to a story that had already ended (vault/stories/d09e2c5a0a, live).
    tail = re.sub(r"[\s\"'»«“”’‹›\)\]}]+$", "", text.strip())
    return bool(tail) and tail[-1] in ".!?…"


# One scene is asked for well under what one call may return: the live stories
# of 2026-09-11 hit a 900-token ceiling in 7 of 9 scenes and were stitched
# together mid-word ("…bestell", "…z"), which the audio then read out.
SCENE_WORDS = "350 bis 450"

# What a scene weighs, when she says so. Every scene used to be asked for the
# same 350-450 words, so a story spent its length on whatever had the most
# scenes rather than on what mattered: the live story of 2026-09-12 gave 1898
# words to walking through the forest and 699 to what had been asked for, and
# the two scenes that were the point were the shortest in it.
SCENE_LENGTHS = {"kurz": "180 bis 250", "normal": SCENE_WORDS,
                 "lang": "600 bis 800"}

# The review is the one call that thinks, and on this route `max_tokens` bounds
# the thinking as well as the answer: fill it and the content comes back empty.
# A 4300-word story needed 24 913 reasoning tokens, so this is roughly double —
# and a ceiling is free, since only what is generated is billed.
REVIEW_MAX_TOKENS = 48_000
SCENE_MAX_TOKENS = 1600

# The shape of a story, for when no outline survives. Five distinct beats beat
# one repeated purpose: the failure mode being guarded against is a story that
# tells its opening eight times over.
_BARE_ARC = ("Der Anfang: die Lage und was die beiden gerade wollen.",
             "Die erste Entwicklung, die etwas verändert.",
             "Der Wendepunkt, an dem es ernst wird.",
             "Die Folge daraus, ausgespielt.",
             "Das Ende, das alles Offene schließt.")


# A corrected passage is prose. Anything that talks *about* the rules is the
# checker thinking out loud, and it must never reach the file — let alone the
# recording, which would read it out.
_META = re.compile(
    r"(korrigierter\s+abschnitt|verst(ö|oe)(ß|ss)e?\b|gegen\s+die\s+regeln|"
    r"der\s+abschnitt\s+(spielt|verst|h(ä|ae)lt)|die\s+regel\s+verlangt|"
    r"ich\s+pr(ü|ue)fe\s+den\s+abschnitt)", re.I)


# A pronoun swapped without its verb: "sagtest er". The rules pass produced
# exactly this while it was forbidden to touch anything but single words, and
# a broken verb form is worse than the inconsistency it was fixing.
_BROKEN_VERB = re.compile(
    # Only endings that can be nothing but second person. A bare "-st"
    # would flag "ist er selbst", which is correct German and none of
    # our business.
    r"\b(?:\w{2,}test|warst|hattest|wurdest|gingst|bliebst|sahst|"
    r"standst|hieltst|nahmst|kamst|zogst|liefst|riefst|fandst|gabst)"
    # "er" only. "sagtest sie" and "sahst es" are correct German — there
    # the pronoun is the object ("you saw her", "you saw it"). "er" is
    # nominative and nothing else, so it can only be a subject whose verb
    # disagrees with it.
    r"\s+er\b", re.I)


def _sentences(text: str) -> list[str]:
    """Split into sentences, keeping the closing quote with the sentence it
    belongs to — „Ja.“ is one sentence, not one and a stray mark. A tail with
    no final mark is still a sentence: dropping it would delete prose, and
    deleting prose is the one thing this pass may never do."""
    text = text.strip()
    if not text:
        return []
    out, ende = [], 0
    for m in re.finditer(r"""\S.*?[.!?…]["'»«“”’‹›]*(?=\s|$)""", text):
        out.append(m.group(0))
        ende = m.end()
    rest = text[ende:].strip()
    if rest:
        out.append(rest)
    return out or [text]


def _dialogue_share(text: str) -> float:
    """How much of a story is spoken aloud, as a fraction of its characters.

    An intention that is not measured is a hope: the scene prompt has asked
    for "action, dialogue and a visible change" all along, and the share still
    fell from 22.6% to 12.9% between two live stories without anyone noticing.
    """
    gesprochen = sum(len(m) for m in
                     re.findall(r"[„\"]([^\"„“]{2,400})[“\"]", text))
    return gesprochen / max(len(text), 1)


def _mixed_address(saetze: list[str]) -> int:
    """How many sentences of a passage disagree with its own majority about
    whether the second figure is spoken to or spoken about.

    Measured rather than assumed: judging sentences one at a time is what
    makes a passage drift between "du gingst" and "er ging", so the number
    that says whether the pass fixed it or caused it has to exist."""
    du = [bool(re.search(r"\b(du|dein\w*|dir|dich)\b", s, re.I)) for s in saetze]
    er = [bool(re.search(r"\b(er|seine\w*|ihm|ihn)\b", s, re.I)) for s in saetze]
    modi = [d for d, e in zip(du, er) if d != e]
    if len(modi) < 2:
        return 0
    return min(sum(modi), len(modi) - sum(modi))


def _numbered(answer: str) -> list[tuple[int, str]]:
    """The lines of a `<number>: <sentence>` answer. Anything else is skipped —
    a preamble, an apology, "OK" — because a line we cannot read is a line we
    must not apply."""
    out: list[tuple[int, str]] = []
    for line in (answer or "").splitlines():
        m = re.match(r"\s*(\d{1,3})\s*[:.)]\s*(.+)", line)
        if m and m.group(2).strip():
            out.append((int(m.group(1)), m.group(2).strip()))
    return out


def _purpose_text(node) -> str:
    """The words of one outline node, whether she wrote a line or the planner
    wrote an object. Putting the dict in a prompt puts braces in her story."""
    if isinstance(node, dict):
        return str(node.get("purpose") or node.get("turn") or "").strip()
    return str(node).strip()


def _trim_to_sentence(text: str) -> str:
    """Cut a truncated completion back to its last complete sentence.

    A scene is one paragraph of the finished story and one stretch of its
    audio, so a scene that stops mid-word is a sentence the listener hears
    break off. Nothing is invented here — the fragment is dropped; a text with
    no sentence boundary at all is returned unchanged rather than emptied."""
    if _has_complete_sentence(text):
        return text
    cut = max(text.rfind(mark) for mark in ".!?…")
    if cut < 0:
        return text
    # keep a closing quote that belongs to the sentence: „Ja.“ stays whole
    end = cut + 1
    while end < len(text) and text[end] in "\"'»«“”’‹›":
        end += 1
    return text[:end].rstrip()


class StoryLab:
    """One queued story at a time; every finished artifact is durable."""

    def __init__(self, *, utility, post, root: Path, voice: QwenVoice,
                 remember=None, character_context=None, user_name: str = "Benni",
                 min_words: int = 1500,
                 scenes: int = 8, gpu_lock: asyncio.Lock | None = None):
        self.utility = utility
        # the LAN GPU's lock; None = the process-wide one (world/vram.py)
        self.gpu_lock = gpu_lock
        self.character_context = character_context
        self.user_name = user_name
        self.post = post
        self.remember = remember
        self.root = root
        self.voice = voice
        self.min_words = min_words
        self.scenes = scenes
        self._tasks: set[asyncio.Task] = set()
        # The docstring's "one at a time", enforced: two jobs share one TTS
        # server, and the first one's closing /unload would pull the model out
        # from under the second mid-synthesis. A second request queues.
        self._one_at_a_time = asyncio.Lock()
        #: Did we turn the LAN voice on and not off again? Only the shutdown
        #: path can know, and only it can hand the card back.
        self._voice_on = False

    def start(self, contract: dict) -> str:
        story_id = contract.get("story_id") or uuid4().hex[:10]
        task = asyncio.create_task(self._queued(story_id, contract),
                                   name=f"story-{story_id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return story_id

    async def close(self) -> None:
        """Stop a story in flight and give the card back.

        A finished story unloads the voice in a `finally`, but a process that
        is killed never reaches it: every story interrupted by a restart on
        2026-09-12 left 8.4 GB on a card the camera shares, until whichever
        later story happened to finish released it. Shutdown is the only place
        that can see this, so it is the place that has to."""
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if not self._voice_on:
            return
        # A fresh client, and not the job's: once a task is cancelled, an await
        # inside its `finally` is cancelled too, so the unload has to happen
        # out here or not at all.
        timeout = httpx.Timeout(connect=10, read=120, write=30, pool=30)
        try:
            async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
                await self.voice.power_off(client)
        finally:
            self._voice_on = False

    async def _queued(self, story_id: str, contract: dict) -> None:
        async with self._one_at_a_time:
            await self._run(story_id, contract)

    async def _complete(self, prompt: str, *, max_tokens: int = 900,
                        persona: bool = True, thinking: bool | None = None) -> str:
        provider = self.utility() if callable(self.utility) else self.utility
        if persona and self.character_context is not None:
            soul = self.character_context() if callable(self.character_context) else self.character_context
            prompt = ("Du schreibst als Teil der Figur Nova. Ihre Persona ist verbindlich: "
                      f"{getattr(soul, 'personality', '')}\n"
                      f"Identität und Art: {getattr(soul, 'backbone', '')}\n"
                      f"Szenario: {getattr(soul, 'scenario', '')}\n"
                      f"Dauerhafte Antriebe: {', '.join(getattr(soul, 'drives', []))}\n"
                      "Lass diese Eigenschaften durch konkrete Entscheidungen, Dialoge und "
                      "Reaktionen sichtbar werden. Erzähle nicht über die Persona, spiele sie.\n\n"
                      + prompt)
        extra = {} if thinking is None else {"thinking": thinking}
        return await provider.complete([{"role": "user", "content": prompt}],
                                       temperature=0.75, max_tokens=max_tokens,
                                       **extra)

    async def _plan(self, story_id: str, theme: str, mood: str,
                    relationship: str, register: str) -> dict:
        """The fallback outline, for when she did not bring one.

        It asks for exactly as many scenes as the story is configured for, and
        it says out loud when it comes back unusable. The old version swallowed
        that in a bare `except` and left an empty plan behind: every scene then
        got the same empty purpose, and the story restarted eight times with
        small variations (live, 2026-09-12) with nothing in the log to show
        for it."""
        plan_raw = await self._complete(
            "Plane eine zusammenhängende deutschsprachige Geschichte. "
            f"{relationship} Thema: {theme}. Stimmung: {mood}. "
            + (f"Register: {register}. " if register else "")
            + f"Sie braucht genau {self.scenes} Szenen mit Anfang, Mitte, "
            "Wendepunkt und einem befriedigenden Ende. Antworte nur "
            "mit JSON: {\"title\": string, \"characters\": [string], "
            "\"scenes\": [{\"purpose\": string, \"turn\": string}]}.",
            max_tokens=1600)
        try:
            found = re.search(r"\{.*\}", plan_raw, re.S)
            plan = json.loads(found.group(0) if found else "")
            if not isinstance(plan, dict) or not plan.get("scenes"):
                raise ValueError("no scenes in the plan")
            return plan
        except Exception as e:
            log.error("story %s: the planner returned no usable outline (%s); "
                      "%d characters came back. Falling back to a bare arc — "
                      "the story will be thinner than it should be.",
                      story_id, e, len(plan_raw or ""))
            return {"title": theme, "characters": [],
                    "scenes": [{"purpose": p} for p in _BARE_ARC]}

    async def _house_rules(self, opening: str) -> str:
        """The two rules a story has to keep, read off its opening.

        Deliberately only two. An earlier version asked for whatever the first
        scene established and got incidental circumstances back as law — "the
        figures have no names", "the weather is dry" — and then corrected the
        story against its own plot, which is how a dawn scene ended up saying
        "it is getting dark" (live, 2026-09-12). Weather, time of day, tense
        and names are story, not rule.

        The cast is not derived either. It is always Nova and him."""
        rules = await self._complete(
            "Lies den Anfang einer Geschichte und notiere in höchstens zwei "
            "kurzen Zeilen, woran sich der Rest halten muss — nur das, sonst "
            "nichts:\n"
            "ERZÄHLPERSON: in welcher Person erzählt wird und wer mit \"du\" "
            "beziehungsweise \"ich\" gemeint ist\n"
            "ANREDE: ob die zweite Figur durchgehend angesprochen (\"du\") "
            "oder über sie gesprochen wird (\"er\"/\"sie\")\n"
            "ZEITFORM: Präsens oder Vergangenheit\n\n"
            f"Die Hauptfiguren stehen fest: Nova und {self.user_name}. Leite "
            "keine weiteren Regeln ab. Wetter, Tageszeit, Zeitform, Orte und "
            "Namen gehören zur Handlung und sind keine Regeln.\n\n"
            f"Anfang:\n{opening[:3000]}",
            max_tokens=300, persona=False)
        return self._clean(rules)

    async def _review(self, parts: list[str], rules: str) -> dict[int, dict[int, str]]:
        """Read the whole story at once and name the sentences that break it.

        This is the one call that thinks. Measured on a 4300-word story it
        spent 24 913 reasoning tokens, took 110 s and cost 1.6 cents — and it
        found every fault a careful reading had found plus one it had missed
        (a belt opened for the second time). Three earlier attempts returned
        nothing because `max_tokens` bounds the thinking as well as the answer
        on this route: fill it and the content comes back empty with
        `finish_reason: length`. The ceiling here is roughly double what the
        measurement needed, and a ceiling costs nothing — only the tokens
        actually generated are billed.

        Why it earns its cost: these faults span the whole story. A clause
        repeated five times and a shirt taken off twice are invisible to a
        checker that only ever sees one passage, however carefully it looks."""
        anteil = _dialogue_share("\n\n".join(parts)) * 100
        karte: list[tuple[int, int]] = []
        zeilen: list[str] = []
        for pi, teil in enumerate(parts):
            for li, satz in enumerate(_sentences(teil)):
                karte.append((pi, li))
                zeilen.append(f"{len(zeilen) + 1}: {satz}")
        try:
            antwort = await self._complete(
                "Du bekommst eine Geschichte, Satz für Satz nummeriert.\n\n"
                f"Verbindliche Form:\n{rules}\n\n"
                "Finde NUR diese vier Fehlerarten:\n"
                "1. Die Anrede weicht von der Form ab.\n"
                "2. Etwas passiert zweimal, obwohl es nur einmal passieren kann "
                "— Kleidung zweimal ausgezogen, ein Raum zweimal betreten, ein "
                "Gürtel zweimal geöffnet.\n"
                "3. Eine Formulierung wiederholt sich auffällig oft.\n"
                "4. Ein Satz BERICHTET über etwas Gesagtes, statt es zu zeigen "
                "(\"Ich sagte ihm, dass ich müde war\", \"wir redeten über den "
                "Weg\") — er könnte gesprochene Rede sein.\n\n"
                f"Gesprochen wird derzeit {anteil:.0f} % der Geschichte; ein "
                "Drittel wäre richtig. Nenne unter Fehlerart 4 so viele Sätze, "
                "dass das erreichbar wird — aber nur solche, in denen wirklich "
                "jemand etwas sagt oder sagen könnte. Beschreibung, Handlung "
                "und innere Sätze bleiben, was sie sind.\n\n"
                "Nenne nur die fehlerhaften Sätze, höchstens 40, je Zeile:\n"
                "<Nummer>: <kurze Begründung>\n"
                "Danach nichts weiter. Findest du nichts, antworte: OK\n\n"
                + "\n".join(zeilen),
                max_tokens=REVIEW_MAX_TOKENS, persona=False, thinking=True)
        except Exception:
            log.warning("the story review failed; nothing is corrected",
                        exc_info=True)
            return {}
        funde: dict[int, dict[int, str]] = {}
        for nummer, grund in _numbered(antwort):
            if not 1 <= nummer <= len(karte):
                continue
            pi, li = karte[nummer - 1]
            # The opening is the law the rest is held against, not a defendant.
            if pi > 0:
                funde.setdefault(pi, {})[li] = grund
        log.info("story review: %d sentences in %d of %d passages",
                 sum(len(v) for v in funde.values()), len(funde), len(parts) - 1)
        return funde

    async def _straighten(self, parts: list[str], rules: str,
                          funde: dict[int, dict[int, str]]) -> tuple[int, int]:
        """Hold the story against its own rules, sentence by sentence.

        The whole passage used to go in and a whole passage came back, which
        left the model free to rewrite everything it touched: 8 of 8 passages
        "corrected" and the story 12% longer, twice. Now it sees numbered
        sentences and may answer only with the numbers that break a rule.
        Every sentence it does not name stays byte-for-byte as it was written
        — not by instruction, but because we never replace it.

        Each returned sentence is checked on its own: no reasoning about the
        rules instead of prose, no pronoun swapped without its verb, and a
        length near the original. A refused sentence costs that sentence, not
        the passage."""
        geaendert = gesamt = 0
        for i in sorted(funde):
            if not 0 < i < len(parts):
                continue
            saetze = _sentences(parts[i])
            ziele = sorted(n + 1 for n in funde[i] if 0 <= n < len(saetze))
            if not ziele:
                continue
            gesamt += len(ziele)
            # Every sentence goes in, because a sentence is judged beside its
            # neighbours — but only the named ones may come back. The pass used
            # to be handed a whole passage and answered with 94% of it.
            nummeriert = "\n".join(f"{n}: {s}" for n, s in enumerate(saetze, 1))
            try:
                answer = await self._complete(
                    "Prüfe eine Geschichte Satz für Satz gegen ihre Regeln.\n\n"
                    f"Regeln:\n{rules}\n\n"
                    "Dein Auftrag sind Erzählperson, Anrede und Zeitform — "
                    "und, wo ein Satz über etwas Gesagtes nur berichtet, ihn in "
                    "gesprochene Rede verwandeln: aus \"Ich sagte ihm, dass ich "
                    "müde war\" wird \"Ich bin müde\", sagte ich.\n"
                    "Ändere alles, was dafür nötig ist — auch die Verbformen, "
                    "die daraus folgen. Aus \"sagtest du\" wird \"sagte er\", "
                    "nicht \"sagtest er\".\n\n"
                    "Handlung, Bilder, Ton und Wortwahl sind NICHT dein "
                    "Auftrag. Dass dir ein Satz nicht gefällt, ist kein Grund.\n\n"
                    "Beurteile jeden Satz im Zusammenhang mit dem davor und "
                    "danach, nicht für sich allein. Die Sätze sind ein "
                    "fortlaufender Absatz, keine Liste.\n"
                    "Wenn du einen Satz änderst, prüfe seine Nachbarn: passen "
                    "sie danach nicht mehr, nenne sie mit. Am Ende muss der "
                    "ganze Absatz einheitlich sein — Anrede und Zeitform dürfen "
                    "innerhalb eines Absatzes nirgends wechseln.\n\n"
                    f"Zu korrigieren sind AUSSCHLIESSLICH die Sätze "
                    f"{', '.join(str(z) for z in ziele)}. Jeder andere Satz "
                    "bleibt, wie er ist, und wird nicht genannt.\n\n"
                    "Antworte NUR mit den Nummern dieser Sätze, je Zeile eine:\n"
                    "<Nummer>: <der angeglichene Satz>\n\n"
                    "Verstößt kein Satz, antworte mit genau: OK\n\n"
                    f"Sätze:\n{nummeriert}",
                    max_tokens=SCENE_MAX_TOKENS, persona=False)
            except Exception:
                log.warning("straightening passage %d failed; left as written",
                            i + 1, exc_info=True)
                continue
            for nummer, satz in _numbered(answer):
                if nummer not in ziele:
                    # It was not asked about this one, so it does not get to
                    # decide about it.
                    continue
                alt = saetze[nummer - 1]
                # "12: OK" means "this one is fine", not "replace it with the
                # word OK" — and against a one-word sentence the length window
                # lets that through. It reached a published story (live,
                # 2026-09-12) and would have been read out loud.
                if satz.strip().strip(".!—-").upper() in ("OK", "IO", ""):
                    continue
                if _META.search(satz):
                    log.warning("passage %d sentence %d answered with reasoning "
                                "instead of prose; kept it", i + 1, nummer)
                    continue
                if _BROKEN_VERB.search(satz) and not _BROKEN_VERB.search(alt):
                    log.warning("passage %d sentence %d swapped a pronoun "
                                "without its verb; kept it", i + 1, nummer)
                    continue
                if not 0.5 * _words(alt) <= _words(satz) <= 1.6 * _words(alt):
                    log.warning("passage %d sentence %d went from %d words to "
                                "%d; kept it", i + 1, nummer, _words(alt),
                                _words(satz))
                    continue
                saetze[nummer - 1] = satz
                geaendert += 1
            parts[i] = " ".join(saetze)
            gemischt = _mixed_address(saetze)
            if gemischt:
                log.info("passage %d still mixes forms of address in %d of %d "
                         "sentences", i + 1, gemischt, len(saetze))
        return geaendert, gesamt

    async def _state_update(self, state: str, scene: str) -> str:
        """Carry the story's *state* forward, not its last 1200 characters.

        The old hand-off was the tail of the previous scene — surface text, not
        state. Nothing told the writer what had already been established, so it
        established it again: the live story of 2026-09-12 said "Wald" 24 times
        because every scene re-introduced the forest. The long-form literature
        is unanimous that a running record belongs here — who is where, what
        changed, what is still open — and that a detailed plan plus a carried
        state is what buys coherence (DOC, +22.5 points over a three-beat plan).

        Never raises. A state note that fails falls back to the old tail: worse,
        but not broken, and it says so in the log."""
        try:
            note = await self._complete(
                "Führe die Zustandsakte einer Geschichte fort. Antworte in "
                "höchstens 120 Wörtern, in vier Zeilen mit genau diesen "
                "Anfängen:\n"
                "ERZÄHLPERSON: in welcher Person erzählt wird und wer damit gemeint ist — wortgleich zur bisherigen Akte\n"
                "ORT: wo die Figuren jetzt sind\n"
                "ZEIT: Tageszeit und Wetter, wie sie JETZT sind\n"
                "FIGUREN: wer dabei ist — mit Anrede und Pronomen, wortgleich zur bisherigen Akte — und was sich an ihnen geändert hat\n"
                "GESCHEHEN: die Tatsachen, die jetzt feststehen\n"
                "OFFEN: was noch ungelöst ist\n\n"
                f"Bisherige Akte:\n{state}\n\nNeue Szene:\n{scene[-2500:]}",
                max_tokens=400, persona=False)
            note = self._clean(note)
            if note and "ORT" in note.upper():
                return note
            log.warning("story state note came back unusable; falling back "
                        "to the previous scene's tail")
        except Exception:
            log.warning("story state note failed; falling back to the tail",
                        exc_info=True)
        return scene[-1200:]

    @staticmethod
    def _clean(text: str) -> str:
        text = re.sub(r"<[^>]+>", "", text)
        return re.sub(r"\s+", " ", text).strip()

    async def _run(self, story_id: str, contract: dict) -> None:
        folder = self.root / story_id
        folder.mkdir(parents=True, exist_ok=True)
        theme = contract.get("theme") or "ein geheimnisvoller Abend"
        mood = contract.get("mood") or "warm, spannend und verspielt"
        relationship = (f"Die beiden Hauptfiguren sind ausschließlich Nova und {self.user_name}. "
                        f"Die Geschichte handelt von Nova und {self.user_name} als gemeinsamem Paar; "
                        "keine beliebigen Ersatzfiguren und kein unbeteiligter Außenplot. "
                        "Der Nutzer wird als anwesender Handlungspartner in die Ereignisse einbezogen.")
        # Her own words, straight from the tool call. She is the only one in
        # this chain who was in the conversation; what she wrote here is the
        # brief, and `register` in particular is not decoration — measured
        # 2026-09-12, the style rules below halve the physical density of an
        # intimate scene and add fade-outs unless the register says otherwise.
        register = str(contract.get("register") or "").strip()
        characters_brief = str(contract.get("characters") or "").strip()
        avoid = str(contract.get("avoid") or "").strip()
        her_scenes = [str(s).strip() for s in (contract.get("scenes") or [])
                      if str(s).strip()]
        # One instruction per scene, hers. The voice honours it per request, so
        # this is what makes scene 7 sound unlike scene 2 instead of the whole
        # story arriving in one register.
        her_moods = [str(m).strip() for m in (contract.get("scene_moods") or [])]
        #: How much room each scene gets, hers to decide. An unknown word falls
        #: back to the ordinary length rather than refusing the story.
        her_lengths = [str(x).strip().lower()
                       for x in (contract.get("scene_lengths") or [])]
        #: What the two of them talk about in each scene, hers to plan. Turning
        #: reported speech around afterwards raised the spoken share from 10%
        #: to 15% and no further: a story written with little speech in it
        #: cannot be talked into having more after the fact.
        her_talk = [str(x).strip() for x in (contract.get("scene_talk") or [])]
        try:
            if her_scenes:
                # She planned it. No second opinion: a planner that re-plans her
                # arc is how the story stopped being hers.
                plan = {"title": str(contract.get("title") or "").strip() or theme,
                        "characters": [],
                        "scenes": [{"purpose": s} for s in her_scenes]}
                log.info("story %s follows her own outline (%d scenes)",
                         story_id, len(her_scenes))
            else:
                plan = await self._plan(story_id, theme, mood, relationship, register)
            (folder / "outline.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
            # One scene per planned scene. The old `i % len(scenes)` ran the
            # configured count regardless and wrapped back to purpose 1, so a
            # four-scene plan asked for eight scenes told the same story twice
            # with small variations — the live complaint of 2026-09-12.
            purposes = [s for s in (plan.get("scenes") or []) if s]
            if not purposes:
                purposes = [{"purpose": "Erzähle die Geschichte weiter."}] * self.scenes
            # The form scene 1 sets, carried to every scene after it. Derived
            # once and passed verbatim rather than travelling in the state
            # note, because the note is written by a model each time and the
            # form is not negotiable. Repairing this afterwards was measured
            # over five runs and never beat not breaking it: the address
            # drifted because every scene decided again.
            rules = ""
            parts: list[str] = []
            # Aligned with `parts`: each text carries the instruction it is to
            # be spoken with ("" = her plain narrating register).
            moods: list[str] = []
            state = "Noch keine Szene erzählt."
            for i, purpose in enumerate(purposes):
                last = i == len(purposes) - 1
                # Where a scene must STOP. Scene 1 of the live story of
                # 2026-09-12 was asked only for "the path narrows" and wrote
                # all the way into finding and entering the shelter — the
                # material of scenes 5 and 6 — so those scenes had to discover
                # it a second time. Naming the next scene and forbidding it is
                # the cheap half of DOC's controller.
                naechste = "" if last else _purpose_text(purposes[i + 1])
                gespraech = her_talk[i] if i < len(her_talk) else ""
                umfang = SCENE_LENGTHS.get(
                    her_lengths[i] if i < len(her_lengths) else "", SCENE_WORDS)
                scene = await self._complete(
                    "Schreibe die nächste Szene eines Hörspiels auf Deutsch. "
                    "Ein Hörspiel lebt von gesprochener Rede: Was die beiden "
                    "erleben, erfährt man vor allem daran, was sie zueinander "
                    "sagen. Beschreibung nur so viel, wie das Ohr braucht, um zu "
                    "wissen, wo sie sind und was geschieht. "
                    f"Szene {i+1}/{len(purposes)}; {relationship} Thema: {theme}; "
                    f"Stimmung: {mood}. "
                    + (f"Verbindliche Form, die Szene 1 gesetzt hat und die in "
                       f"jedem Satz gilt: {rules} " if rules else "")
                    + (f"Figuren und gemeinsame Vorgeschichte: {characters_brief}. "
                       if characters_brief else "")
                    + f"Planabsicht: {_purpose_text(purpose)} "
                    + f"Bisheriger Zustand: {state} "
                    "Die Szenerie ist bereits eingeführt; führe sie nicht neu ein "
                    "und fange nicht von vorne an. "
                    "Zeige überwiegend konkrete Handlung, Dialog und eine sichtbare Veränderung. "
                    + (f"In dieser Szene sprechen die beiden über: {gespraech} "
                       "Schreib das als gesprochene Rede aus, nicht als Bericht "
                       "darüber. " if gespraech else "")
                    + "Mindestens ein Drittel der Szene ist gesprochener Dialog: "
                    "die beiden reden miteinander, statt dass über sie berichtet "
                    "wird. Auch Streit, Necken, Halbsätze und Schweigen mitten im "
                    "Satz gehören dazu. "
                    "Vermeide langes Lamentieren, Selbstmitleid, wiederholte Gefühle und "
                    "poetische Füllsätze; innere Gedanken nur kurz und wenn sie eine Entscheidung "
                    "vorbereiten. Jeder Absatz soll Handlung, Dialog oder eine neue Information "
                    "bringen. Keine Szene darf nur aus Grübeln bestehen. "
                    # AFTER the style rules, deliberately: those rules say "move
                    # the plot on", which is the opposite of what a sustained
                    # intimate or physical scene does, and the model obeys the
                    # last word. Measured: with the rules alone such a scene
                    # came back at half the physical density and skipped the
                    # act with "später"/"danach"; with this line it does not.
                    + (f"Register, das Vorrang vor den Stilregeln hat: {register}. "
                       "Schreibe die verlangten Stellen wirklich aus, blende nicht ab "
                       "und überspringe sie nicht mit einem Zeitsprung. "
                       if register else "")
                    + (f"Die NÄCHSTE Szene ist: {naechste} Erzähle sie NICHT mit; höre auf, bevor das passiert. " if naechste else "")
                    + "Erzählperspektive, Anreden und Pronomen bleiben in der "
                      "ganzen Geschichte gleich; wechsle sie niemals. Wenn in der "
                      "Akte steht, wer mit \"du\" gemeint ist, gilt das weiter. "
                    + (f"Vermeide ausdrücklich: {avoid}. " if avoid else "")
                    + "Keine Zusammenfassung, keine Überschrift, nur erzählender Text. "
                    + f"Umfang: {umfang} Wörter; beende die Szene mit einem "
                    "vollständigen Satz. "
                    + ("Diese Szene ist die letzte und muss alle offenen "
                       "Handlungsfäden schließen. " if last else ""),
                    max_tokens=SCENE_MAX_TOKENS)
                scene = _trim_to_sentence(self._clean(scene))
                parts.append(scene)
                moods.append(her_moods[i] if i < len(her_moods) else "")
                if not rules:
                    # After the opening, not before: the first scene is what
                    # decides the form, so there is nothing to state until it
                    # exists.
                    try:
                        rules = await self._house_rules(scene)
                        log.info("story %s writes to its own form: %s",
                                 story_id, rules.replace(chr(10), " | ")[:160])
                    except Exception:
                        log.warning("story %s: could not read its own form from "
                                    "scene 1; the later scenes decide for "
                                    "themselves", story_id, exc_info=True)
                state = await self._state_update(state, scene)
            story = "\n\n".join(parts)
            # Do not let a short model completion masquerade as the configured
            # long-form contract. Add bounded continuation scenes before the
            # finale pass, so the final pass remains the last word in the story.
            extension_rounds = 0
            while _words(story) < self.min_words:
                extension_rounds += 1
                if extension_rounds > 4:
                    raise RuntimeError(
                        f"Story blieb mit {_words(story)} Wörtern unter dem "
                        f"Minimum von {self.min_words}."
                    )
                before = _words(story)
                extra = _trim_to_sentence(self._clean(await self._complete(
                    "Die Geschichte ist noch nicht lang genug für ihren Vertrag. "
                    f"Schreibe eine weitere zusammenhängende Szene auf Deutsch, "
                    f"die mindestens {min(700, self.min_words - before + 120)} "
                    "Wörter umfasst. Führe Nova und den Nutzer als anwesende "
                    "Handlungspartner weiter, bringe eine konkrete Veränderung "
                    "und bereite den späteren Abschluss vor. Keine Überschrift, "
                    "keine Zusammenfassung, nur Erzähltext. Bisheriger Schluss:\n\n"
                    f"{story[-1800:]}",
                    max_tokens=SCENE_MAX_TOKENS)))
                if _words(extra) == 0:
                    raise RuntimeError("Story-Modell lieferte keine Erweiterung.")
                parts.append(extra)
                moods.append("")
                story = "\n\n".join(parts)
            # Always give the host model a dedicated finale pass. A syntactically
            # complete last sentence is not the same thing as a resolved ending.
            needs_finale = True
            if needs_finale:
                tail = story[-3200:]
                # It is told to close every open thread, so it has to be able
                # to see them: the outline says what was promised and the state
                # note says what is still unresolved. Showing it only the last
                # 3200 characters asked it to tie off threads it could not know.
                bogen = "; ".join(_purpose_text(s)
                                  for s in (plan.get("scenes") or []))
                extra = await self._complete(
                    "Schreibe den abschließenden Abschnitt dieser deutschsprachigen Geschichte. "
                    "Der bisherige Text kann mitten in einem Satz abbrechen. Setze genau dort "
                    "sinnvoll an, schließe alle offenen Handlungsfäden und gib der Geschichte "
                    "ein klares, befriedigendes Ende. Schreibe mindestens 220 Wörter, nur "
                    "Erzähltext, keine Zusammenfassung und keine Überschrift. Der allerletzte "
                    "Satz muss vollständig sein und mit Punkt, Fragezeichen oder Ausrufezeichen "
                    "enden.\n\n"
                    f"Der geplante Bogen der ganzen Geschichte: {bogen}\n\n"
                    f"Stand der Dinge, besonders das Offene:\n{state}\n\n"
                    f"Hier ist der bisherige Schluss als Kontext:\n\n{tail}",
                    max_tokens=SCENE_MAX_TOKENS)
                parts.append(self._clean(extra))
                moods.append("")
                story = "\n\n".join(parts)
            # A provider may still truncate despite the explicit contract. Never
            # publish an audio file that ends halfway through a sentence: drop
            # only the incomplete tail and add a deterministic closing sentence.
            if not _has_complete_sentence(story) and parts:
                # Trim the PART, not just the joined text: the audio is spoken
                # from the parts, so trimming only `story` would publish a text
                # that ends cleanly and a recording that still reads the broken
                # tail out loud.
                parts[-1] = _trim_to_sentence(parts[-1])
                parts.append("Und damit war ihre gemeinsame Geschichte für "
                             "diesen Abend zu Ende.")
                moods.append("")
                story = "\n\n".join(parts)
            # The last word before the file exists: hold the whole story
            # against the rules its own opening set. Costs one short "OK" per
            # passage that is already fine.
            try:
                if rules:
                    funde = await self._review(parts, rules)
                    geaendert, gesamt = await self._straighten(parts, rules, funde)
                    story = "\n\n".join(parts)
                    log.info("story %s: straightened %d of %d sentences",
                             story_id, geaendert, gesamt)
            except Exception:
                log.warning("story %s: the rules pass failed; publishing as "
                            "written", story_id, exc_info=True)
            log.info("story %s: %.0f%% of it is spoken aloud", story_id,
                     _dialogue_share(story) * 100)
            story_path = folder / "story.md"
            story_path.write_text(f"# {plan.get('title', theme)}\n\n{story}\n", encoding="utf-8")
            if self.remember is not None:
                title = str(plan.get("title", theme))
                characters = ", ".join(str(x) for x in plan.get("characters", []))
                scenes = "; ".join(str(s.get("purpose", ""))
                                     for s in plan.get("scenes", [])
                                     if isinstance(s, dict))
                summary = (f"Nova und {self.user_name} sind die Hauptfiguren. "
                           f"Figuren: {characters or 'Nova und ' + self.user_name}. "
                           f"Handlungsbogen: {scenes or 'Anfang, Wendepunkt und Ende'}. "
                           f"Anfang: {story[:900]}\nEnde: {story[-900:]}")
                await self.remember(story_id, title, theme, summary,
                                    str(story_path))
            audio_path = await self._tts(list(zip(parts, moods)),
                                         folder / "story.wav")
            audio_path = await self._telegram_audio(audio_path)
            self.post("assistant", f"Deine Geschichte „{plan.get('title', theme)}“ ist fertig und vertont.",
                      audio_path=str(audio_path), proactive=True,
                      channel=contract.get("_channel"),
                      corr_id=contract.get("_corr_id"))
        except Exception:
            log.exception("story job %s failed", story_id)
            self.post("assistant", "Die Geschichte konnte diesmal nicht fertig vertont werden.",
                      proactive=True, channel=contract.get("_channel"),
                      corr_id=contract.get("_corr_id"))

    async def _tts(self, sections: list[tuple[str, str]], target: Path) -> Path:
        # The TTS server renders on the gaming PC's card, which the ComfyUI
        # camera shares: the whole load -> speak -> unload holds that card's
        # lock, so a photo waits for the voice instead of starving it.
        lock = self.gpu_lock or remote_gpu_lock()
        if lock.locked():
            log.info("story TTS waits for the LAN GPU (a render is running)")
        async with lock:
            return await self._tts_on_card(sections, target)

    async def _tts_on_card(self, sections: list[tuple[str, str]],
                           target: Path) -> Path:
        pcm = bytearray()
        rate = self.voice.sample_rate
        # One (passage, instruction) pair per request. A scene may be longer
        # than one request may carry, so it becomes several passages — all
        # spoken in that scene's register, which is what makes the tense scene
        # sound tense and the one after it not.
        parts: list[tuple[str, str]] = [
            (passage, instruct)
            for text, instruct in sections
            for passage in passages(text)]
        timeout = httpx.Timeout(connect=15, read=600, write=30, pool=30)
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            try:
                await self.voice.power_on(client)
                self._voice_on = True
                for index, (passage, instruct) in enumerate(parts):
                    pcm.extend(await self.voice.speak(client, passage, instruct))
                    # A short natural breath between passages. PCM is 16-bit
                    # mono, so this is silent audio, not a blocking sleep.
                    if index < len(parts) - 1:
                        pcm.extend(b"\0\0" * int(rate * 0.35))
            finally:
                # Always, not only after a load that answered: a load that
                # timed out here kept loading on the server and held 7 GB
                # nobody released (2026-09-11). Unloading is what gives the
                # card back — 7336 MiB, measured — so it is not optional.
                await self.voice.power_off(client)
                self._voice_on = False
        log.info("story audio: %d passages (%d with a register), %.1f s",
                 len(parts), sum(1 for _, i in parts if i), len(pcm) / 2 / rate)
        with wave.open(str(target), "wb") as out:
            out.setnchannels(1); out.setsampwidth(2); out.setframerate(rate)
            out.writeframes(bytes(pcm))
        return target

    async def _telegram_audio(self, wav_path: Path) -> Path:
        """Telegram rejects the large lossless WAV; make a compact MP3."""
        mp3_path = wav_path.with_suffix(".mp3")
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-loglevel", "error", "-i", str(wav_path),
            "-codec:a", "libmp3lame", "-b:a", "64k", str(mp3_path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        _, err = await proc.communicate()
        if proc.returncode != 0 or not mp3_path.is_file():
            raise RuntimeError(f"ffmpeg audio compression failed: {err.decode(errors='replace')[:300]}")
        return mp3_path
