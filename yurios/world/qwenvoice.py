"""Qwen3-TTS on the LAN card, with the card handed back when the voice is done.

Two services, one machine, two jobs:

* ``:8091`` speaks — ``POST /v1/audio/speech``, OpenAI-shaped, PCM16 at 24 kHz.
* ``:8092`` owns the model's residency — ``POST /an`` loads it (~110 s),
  ``GET /zustand`` reports the progress, ``POST /aus`` unloads it.

The split is the point. A story is minutes of work on a card a camera shares,
so the voice takes the card, speaks, and gives it back; measured on the
reference rig, ``/aus`` returns 7336 MiB (Rook's ``STIMME_SCHALTEN.md``). An
earlier build kept the model resident because unloading only happened after a
successful load — here it happens in a ``finally``, because unloading a model
that is already gone is free and forgetting one is not.

The synthesis itself is deliberately *not* streamed: the model plans prosody
over a whole request, so one request per passage reads like one breath, while
a stream of short slices reads like a list. That is the same reason passages
are split at sentence boundaries only, never mid-sentence.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import httpx

log = logging.getLogger("world.qwenvoice")

# One request must stay clear of the model's own generation ceiling (~2048
# tokens, about 2.7 minutes of speech): a request that hits it returns audio
# that stops mid-sentence. German narration runs near 2.5 words a second, so
# 200 words is roughly 80 seconds — long enough for one arc of prosody, with
# room to spare.
SPEAK_MAX_WORDS = 200

# Loading the weights takes ~110 s on the reference rig; the ceiling is for a
# cold page cache, not for the normal case.
LOAD_TIMEOUT_S = 420.0
LOAD_POLL_S = 3.0


def passages(text: str, max_words: int = SPEAK_MAX_WORDS) -> list[str]:
    """Split a story into speakable passages at sentence boundaries.

    Paragraphs are kept whole while they fit, because a paragraph is the unit
    the story was written in. A paragraph over the ceiling is split between
    sentences — never inside one, which is what makes a clause land flat.
    """
    out: list[str] = []
    for paragraph in (p.strip() for p in re.split(r"\n\s*\n", text)):
        if not paragraph:
            continue
        if len(paragraph.split()) <= max_words:
            out.append(paragraph)
            continue
        current: list[str] = []
        for sentence in re.split(r"(?<=[.!?…])\s+", paragraph):
            if not sentence.strip():
                continue
            if current and len(" ".join(current + [sentence]).split()) > max_words:
                out.append(" ".join(current))
                current = []
            current.append(sentence)
        if current:
            out.append(" ".join(current))
    return out


class QwenVoice:
    """The LAN voice: turn it on, speak passages, always turn it off."""

    def __init__(self, *, speech_url: str, supervisor_url: str,
                 ref_audio: str, ref_text: str, language: str = "German",
                 token: str = "", sample_rate: int = 24000):
        self.speech_url = speech_url.rstrip("/")
        self.supervisor_url = supervisor_url.rstrip("/")
        self.ref_audio = ref_audio
        self.ref_text = ref_text
        self.language = language
        self.token = token
        self.sample_rate = sample_rate

    # -- the card ----------------------------------------------------------

    @property
    def _headers(self) -> dict[str, str]:
        return {"x-companion-token": self.token} if self.token else {}

    async def power_on(self, client: httpx.AsyncClient) -> None:
        """Ask for the model, then wait for it. Raises if it never arrives."""
        r = await client.post(f"{self.supervisor_url}/an", headers=self._headers,
                              timeout=httpx.Timeout(60, connect=10))
        r.raise_for_status()
        state: dict[str, Any] = r.json() if r.content else {}
        if state.get("bereit"):
            log.info("story voice was already loaded (%s MiB VRAM)", state.get("vram_mib"))
            return
        loop = asyncio.get_running_loop()
        deadline = loop.time() + LOAD_TIMEOUT_S
        while loop.time() < deadline:
            await asyncio.sleep(LOAD_POLL_S)
            z = await client.get(f"{self.supervisor_url}/zustand", headers=self._headers,
                                 timeout=httpx.Timeout(30, connect=10))
            z.raise_for_status()
            state = z.json()
            if state.get("bereit"):
                log.info("story voice ready after %.0f s (%s MiB VRAM)",
                         state.get("seit_s") or 0.0, state.get("vram_mib"))
                return
            if state.get("vorgang") == "fehler":
                raise RuntimeError(f"story voice failed to load: {state.get('anzeige')}")
        raise TimeoutError(f"story voice did not load within {LOAD_TIMEOUT_S:.0f} s")

    async def power_off(self, client: httpx.AsyncClient) -> None:
        """Give the card back. Never raises: a story is already written."""
        try:
            r = await client.post(f"{self.supervisor_url}/aus", headers=self._headers,
                                  timeout=httpx.Timeout(120, connect=10))
            state = r.json() if r.content else {}
            log.info("story voice unloaded (%s MiB VRAM left in use)", state.get("vram_mib"))
        except Exception:
            log.warning("could not unload the story voice — the LAN card may "
                        "still be holding it", exc_info=True)

    # -- speaking ----------------------------------------------------------

    def _body(self, text: str) -> dict[str, Any]:
        return {
            "model": "qwen3-tts",
            "input": text,
            "language": self.language,
            # "Base" is the cloning model: one fixed timbre from the reference
            # clip, rather than a voice re-designed (and drifting) per call.
            "task_type": "Base",
            "ref_audio": self.ref_audio,
            "ref_text": self.ref_text,
            "response_format": "pcm",
        }

    async def speak(self, client: httpx.AsyncClient, text: str) -> bytes:
        r = await client.post(f"{self.speech_url}/v1/audio/speech",
                              json=self._body(text))
        r.raise_for_status()
        return r.content
