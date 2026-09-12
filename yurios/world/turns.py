"""Channel-agnostic text turns (SPEC §10.5) — the inbound half of the seam.

The voice route owns the *audio* turn: mic → STT → `TurnController` → TTS,
with barge-in and latency masking. Every other medium — the terminal, Telegram,
a plain HTTP caller, one day a game engine's NPC — is a **text** conversation,
and they all share this one runner. It is the YuriOS frontend rule
("user input becomes a `user_message` signal; frontends talk to the host,
never to the brain directly") made concrete: a channel hands text in here and
renders `message` events off the EventHub; it never touches the brain.

One turn, end to end, mirroring the voice route's forks minus the audio:

  - the user's line joins the transcript (`message` on the hub) and the
    SignalBus (`user_message` — the mind's ENGAGED preempt, SPEC §15.3);
  - `turn_started`/`turn_ended` bracket the turn (the mind knows she's talking);
  - brain tokens stream through the `EmotionParser`: tags drive the face on the
    puppet lane (`controller.set_expression`, voice fork #5) and are stripped
    from the shown text; completed sentences accumulate as a `draft` on the hub;
  - a clean turn persists the *verbatim* reply (tags kept, B2's corpus rule),
    commits the shown text as a `message`, and tees `turn_committed` onto the
    bus (the mind's REFLECT share: world model, promise extraction);
  - a mid-stream failure publishes `draft_cancel` and leaves **no trace** —
    no transcript entry, no persist, no signal (B2 §4.4's rule, kept).

Turns from every text channel serialise on one lock: the persist path and the
session store aren't concurrent-safe, and one companion holds one conversation
at a time. The voice route's turns are per-connection and already torn down
before a new one starts; text turns simply queue behind each other.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from contextlib import nullcontext

from yurios.desktop.voice.emotion import EmotionParser
from yurios.desktop.voice.sentences import cut_sentences
from .tooltags import ToolCall

log = logging.getLogger("world.turns")

# What a story job needs that only she can know. The regex decides THAT a story
# was asked for; this decides WHAT it is, and it has to, because she is the only
# part of the chain that was in the conversation — the writer downstream sees
# these fields and nothing else.
_BRIEF_PROMPT = (
    "Du bist Nova. {who} bittet dich gerade um eine Geschichte. Schreibe den "
    "Auftrag für die Geschichte, die DU erzählen willst — nicht die Geschichte "
    "selbst.\n\nSeine Bitte, wörtlich:\n{text}\n\n{recent}"
    "Antworte nur mit JSON:\n"
    "{{\"title\": string — der NAME der Geschichte, wenige Worte. Er "
    "steht auf seinem Telefon neben der Aufnahme, also ein Titel und keine "
    "Notiz an dich selbst,\n"
    " \"theme\": string — worum es geht, seine eigenen Worte wo sie zählen,\n"
    " \"mood\": string — die Stimmung,\n"
    " \"register\": string — Ton und wie weit es geht, ausdrücklich. Wenn er "
    "deutlich oder explizit gebeten hat, schreib das hier hin und beschönige "
    "es nicht; dieses Feld hat Vorrang vor den Stilregeln des Schreibers.\n"
    " \"characters\": string — wer vorkommt und was aus eurer gemeinsamen "
    "Geschichte hineingehört; nenne Anrede und Pronomen jeder Figur, etwa "
    "Benni: er/ihm,\n"
    " \"avoid\": string — was diese Geschichte nicht tun soll,\n"
    " \"scenes\": [string] — deine Gliederung, {n} Szenen, je eine Zeile, in "
    "der Reihenfolge, in der sie erzählt werden. Jede Zeile nennt Ort, wer "
    "dabei ist, was passiert und was sich dadurch ändert,\n"
    " \"scene_lengths\": [string] — wie viel Raum jede Szene bekommt: "
    "\"kurz\", \"normal\" oder \"lang\", gleiche Anzahl und Reihenfolge wie scenes. Das "
    "Gewicht gehört dem, worum er gebeten hat — der Weg dorthin ist nicht die "
    "Geschichte und darf kurz sein,\n"
    " \"scene_subtext\": [string] — was einer von euch in dieser Szene "
    "sagen will und NICHT sagt, gleiche Anzahl und Reihenfolge wie scenes. "
    "Nicht das Thema, sondern das darunter. Daran entsteht Dialog, weil ihr "
    "darum herumredet,\n"
    " \"scene_sense\": [string] — ein konkretes Detail zum Riechen, Hören "
    "oder Anfassen je Szene,\n"
    " \"scene_moods\": [string] — wie jede Szene KLINGEN soll, gleiche Anzahl "
    "und Reihenfolge wie scenes: kurze Anweisung an die Stimme, etwa "
    "\"fröhlich und leicht\", \"gedämpft und spannungsvoll, fast flüsternd\", "
    "\"ruhig und warm erzählend\"}}")


async def _story_brief(rt, text: str) -> dict:
    """Her brief for her own story, or today's bare contract if she can't write
    one. Never raises: a brief that failed must still leave a story startable,
    and the reason has to be in the log rather than in a silently thinner
    story (the whole lesson of 2026-09-12)."""
    fallback = {"theme": text, "mood": "stimmig, handlungsorientiert",
                "length_minutes": 10}
    provider = getattr(getattr(rt.brain, "state", None), "utility", None)
    if provider is None:
        return fallback
    lines = []
    for entry in (getattr(rt, "transcript", None) or [])[-8:]:
        role = "Du" if entry.get("role") == "assistant" else "Er"
        said = str(entry.get("text", ""))[:300]
        if said:
            lines.append(f"{role}: {said}")
    recent = ("Woran ihr gerade wart:\n" + "\n".join(lines) + "\n\n") if lines else ""
    try:
        raw = await provider.complete(
            [{"role": "user", "content": _BRIEF_PROMPT.format(
                who=rt.cfg.user_name, text=text, recent=recent,
                n=rt.cfg.story_scenes)}],
            temperature=0.4, max_tokens=1600)
        found = re.search(r"\{.*\}", raw or "", re.S)
        data = json.loads(found.group(0) if found else "")
        if not isinstance(data, dict) or not data.get("theme"):
            raise ValueError("no theme in her brief")
        brief: dict = {"theme": str(data["theme"]), "length_minutes": 10,
                 "title": str(data.get("title") or ""),
                 "mood": str(data.get("mood") or fallback["mood"]),
                 "register": str(data.get("register") or ""),
                 "characters": str(data.get("characters") or ""),
                 "avoid": str(data.get("avoid") or ""),
                 "scenes": [str(s) for s in (data.get("scenes") or []) if str(s).strip()],
                 "scene_moods": [str(m) for m in (data.get("scene_moods") or [])],
                 "scene_lengths": [str(x) for x in (data.get("scene_lengths") or [])],
                 "scene_subtext": [str(x) for x in (data.get("scene_subtext") or [])],
                 "scene_sense": [str(x) for x in (data.get("scene_sense") or [])]}
        log.info("story brief: %d scenes, register %r",
                 len(brief["scenes"]), str(brief["register"])[:60])
        return brief
    except Exception as e:  # noqa: BLE001 — a thin story beats no story
        log.warning("story brief failed (%s); falling back to the bare "
                    "contract — the story will not carry her register", e)
        return fallback

# A *new* story is asked for with an indefinite noun ("eine Geschichte", "noch
# eine Gutenachtgeschichte", "a story") next to a request ("erzähl", "schreib",
# "kannst du", "ich hätte gern"). A definite one ("die Geschichte von gestern")
# is a story that already exists, and a recall question about it must reach the
# model and her memory — not start a second ten-minute job in its place.
_NEW_STORY = re.compile(
    r"\b(?:eine|ne|'ne|noch\s+eine|neue|a|another)\s+(?:\w+\s+){0,3}?"
    r"(?:\w*geschichte|story|märchen|maerchen)\b")
_STORY_REQUEST = re.compile(
    r"\b(?:erzähl|erzaehl|schreib|dicht|denk\s+dir|ausdenk|mach\s+mir|vorles|"
    r"kannst\s+du|könntest\s+du|koenntest\s+du|willst\s+du|magst\s+du|"
    r"ich\s+(?:will|möchte|moechte|hätte\s+gern|haette\s+gern)|bitte|"
    r"tell\s+me|write\s+me|write)")
_STORY_RECALL = re.compile(
    r"\b(?:erinner\w*|weißt\s+du\s+noch|weisst\s+du\s+noch|wie\s+ging|"
    r"nochmal|noch\s+mal|remember|"
    r"(?:letzte[nr]?|gestrige[nr]?|vorige[nr]?)\s+(?:\w*geschichte|story))\b")


class TextTurns:
    """The one text-turn runner every channel shares (built once, on Runtime)."""

    def __init__(self, rt):
        self.rt = rt
        self._lock = asyncio.Lock()

    @staticmethod
    def _story_intent(text: str) -> bool:
        """Detect an explicit story request before the free-form model pass.

        Image requests already have a reliable host-side handoff. Long stories
        need the same boundary: a model must not silently turn an expensive,
        asynchronous job into an ordinary paragraph just because the wording
        also says "short" or "erotic".
        """
        line = text.casefold()
        if _STORY_RECALL.search(line):
            return False
        return bool(_NEW_STORY.search(line) and _STORY_REQUEST.search(line))

    async def greet(self, *, channel: str,
                    session_id: str | None = None) -> dict:
        """She speaks first, in text (SPEC §7, §9.3) — the voice route's greeting
        fork with the audio taken out.

        The same three properties the voice path has, for every text channel:
        the opener comes from `brain.stream_greeting` (which plays `BOOTSTRAP.md`'s
        authored cold open on the first-ever arrival, then retires it, §5.4); it
        is committed as a **proactive** message, because she was not answering
        anything; and it is never persisted as a turn — an opener is not a turn
        the user took, so it leaves no journal entry and no corpus line.

        Greeting is once per session per run, checked and marked under the turn
        lock: a terminal that reconnects to a conversation it is already having
        is not a new arrival (the voice route's `rt.greeted`, shared with it so
        opening the CLI beside a live headset cannot greet twice). Returns
        {"session_id": …, "message": entry-or-None}; `None` means she was
        already greeted this run, or said nothing."""
        rt = self.rt
        async with self._lock:
            session_id = rt.brain.resolve_session(session_id)
            if session_id in rt.greeted:
                return {"session_id": session_id, "message": None}
            # NOT marked greeted yet: a greeting that dies mid-stream is a
            # greeting that did not happen, and rolls back like every other turn
            # here. Marking on entry meant one failed stream cost her the
            # opener for the whole run. The turn lock serialises this method, so
            # the check above cannot race with the mark below.
            await rt.park_gate.wait()          # the §7.6 door, as in `run`
            rt.turn_started(proactive=True)
            # A cold open is a scene, not an utterance: what she shows is the
            # card's text, whole, and the parser below only decides what a voice
            # would have said of it (the voice route's rule, §5.4).
            cold = rt.brain.cold_open()
            if cold:
                rt.hub.publish("draft", {"text": cold})
            parser = EmotionParser(default=rt.cfg.expression_default)
            shown: list[str] = []
            buf = ""
            prev_events = 0
            try:
                async for token in rt.brain.stream_greeting(session_id):
                    speakable = parser.push(token)
                    while len(parser.events) > prev_events:
                        rt.controller.set_expression(
                            parser.events[prev_events].expression, 1.0, reset_ms=0)
                        prev_events += 1
                    buf += speakable
                    done, buf = cut_sentences(buf)
                    for s in done:
                        shown.append(s)
                        if not cold:               # …unless the text is given
                            rt.hub.publish("draft", {"text": " ".join(shown)})
                parser.finish()
                if buf.strip():
                    shown.append(buf.strip())
            except Exception:
                # nothing was committed and nothing was appended (a greeting
                # never puts a user line in the window), so there is nothing to
                # roll back — the draft simply never becomes a message.
                rt.hub.publish("draft_cancel", {})
                log.exception("text greeting failed mid-stream (channel %s)", channel)
                raise
            finally:
                rt.turn_ended()

            # The stream finished, so she has now greeted this session — whether
            # or not it produced text. A silent-but-clean greeting is still an
            # arrival; only a failure above (which raised) leaves it un-marked.
            rt.greeted.add(session_id)
            entry = None
            text = cold or (" ".join(shown) if shown else "")
            if text:
                entry = rt.post_message("assistant", text,
                                        proactive=True, channel=channel,
                                        session_id=session_id)
            return {"session_id": session_id, "message": entry}

    async def run(self, text: str, *, channel: str,
                  session_id: str | None = None,
                  client_id: str | None = None,
                  image_id: str | None = None) -> dict:
        """Drive one text turn. Returns {"session_id": …, "message": entry}
        (`message` is None for an empty reply). Raises on a mid-stream brain
        failure — the caller decides how to surface it; nothing was committed.

        `image_id` is a picture the sender put on the shelf first
        (`POST /api/uploads`, SPEC §35). It rides two lanes from here: the
        transcript entry carries its URL, so every room shows what was sent, and
        the brain gets the bytes for this one prompt. An id that no longer
        resolves raises `LookupError` rather than quietly sending the words
        alone — a picture that silently didn't arrive is the worst of the three
        outcomes."""
        rt = self.rt
        attachment = None
        if image_id:
            attachment = rt.uploads.get(image_id)
            if attachment is None:
                raise LookupError(f"no such picture: {image_id}")
        async with self._lock:
            session_id = rt.brain.resolve_session(session_id)
            user_entry = rt.post_message("user", text, channel=channel,
                                         client_id=client_id,
                                         session_id=session_id,
                                         image_url=attachment.url
                                         if attachment else None)
            rt.signals.post("user_message", {"text": text}, source=channel)
            # A selfie may be holding her brain's VRAM right now (§7.6): wait
            # at the door rather than loading the chat model back onto a card
            # the render hasn't finished with. Her line is already in the chat
            # above, so the wait is visible as her thinking, not as a freeze.
            # BEFORE `turn_started`, always: the park's quiet gate waits on
            # that very counter, so a turn that announced itself and then
            # blocked here would be waiting on a park waiting on it.
            await rt.park_gate.wait()
            rt.turn_started()
            # A story request is a durable, long-running operation. Route it
            # through the same Guard -> MCP -> host-realisation path as a
            # marker, but decide the intent before DeepSeek can answer with a
            # normal paragraph. This is the hardened counterpart to the
            # voluntary token-stream marker described in docs/tools.md.
            if self._story_intent(text) and getattr(rt, "_tool_runner", None) is not None \
                    and hasattr(rt.brain, "_execute"):
                # The trigger is a regex, but the brief is hers. It used to be
                # three hardcoded values, which is why every story arrived with
                # the same mood and no register at all, and why an explicit
                # wish reached the writer as one clause inside `theme` — where
                # the scene prompt's style rules then smoothed it away.
                args = await _story_brief(rt, text)
                # Inside the turn's scope, like a marker would be: the job
                # finishes minutes later off-turn and reads its channel and
                # corr_id from here — without it the audio has no address.
                turn_context = getattr(rt.brain, "turn_context", None)
                context = turn_context(channel=channel, client_id=client_id,
                                       session_id=session_id) \
                    if turn_context else nullcontext()
                try:
                    with context:
                        result = await rt.brain._execute(
                            ToolCall(tool="tell_story", args=args),
                            rt.brain.guard.turn())
                finally:
                    rt.turn_ended()
                # `_execute` never raises; a refused or failed call comes back
                # as "denied (…)" / "error (…)", and promising audio then is
                # the one thing a delivery contract must never do.
                if str(result).startswith(("denied", "error")):
                    ack = ("Ich kann die Geschichte gerade nicht starten "
                           f"({result}). Versuch es gleich noch einmal.")
                else:
                    ack = ("Ich plane und vertone jetzt eine vollständige Geschichte. "
                           "Du bekommst sie danach hier als Audio.")
                entry = rt.post_message("assistant", ack, channel=channel,
                                        session_id=session_id)
                rt.signals.post("turn_committed", {"text": text, "reply": ack},
                                source=channel)
                return {"session_id": session_id, "message": entry,
                        "user_message": user_entry, "story_result": result,
                        "active_selfies": []}
            parser = EmotionParser(default=rt.cfg.expression_default)
            raw: list[str] = []          # model output verbatim (tags kept, for persist)
            shown: list[str] = []        # committed sentences, tags stripped
            buf = ""
            prev_events = 0
            turn_context = getattr(rt.brain, "turn_context", None)
            context = turn_context(channel=channel, client_id=client_id,
                                   session_id=session_id) \
                if turn_context else nullcontext()
            # The image part is the brain's business, not the runner's: what
            # this passes is the data url, and only when there is one, so a
            # brain seam that never heard of pictures (the route tests' fake)
            # keeps its two-argument signature.
            reply_kw = {"image": rt.uploads.data_url(attachment)} \
                if attachment else {}
            try:
                with context:
                    async for token in rt.brain.stream_reply(session_id, text,
                                                             **reply_kw):
                        raw.append(token)
                        speakable = parser.push(token)
                        # a closed tag drives the face before the text after it
                        while len(parser.events) > prev_events:
                            rt.controller.set_expression(
                                parser.events[prev_events].expression, 1.0, reset_ms=0)
                            prev_events += 1
                        buf += speakable
                        done, buf = cut_sentences(buf)
                        for s in done:
                            shown.append(s)
                            rt.hub.publish("draft", {"text": " ".join(shown)})
                    parser.finish()
                    if buf.strip():
                        shown.append(buf.strip())
            except asyncio.CancelledError:
                rt.hub.publish("draft_cancel", {})
                rt.brain.abandon(session_id)
                raise
            except Exception:
                # a turn that didn't happen leaves no trace (B2 §4.4) — including
                # in her memory: stream_reply already put the user's line in the
                # session window, and only persist writes her half, so an
                # un-rolled-back failure leaves a question the next prompt reads
                # as still unanswered (she answers it again, in the next turn).
                rt.hub.publish("draft_cancel", {})
                rt.brain.abandon(session_id)
                log.exception("text turn failed mid-stream (channel %s)", channel)
                raise
            finally:
                rt.turn_ended()

            entry = None
            if not shown:
                rt.brain.abandon(session_id)   # nothing to commit — same rollback
            if shown:
                reply = " ".join(shown)
                entry = rt.post_message("assistant", reply, channel=channel,
                                        session_id=session_id)
                # Persisting is another model call — the memory extractor's —
                # and it runs *after* `turn_ended`, so as far as the parker is
                # concerned the room has already gone quiet. Held, so a render
                # that starts in this gap waits for it instead of unloading the
                # model it is talking to (§7.6). No `wait` first: the turn came
                # through the gate at the top, and this is the same turn.
                async with rt.park_gate.hold():
                    await rt.brain.persist(session_id, text, "".join(raw))
                rt.signals.post("turn_committed",
                                {"text": text, "reply": reply}, source=channel)
            selfies = rt.selfies.active_ids(client_id) if rt.selfies else []
            return {"session_id": session_id, "message": entry,
                    "user_message": user_entry, "active_selfies": selfies}
