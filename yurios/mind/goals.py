"""Goals & intentions (SPEC §22) — `goals.md` is the store, human-readable.

This is what gives the background loop direction; without it an always-on
agent is a screensaver. Every goal carries **provenance** (who created it,
from what), a **commitment strategy** (how hard it defends itself against a
changing world), and a lifecycle: pending → active → waiting → done|abandoned.

Goal genesis is designed, not assumed — a store only the user writes to
starves the loop within weeks. The sources, stamped on every goal:
  * `user:*`     — explicit asks ("remind me to…").
  * `promise:*`  — REFLECT scans her own replies for commitments she made
    ("I'll look into that") and files each one; a companion who forgets her
    own promises is worse than one who forgets yours.
  * `maintenance:*` — DREAM backlog, knowledge drops.
  * `followup:<goal>` — work she finished, waiting to be mentioned.
The file itself is a markdown checklist: `cat vault/goals.md` reads as her
to-do list, because it is one.

A promise splits two ways and the difference decides everything downstream.
"I'll tell you when it lands" is a `reach_out`: the whole content is that you
hear it, so its act is a message and Gate 2 rules on it. "I'll look into that"
is a `task`: the content is work, its act is a working step, and it may reach
for a hand (§26.2). Filing both as `reach_out` — which is what this did until
`promise_kind` — meant she talked about everything and did none of it.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from typing import Iterable

from yurios.kernel.clock import Clock

from .util import iso_of, new_id, ts_of_iso
from .vaultio import MindVault

COMMITMENTS = ("blind", "single-minded", "open-minded")

# Content-word overlap catches one intention rephrased without collapsing two
# generic relationship goals whose embeddings happen to sit close together.
GOAL_ECHO_THRESHOLD = 0.28
_ECHO_STOP = frozenset("""
a an the and or but if then so to of in on at for with without from by as is
are was were be been being it its this that these those there here you your
yours i me my mine we our us he him his she her hers they them their do does
did done doing have has had having will would can could should may might must
not no nor now just about into over under before after again more most one two
three what which who whom when where why how all any both each few other some
such only own same than too very
""".split())


@dataclass
class Goal:
    id: str
    text: str
    kind: str = "task"          # reach_out | task | maintenance
    priority: float = 0.5
    due: str | None = None      # ISO — when this becomes time-sensitive
    commitment: str = "single-minded"
    provenance: str = "user"    # source[:detail]
    state: str = "pending"      # pending | active | waiting | done | abandoned
    created: str = ""
    meta: dict = field(default_factory=dict)

    def is_due(self, clock: Clock, horizon_hours: float = 12.0) -> bool:
        if self.due is None:
            return False
        return (ts_of_iso(self.due) - clock.now()) / 3600 <= horizon_hours

    @property
    def dated(self) -> bool:
        """Whether `due` is a real time, not a filer's shelf life (SPEC §18.2)."""
        return self.due is not None and not self.provenance.startswith(SHELF_LIFE)

    def is_stale(self, clock: Clock) -> bool:
        """Past due — whether it is defended or dropped is the commitment's call."""
        return self.due is not None and ts_of_iso(self.due) < clock.now()

    @property
    def steps(self) -> int:
        """How many working ticks this goal has already had (SPEC §22).

        The horizon lives in `meta` rather than in a column because it is
        bookkeeping the *loop* keeps, not something a person reading
        `goals.md` as a checklist needs a heading for.
        """
        try:
            return int(self.meta.get("steps", 0))
        except (TypeError, ValueError):
            return 0

    @property
    def dispatched(self) -> dict:
        """The tool call this goal is waiting on, or `{}` (SPEC §22, §7.3).

        Written when a `tool_step` starts work the loop will not await, read
        when `task_completion` comes back to decide which goal it belongs to.
        """
        d = self.meta.get("dispatched")
        return d if isinstance(d, dict) else {}

    @property
    def product(self) -> dict:
        """What this goal's dispatched work actually made, or `{}` (SPEC §18.2a).

        `dispatched` is the call she is waiting on; this is what came back from
        it and is still sitting in the gallery. The landing rule keeps the lab
        from posting its own product, which is right — but it left the product
        with nowhere to *be* between the render and the delivery, so a photo she
        took for you could only ever be described, never sent. Kept in `meta`
        for `dispatched`'s reason: it is the loop's bookkeeping, not a column
        somebody reading `goals.md` as a checklist needs.
        """
        p = self.meta.get("product")
        return p if isinstance(p, dict) else {}

    @property
    def held_picture(self) -> str:
        """The `image_url` of a picture this goal holds and nobody has seen, or "".

        A product marked `deliver: chat` is already in the conversation; every
        other one — a vault product, or a legacy one with no marker — is still
        waiting to be shown (SPEC §18.2a).
        """
        p = self.product
        if p.get("deliver") == "chat":
            return ""
        return str(p.get("image_url") or "")


def _echo_words(text: str) -> set[str]:
    return {word for word in re.findall(r"[a-z]+", (text or "").lower())
            if len(word) >= 3 and word not in _ECHO_STOP}


def night_owned(text: str) -> bool:
    """True when the text is DREAM's job, not a daytime desk task (SPEC §22.1b).

    Strategy used to refile the `maintenance:dream` leftover as "list diary
    vs kept-memory", and she then spent days on `list_notes` looking for a
    folder that is not on the desk. Consolidation writes
    `memory/semantic/facts.md`; `workspace/diary/` is a separate night job.
    A standing research job writes the morning brief. None of those is a hand.
    """
    t = " ".join((text or "").lower().split())
    if "kept-memory" in t or "kept memory" in t:
        return True
    if "unconsolidated" in t:
        return True
    if "consolidat" in t and any(
            w in t for w in ("night", "nights", "diary", "memory")):
        return True
    if any(p in t for p in ("morning brief", "market brief", "overnight brief",
                            "market-brief")):
        return True
    if "wake" in t and any(
            p in t for p in ("spot price", "s&p", "bitcoin", "gold",
                             "market number")):
        return True
    return False


def echoes(text: str, existing: Iterable[Goal]) -> Goal | None:
    """Return the open goal that expresses the same content, if one exists."""
    words = _echo_words(text)
    for goal in existing:
        if getattr(goal, "text", "").strip().lower() == (text or "").strip().lower():
            return goal
    # One content word ("see what's there") is too little evidence for a
    # repository-wide semantic merge. Exact equality above still deduplicates it.
    if len(words) < 2:
        return None
    for goal in existing:
        theirs = _echo_words(getattr(goal, "text", ""))
        if len(theirs) < 2:
            continue
        shared = words & theirs
        overlap = len(shared) / min(len(words), len(theirs))
        if len(shared) >= 3 and overlap >= GOAL_ECHO_THRESHOLD:
            return goal
    return None


#: How long a goal she files with `create_goal` may be (SPEC §22.1d). It is a
#: title on her list, one line of `goals.md`; the reasons behind it are the
#: `think` line she writes above the call, which is kept beside it.
GOAL_TEXT_MAX = 200


def goal_shape_refused(args: dict | None) -> str:
    """Why `create_goal`'s arguments are not a goal's shape, or "" (§22.1d).

    The one refusal she can fix by writing the same call again, which is why
    `handwork` lets her do that without spending one of the step's calls.
    """
    text = " ".join(str((args or {}).get("text") or "").split())
    kind = str((args or {}).get("kind") or "task")
    if not text:
        return "a goal needs text saying what you'll do"
    if len(text) > GOAL_TEXT_MAX:
        return (f"a goal is one line of at most {GOAL_TEXT_MAX} characters and "
                f"this one is {len(text)} — write just what you'll do, and put "
                "why in the `think` line above it")
    if "|" in text:
        return "a goal's text cannot contain '|'"
    if kind not in ("task", "reach_out"):
        return 'kind is "task" or "reach_out"'
    return ""


#: Provenance of a goal a goal step filed with `create_goal` (SPEC §22.1c): the
#: step's own goal is the parent, and it has at most one such goal open.
STEP_GOAL = "goal:"

#: Provenance of a goal she filed in her free time (SPEC §22.7): nothing on her
#: list was ready, she looked back over her days, and this is what she chose.
MUSE_GOAL = "muse:"

#: Provenance of a goal she filed after reading a document you handed her
#: (SPEC §34.6): `handed:<desk path>`. Not one of her own-judgement goals — it
#: traces back to something you did, so neither the filing switch nor the cap
#: on goals of her own applies to it.
HANDED_GOAL = "handed:"

#: Goals she filed on her own judgement rather than for a parent goal — the
#: night's stock-take (`strategy:<day>`, §22.1b) and her free time. These are
#: what `MIND_SELF_GOALS_MAX` counts and what the inner-life panel marks hers.
OWN_JUDGEMENT = ("strategy:", MUSE_GOAL)

#: Goals whose `due` is a shelf life, not an appointment (SPEC §18.2). Nothing
#: she files names a date — `create_goal` takes none — so the one these carry
#: is only when an untouched goal lets go of itself. Gate 2 scores them as
#: undated: a three-day expiry read as a deadline denied her the waiting
#: credit and held the reach-out under the threshold until its last six hours.
SHELF_LIFE = (STEP_GOAL, HANDED_GOAL, *OWN_JUDGEMENT)

#: Provenance prefixes that name a parent goal, deduplicated on the parent.
PARENTED = ("followup:", "told:", STEP_GOAL)


def already_carrying(text: str, provenance: str,
                     existing: Iterable[Goal]) -> Goal | None:
    """Is this a goal she is already carrying? Two questions, not one.

    A goal that names a parent — `followup:<id>`, the news half of a promise
    she has now kept (§22.1) — is a *second act* about content she already has
    a goal for. Comparing its words to anything is therefore the wrong test:
    the follow-up quotes its parent in order to say what it is about, scores an
    overlap of 1.0 against it, and is merged back into the very goal it exists
    to report on. That is not a near miss. Of the first seven promises she
    kept, the four whose text ran to three content words or more were never
    mentioned to anybody — she did the research, she wrote the thing, and the
    goal that would have told them was quietly absorbed on the way out. The
    three that survived were the short ones ("see what's there"), which fell
    under `echoes`' three-word floor by luck.

    The boilerplate is the same trap from the other side: two follow-ups of
    *different* parents share "tell", "came" and "goals" and merge into each
    other at 0.38 overlap, so the second promise kept while the first is still
    waiting to be told loses its news too.

    For these the honest question is whether this parent already has one —
    which is an exact test, and idempotent. Everything else asks the original
    question: is this the same intention, rephrased (`echoes`)?

    A message she decided to send from a goal step (`told:<id>`, §18.2b) is
    the same shape for the same reason: it quotes the goal it came from, and
    the rewording test would fold her words back into the goal they finish.

    A goal a step filed (`goal:<id>`) is exact on its parent here because that
    is the cap — one open at a time per parent; the rewording test against
    every *other* open goal is `acts.file_from_step`'s, which can say which
    goal she is already carrying rather than handing one back unexplained.
    """
    if provenance.startswith(PARENTED):
        return next((g for g in existing if g.provenance == provenance), None)
    return echoes(text, existing)



LINE_RE = re.compile(r"^- \[(?P<done>[ x~])\] \((?P<id>[\w-]+)\) (?P<text>.*?)"
                     r"(?P<fields>(?: \| \w[\w-]*: [^|]*)*)$")


def trim(text: str, limit: int = 200) -> str:
    """One line, short enough that `goals.md` still reads as a checklist —
    the meta field rides on the goal's own line."""
    one = " ".join((text or "").split())
    return one if len(one) <= limit else one[:limit - 1].rstrip() + "…"


class GoalStore:
    def __init__(self, vault: MindVault, clock: Clock):
        self.vault = vault
        self.clock = clock

    # ------------------------------------------------------------------ parse

    def all(self) -> list[Goal]:
        goals = []
        for line in self.vault.read("goals.md").splitlines():
            m = LINE_RE.match(line.strip())
            if not m:
                continue
            fields = dict(re.findall(r"\| (\w[\w-]*): ([^|]*)", m.group("fields")))
            state = fields.get("state", "").strip()
            if not state:
                state = {"x": "done", "~": "abandoned"}.get(m.group("done"), "pending")
            goals.append(Goal(
                id=m.group("id"), text=m.group("text").strip(),
                kind=fields.get("kind", "task").strip(),
                priority=float(fields.get("priority", 0.5)),
                due=fields.get("due", "").strip() or None,
                commitment=fields.get("commit", "single-minded").strip(),
                provenance=fields.get("from", "user").strip(),
                state=state, created=fields.get("created", "").strip(),
                meta=_parse_meta(fields.get("meta", ""))))
        return goals

    def open_goals(self) -> list[Goal]:
        return [g for g in self.all() if g.state in ("pending", "active", "waiting")]

    def desk_labels(self, desk: str = "goals/{id}.md", *,
                    this: str = "") -> dict[str, str]:
        """Which goal each desk file belongs to, for the desk digest (§22.3).

        By id alone, `goals/g-8c6059baa064.md` and four others like it named
        nothing — live, 4 Oct, a skill goal's step took the newest of them,
        the finished diary goal's, for its own. `this` marks the goal asking.
        """
        return {desk.format(id=g.id):
                ("this goal" if g.id == this else f"“{trim(g.text, 60)}” ({g.state})")
                for g in self.all()}

    def get(self, goal_id: str) -> Goal | None:
        return next((g for g in self.all() if g.id == goal_id), None)

    # ------------------------------------------------------------------ write

    def _render(self, goals: list[Goal]) -> str:
        lines = ["# Goals", ""]
        for g in goals:
            box = {"done": "x", "abandoned": "~"}.get(g.state, " ")
            parts = [f"- [{box}] ({g.id}) {g.text}",
                     f"kind: {g.kind}", f"priority: {g.priority}"]
            if g.due:
                parts.append(f"due: {g.due}")
            parts += [f"commit: {g.commitment}", f"from: {g.provenance}",
                      f"state: {g.state}", f"created: {g.created}"]
            if g.meta:
                parts.append(f"meta: {json.dumps(g.meta, ensure_ascii=False)}")
            lines.append(" | ".join(parts))
        return "\n".join(lines) + "\n"

    def _save(self, goals: list[Goal]) -> None:
        self.vault.write("goals.md", self._render(goals))

    def add(self, text: str, *, kind: str = "task", priority: float = 0.5,
            due: str | None = None, commitment: str = "single-minded",
            provenance: str = "user", meta: dict | None = None) -> Goal:
        goals = self.all()
        open_goals = [g for g in goals
                      if g.state in ("pending", "active", "waiting")]
        existing = already_carrying(text, provenance, open_goals)
        if isinstance(existing, Goal):
            return existing
        g = Goal(id=new_id("g"), text=text, kind=kind, priority=priority, due=due,
                 commitment=commitment, provenance=provenance,
                 created=iso_of(self.clock.now()), meta=meta or {})
        goals.append(g)
        self._save(goals)
        return g

    def set_state(self, goal_id: str, state: str) -> None:
        self.update(goal_id, state=state)

    def update(self, goal_id: str, *, state: str | None = None,
               meta: dict | None = None, priority: float | None = None,
               due: str | None = None) -> Goal | None:
        """One read-modify-write over `goals.md`, for everything a working tick
        changes about a goal at once.

        `set_state` used to be the only mutator, which is why the lifecycle was
        never used: advancing a goal means moving its state *and* recording what
        step it is on *and* often when to look at it again, and three separate
        rewrites of the checklist would be three chances to lose one of them.
        `meta` is merged, not replaced — the provenance a dispatched tool call
        leaves behind must survive the next step's step-counter bump.
        """
        goals = self.all()
        found: Goal | None = None
        for g in goals:
            if g.id != goal_id:
                continue
            found = g
            if state is not None:
                g.state = state
            if meta:
                g.meta = {**g.meta, **meta}
            if priority is not None:
                g.priority = priority
            if due is not None:
                g.due = due
        if found is not None:
            self._save(goals)
        return found

    def picture_shown(self, image_url: str) -> list[Goal]:
        """A picture reached the chat: no goal holding it may send it again.

        One photo rides on two goals once it is handed on — the goal that made
        it and the follow-up `offer_the_picture` files to send it (§18.2a) —
        and whichever delivered it only marked itself. Live, 5 Oct: the
        follow-up sent the picture at 09:01, the parent went on telling chat
        "the picture is already taken; Grant hasn't seen it yet", and at 09:04
        she showed it again. Every holder is re-marked `deliver: chat`; an
        open follow-up holding it is done, its whole errand having happened.
        Returns the goals changed.
        """
        if not image_url:
            return []
        goals = self.all()
        changed = []
        for g in goals:
            if str(g.product.get("image_url") or "") != image_url:
                continue
            if g.product.get("deliver") != "chat":
                g.meta = {**g.meta, "product": {**g.product, "deliver": "chat"}}
                changed.append(g)
            if (g.state in ("pending", "active", "waiting")
                    and g.provenance.startswith("followup:")):
                g.state = "done"
                if g not in changed:
                    changed.append(g)
        if changed:
            self._save(goals)
        return changed

    def reconsider(self) -> list[Goal]:
        """Apply commitment strategies to stale goals (SPEC §22.2): blind is
        defended, open-minded drops the moment it stops being timely.

        Returns the goals *this* pass let go of, not every goal that happens to
        be abandoned. The difference is the whole value of the return: the day
        rollover journals a line per goal it gets back (`loop._day_rollover`),
        so the wider reading re-announced "let go of: X (the moment for it
        passed)" every morning for every goal ever dropped — including ones you
        let go of yourself, hours earlier, by hand.
        """
        goals = self.all()
        dropped = []
        for g in goals:
            if g.state not in ("pending", "waiting"):
                continue
            if g.is_stale(self.clock) and g.commitment == "open-minded":
                g.state = "abandoned"
                dropped.append(g)
        if dropped:
            self._save(goals)
        return dropped


def _parse_meta(raw: str) -> dict:
    raw = raw.strip()
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else {}
    except json.JSONDecodeError:
        return {}


# --- REFLECT's promise scan (SPEC §22.1) ---------------------------------------
#
# A promise is not only "I'll". Live, the commitments that went unfiled were the
# softer ones — "let me look that up", "I can dig into that tonight", "I want to
# read more about it before I answer" — which is exactly the register a warm
# companion makes most of her commitments in. So the scan is a small family of
# openers rather than one, with three rules holding it steady:
#
#   * **the negation guard is shared.** "I'll never leave", "let me not push",
#     "I can't look that up" are not promises and must never become goals; the
#     guard is written once, in `_NEGATED`, and every opener consults it.
#   * **the cap and the dedupe stay.** At most three per exchange (a reply that
#     enumerates six things is not six commitments), and the same commitment
#     phrased twice in one reply is one goal.
#   * **conditionals are left alone.** "if you want, I can…" is an offer she is
#     waiting on an answer to, not something she has taken on — filing it makes
#     her chase work nobody asked for.
PROMISE_RES = (
    # "I'll look into it", "I will read it tonight"
    re.compile(r"\bI(?:'ll| will| shall)\s+(.{4,100}?)(?:[.!?\n]|$)", re.I),
    # "let me look that up" — an offer she has already started making good on
    re.compile(r"\blet me\s+(.{4,100}?)(?:[.!?\n]|$)", re.I),
    # "I can look that up", "I could dig into it" — capability offered as intent
    re.compile(r"\bI(?: can| could| should)\s+(.{4,100}?)(?:[.!?\n]|$)", re.I),
    # "I want to read up on it first", "I'd like to think about it"
    re.compile(r"\bI(?: want to| need to|'d like to| mean to)\s+"
               r"(.{4,100}?)(?:[.!?\n]|$)", re.I),
    # "I'm going to sit with that"
    re.compile(r"\bI(?:'m| am) going to\s+(.{4,100}?)(?:[.!?\n]|$)", re.I),
)
REMIND_RE = re.compile(r"\bremind me to\s+(.{4,100}?)(?:[.!?\n]|$)", re.I)

# --- the same scan in German (SPEC §22.1g) -------------------------------------
#
# Every opener above is English, so a companion who answers in German could make
# no promise the store would keep: "Ich mach's fertig und meld mich" filed
# nothing, the turn ended, and the work she had just taken on evaporated. The
# German family is not a translation of the English one, because the grammar
# will not allow it, and three traps decided its shape — each measured on a live
# companion's German replies before any of this was written:
#
#   * **the present tense is also the future.** "Ich mach das Licht an" is what
#     she is doing *now*; "ich mach's fertig" is a commitment. A bare "ich" plus a
#     verb matched most of what she says. So the present-tense opener is a closed
#     list of *working* verbs, and a separable one only counts when its particle
#     ends the clause — "ich schau nach" is looking something up, "ich schau nach
#     dem Eingang" is looking towards a door.
#   * **"werden" is also "to become".** "Ich werd rot" is a blush, not a plan,
#     and every one of the ten "ich werd…" lines in the sample was that. The
#     future therefore needs an infinitive to end the clause, and it is only ever
#     `soft` — a noun ending in -en still slips through ("bis in die Zehen"), and
#     only the review can tell.
#   * **negation follows the verb.** "Ich schau nicht nach" puts "nicht" second,
#     where the English guard, which reads the opening word, never looks.
#
# Measured on 411 German replies: the working-verb opener matched nine lines and
# all nine were commitments; one reply in sixteen offers any candidate at all.
_DE_END = r"(?:[.!?\n—–]|$)"
#: A separated particle ("nach", "durch", "raus", "aus") counts only at the end
#: of its clause — that is what tells the verb apart from the preposition.
_DE_PARTICLE_END = r"(?=\s*(?:[,.!?\n—–]|$))"
PROMISE_RES_DE = (
    # "Ich werde das nachlesen", "ich werd's dir zeigen" — the future puts its
    # infinitive last; "ich werd rot" ends on an adjective and is refused
    (re.compile(r"\bich\s+werd(?:e|')?\s+((?:[^.!?\n—–,]{0,90}?\s)?\w{2,}(?:en|ern|eln))"
                + _DE_PARTICLE_END, re.I), "soft"),
    # "Ich kümmere mich drum", "ich kümmer mich um die App"
    (re.compile(r"\bich\s+(k[üu]mmer(?:e)?\s+mich\s+.{2,100}?)" + _DE_END, re.I),
     "strong"),
    # "Ich meld mich, wenn ich fertig bin", "ich sag dir Bescheid" — the news half
    (re.compile(r"\bich\s+((?:meld(?:e)?\s+mich|sag(?:e)?\s+dir\s+bescheid"
                r"|geb(?:e)?\s+dir\s+bescheid)\b.{0,100}?)" + _DE_END, re.I), "strong"),
    # "Lass mich das nachsehen" — German's "let me"
    (re.compile(r"\blass\s+mich\s+(.{4,100}?)" + _DE_END, re.I), "soft"),
    # the present tense as the future — working verbs only
    (re.compile(r"\bich\s+((?:"
                r"(?:schau|guck|seh|sieh)\w*\b[^.!?\n—–,]{0,50}?\bnach" + _DE_PARTICLE_END
                + r"|les\w*\b[^.!?\n—–,]{0,60}?\b(?:nach|durch)" + _DE_PARTICLE_END
                + r"|(?:such|find)\w*\b[^.!?\n—–,]{0,50}?\b(?:raus|heraus)"
                + _DE_PARTICLE_END
                + r"|mach\w*\b[^.!?\n—–,]{0,40}?\bfertig\b"
                r"|arbeit\w*\s+(?:da)?(?:ran|dran)\b"
                r"|probier\w*\b[^.!?\n—–,]{0,50}?\baus" + _DE_PARTICLE_END
                + r"|geh\w*\s+(?:\w+\s+){0,3}(?:nachsehen|nachschauen|nachgucken"
                r"|nachlesen|gucken|schauen|lesen|suchen|pr[üu]fen)\b"
                r"|(?:recherchier|pr[üu]f|test|bau|schreib)\w*\b"
                r"|geh\w*\s+an\s+die\s+arbeit\b"
                r"|mach\w*\s+mich\s+(?:an\s+die\s+arbeit|(?:da)?ran|dran)\b"
                r")[^.!?\n]{0,70}?)" + _DE_END, re.I), "soft"),
    # "Dann bau ich mir das Gerüst", "Erst schau ich mir an, was drauf ist",
    # "Jetzt bau ich." — a time word in front puts the verb *before* "ich" (the
    # verb-second rule), which is how German announces a sequence of steps. The
    # time word is itself the future marker here, so no particle is asked for;
    # the verbs are the same closed working list, plus the telling ones.
    (re.compile(r"\b(?:dann|jetzt|gleich|erst|zuerst|morgen|nachher|sp[äa]ter|danach"
                r"|sofort|heute\s+abend|als\s+n[äa]chstes|bis\s+dahin)\s+"
                r"((?:bau|schreib|schau|guck|les|such|find|pr[üu]f|test|probier|versuch"
                r"|recherchier|fang|flash|arbeit|h[öo]r|meld|erz[äa]hl|komm)"
                r"\w*(?:'s)?\s+ich\b[^.!?\n—–]{0,80}?)" + _DE_END, re.I), "soft"),
)
#: An opener right after an opening quotation mark is her quoting a line, not
#: making one — `ich hab dir gesagt „ich meld mich", und …` is about a promise,
#: and filing it would chase work nobody took on. True in either language.
_QUOTE_OPEN = frozenset("„\"‚«»“")
#: "Erinnere mich daran, morgen die App zu testen" — the user's explicit ask.
REMIND_RE_DE = re.compile(
    r"\berinner(?:e|')?\s+mich\s+(?:(?:bitte|mal|doch)\s+)*(?:da)?(?:ran)?,?\s*"
    r"(?:dass\s+ich\s+)?(.{4,100}?)" + _DE_END, re.I)
#: German puts "nicht" after the verb: "ich schau nicht nach" negates in word two.
#: "Ich meld mich nicht, bis es steht" is the one "nicht" that is not a refusal —
#: it means *only once*, and that is a promise with its moment named.
_NEGATED_DE = re.compile(r"^(?:\S+\s+){0,4}?(?:nicht|nie|niemals|kein\w*)\b"
                         r"(?!,?\s*(?:bis|bevor|eher|vorher)\b)", re.I)

#: What the opener may not be followed by. `not`/`never` invert the promise;
#: the modal "have to"/"had to" turns "I can" into a report rather than an offer.
_NEGATED = re.compile(r"^(?:never|not|n't|no\b|hardly|barely)\b", re.I)

#: An opener sitting inside a conditional is a question, not a commitment. Only
#: the clause the opener starts is inspected, so "I'll read it if you send it"
#: still files — she committed and named a precondition, which is different from
#: "if you like, I can read it".
_CONDITIONAL = re.compile(
    r"\b(?:if|unless|whether|wenn|falls|ob|sofern)\b[^.!?\n]{0,60}$", re.I)

#: …and the same offer, phrased with the condition on the end. "if you send it"
#: is a precondition on work she took on; "if you want" is her asking. Only the
#: second is excluded, and only because it is the one she is waiting on an
#: answer to.
_TRAILING_OFFER = re.compile(
    r",?\s*\b(?:(?:if|unless)\s+(?:you|you'd|you would)\s+"
    r"(?:want|like|wish|prefer|care)"
    r"|(?:wenn|falls)\s+du\s+(?:(?:das|es)\s+)?(?:willst|magst|m[öo]chtest"
    r"|lust\s+hast))\b.*$", re.I)

# Quoted examples are evidence about what somebody said, not a new commitment in
# the reply that quotes them. Straight/curly double quotes and Markdown code spans
# cover the live false positive without treating the apostrophe in "I'll" as a quote.
_QUOTED = re.compile(r'"[^"\n]*"|“[^”\n]*”|`[^`\n]*`')


def _clean(text: str) -> str:
    return text.strip().rstrip(",;:").strip()


@dataclass(frozen=True)
class PromiseCandidate:
    """One possible commitment, preserving enough source for semantic review."""

    index: int
    text: str
    provenance: str
    source: str
    start: int
    confidence: str                 # explicit | strong | soft

    def as_dict(self) -> dict:
        return {"index": self.index, "text": self.text,
                "provenance": self.provenance, "source": self.source,
                "start": self.start, "confidence": self.confidence}


@dataclass(frozen=True)
class PromiseDecision:
    """The one canonical unresolved objective accepted from an exchange."""

    text: str
    kind: str
    rationale: str
    success: str
    candidates: tuple[int, ...]


class PromiseReviewError(ValueError):
    """A utility completion did not satisfy the promise-review contract."""


PROMISE_REVIEW_SYSTEM = """\
You are reviewing possible commitments extracted from one completed conversation.
Decide whether the character left any NEW, UNRESOLVED work after the reply ended.
Conversational framing, principles, feelings, capabilities, hypotheticals, and work
already completed in the same reply are not goals. Merge clauses that are steps of
one intention. Judge only the supplied candidates and user request. Return at most
one concrete, standalone objective grounded in their subject and object. Never copy
another task or invent work. If every candidate is framing, return null.

The final state at the END of the assistant reply controls. If the reply later gives
the result, confirmation, answer, artifact, or explicitly says the work is complete,
return null even when an earlier sentence said "I will" or "let me" do that work —
but only when completion is supported by the supplied exchange or tool outcomes.
Do not treat the assistant's own narration as proof that an action happened. An `ok`
tool outcome proves only what that tool returned: `list_notes` proves an index of paths
and sizes, not that any file was read; `read_note` proves only the returned file and
line range; and quantified work such as "read them all" requires outcomes covering the
claimed scope. An error or denial proves nothing completed. A result whose status is
`started` proves dispatch, not that the resulting work finished.

A candidate with provenance `promise:asked-and-agreed` is the user's own request for
something deliverable (a picture, a comic page, a song), which the reply accepted.
It is owed work unless a tool outcome in this exchange already started or delivered
that very thing; a started earlier page does not cover a later one.

`success` must be something the character can observe herself: a tool result, a
message or picture she delivered, a file on her desk. Never the user's reply,
confirmation or reaction — she cannot see it from her own work, and a goal that waits
for it never ends. "The page reached him" is observable (she sent it); "he confirms
he got it" is not.

Return exactly one JSON object and no prose:
{"goal": null}
or
{"goal":{"text":"imperative objective","kind":"task|reach_out",\
"rationale":"why it remains owed","success":"observable completion",\
"candidates":[0]}}
"""

PROMISE_REVIEW_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "promise_review",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["goal"],
            "properties": {
                "goal": {
                    "anyOf": [
                        {"type": "null"},
                        {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["text", "kind", "rationale", "success",
                                         "candidates"],
                            "properties": {
                                "text": {"type": "string"},
                                "kind": {"type": "string",
                                         "enum": ["task", "reach_out"]},
                                "rationale": {"type": "string"},
                                "success": {"type": "string"},
                                "candidates": {
                                    "type": "array",
                                    "items": {"type": "integer"},
                                    "minItems": 1,
                                    "uniqueItems": True,
                                },
                            },
                        },
                    ],
                },
            },
        },
    },
}


def discover_promise_candidates(reply: str, user_msg: str) -> list[PromiseCandidate]:
    """Find possible commitments in source order; make no semantic decision."""
    reply = reply or ""
    quoted = [(match.start(), match.end()) for match in _QUOTED.finditer(reply)]

    def is_quoted(start: int) -> bool:
        return any(left <= start < right for left, right in quoted)

    raw: list[tuple[int, str, str, str, str]] = []
    seen: set[tuple[str, str]] = set()

    def take(start: int, text: str, provenance: str, source: str,
             confidence: str) -> None:
        if _TRAILING_OFFER.search(text):
            return
        text = _clean(text)
        key = (text.lower(), provenance)
        if (not text or _NEGATED.match(text) or _NEGATED_DE.match(text)
                or key in seen):
            return
        seen.add(key)
        raw.append((start, text, provenance, source.strip(), confidence))

    # Explicit reminders are already decisions by the user. They remain first
    # for compatibility with the old three-item cap and bypass model review.
    for remind in (REMIND_RE, REMIND_RE_DE):
        for match in remind.finditer(user_msg or ""):
            take(match.start(), match.group(1), "user:remind-me", match.group(0),
                 "explicit")

    patterns = (
        (PROMISE_RES[0], "strong"),
        (PROMISE_RES[1], "soft"),
        (PROMISE_RES[2], "soft"),
        (PROMISE_RES[3], "soft"),
        (PROMISE_RES[4], "strong"),
        *PROMISE_RES_DE,
    )
    found: list[tuple[int, str, str, str, str]] = []
    for pattern, confidence in patterns:
        for match in pattern.finditer(reply):
            if is_quoted(match.start()):
                continue
            before = reply[:match.start()].rsplit("\n", 1)[-1]
            if _CONDITIONAL.search(before):
                continue
            if before.rstrip()[-1:] in _QUOTE_OPEN:     # „ich meld mich" (§22.1g)
                continue
            found.append((match.start(), match.group(1),
                          "promise:her-own-words", match.group(0), confidence))
    for item in sorted(found, key=lambda value: value[0]):
        take(*item)

    # User reminders precede assistant candidates; each group retains source order.
    explicit = [item for item in raw if item[2].startswith("user:")]
    assistant = sorted((item for item in raw if item[2].startswith("promise:")),
                       key=lambda value: value[0])
    ordered = [*explicit, *assistant][:6]
    return [PromiseCandidate(index=i, start=item[0], text=item[1],
                             provenance=item[2], source=item[3],
                             confidence=item[4])
            for i, item in enumerate(ordered)]


#: Something one can be handed: a picture, a page, a song. Asked for and agreed
#: to, it is owed even when her reply never says "I will" — "Ja. Als Comic.
#: Genau so zeichne ich's." promised a page and no pattern above saw it.
_DELIVERABLE = (r"comics?|bild(?:er|chen)?|fotos?|selfies?|seiten?|zeichnung(?:en)?"
                r"|geschichten?|storys?|stor(?:y|ies)|lied(?:er)?|songs?|musik"
                r"|gedichte?|videos?|pictures?|photos?|drawings?|pages?|poems?"
                r"|music|panels?|skizzen?|sketch(?:es)?")
_ASK = (r"ich\s+(?:will|möchte|moechte|hätte\s+gern|haette\s+gern|wünsch\w*|wuensch\w*)"
        r"|(?:kannst|könntest|koenntest|würdest|wuerdest|magst)\s+du"
        r"|mach|zeichne|mal(?:e)?\s+mir|schick|schreib|spiel|sing|zeig|gib\s+mir"
        r"|i\s+(?:want|would\s+like|'d\s+like)|(?:can|could|would|will)\s+you"
        r"|draw|make|send|write|play|sing|show\s+me|give\s+me")
_ASKED_RE = re.compile(
    rf"[^.!?\n]*\b(?:{_ASK})\b[^.!?\n]*\b(?:{_DELIVERABLE})\b[^.!?\n]*"
    rf"|[^.!?\n]*\b(?:{_DELIVERABLE})\b[^.!?\n]*\b(?:{_ASK})\b[^.!?\n]*", re.I)
#: Her reply turning it down. Only the opening counts: a "nein" deep in a
#: story is not her answer to the request.
_DECLINED_RE = re.compile(
    r"^\W*(?:\[[^\]]*\]\s*)?(?:nein|nee|lieber\s+nicht|das\s+geht\s+nicht"
    r"|kann\s+ich\s+nicht|mach\s+ich\s+nicht|no\b|nope|i\s+can'?t|i\s+won'?t)", re.I)


def asked_and_agreed(user_msg: str, reply: str, *,
                     index: int = 0) -> PromiseCandidate | None:
    """The user asked for something deliverable and her reply did not refuse.

    A candidate, not a decision: it goes to the promise review like any soft
    candidate, and the review — which also sees what her tools did in that
    turn — decides whether it is still owed. It names his words, so a goal
    filed from it knows what he actually asked for."""
    match = _ASKED_RE.search(user_msg or "")
    if not match or not (reply or "").strip() or _DECLINED_RE.search(reply):
        return None
    text = _clean(match.group(0))
    if not text:
        return None
    return PromiseCandidate(index=index, text=trim(text, 200),
                            provenance="promise:asked-and-agreed",
                            source=trim(text, 300), start=len(reply or ""),
                            confidence="soft")


def picture_offered(goal: "Goal") -> bool:
    """Whether the picture this goal holds *now* was already handed on (§18.2a).

    `meta.offered` used to name only the heir, so it meant "a picture went" —
    and a goal that drew again kept a new page that nothing would ever send.
    Live, 15 Oct: seven redrawn comic pages on one goal, one delivered, the
    rest "lying on her desk", and her telling the user so every morning. The
    mark now names the picture as well. A bare heir id was written before
    that and covered whatever the goal held then; it is read as it was meant."""
    offered = goal.meta.get("offered")
    if not offered:
        return False
    if isinstance(offered, dict):
        return offered.get("picture") == str(goal.product.get("image_url") or "")
    return True


def offered_mark(goal: "Goal", heir_id: str) -> dict:
    """The `meta.offered` value for handing this goal's current picture to `heir_id`."""
    return {"heir": heir_id, "picture": str(goal.product.get("image_url") or "")}


def fallback_promises(candidates: list[PromiseCandidate]) -> list[PromiseCandidate]:
    """High-precision behavior when semantic review is unavailable."""
    return [candidate for candidate in candidates
            if candidate.confidence in ("explicit", "strong")][:1]


_TIMER_PROMISE_RE = re.compile(
    r"\b(?:timer|countdown|clock|remind|seconds?|minutes?|hours?|"
    r"goes? off|fires?|rings?|when (?:it(?:'s| is)?|the) time)\b", re.I)


def timer_for_promise(tool_outcomes: list[dict] | None,
                      *evidence: str) -> dict | None:
    """Return the successful timer contract that owns this promise, if any.

    The timer board and the goal store are deliberately separate, but a turn can
    create both: "say hi in five minutes" schedules the countdown and REFLECT
    files the future delivery as a promise. The timer id is their stable join.
    """
    text = " ".join(str(item or "") for item in evidence)
    for outcome in tool_outcomes or []:
        if outcome.get("tool") != "set_timer" or outcome.get("verdict") != "ok":
            continue
        try:
            result = json.loads(str(outcome.get("result") or ""))
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(result, dict) or not result.get("id"):
            continue
        label = str(result.get("label") or outcome.get("args", {}).get("label") or "")
        if (_TIMER_PROMISE_RE.search(text)
                or (label.strip() and label.strip().lower() in text.lower())):
            return result
    return None


def promise_review_messages(*, user_text: str, reply: str,
                             candidates: list[dict], capabilities: list[str],
                             tool_outcomes: list[dict] | None = None) -> list[dict]:
    payload = {
        "user_request": user_text,
        "assistant_reply": reply,
        "candidates": candidates,
        "available_capabilities": capabilities,
        "tool_outcomes": tool_outcomes or [],
    }
    return [{"role": "system", "content": PROMISE_REVIEW_SYSTEM},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]


def _json_payload(raw: str) -> dict:
    """The review's JSON — strictly, but saying which way it failed.

    The parser stays strict about prose: a model that wraps the object in
    commentary has not followed the contract, and this decides whether a goal
    enters her store, so a near-miss is refused rather than dug out of the
    sentence around it (`test_promise_review_parser_is_strict`).

    What it does *not* conflate any more is an empty answer with a malformed
    one. Empty is not the model failing the contract — it is the route saying
    nothing at all, most often because it would not serve the JSON Schema
    (§2.4, handled a layer down) — and reported as "was not JSON" it sent every
    reader looking at the parser for a bug that was never here (SPEC §22.1a).
    """
    text = re.sub(r"^\s*<think>.*?</think>\s*", "", raw or "", flags=re.S)
    if not text.strip():
        raise PromiseReviewError("promise review came back empty")
    fence = re.fullmatch(r"\s*```(?:json)?\s*(.*?)\s*```\s*", text, flags=re.S | re.I)
    if fence:
        text = fence.group(1)
    try:
        value = json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise PromiseReviewError("promise review was not JSON") from exc
    if not isinstance(value, dict) or set(value) != {"goal"}:
        raise PromiseReviewError("promise review must contain only goal")
    return value


def parse_promise_review(raw: str, *, candidate_count: int) -> PromiseDecision | None:
    """Parse the strict utility contract; `None` is a valid no-goal decision."""
    value = _json_payload(raw)
    goal = value["goal"]
    if goal is None:
        return None
    required = {"text", "kind", "rationale", "success", "candidates"}
    if not isinstance(goal, dict) or set(goal) != required:
        raise PromiseReviewError("promise goal fields do not match the contract")
    text = " ".join(str(goal["text"]).split()).strip()
    rationale = " ".join(str(goal["rationale"]).split()).strip()
    success = " ".join(str(goal["success"]).split()).strip()
    kind = goal["kind"]
    indexes = goal["candidates"]
    if (not text or len(text) > 240 or "|" in text
            or kind not in ("task", "reach_out")
            or not rationale or len(rationale) > 400
            or not success or len(success) > 400
            or not isinstance(indexes, list) or not indexes
            or any(type(index) is not int for index in indexes)
            or len(set(indexes)) != len(indexes)
            or any(index < 0 or index >= candidate_count for index in indexes)):
        raise PromiseReviewError("invalid canonical promise goal")
    return PromiseDecision(text=text, kind=kind, rationale=rationale,
                           success=success, candidates=tuple(indexes))


def promise_decision_grounded(decision: PromiseDecision, candidates: list[dict],
                              user_text: str) -> bool:
    """A canonical objective must retain subject matter from its source exchange."""
    source = " ".join([
        user_text,
        *(str(candidates[index].get("text", "")) for index in decision.candidates),
        *(str(candidates[index].get("source", "")) for index in decision.candidates),
    ])
    return bool(_echo_words(decision.text) & _echo_words(source))


#: Telling verbs: the promise whose whole substance is that *they* hear it.
#:
#: Anchored at the front of the captured predicate on purpose. "I'll read it and
#: let you know" is work with a report on the end, and the work is the half she
#: has to do first — so it files as a `task`, and finishing it is what schedules
#: the telling (`_offer_to_tell`). "I'll let you know once I've read it" leads
#: with the report and files as a `reach_out`. The leading verb is the promise;
#: everything after it is when.
_TELLING_RE = re.compile(
    # optional throat-clearing before the verb — "probably tell you", "try to
    # let you know", "go and ask you". Deliberately a closed list: a wildcard
    # here would eat the verb it is supposed to be standing in front of.
    r"^(?:(?:probably|definitely|certainly|then|also|just|first|soon|quickly|"
    r"actually|maybe)\s+|(?:try|make sure|be sure|remember)\s+to\s+|"
    r"(?:go|come)\s+(?:and\s+)?)*"
    r"(?:"
    r"(?:tell|remind|update|warn|notify|ping|message|text|email|call|show|"
    r"send|ask|nudge|alert|answer)\s+you\b"
    r"|let\s+you\s+know"
    r"|keep\s+you\s+(?:posted|updated|informed|in\s+the\s+loop)"
    r"|fill\s+you\s+in"
    r"|(?:get|come|circle)\s+back\s+to\s+you"
    r"|(?:report|check|circle)\s+back\b"
    r"|follow\s+up\s+with\s+you"
    r"|run\s+(?:it|this|that|them)\s+(?:by|past)\s+you"
    r"|share\s+(?:it|this|that|them)\s+with\s+you"
    r")", re.I)


#: The German telling verbs, in both places German puts a verb. Verb-first is
#: what the present-tense openers capture ("meld mich, wenn ich fertig bin");
#: verb-last is what the future leaves behind once "ich werde" is taken off
#: ("dir Bescheid sagen"). The verb-last form only counts when the telling is the
#: whole clause — "das nachlesen und dir Bescheid sagen" leads with the reading,
#: and the rule above holds in both languages: the work she names first is the
#: promise, and the telling after it is when.
_TELLING_DE_RE = re.compile(
    r"^(?:(?:dann|gleich|kurz|auch|sofort|nachher|sp[äa]ter|erst)\s+)*"
    r"(?:meld\w*\s+(?:ich\s+)?mich|sag\w*\s+(?:ich\s+)?dir"
    r"|geb\w*\s+(?:ich\s+)?dir\s+bescheid"
    r"|(?:erz[äa]hl|zeig|schreib)\w*(?:'s)?\s+(?:ich\s+)?(?:(?:es|das)\s+)?dir"
    r"|komm\w*\s+(?:\w+\s+){0,3}zur[üu]ck)"
    r"|^(?!.*\bund\b)(?:\S+\s+){0,4}?(?:bescheid\s+(?:sagen|geben)|mich\s+melden"
    r"|(?:(?:es|das)\s+)?dir\s+(?:sagen|zeigen|erz[äa]hlen|schreiben))\s*$", re.I)


def promise_kind(text: str, provenance: str) -> str:
    """`task` or `reach_out` — which half of a promise this one is (§22.1).

    Only her own words are ever a `task`. An explicit "remind me to…" is a
    `reach_out` by definition however it is phrased: the entire thing they asked
    for is to be told, and doing it quietly would be doing the opposite.

    Anything else defaults to `reach_out`, which is the behaviour every goal had
    before this existed — an unrecognised provenance should not silently gain a
    capability, it should keep the one it had.
    """
    if not provenance.startswith("promise:"):
        return "reach_out"
    text = _clean(text)
    telling = _TELLING_RE.match(text) or _TELLING_DE_RE.match(text)
    return "reach_out" if telling else "task"


def extract_promises(reply: str, user_msg: str) -> list[tuple[str, str]]:
    """Scan an exchange for commitments: hers, in her own words, and the
    user's explicit remind-me asks. Returns (text, provenance) pairs.

    Hers first and capped at three, because a reply that offers a list is one
    conversation, not a day's work — and a companion who files everything she
    ever said "I could" about ends up with a to-do list she can only fail."""
    return [(candidate.text, candidate.provenance)
            for candidate in discover_promise_candidates(reply, user_msg)[:3]]
