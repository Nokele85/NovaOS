"""The host-side story handoff in TextTurns (docs/tools.md → tell_story).

Two things the handoff must get right, both found live: a question *about* a
story she already told has to reach the model and her memory rather than start
a second ten-minute job, and the job it does start has to inherit the turn's
channel and corr_id — it finishes off-turn, and that is its only address."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from yurios.kernel import correlate
from yurios.world.turns import TextTurns


@pytest.mark.parametrize("text", [
    "Erzähl mir eine Geschichte über einen Leuchtturm",
    "Schreib mir bitte eine lange Gutenachtgeschichte",
    "Kannst du mir eine Story über Drachen erzählen?",
    "Ich hätte gern noch eine Geschichte",
    "Erzähl mir eine Geschichte über das letzte Einhorn",
    "tell me a story about the sea",
])
def test_a_request_for_a_new_story_is_a_story_intent(text):
    assert TextTurns._story_intent(text)


@pytest.mark.parametrize("text", [
    "Erinnerst du dich an die Geschichte vom Leuchtturm?",
    "Wie ging die Geschichte gestern aus?",
    "Weißt du noch, wie unsere letzte Geschichte endete?",
    "Erzähl mir die letzte Geschichte nochmal",
    "Was ist die Story hinter deinem Namen?",
    "Die Geschichte mit dem Router nervt mich",
    "Mach mal Musik an",
    # SPEC §7.8, found live (2026-10-08): a story to be drawn is hers to answer
    "Das sieht gut aus und nun mach mir eine Comic Story ich glaub an dich",
    "Kannst du mir eine Geschichte zeichnen?",
    "Erzähl mir eine Bildergeschichte in vier Panels",
    "Mal mir bitte eine kleine Geschichte",
    "draw me a story about the sea",
])
def test_recall_and_everyday_mentions_are_not(text):
    assert not TextTurns._story_intent(text)


class _Brain:
    guard = SimpleNamespace(turn=lambda: None)

    def __init__(self, result="{\"status\": \"started\"}", exc=None):
        self.result, self.exc, self.seen = result, exc, None

    def resolve_session(self, session_id):
        return session_id or "s1"

    def turn_context(self, **kw):
        return correlate.scope(kind=correlate.CHAT_TURN, **kw)

    async def _execute(self, call, turn):
        self.seen = correlate.current()
        if self.exc is not None:
            raise self.exc
        return self.result


class _RT:
    def __init__(self, brain):
        self.brain, self._tool_runner, self.in_flight = brain, object(), 0
        self.posted: list[tuple[str, dict]] = []
        self.signals = SimpleNamespace(post=lambda *a, **k: None)
        self.park_gate = SimpleNamespace(wait=self._open)
        self.uploads = None
        self.stopping = asyncio.Event()

    async def _open(self):
        return None

    def post_message(self, role, text, **kw):
        self.posted.append((text, kw))
        return {"id": str(len(self.posted)), "text": text}

    def turn_started(self, **kw):
        self.in_flight += 1

    def turn_ended(self):
        self.in_flight -= 1


def test_the_story_job_inherits_the_turns_channel_and_corr_id():
    rt = _RT(_Brain())
    asyncio.run(TextTurns(rt).run("Erzähl mir eine Geschichte", channel="telegram"))
    assert rt.brain.seen is not None
    assert rt.brain.seen.channel == "telegram"
    assert rt.brain.seen.corr_id
    assert rt.in_flight == 0
    assert "vertone" in rt.posted[-1][0]


def test_a_refused_story_is_not_promised():
    rt = _RT(_Brain(result="denied (rate)"))
    asyncio.run(TextTurns(rt).run("Erzähl mir eine Geschichte", channel="telegram"))
    ack = rt.posted[-1][0]
    assert "nicht starten" in ack and "vertone" not in ack


def test_a_cancelled_story_turn_does_not_leak_the_turn_counter():
    rt = _RT(_Brain(exc=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(TextTurns(rt).run("Erzähl mir eine Geschichte", channel="cli"))
    assert rt.in_flight == 0
