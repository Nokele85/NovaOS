"""Asynchronous long-form story production (SPEC §7.8): brief -> scenes -> editor -> TTS -> Telegram."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import wave
from pathlib import Path
from uuid import uuid4

import httpx

from .qwenvoice import QwenVoice, passages, rejoin_abbreviations
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

# The editor is the one call that thinks, and on this route `max_tokens` bounds
# the thinking as well as the answer: fill it and the content comes back empty.
# A 4300-word story needed 24 913 reasoning tokens, so this is roughly double —
# and a ceiling is free, since only what is generated is billed.
REVIEW_MAX_TOKENS = 48_000

# DeepSeek publishes a temperature per use case: 0.0 for code, 1.0 for data
# work, 1.3 for conversation — and 1.5 for creative writing. Her scenes were
# written at 0.75, nearer the coding end than the writing one, which is the
# setting that pushes a model towards the most obvious next phrase. The most
# obvious way to render a scene is to report it, so five prompt experiments
# moved the spoken share not at all while the dial stood in the wrong place.
# Planning, the state note and the review keep a low temperature: those want
# to be right, not surprising.
#
# 1.5 measured: the spoken share rose from 13% to 20% and repeated phrasing
# fell — and the writing stopped obeying. It kept neither its own form (9.3%
# of sentences mixed the address, against 0.8-2.5% elsewhere) nor its own
# register, where an outline that said "heftiger Sex, explizit" came back at a
# fifth of the usual physical density. 1.2 then bought nothing at all —
# 12.1% spoken against 13.0% at 0.75 — and still cost density. The dial
# does not trade gradually, it tips, so it stays where the story is best.
WRITING_TEMPERATURE = 0.75
SCENE_MAX_TOKENS = 1600

#: The form a story is told in when her brief names none. It used to be read
#: off scene 1 afterwards, which left it to chance: in the A/B run of
#: 2026-09-14 scene 1 happened to tell the story from Benni's side with Nova in
#: the third person, and an editor asked to hold it to "Ich-Erzählerin Nova"
#: then patched six sentences and left every passage mixed. A form is set
#: before the first word, or it is not a rule.
DEFAULT_FORM = ("ERZÄHLPERSON: Ich-Erzählerin ist Nova. ANREDE: {user} wird "
                "durchgehend mit „du“ angesprochen, alle anderen Figuren in der "
                "dritten Person. ZEITFORM: Präsens.")

#: One reading by a German fiction editor, in the order an editor works:
#: from the large to the small (lektorat.ai, textorat.net), with one
#: perspective figure per scene (storyanalyse.de) and direct speech punctuated
#: after Duden. It replaced three passes — review, straighten, dialogue — that
#: were allowed to report four kinds of fault and nothing else, which is why
#: a story with an unintroduced character, a vanished threat and three endings
#: came back "straightened 0 of 0". Measured on the same text: 10 calls and
#: 5.34 cents before, 1 call and 1.90 cents here.
LEKTOR_PROMPT = """Du bist Lektorin für deutschsprachige Belletristik. Vor dir liegt das Manuskript eines Hörspiels, Satz für Satz nummeriert; [Absatz n] markiert den Beginn einer neuen Szene.

Verbindliche Form: {form}
Register (gewollt, niemals abschwächen): {register}

Arbeite wie im Lektorat vom Großen zum Kleinen, in dieser Reihenfolge:
1. handlung — Logiklücken, Widersprüche, unmotivierte Zufälle, zeitliche Unstimmigkeiten, verlorene Fäden (etwas wird angekündigt oder droht und ist danach verschwunden).
2. figuren — eine Figur taucht auf oder wird erwähnt, ohne eingeführt zu sein; eine Figur weiß oder tut etwas, das nicht zu ihr passt; ein Gegenstand wechselt ohne Grund (erst ein Beil, dann eine Axt).
3. uebergang — eine Szene erzählt noch einmal, was am Ende der vorigen schon passiert ist; ein Satz wiederholt fast wörtlich einen Satz kurz davor; ein Sprung ohne Übergang; mehr als ein Ende.
4. perspektive — Abweichung von der verbindlichen Form, Perspektivbruch (die Erzählstimme weiß, was sie nicht wissen kann), Head-Hopping.
5. tempus — die Erzählzeit wechselt ohne Grund.
6. sprache — Wortwiederholungen und wiederkehrende Formulierungen oder Bilder auf engem Raum, Stilbrüche, Füllwörter.
7. rede — unklarer Sprecher, gehäufte Redebegleitsätze, Zeichensetzung der wörtlichen Rede nicht nach Duden („…“, sagte sie. / Sie sagte: „…“).
{verdacht}
Was du NICHT tust: die Handlung ändern, Deutliches abschwächen, Szenen kürzen, nach Geschmack umformulieren.

Für jeden Fund:
- "satz": die Nummer des Satzes,
- "kategorie": eine der sieben oben,
- "befund": kurz, was falsch ist,
- "korrektur": der korrigierte Satz, NUR wenn sich der Fehler innerhalb genau dieses einen Satzes beheben lässt; "STREICHEN", wenn der Satz fast wörtlich wiederholt, was in den Sätzen unmittelbar davor schon steht; sonst "".
Eine Abweichung der Perspektive meldest du immer mit leerer Korrektur — sie betrifft mehr als einen Satz und wird nicht Satz für Satz umgebaut.

Höchstens 60 Funde, die wichtigsten zuerst. Antworte als json: {{"funde": [{{"satz": 1, "kategorie": "...", "befund": "...", "korrektur": "..."}}]}}

Manuskript:
{text}"""

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
    return rejoin_abbreviations(out) or [text]


def _dialogue_share(text: str) -> float:
    """How much of a story is spoken aloud, as a fraction of its characters.

    An intention that is not measured is a hope: the scene prompt has asked
    for "action, dialogue and a visible change" all along, and the share still
    fell from 22.6% to 12.9% between two live stories without anyone noticing.
    """
    gesprochen = sum(len(m) for m in
                     re.findall(r"[„\"]([^\"„“]{2,400})[“\"]", text))
    return gesprochen / max(len(text), 1)


#: Function words carry no event, so they only make echoes look alike.
_FUELLWOERTER = frozenset("""
und der die das ein eine einen einem einer ich du er sie es mich dich mir dir
sich nicht wie als auf den dem des zum zur mit von vor hinter über unter für
ist war hat habe hast haben wird werden noch nur schon dann aber oder wenn
weil dass sein ihre ihr mein dein bis aus bei nach seit doch ganz mehr sehr
""".split())


def _echoes(parts: list[str], hoechstens: int = 14) -> list[str]:
    """Things that turn up in more than one passage.

    Nouns, not word sequences. The fault this exists for is the same
    event written twice in different words — "der Schlüssel dreht sich"
    and "ich schließe auf" share nothing to match on, but both need the
    key. A door that closes in two passages and a key that turns in
    three went straight past the reviewer with 37 other sentences named
    (live, 2026-09-12), and counting is what a computer is good at.

    Suspects, not a verdict: a coat can be worn all evening and a bed
    can be lain in twice. Only reading tells a repeat from a return."""
    vor: dict[str, set[int]] = {}
    for pi, teil in enumerate(parts, 1):
        # German writes its nouns large, which is the whole parser we need.
        for wort in re.findall(r"\b[A-ZÄÖÜ][a-zäöüß]{3,}\b", teil):
            klein = wort.lower()
            if klein not in _FUELLWOERTER:
                vor.setdefault(klein, set()).add(pi)
    # Few passages, and next to each other. A thing in nine passages is a
    # motif; a thing in two adjoining ones is a scene that overshot into
    # the next and a scene that did it again. Ranking by frequency finds
    # hands and breath, which is the opposite of the question.
    verdacht = []
    for wort, ps in vor.items():
        folge = sorted(ps)
        if len(folge) == 2 and folge[1] - folge[0] == 1:
            verdacht.append((folge, wort))
    return [f"{w} in Absatz {folge[0]} und {folge[1]}"
            for folge, w in sorted(verdacht)[:hoechstens]]


def _findings(antwort: str) -> list | None:
    """The editor's findings, or None when the answer holds none.

    An empty list is an answer ("nothing to fix"); a missing one is not."""
    found = re.search(r"\{.*\}", antwort or "", re.S)
    if not found:
        return None
    try:
        daten = json.loads(found.group(0))
    except ValueError:
        return None
    if not isinstance(daten, dict):
        return None
    funde = daten.get("funde", daten.get("findings"))
    return funde if isinstance(funde, list) else None


def _repeats(satz: str, frueher: str) -> bool:
    """Does `satz` say again what `frueher` already said?

    The editor may strike a sentence only on this evidence: most of its
    content words are already in one sentence just before it. "Yara sagte
    eine Nummer ins Gerät, Datum, Uhrzeit, dann nichts mehr." after its
    near-twin passes; a new sentence that shares a name does not."""
    def inhalt(s: str) -> set[str]:
        return {w.lower() for w in re.findall(r"\b[\wÄÖÜäöüß]{4,}\b", s)
                if w.lower() not in _FUELLWOERTER}
    neu, alt = inhalt(satz), inhalt(frueher)
    return bool(neu) and len(neu & alt) / len(neu) >= 0.6


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


def _purpose_text(node) -> str:
    """The words of one outline node, whether she wrote a line or the planner
    wrote an object. Putting the dict in a prompt puts braces in her story."""
    if isinstance(node, dict):
        return str(node.get("purpose") or node.get("turn") or "").strip()
    return str(node).strip()


#: Who, not where — the chunk of an outline line that names the cast.
_WER = re.compile(r"(?i)^(beide|wir|ich|er|sie|du|nova|benni|zu zweit|allein)\b")


def _last_sentences(part: str, n: int = 2) -> str:
    """The seam: the last couple of sentences of the scene before.

    The state note says what has changed; it does not say what the listener
    just heard. Handing the writer the actual last words is what lets the
    next scene continue instead of setting up again."""
    satzen = _sentences(part)
    return " ".join(s.strip() for s in satzen[-n:]).strip()


def _opening(purpose: str) -> str:
    """The ground a scene has to start on: the first thing its brief names.

    Her outline lines are a place, then who is in it, then the beats:
    "Gehweg und Straße, beide, wir laufen schnell, Hände finden sich — die
    Ungeduld wird körperlich." The scenes that went wrong all skipped that
    first chunk and opened on a later beat, so it is the one part of the
    line the writer is told to start on rather than merely to cover."""
    kopf = purpose.split("—")[0].strip()
    teile = [t.strip() for t in kopf.split(",") if t.strip()]
    ort: list[str] = []
    for teil in teile[:2]:
        # "beide", "wir", a name: that says who, not where, and two scenes
        # in the same flat are told apart by the second chunk ("Wohnung,
        # Bett"), so the place is as many leading chunks as are places.
        if _WER.match(teil):
            break
        ort.append(teil)
    return ", ".join(ort)


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
                        persona: bool = True, thinking: bool | None = None,
                        temperature: float = 0.75,
                        response_format: dict | None = None) -> str:
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
        extra: dict = {} if thinking is None else {"thinking": thinking}
        if response_format is not None:
            extra["response_format"] = response_format
        return await provider.complete([{"role": "user", "content": prompt}],
                                       temperature=temperature, max_tokens=max_tokens,
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

    async def _verify_scene(self, scene: str, davor: str, auftrag: str,
                            danach: str) -> tuple[str, str]:
        """What this scene failed to do, or "" if it did it.

        Checked here rather than at the end, because a scene that went
        somewhere else is the ground the next one builds on: passage 2
        walked into the stairwell it was not due in, and passage 3 then
        unlocked the same door a second time (live, 2026-09-12). The
        end-of-story review missed both while naming 37 other sentences —
        it reads for contradictions and cannot know where a scene was
        supposed to be.

        Only one of the two findings is repaired, and it is the narrow
        one: did the scene start where it was supposed to. Two wider
        questions were measured on live stories and both failed. "Does
        it tick every element of its brief" fired on six of eight scenes
        and all six rewrites failed the same check again — her outline
        packs four or five elements into a line, and 400 words cannot
        hold them all without becoming a list. "Does it touch the next
        scene's material" fired on seven of eight, because leading into
        the next scene is what an ending is. What is left is the fault
        actually reported: a scene whose own opening ground never
        appears, so it begins in the stairwell it was not due in.

        A fifth of a cent for eight scenes, against 1.6 cents for the
        end review that did not catch this."""
        try:
            antwort = await self._complete(
                "Du prüfst EINE Szene gegen ihren Auftrag.\n\n"
                f"Vorher galt: {davor}\n"
                f"Diese Szene soll leisten: {auftrag}\n"
                f"Die NÄCHSTE Szene ist: {danach}\n\n"
                "Die nächste Szene darf angesteuert werden — eine Szene "
                "darf zur Tür führen. Sie darf nicht hindurchgehen und "
                "oben ankommen. Nur das Erledigen ist ein Verstoß, nicht "
                "das Hinführen.\n\n"
                "Antworte in genau zwei Zeilen:\n"
                "AUFTRAG: erfüllt / verfehlt <was fehlt>\n"
                "BEGINN: richtig / FALSCH <wo die Szene stattdessen "
                "einsetzt> — FALSCH nur dann, wenn der Ort oder die "
                "Lage, mit der ihr Auftrag anfängt, im Text überhaupt "
                "nicht vorkommt und die Szene schon weiter ist. Endete "
                "die vorige Szene schon an diesem Ort, ist es richtig, dort "
                "direkt weiterzuerzählen, ohne ihn neu zu nennen.\n\n"
                f"Szene:\n{scene[:5000]}",
                max_tokens=300, persona=False, temperature=0.2)
        except Exception:
            log.warning("scene check failed; the scene stands", exc_info=True)
            return "", ""
        offen = anfang = ""
        for zeile in (antwort or "").splitlines():
            gross = zeile.upper()
            if gross.startswith("AUFTRAG") and "VERFEHLT" in gross:
                offen = zeile.strip()
            elif gross.startswith("BEGINN") and "FALSCH" in gross:
                anfang = zeile.strip()
        return offen, anfang

    async def _lektorat(self, parts: list[str], rules: str,
                        register: str) -> tuple[int, int]:
        """Read the finished story once as its editor, and apply only what is
        safe to apply (SPEC §7.8).

        What is safe was measured on two copies of one story (2026-09-14). A
        correction inside one sentence — an axe that should be the hatchet from
        before, "sagte ich zu dir" when the one counting was Yara, a perfect
        tense in a past-tense story — stays, under the guards the old passes
        had. A deletion stays only if the sentence really repeats one just
        before it: the writer does produce those back to back ("Yara sagte eine
        Nummer ins Gerät …" twice), and a deletion that is not one is prose
        lost. A perspective finding is never applied: patched a sentence at a
        time it left every passage of that story half "ich" and half "Nova".
        Everything not applied is logged as a note for a reader.

        Returns (sentences changed, notes left)."""
        saetze = [_sentences(teil) for teil in parts]
        karte: list[tuple[int, int]] = []
        zeilen: list[str] = []
        for pi, ss in enumerate(saetze):
            zeilen.append(f"[Absatz {pi + 1}]")
            for li, satz in enumerate(ss):
                karte.append((pi, li))
                zeilen.append(f"{len(karte)}: {satz}")
        verdacht = _echoes(parts)
        auftrag = LEKTOR_PROMPT.format(
            form=rules, register=register or "wie geschrieben",
            verdacht=("\nDiese Dinge kommen in benachbarten Absätzen vor — "
                      "prüfe, ob etwas doppelt erzählt wird oder ob es "
                      "dorthin gehört:\n" + "\n".join(verdacht) + "\n")
            if verdacht else "",
            text="\n".join(zeilen))
        funde = None
        # Twice at most. DeepSeek documents that its JSON mode "may
        # occasionally return empty content", and the first live run of this
        # editor did exactly that on one of two stories (2026-09-14).
        for versuch in (1, 2):
            antwort = await self._complete(
                auftrag, max_tokens=REVIEW_MAX_TOKENS, persona=False,
                thinking=True, temperature=0.2,
                response_format={"type": "json_object"})
            funde = _findings(antwort)
            if funde is not None:
                break
            log.warning("lektorat attempt %d returned no findings list "
                        "(%d characters: %r)", versuch, len(antwort or ""),
                        (antwort or "")[:160])
        if funde is None:
            raise ValueError("the editor returned no findings list")

        geaendert = hinweise = 0
        for fund in funde[:60]:
            if not isinstance(fund, dict):
                continue
            nummer = fund.get("satz")
            if not isinstance(nummer, int) or not 1 <= nummer <= len(karte):
                continue
            pi, li = karte[nummer - 1]
            alt = saetze[pi][li]
            if not alt:
                continue                     # already struck by an earlier finding
            kategorie = str(fund.get("kategorie") or "").strip().lower()
            befund = str(fund.get("befund") or "").strip()
            neu = str(fund.get("korrektur") or "").strip()
            if not neu or neu.upper() in ("OK", "IO") or kategorie == "perspektive":
                hinweise += 1
                log.info("lektorat note [%s] sentence %d: %s", kategorie, nummer,
                         befund[:160])
                continue
            if neu.upper() == "STREICHEN":
                davor = [s for ss in saetze for s in ss][:nummer - 1]
                davor = [s for s in davor if s][-3:]
                noch_da = sum(1 for s in saetze[pi] if s)
                if kategorie in ("uebergang", "sprache") and noch_da > 1 \
                        and any(_repeats(alt, frueher) for frueher in davor):
                    saetze[pi][li] = ""
                    geaendert += 1
                else:
                    hinweise += 1
                    log.info("lektorat wanted sentence %d struck, but it does "
                             "not repeat one just before it; kept: %s",
                             nummer, befund[:120])
                continue
            if _META.search(neu) or (_BROKEN_VERB.search(neu)
                                     and not _BROKEN_VERB.search(alt)):
                log.warning("lektorat answered sentence %d with reasoning or a "
                            "broken verb; kept it", nummer)
                hinweise += 1
                continue
            if not 0.5 * _words(alt) <= _words(neu) <= 1.6 * _words(alt):
                log.info("lektorat turned sentence %d from %d words into %d; "
                         "kept it: %s", nummer, _words(alt), _words(neu),
                         befund[:100])
                hinweise += 1
                continue
            saetze[pi][li] = neu
            geaendert += 1
        for pi, ss in enumerate(saetze):
            parts[pi] = " ".join(s for s in ss if s)
            gemischt = _mixed_address([s for s in ss if s])
            if gemischt:
                log.info("passage %d still mixes forms of address in %d "
                         "sentences", pi + 1, gemischt)
        return geaendert, hinweise

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
            # The form, set before the first word and carried verbatim to every
            # scene (SPEC §7.8). Hers when her brief names one; the house form otherwise.
            # Repairing a form afterwards was measured over five runs and never
            # beat not breaking it, and reading it off scene 1 left it to chance.
            rules = str(contract.get("form") or "").strip() \
                or DEFAULT_FORM.format(user=self.user_name)
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
                anfang = _opening(_purpose_text(purpose))
                naht = _last_sentences(parts[-1]) if parts else ""
                bisher = "\n\n".join(parts)
                umfang = SCENE_LENGTHS.get(
                    her_lengths[i] if i < len(her_lengths) else "", SCENE_WORDS)
                szenen_prompt = (
                    "Schreibe die nächste Szene eines Hörspiels auf Deutsch. "
                    "Ein Hörspiel lebt von gesprochener Rede: Was die beiden "
                    "erleben, erfährt man vor allem daran, was sie zueinander "
                    "sagen. Beschreibung nur so viel, wie das Ohr braucht, um zu "
                    "wissen, wo sie sind und was geschieht. "
                    f"Szene {i+1}/{len(purposes)}; {relationship} Thema: {theme}; "
                    f"Stimmung: {mood}. "
                    + (f"Verbindliche Form, die in jedem Satz gilt: {rules} "
                       "Es gibt genau eine Erzählstimme: Sie weiß nur, was sie "
                       "selbst sieht, hört oder erschließen kann. "
                       if rules else "")
                    + (f"Figuren und gemeinsame Vorgeschichte: {characters_brief}. "
                       if characters_brief else "")
                    + f"Planabsicht: {_purpose_text(purpose)} "
                    + f"Bisheriger Zustand, vor allem was noch offen ist: {state} "
                    # The whole story so far, not a summary of it. Each scene
                    # used to see a 120-word state note and the last two
                    # sentences, and what a summary drops the next scene
                    # invents: in 325edf7838 a character nobody had introduced
                    # was "still screaming" in scene 4, a threat closing in at
                    # the end of scene 6 was gone by scene 7, and scenes cut to
                    # their next milestone without the way there. Eight scenes
                    # are about 3 600 words, which one scene call carries.
                    + (f"Die Geschichte bis hierher, vollständig:\n<<<\n{bisher}\n>>>\n"
                       if bisher else "")
                    # Where the scene belongs, not where it must arrive again.
                    # "The scene starts here, the first paragraph plays there"
                    # cured scenes that skipped their own opening, and forced a
                    # second arrival wherever the scene before had already
                    # arrived: 325edf7838 ended scene 5 at a supermarket car
                    # park and opened scene 6 describing the same car park.
                    + ((f"Diese Szene gehört an diesen Ort: {anfang}. "
                        + ("Endet die Geschichte bis hierher schon dort, "
                           "erzählst du dort direkt weiter und beschreibst den "
                           "Ort nicht noch einmal; sonst beginnt der erste "
                           "Absatz dort. " if bisher
                           else "Der erste Absatz spielt dort. "))
                       if anfang else "")
                    + ("Schreibe unmittelbar nach dem letzten Satz der "
                       "Geschichte weiter. Was dort schon steht, wird nicht "
                       "noch einmal erzählt — keine Ankunft, keine "
                       "Beschreibung, keine Handlung zweimal. Wer eingeführt "
                       "ist, bleibt eingeführt; wer neu dazukommt, wird "
                       "eingeführt. Was offen ist, wird nicht vergessen. "
                       if bisher else "")
                    + "Zeige überwiegend konkrete Handlung, Dialog und eine sichtbare Veränderung. "
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
                       "Auch dabei wird gesprochen — Fragen, Anweisungen, Namen, "
                       "halbe Sätze, ein Lachen zwischendurch. Stumme Körper sind "
                       "unwahrscheinlicher als redende. "
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
                       "Handlungsfäden schließen. " if last else ""))
                scene = await self._complete(
                    szenen_prompt, max_tokens=SCENE_MAX_TOKENS,
                    temperature=WRITING_TEMPERATURE)
                scene = _trim_to_sentence(self._clean(scene))
                # Checked before it counts. A scene that went somewhere
                # else is the ground the next one builds on, and by the
                # end of the story that is no longer repairable — the
                # end review read past exactly this twice.
                davor = state + (f" Die vorige Szene endete so: {naht}"
                                 if naht else "")
                offen, anfang = await self._verify_scene(
                    scene, davor, _purpose_text(purpose), naechste or "keine")
                if offen:
                    # Logged, not repaired. Her outline lines pack four or five
                    # elements into a sentence, so this asks "did you tick every
                    # box" — a 400-word scene often cannot without becoming a
                    # list. Six such repairs were attempted on one live story
                    # and all six failed the same check again.
                    log.info("story %s scene %d did not tick every box: %s",
                             story_id, i + 1, offen[:110])
                if anfang:
                    log.info("story %s scene %d: %s — one repair",
                             story_id, i + 1, anfang[:120])
                    zweiter = _trim_to_sentence(self._clean(
                        await self._complete(
                            szenen_prompt + "\n\nEin erster Versuch hat "
                            "den Anfang der Szene übersprungen: "
                            f"{anfang} Schreibe sie neu und setze "
                            "dort ein, wo ihr Auftrag anfängt — der Ort "
                            "und die Lage aus seiner ersten Zeile müssen "
                            "vorkommen.",
                            max_tokens=SCENE_MAX_TOKENS,
                            temperature=WRITING_TEMPERATURE)))
                    # Only if it is actually better. A repair that fixes
                    # the brief and loses half the scene is not one.
                    if _words(zweiter) >= 0.7 * _words(scene):
                        _, rest = await self._verify_scene(
                            zweiter, davor, _purpose_text(purpose),
                            naechste or "keine")
                        if not rest:
                            log.info("story %s scene %d: repaired",
                                     story_id, i + 1)
                            scene = zweiter
                        else:
                            log.info("story %s scene %d: still %s — kept the "
                                     "first", story_id, i + 1, rest[:80])
                    else:
                        log.info("story %s scene %d: the repair came back at "
                                 "%d of %d words — kept the first", story_id,
                                 i + 1, _words(zweiter), _words(scene))
                parts.append(scene)
                moods.append(her_moods[i] if i < len(her_moods) else "")
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
                    max_tokens=SCENE_MAX_TOKENS,
                    temperature=WRITING_TEMPERATURE)))
                if _words(extra) == 0:
                    raise RuntimeError("Story-Modell lieferte keine Erweiterung.")
                parts.append(extra)
                moods.append("")
                story = "\n\n".join(parts)
            # A finale only after extension scenes (SPEC §7.8). The last planned scene is
            # already told to close every thread, and it now sees the whole
            # story to do it; a finale appended after it anyway gave every
            # story two endings, and 325edf7838 three. Extension scenes are
            # told to *prepare* the close, so they alone still need one.
            needs_finale = extension_rounds > 0
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
                    max_tokens=SCENE_MAX_TOKENS,
                    temperature=WRITING_TEMPERATURE)
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
            # The last word before the file exists: one reading by its editor.
            try:
                geaendert, hinweise = await self._lektorat(parts, rules, register)
                story = "\n\n".join(parts)
                log.info("story %s: lektorat changed %d sentences and left %d "
                         "notes", story_id, geaendert, hinweise)
            except Exception:
                log.warning("story %s: the lektorat failed; publishing as "
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
                # Always (SPEC §7.8), not only after a load that answered: a load that
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
