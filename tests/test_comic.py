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
    compose_page,
    panel_sizes,
    parse_layout,
    rows,
)
from yurios.world.selfies import SelfieLab, build_forge
from yurios.world.tools.server import build_server

PANELS = [{"picture": "wide shot, a ferryman at black water", "text": "Jede Nacht wartet er."},
          {"picture": "close on his hand on the oar", "text": ""},
          {"picture": "a stranger on the jetty", "text": "„Ich will nur reden.“"}]


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
        assert panel["picture"] in req.prompt and "an old ferryman" in req.prompt
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
