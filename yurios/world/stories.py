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

    async def _complete(self, prompt: str, *, max_tokens: int = 900) -> str:
        provider = self.utility() if callable(self.utility) else self.utility
        if self.character_context is not None:
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
        try:
            plan_raw = await self._complete(
                "Plane eine zusammenhängende deutschsprachige Geschichte. "
                f"{relationship} Thema: {theme}. Stimmung: {mood}. Sie braucht Anfang, "
                "Mitte, Wendepunkt und ein befriedigendes Ende. Antworte nur "
                "mit JSON: {\"title\": string, \"characters\": [string], "
                "\"scenes\": [{\"purpose\": string, \"turn\": string}]}.")
            try:
                found = re.search(r"\{.*\}", plan_raw, re.S)
                plan = json.loads(found.group(0) if found else "")
            except Exception:
                plan = {"title": theme, "characters": [], "scenes": []}
            (folder / "outline.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
            parts: list[str] = []
            state = "Noch keine Szene erzählt."
            for i in range(self.scenes):
                purpose = (plan.get("scenes") or [{}])[i % max(1, len(plan.get("scenes") or [{}]))]
                scene = await self._complete(
                    "Schreibe die nächste Szene einer langen Geschichte auf Deutsch. "
                    f"Szene {i+1}/{self.scenes}; {relationship} Thema: {theme}; Stimmung: {mood}. "
                    f"Planabsicht: {purpose}. Bisheriger Zustand: {state}. "
                    "Zeige überwiegend konkrete Handlung, Dialog und eine sichtbare Veränderung. "
                    "Vermeide langes Lamentieren, Selbstmitleid, wiederholte Gefühle und "
                    "poetische Füllsätze; innere Gedanken nur kurz und wenn sie eine Entscheidung "
                    "vorbereiten. Jeder Absatz soll Handlung, Dialog oder eine neue Information "
                    "bringen. Keine Szene darf nur aus Grübeln bestehen. "
                    "Keine Zusammenfassung, keine Überschrift, nur erzählender Text. "
                    f"Umfang: {SCENE_WORDS} Wörter; beende die Szene mit einem "
                    "vollständigen Satz. "
                    "Die letzte Szene muss alle offenen Handlungsfäden schließen.",
                    max_tokens=SCENE_MAX_TOKENS)
                scene = _trim_to_sentence(self._clean(scene))
                parts.append(scene)
                state = scene[-1200:]
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
                story = "\n\n".join(parts)
            # Always give the host model a dedicated finale pass. A syntactically
            # complete last sentence is not the same thing as a resolved ending.
            needs_finale = True
            if needs_finale:
                tail = story[-3200:]
                extra = await self._complete(
                    "Schreibe den abschließenden Abschnitt dieser deutschsprachigen Geschichte. "
                    "Der bisherige Text kann mitten in einem Satz abbrechen. Setze genau dort "
                    "sinnvoll an, schließe alle offenen Handlungsfäden und gib der Geschichte "
                    "ein klares, befriedigendes Ende. Schreibe mindestens 220 Wörter, nur "
                    "Erzähltext, keine Zusammenfassung und keine Überschrift. Der allerletzte "
                    "Satz muss vollständig sein und mit Punkt, Fragezeichen oder Ausrufezeichen "
                    "enden. Hier ist der bisherige Schluss als Kontext:\n\n"
                    f"{tail}", max_tokens=SCENE_MAX_TOKENS)
                story += "\n\n" + self._clean(extra)
            # A provider may still truncate despite the explicit contract. Never
            # publish an audio file that ends halfway through a sentence: drop
            # only the incomplete tail and add a deterministic closing sentence.
            if not _has_complete_sentence(story):
                boundaries = [story.rfind(mark) for mark in ".!?…"]
                last_boundary = max(boundaries)
                if last_boundary >= 0:
                    story = story[:last_boundary + 1].rstrip()
                story += ("\n\nUnd damit war ihre gemeinsame Geschichte für diesen Abend "
                          "zu Ende.")
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
            audio_path = await self._tts(story, folder / "story.wav")
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

    async def _tts(self, text: str, target: Path) -> Path:
        # The TTS server renders on the gaming PC's card, which the ComfyUI
        # camera shares: the whole load -> speak -> unload holds that card's
        # lock, so a photo waits for the voice instead of starving it.
        lock = self.gpu_lock or remote_gpu_lock()
        if lock.locked():
            log.info("story TTS waits for the LAN GPU (a render is running)")
        async with lock:
            return await self._tts_on_card(text, target)

    async def _tts_on_card(self, text: str, target: Path) -> Path:
        pcm = bytearray()
        rate = self.voice.sample_rate
        parts = passages(text)
        timeout = httpx.Timeout(connect=15, read=600, write=30, pool=30)
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            try:
                await self.voice.power_on(client)
                for index, passage in enumerate(parts):
                    pcm.extend(await self.voice.speak(client, passage))
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
        log.info("story audio: %d passages, %.1f s", len(parts), len(pcm) / 2 / rate)
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
