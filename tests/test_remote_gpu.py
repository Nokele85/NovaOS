"""The gaming PC's one card, shared by the story TTS and the ComfyUI camera.

2026-09-11, 22:23-22:43: a story's TTS load and a selfie render ran at once on
a 12 GB card; the render timed out, the load timed out on this side and then
finished on the server, and held 7 GB nobody unloaded - so the next selfie
timed out as well. These pin the three halves of the fix."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from yurios.kernel.clock import VirtualClock
from yurios.world import stories, vram
from yurios.world.qwenvoice import QwenVoice
from yurios.world.selfies import SelfieLab
from yurios.world.stories import StoryLab


@pytest.fixture(autouse=True)
def _fresh_lock():
    vram.reset_remote_gpu_lock()
    yield
    vram.reset_remote_gpu_lock()


def _lab(**kw) -> StoryLab:
    return StoryLab(utility=None, post=lambda *a, **k: None, root=kw.pop("root"),
                    voice=QwenVoice(speech_url="http://tts",
                                    supervisor_url="http://tts:8092",
                                    speaker="sohee"),
                    **kw)


def test_the_card_is_given_back_even_when_the_load_timed_out(tmp_path, monkeypatch):
    calls: list[str] = []

    class Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, **kw):
            calls.append(url.rsplit("/", 1)[-1])
            if url.endswith("/an"):
                raise httpx.ReadTimeout("the card was busy")
            return SimpleNamespace(content=b"", json=lambda: {})

    monkeypatch.setattr(stories.httpx, "AsyncClient", Client)
    with pytest.raises(httpx.ReadTimeout):
        asyncio.run(_lab(root=tmp_path)._tts([("Ein Satz.", "")], tmp_path / "s.wav"))
    assert calls == ["an", "aus"]


def test_the_story_voice_waits_while_a_render_holds_the_card(tmp_path):
    order: list[str] = []

    async def scenario():
        lock = asyncio.Lock()
        lab = _lab(root=tmp_path, gpu_lock=lock)

        async def on_card(sections, target):
            order.append("tts")
            return target

        lab._tts_on_card = on_card
        await lock.acquire()                      # a render is running
        job = asyncio.create_task(lab._tts([("x", "")], tmp_path / "s.wav"))
        await asyncio.sleep(0.01)
        order.append("render done")
        lock.release()
        await job

    asyncio.run(scenario())
    assert order == ["render done", "tts"]


def test_a_comfyui_camera_queues_behind_the_same_lock():
    async def check():
        remote = SimpleNamespace(backend=SimpleNamespace(REMOTE_GPU=True))
        hosted = SimpleNamespace(backend=SimpleNamespace())
        kw = dict(clock=VirtualClock(0), post=lambda *a, **k: {},
                  speak=lambda text: asyncio.sleep(0))
        assert SelfieLab(remote, **kw)._render_lock is vram.remote_gpu_lock()
        assert SelfieLab(hosted, **kw)._render_lock is not vram.remote_gpu_lock()

    asyncio.run(check())


def test_the_comfyui_backend_declares_the_lan_gpu():
    from yurios.forge.backends.comfyui import ComfyUIBackend
    assert ComfyUIBackend.REMOTE_GPU is True
