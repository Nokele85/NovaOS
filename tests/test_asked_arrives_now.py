"""What he asks for arrives while he is still there (SPEC §18.2, §22.1).

The night it was written: he asked for a comic page at 22:50, her reply said
"Ja. Als Comic. Genau so zeichne ich's." and called no tool, no pattern took it
for a promise, and when a goal did get filed later it knew only "Warum kommt
nix?". The page she finally drew was then held by quiet hours until 09:00 —
while he sat there waiting for it.
"""
from __future__ import annotations

import datetime
import json

from yurios.kernel.clock import VirtualClock
from yurios.mind.goals import asked_and_agreed
from yurios.mind.policy import quiet_hour, score_interrupt

from .conftest import SIM_START, ScriptedUtility, make_mind
from .test_mind_loop import ReviewUtility, review_goal

SAID = "Here is the second page."


def _gate(clock, **kw):
    args = dict(clock=clock, relevance=0.1, time_sensitivity=0.2,
                last_contact_out=clock.now(), interrupts_today=0,
                max_interrupts_per_day=3, threshold=0.99)
    return score_interrupt(**{**args, **kw})


def _cfg(cfg):
    return cfg.model_copy(update={"dream_enabled": False, "user_name": "Sam"})


# --- Gate 2 --------------------------------------------------------------------

def test_quiet_hours_do_not_hold_back_someone_who_is_awake():
    clock = VirtualClock(start=SIM_START.timestamp())
    clock.advance(14 * 3600)                                   # 23:00
    assert quiet_hour(clock.now())
    assert _gate(clock, decided=True).outcome == "SILENT"
    awake = _gate(clock, decided=True, awake=True)
    assert awake.outcome == "SUGGEST"
    assert awake.factors["availability"] == 1.0 and awake.factors["awake"]


def test_an_answer_while_he_is_awake_is_not_counted_against_the_cap():
    clock = VirtualClock(start=SIM_START.timestamp())
    assert _gate(clock, decided=True, interrupts_today=3).outcome == "SILENT"
    assert _gate(clock, decided=True, interrupts_today=3,
                 awake=True).outcome == "SUGGEST"
    # Her own idea is still an interruption, awake or not.
    capped = _gate(clock, interrupts_today=3, awake=True, threshold=0.1)
    assert capped.outcome == "SILENT" and capped.factors["capped"]


# --- delivery ------------------------------------------------------------------

async def test_a_message_parked_for_the_morning_goes_once_he_writes(
        cfg, seeded_vault):
    clock = VirtualClock(start=datetime.datetime(2026, 7, 6, 23, 0).timestamp())
    utility = ScriptedUtility(
        f'think goal complete\nuse tell_them {{"text": "{SAID}"}}')
    rig = make_mind(_cfg(cfg), seeded_vault, clock=clock, utility=utility)
    goal = rig.mind.goals.add("send Sam the page", kind="task", priority=0.9)

    await rig.mind.tick()
    told = next(g for g in rig.mind.goals.open_goals()
                if g.provenance == f"told:{goal.id}")
    for _ in range(6):
        if rig.mind.goals.get(told.id).state == "waiting":
            break
        rig.clock.advance(60)
        await rig.mind.tick()
    assert rig.mind.goals.get(told.id).meta["held"]["reason"] == "quiet hours"
    assert rig.post.proactive() == []

    rig.say("where is it?", reply="Coming.")
    for _ in range(30):
        rig.clock.advance(60)
        await rig.mind.tick()
        if rig.post.proactive():
            break
    assert rig.post.proactive()[0]["text"] == SAID
    assert rig.clock.now() < datetime.datetime(2026, 7, 7, 9, 0).timestamp()


# --- the promise nobody wrote down ---------------------------------------------

def test_a_request_her_reply_accepts_is_a_candidate():
    asked = asked_and_agreed("Ich will das als Comic",
                             "Ja. Als Comic. Genau so zeichne ich's.")
    assert asked is not None
    assert asked.provenance == "promise:asked-and-agreed"
    assert asked.confidence == "soft"                          # the review decides
    assert "Comic" in asked.text
    assert asked_and_agreed("draw me a picture of the lake",
                            "On it.") is not None


def test_no_candidate_without_a_request_or_with_a_refusal():
    assert asked_and_agreed("Warum kommt nix?", "Moment.") is None
    assert asked_and_agreed("I like comics", "Me too.") is None
    assert asked_and_agreed("Ich will das als Comic",
                            "Nein, das zeichne ich nicht.") is None
    assert asked_and_agreed("Ich will das als Comic", "") is None


async def test_the_review_sees_the_request_and_the_goal_knows_what_was_asked(
        cfg, seeded_vault):
    utility = ReviewUtility(review_goal("das als Comic zeichnen"))
    rig = make_mind(cfg, seeded_vault, utility=utility)
    rig.say("Mach mir die zweite Szene", reply="Gleich.")
    rig.say("Ich will das als Comic",
            reply="Ja. Als Comic. Genau so zeichne ich's.")

    for _ in range(4):
        await rig.mind.tick()
        if rig.mind.goals.open_goals():
            break
    payload = json.loads(utility.review_messages[-1][-1]["content"])
    assert payload["user_request"] == "Ich will das als Comic"
    assert any(c["provenance"] == "promise:asked-and-agreed"
               for c in payload["candidates"])
    [goal] = [g for g in rig.mind.goals.open_goals()
              if g.text == "das als Comic zeichnen"]
    assert "zweite Szene" in goal.meta["about"]
    assert "als Comic" in goal.meta["about"]
