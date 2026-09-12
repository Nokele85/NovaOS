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
    r"(korrigierter\s+abschnitt|verst(ö|oe)(ß|ss)e?|gegen\s+die\s+regeln|"
    r"der\s+abschnitt\s+(spielt|verst|h(ä|ae)lt)|die\s+regel\s+verlangt|"
    r"ich\s+pr(ü|ue)fe\s+den\s+abschnitt)", re.I)


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

    def start(self, contract: dict) -> str:
        story_id = contract.get("story_id") or uuid4().hex[:10]
        task = asyncio.create_task(self._queued(story_id, contract),
                                   name=f"story-{story_id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return story_id

    async def _queued(self, story_id: str, contract: dict) -> None:
        async with self._one_at_a_time:
            await self._run(story_id, contract)

    async def _complete(self, prompt: str, *, max_tokens: int = 900,
                        persona: bool = True) -> str:
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
        return await provider.complete([{"role": "user", "content": prompt}],
                                           temperature=0.75, max_tokens=max_tokens)

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
            "PRONOMEN: welches Pronomen jede Figur bekommt\n\n"
            f"Die Hauptfiguren stehen fest: Nova und {self.user_name}. Leite "
            "keine weiteren Regeln ab. Wetter, Tageszeit, Zeitform, Orte und "
            "Namen gehören zur Handlung und sind keine Regeln.\n\n"
            f"Anfang:\n{opening[:3000]}",
            max_tokens=300, persona=False)
        return self._clean(rules)

    async def _straighten(self, parts: list[str], rules: str) -> int:
        """Hold every later passage against the opening's rules, in place.

        DOC dropped Re3's edit pass as cost without proportional gain, and it
        was right about Re3's: "fix what is wrong" over a whole draft is vague
        and expensive. This one is narrow — named, checkable rules that we have
        watched break twice — and it is cheap because a passage that already
        obeys costs one short "OK" instead of a rewrite.

        Two guards, because a repair pass that damages is worse than none: a
        correction that comes back much shorter than the original is a summary,
        not a correction, and is refused; and any failure leaves the passage
        exactly as it was."""
        corrected = 0
        for i in range(1, len(parts)):
            try:
                answer = await self._complete(
                    "Prüfe einen Abschnitt einer Geschichte gegen feste Regeln.\n\n"
                    "Du korrigierst. Du schreibst NICHT um und du verbesserst "
                    "nichts.\n"
                    "- Kein Satz wird hinzugefügt, keiner gestrichen, keiner "
                    "umgestellt.\n"
                    "- Geändert werden nur einzelne Wörter: Pronomen, Anreden, "
                    "Zeit- und Wetterangaben.\n"
                    "- Jeder Satz, der keine Regel verletzt, kommt WORTGLEICH "
                    "zurück.\n"
                    "- Der Abschnitt ist danach genauso lang wie vorher.\n"
                    "Eine schönere Formulierung ist kein Grund für eine Änderung. "
                    "Dass dir etwas nicht gefällt, auch nicht.\n\n"
                    f"Regeln:\n{rules}\n\n"
                    "Hält der Abschnitt alle Regeln ein, antworte mit genau: OK\n"
                    "Sonst gib den korrigierten Abschnitt zurück, ohne Vorrede "
                    "und ohne Überschrift.\n\n"
                    f"Abschnitt:\n{parts[i]}",
                    max_tokens=SCENE_MAX_TOKENS, persona=False)
            except Exception:
                log.warning("straightening passage %d failed; left as written",
                            i + 1, exc_info=True)
                continue
            fixed = _trim_to_sentence(self._clean(answer))
            # It was told "no preamble" and ignored it: two passages of the
            # live story of 2026-09-12 were published with the checker's own
            # reasoning in front of them ("Verstöße: 1. Wetter …"), and the
            # length window waved them through. Prose about the rules is never
            # a corrected passage.
            if _META.search(fixed):
                log.warning("straightening passage %d answered with its own "
                            "reasoning instead of prose; kept the original",
                            i + 1)
                continue
            if not fixed or fixed.strip().upper().rstrip(".") == "OK":
                continue
            # Symmetric on purpose. The first guard caught a correction that
            # was really a summary; the live run of 2026-09-12 showed the other
            # direction — 8 of 8 passages "corrected" and the story 12% longer,
            # which is not a correction either but a rewrite with new prose in
            # it. A fix for a pronoun does not change the length.
            vorher, nachher = _words(parts[i]), _words(fixed)
            if not 0.75 * vorher <= nachher <= 1.2 * vorher:
                log.warning("straightening passage %d went from %d words to %d "
                            "— that is a rewrite, not a correction; kept the "
                            "original", i + 1, vorher, nachher)
                continue
            parts[i] = fixed
            corrected += 1
        return corrected

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
                scene = await self._complete(
                    "Schreibe die nächste Szene einer langen Geschichte auf Deutsch. "
                    f"Szene {i+1}/{len(purposes)}; {relationship} Thema: {theme}; "
                    f"Stimmung: {mood}. "
                    + (f"Figuren und gemeinsame Vorgeschichte: {characters_brief}. "
                       if characters_brief else "")
                    + f"Planabsicht: {_purpose_text(purpose)} "
                    + f"Bisheriger Zustand: {state} "
                    "Die Szenerie ist bereits eingeführt; führe sie nicht neu ein "
                    "und fange nicht von vorne an. "
                    "Zeige überwiegend konkrete Handlung, Dialog und eine sichtbare Veränderung. "
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
                    + f"Umfang: {SCENE_WORDS} Wörter; beende die Szene mit einem "
                    "vollständigen Satz. "
                    + ("Diese Szene ist die letzte und muss alle offenen "
                       "Handlungsfäden schließen. " if last else ""),
                    max_tokens=SCENE_MAX_TOKENS)
                scene = _trim_to_sentence(self._clean(scene))
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
                rules = await self._house_rules(parts[0])
                if rules:
                    fixed = await self._straighten(parts, rules)
                    story = "\n\n".join(parts)
                    log.info("story %s: straightened %d of %d passages",
                             story_id, fixed, len(parts) - 1)
            except Exception:
                log.warning("story %s: the rules pass failed; publishing as "
                            "written", story_id, exc_info=True)
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
