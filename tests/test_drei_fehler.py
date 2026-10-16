"""Three faults one comic goal ran into, 14–16 Oct (SPEC §18.2a, §22.1h, §22.3).

A goal promised a comic page, drew it again and again, and every morning told
the user it was stuck: each new page stayed on the goal because `offered` said
"a picture went" without saying which; the step horizon never met a goal whose
every step drew or told; and its success was "he confirms he got it", which she
cannot see.
"""
from __future__ import annotations

from yurios.mind import acts
from yurios.mind.goalwork import CEILING_HORIZONS, past_ceiling
from yurios.mind.goals import PROMISE_REVIEW_SYSTEM, offered_mark, picture_offered

from .conftest import ScriptedUtility, make_mind


def _cfg(cfg, steps=3):
    return cfg.model_copy(update={"dream_enabled": False, "user_name": "Sam",
                                  "mind_goal_max_steps": steps})


# --- 1. each picture is handed on once, a new picture again ---------------------

def test_the_offered_mark_names_the_picture(cfg, seeded_vault):
    rig = make_mind(_cfg(cfg), seeded_vault)
    g = rig.mind.goals.add("draw page two", kind="task", priority=0.7,
                           meta={"product": {"image_url": "/selfies/p1.png"}})
    assert not picture_offered(g)
    rig.mind.goals.update(g.id, meta={"offered": offered_mark(g, "g-heir")})
    assert picture_offered(rig.mind.goals.get(g.id))
    rig.mind.goals.update(g.id, meta={"product": {"image_url": "/selfies/p2.png"}})
    assert not picture_offered(rig.mind.goals.get(g.id))      # a new page is new
    rig.mind.goals.update(g.id, meta={"offered": "g-old-heir"})
    assert picture_offered(rig.mind.goals.get(g.id))          # legacy marks stay as they were


def test_a_redrawn_picture_rides_with_the_next_message(cfg, seeded_vault):
    rig = make_mind(_cfg(cfg), seeded_vault)
    g = rig.mind.goals.add("draw page two", kind="task", priority=0.7,
                           meta={"product": {"image_url": "/selfies/p1.png"}})
    _, _, first = acts.queue_telling(rig.mind, {"text": "here it is"}, goal_id=g.id)
    assert rig.mind.goals.get(first).product["image_url"] == "/selfies/p1.png"
    rig.mind.goals.update(first, state="done")
    rig.mind.goals.update(g.id, meta={"product": {"image_url": "/selfies/p2.png"}})
    _, _, second = acts.queue_telling(rig.mind, {"text": "and the new one"}, goal_id=g.id)
    assert second != first
    assert rig.mind.goals.get(second).product["image_url"] == "/selfies/p2.png"
    # …and the same picture is never handed on twice
    rig.mind.goals.update(second, state="done")
    _, _, third = acts.queue_telling(rig.mind, {"text": "again"}, goal_id=g.id)
    assert not rig.mind.goals.get(third).product.get("image_url")


# --- 2. the ceiling: a goal's steps are spent for good ---------------------------

async def test_a_goal_past_the_ceiling_is_let_go_without_another_step(cfg, seeded_vault):
    utility = ScriptedUtility("think one more try\nuse draw_comic {}")
    rig = make_mind(_cfg(cfg, steps=3), seeded_vault, utility=utility)
    g = rig.mind.goals.add("draw the page fresh until it reaches them", kind="task",
                           priority=0.7, commitment="single-minded",
                           meta={"steps": CEILING_HORIZONS * 3,
                                 "product": {"image_url": "/selfies/p7.png"}})
    assert past_ceiling(rig.mind, rig.mind.goals.get(g.id))
    calls = len(utility.calls)
    acted, _, notes = await rig.mind._act_goal_work(rig.mind.goals.get(g.id))
    assert len(utility.calls) == calls                       # no model, no drawing
    assert rig.mind.goals.get(g.id).state == "abandoned"
    assert acted["state"] == "abandoned" and "let go" in notes[0]
    heir = next(x for x in rig.mind.goals.open_goals() if x.provenance == f"followup:{g.id}")
    assert heir.product["image_url"] == "/selfies/p7.png"    # the last page still goes


def test_below_the_ceiling_and_blind_goals_keep_working(cfg, seeded_vault):
    rig = make_mind(_cfg(cfg, steps=3), seeded_vault)
    g = rig.mind.goals.add("a", kind="task", meta={"steps": CEILING_HORIZONS * 3 - 1})
    b = rig.mind.goals.add("birthday", kind="task", commitment="blind", meta={"steps": 99})
    assert not past_ceiling(rig.mind, g)
    assert not past_ceiling(rig.mind, b)


# --- 3. success is what she can see ----------------------------------------------

def test_the_review_asks_for_success_she_can_observe():
    flat = " ".join(PROMISE_REVIEW_SYSTEM.split())
    assert "Never the user's reply, confirmation or reaction" in flat
