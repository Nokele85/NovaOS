"""A comic page (SPEC §7.6b): the hand, the lab's job, and the page the host cuts.

Found live (2026-10-08): asked for a comic with only `show_picture`, she
rendered panel 1 and panel 2 nine times in twelve minutes and one frame arrived."""
from __future__ import annotations

import asyncio
import io
import json

import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from PIL import Image

from yurios.world.comic import (
    HALF,
    MAX_TEXT,
    PAGE_W,
    TALL,
    WIDE,
    arrangements,
    cast_of,
    check_question,
    compose_page,
    figures_in,
    panel_prompt,
    passes,
    with_her,
    panel_sizes,
    parse_layout,
    rows,
)
from yurios.world.selfies import SelfieLab, build_forge
from yurios.world.tools.server import build_server

PANELS = [{"picture": "wide shot, a ferryman at black water", "who": [],
           "text": "Jede Nacht wartet er."},
          {"picture": "close on his hand on the oar", "who": [], "text": ""},
          {"picture": "a stranger on the jetty", "who": [], "text": "„Ich will nur reden.“"}]


def _png(size, colour=(40, 60, 80)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, colour).save(out, "PNG")
    return out.getvalue()


def test_a_page_is_cut_her_way_or_a_different_way_each_time():
    """SPEC §7.6b: rows of one wide, two square or three tall panels — hers
    when she says, otherwise picked afresh, so two pages are not cut alike."""
    assert parse_layout("1-2-2", 5) == [1, 2, 2] and rows(4, "3 1") == [3, 1]
    assert panel_sizes([1, 3, 2]) == [WIDE, TALL, TALL, TALL, HALF, HALF]
    with pytest.raises(ValueError, match="holds 4 panels"):
        parse_layout("2-2", 5)
    with pytest.raises(ValueError, match="rows of 1, 2 or 3"):
        parse_layout("4-1", 5)
    for n in range(2, 7):
        for page in arrangements(n):
            assert sum(page) == n and page.count(1) <= 2
        assert len({tuple(rows(n, seed=k)) for k in range(600)}) == len(arrangements(n))
    assert len(arrangements(4)) >= 4
    assert [1, 1, 1] not in arrangements(3)          # a column is not a page
    # every arrangement is a page the host can actually cut
    for page in arrangements(5):
        shots = [(_png(size), "x") for size in panel_sizes(page)]
        assert Image.open(io.BytesIO(compose_page(shots, page=page))).width == PAGE_W


def test_the_page_holds_every_panel_and_grows_with_her_words():
    """SPEC §7.6b: one page, her words in type under the panels."""
    silent = compose_page([(_png(HALF), ""), (_png(HALF), "")])
    spoken = compose_page([(_png(HALF), "Er hört zu."), (_png(HALF), "")],
                          title="Der Fährmann")
    long = compose_page([(_png(HALF), "wort " * 40), (_png(HALF), "")],
                        title="Der Fährmann")
    a, b, c = (Image.open(io.BytesIO(x)) for x in (silent, spoken, long))
    assert a.width == b.width == c.width == PAGE_W
    assert a.height < b.height < c.height        # a band for the caption, more for more
    # the panel is on the page and not paper: its colour is found inside the frame
    assert b.getpixel((PAGE_W // 4, b.height // 2)) == (40, 60, 80)
    with pytest.raises(ValueError):
        compose_page([])


def result_text(r) -> str:
    return r.content[0].text


async def test_draw_comic_takes_the_page_as_she_wrote_it():
    """SPEC §7.6b: the contract carries the page; what cannot be a page is
    refused with what to change."""
    srv = build_server(max_minutes=180)
    async with create_connected_server_and_client_session(srv._mcp_server) as s:
        r = await s.call_tool("draw_comic", {
            "title": "Der Fährmann", "cast": "an old ferryman, long coat",
            "panels": PANELS})
        assert not r.isError
        data = json.loads(result_text(r))
        assert data["kind"] == "comic" and data["status"] == "started" and data["id"]
        assert [p["picture"] for p in data["panels"]] == [p["picture"] for p in PANELS]
        assert data["panels"][1]["text"] == ""

        one = await s.call_tool("draw_comic", {"panels": PANELS[:1]})
        assert one.isError and "2 to 6" in result_text(one)
        unsaid = await s.call_tool("draw_comic", {"cast": {"Ferryman": "old"}, "panels": [
            {"picture": "he waits", "who": ["Ferryman"]}, {"picture": "he turns"}]})
        assert unsaid.isError and 'panel 2 needs "who"' in result_text(unsaid)
        assert "Ferryman, me" in result_text(unsaid)
        tall = await s.call_tool("draw_comic", {"layout": "1-1-1", "panels": PANELS})
        assert tall.isError and "two at most" in result_text(tall)
        blind = await s.call_tool("draw_comic", {
            "panels": [{"text": "words only"}, PANELS[0]]})
        assert blind.isError and "panel 1" in result_text(blind)
        cut = await s.call_tool("draw_comic", {"panels": PANELS, "layout": "2-2"})
        assert cut.isError and "holds 4 panels" in result_text(cut)
        wordy = await s.call_tool("draw_comic", {
            "panels": [PANELS[0], {"picture": "x", "text": "w" * (MAX_TEXT + 1)}]})
        assert wordy.isError and str(MAX_TEXT) in result_text(wordy)


class Recorder:
    def __init__(self):
        self.posts, self.cues = [], []

    def post(self, role, text, **kw):
        self.posts.append({"role": role, "text": text, **kw})

    async def speak(self, cue):
        self.cues.append(cue)
        return True


async def test_the_lab_draws_every_panel_and_sends_one_page(cfg, clock):
    """SPEC §7.6b: every panel through the camera without her likeness, one
    seed, one style and one cast line across them — and ONE message."""
    forge, _ = build_forge(cfg)
    backend = forge.backend
    seen = []
    generate = backend.generate

    def spy(req):
        seen.append(req)
        return generate(req)

    backend.generate = spy
    backend.release_after_render = True
    released = []
    backend._release = lambda: released.append(backend.release_after_render)
    rec = Recorder()
    lab = SelfieLab(forge, clock=clock, post=rec.post, speak=rec.speak)
    lab.start({"id": "c1", "kind": "comic", "title": "Der Fährmann",
               "cast": "an old ferryman, long coat", "style": "", "avoid": "colour",
               "layout": "1-2", "panels": PANELS, "status": "started", "_channel": "telegram"})
    await asyncio.gather(*lab._tasks, return_exceptions=True)

    assert len(seen) == 3
    assert {r.seed for r in seen} == {seen[0].seed} and seen[0].seed is not None
    assert [(r.width, r.height) for r in seen] == [WIDE, HALF, HALF]
    for req, panel in zip(seen, PANELS):
        assert panel["picture"] in req.prompt
        assert "graphic novel illustration" in req.prompt
        assert forge.character.identity not in req.prompt       # she is not in it
        assert "speech bubbles" in req.negative_prompt and "colour" in req.negative_prompt
    # the models were kept for the page and handed back once, at its end
    assert released == [True]

    (post,) = rec.posts
    assert post["channel"] == "telegram"
    png = cfg.selfie_dir / post["image_url"].removeprefix("/selfies/")
    assert Image.open(png).width == PAGE_W
    meta = json.loads(png.with_suffix(".json").read_text())
    assert meta["comic"]["layout"] == "1-2"
    assert meta["kind"] == "comic" and meta["template"]["look"] == "Der Fährmann"
    assert [p["text"] for p in meta["comic"]["panels"]] == [p["text"] for p in PANELS]



CAST = {"Ferryman": "about fifty, gaunt, long dark coat", "Stranger": "young, pale, light coat"}


def test_a_panel_holds_only_the_figures_that_are_in_it():
    """SPEC §7.6b: named figures, `who` per panel, the head-count in words —
    and "not known" is never read as "nobody"."""
    named, plain = cast_of(CAST)
    assert plain == "" and list(named) == ["Ferryman", "Stranger"]
    assert cast_of("an old man") == ({}, "an old man")

    alone = {"picture": "he waits at the black water", "who": ["ferryman"]}
    empty = {"picture": "the empty boat drifts", "who": []}
    named_in_words = {"picture": "the Stranger steps out of the fog"}
    unknown = {"picture": "he turns his head"}
    assert figures_in(alone, named) == ["Ferryman"]
    assert figures_in(empty, named) == []
    assert figures_in(named_in_words, named) == ["Stranger"]
    assert figures_in(unknown, named) is None

    prompt, leave_out = panel_prompt("ink", alone, named, "")
    assert "Ferryman: about fifty" in prompt and "Stranger" not in prompt
    assert "Exactly one person" in prompt and "second person" in leave_out
    prompt, leave_out = panel_prompt("ink", empty, named, "")
    assert "No people" in prompt and "gaunt" not in prompt and "person" in leave_out
    prompt, leave_out = panel_prompt("ink", unknown, named, "")
    assert "No people" not in prompt and leave_out == ""
    # the older, one-line cast still goes into every panel
    assert "an old man" in panel_prompt("ink", unknown, {}, "an old man")[0]

    asked = check_question("black ink", empty, named)
    assert "Nobody should be visible" in asked and "the empty boat drifts" in asked
    assert passes("OK") and passes("") and passes("looks fine to me")
    assert not passes("WRONG: someone stands in the boat") and not passes("`wrong: colour")


async def test_who_must_be_in_the_cast():
    """SPEC §7.6b: a `who` naming someone outside `cast` is refused with the
    names she does have."""
    srv = build_server(max_minutes=180)
    async with create_connected_server_and_client_session(srv._mcp_server) as s:
        ok = await s.call_tool("draw_comic", {"cast": CAST, "panels": [
            {"picture": "he waits", "who": ["ferryman"], "text": "Er wartet."},
            {"picture": "the empty boat", "who": []}]})
        assert not ok.isError
        data = json.loads(result_text(ok))
        assert data["cast"] == CAST
        assert [p.get("who") for p in data["panels"]] == [["Ferryman"], []]
        assert "not finished" in data["note"]
        bad = await s.call_tool("draw_comic", {"cast": CAST, "panels": [
            {"picture": "x", "who": ["Benni"]}, {"picture": "y", "who": []}]})
        assert bad.isError and "Ferryman, Stranger" in result_text(bad)


async def test_a_panel_that_contradicts_her_is_drawn_again(cfg, clock):
    """SPEC §7.6b: each panel is shown to a reader with eyes; a refusal costs a
    redraw with a new seed, bounded, and what is still refused is named."""
    forge, _ = build_forge(cfg)
    seen = []
    generate = forge.backend.generate

    def spy(req):
        seen.append(req)
        return generate(req)

    forge.backend.generate = spy
    asked = []

    async def judge(data, question):
        asked.append(question)
        if "empty boat" in question:
            return "WRONG: someone stands in the boat"          # never comes right
        return "WRONG: two people" if len(asked) == 1 else "OK"  # right on the 2nd try

    rec = Recorder()
    lab = SelfieLab(forge, clock=clock, post=rec.post, speak=rec.speak, judge=judge)
    lab.start({"id": "c2", "kind": "comic", "title": "Der Fährmann", "cast": CAST,
               "style": "black and white ink", "layout": "2", "status": "started",
               "panels": [{"picture": "he waits", "who": ["Ferryman"], "text": "Er wartet."},
                          {"picture": "the empty boat", "who": [], "text": "Leer."}]})
    await asyncio.gather(*lab._tasks, return_exceptions=True)

    # panel 1: two draws (refused once); panel 2: three (refused to the limit)
    assert len(seen) == 5
    assert len({r.seed for r in seen[:2]}) == 2
    for req in seen:
        assert req.prompt.startswith("black and white ink")     # her look, said first
        assert forge.character.quality_preamble.strip()[:20] not in req.prompt
    assert "Ferryman: about fifty" in seen[0].prompt and "Stranger" not in seen[0].prompt
    assert "No people" in seen[-1].prompt

    (post,) = rec.posts
    meta = json.loads((cfg.selfie_dir / post["image_url"].removeprefix("/selfies/"))
                      .with_suffix(".json").read_text())
    assert meta["comic"]["drawn"] == [2, 3]
    assert meta["comic"]["doubts"] == ["panel 2: WRONG: someone stands in the boat"]
    assert "not everything came out" in meta["template"]["look"]
    assert "panel 2" in rec.cues[0]                # …and she is told, not left to boast


def test_me_is_her_and_needs_no_description():
    """SPEC §7.6b: `me` resolves to her locked identity plus what she wears —
    by `who`, never by the two letters turning up inside "home"."""
    named, _ = cast_of({"me": "in a yellow raincoat", "Stranger": "young, pale"})
    panels = [{"picture": "at home, time passes", "who": ["me"]},
              {"picture": "the Stranger alone", "who": ["Stranger"]},
              {"picture": "nobody at home"}]
    named, panels = with_her(named, panels, "Nova", "a silver-haired woman, grey eyes")
    assert named["Nova"] == "a silver-haired woman, grey eyes. in a yellow raincoat"
    assert "me" not in named and panels[0]["who"] == ["Nova"]
    assert "silver-haired" in panel_prompt("ink", panels[0], named, "")[0]
    assert "silver-haired" not in panel_prompt("ink", panels[1], named, "")[0]
    assert figures_in({"picture": "at home, some time later"}, {"me": "x"}) is None
    # a page she is not in is left exactly as it was
    assert with_her({"A": "x"}, [{"picture": "p"}], "Nova", "id") == ({"A": "x"}, [{"picture": "p"}])


async def test_she_can_put_herself_in_a_panel(cfg, clock):
    """SPEC §7.6b: `who: ["me"]` with no cast at all is enough."""
    srv = build_server(max_minutes=180)
    async with create_connected_server_and_client_session(srv._mcp_server) as s:
        r = await s.call_tool("draw_comic", {"panels": [
            {"picture": "she sits at the window", "who": ["me"]},
            {"picture": "the empty street", "who": []}]})
        assert not r.isError
        contract = json.loads(result_text(r))
    assert contract["cast"] == {"me": ""}

    forge, _ = build_forge(cfg)
    seen = []
    generate = forge.backend.generate

    def spy(req):
        seen.append(req)
        return generate(req)

    forge.backend.generate = spy
    rec = Recorder()
    lab = SelfieLab(forge, clock=clock, post=rec.post, speak=rec.speak)
    lab.start(contract)
    await asyncio.gather(*lab._tasks, return_exceptions=True)
    assert forge.character.identity in seen[0].prompt       # she is in the first…
    assert forge.character.identity not in seen[1].prompt   # …and not in the empty street
    assert "No people" in seen[1].prompt


def test_a_cast_written_as_a_sentence_is_read_as_its_figures():
    """SPEC §7.6b: `Name: look. Name: look.` is the object, written out."""
    named, plain = cast_of("Der Fährmann: um die fünfzig, hager, langer Mantel. "
                           "me: schlank, langes dunkles Haar.")
    assert plain == ""
    assert named == {"Fährmann": "um die fünfzig, hager, langer Mantel",
                     "me": "schlank, langes dunkles Haar"}
    assert cast_of("an old man in a long coat") == ({}, "an old man in a long coat")


async def test_the_camera_is_given_english(cfg, clock):
    """SPEC §7.6b: style, looks and pictures go to the renderer in English;
    an answer of the wrong shape leaves her words as they are."""
    from yurios.world.comic import english_answer
    assert english_answer("no json here", ["a", "b"]) == ["a", "b"]
    assert english_answer('["x"]', ["a", "b"]) == ["a", "b"]
    assert english_answer('sure: ["x", "y"]', ["a", "b"]) == ["x", "y"]

    forge, _ = build_forge(cfg)
    seen = []
    generate = forge.backend.generate

    def spy(req):
        seen.append(req)
        return generate(req)

    forge.backend.generate = spy

    async def english(request):
        lines = json.loads(request[request.index("["):])
        return json.dumps([f"EN({x})" if x else "" for x in lines])

    rec = Recorder()
    lab = SelfieLab(forge, clock=clock, post=rec.post, speak=rec.speak, english=english)
    lab.start({"id": "c3", "kind": "comic", "cast": {"Fährmann": "hager"},
               "style": "schwarzweiß Tusche", "layout": "2", "status": "started",
               "panels": [{"picture": "er wartet", "who": ["Fährmann"], "text": "Er wartet."},
                          {"picture": "das leere Boot", "who": [], "text": "Leer."}]})
    await asyncio.gather(*lab._tasks, return_exceptions=True)
    assert seen[0].prompt.startswith("EN(schwarzweiß Tusche)")
    assert "EN(er wartet)" in seen[0].prompt and "Fährmann: EN(hager)" in seen[0].prompt
    assert "EN(das leere Boot)" in seen[1].prompt
