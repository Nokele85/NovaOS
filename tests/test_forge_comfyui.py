from yurios.forge.backends.comfyui import ComfyUIBackend
from yurios.forge.types import GenRequest


def test_krea2_workflow_is_remote_and_optional_lora_is_wired():
    backend = ComfyUIBackend(lora_name="identity.safetensors", prompt_trigger="nova")
    graph = backend._workflow(GenRequest("a portrait", negative_prompt="bad"), 7)
    assert graph["2"]["inputs"]["type"] == "krea2"
    assert graph["70"]["inputs"]["lora_name"] == "identity.safetensors"
    assert graph["30"]["inputs"]["seed"] == 7
    assert graph["10"]["inputs"]["prompt"].startswith("nova a portrait")


def test_remote_backend_releases_comfy_models_after_success(monkeypatch):
    backend = ComfyUIBackend(timeout=10)
    calls = []

    def fake_request(path, payload=None, timeout=15):
        calls.append((path, payload))
        if path == "/system_stats":
            return {}
        if path == "/prompt":
            return {"prompt_id": "p1"}
        if path == "/history/p1":
            return {"p1": {"status": {"completed": True}, "outputs": {
                "50": {"images": [{"filename": "x.png", "subfolder": "", "type": "output"}]}}}}
        if path == "/free":
            return {}
        raise AssertionError(path)

    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def read(self):
            return b"PNG"

    monkeypatch.setattr(backend, "_request", fake_request)
    monkeypatch.setattr("yurios.forge.backends.comfyui.time.sleep", lambda _: None)
    monkeypatch.setattr("yurios.forge.backends.comfyui.urllib.request.urlopen", lambda *a, **k: Response())
    result = backend.generate(GenRequest("a portrait"))
    assert result.data == b"PNG"
    assert calls[-1][0] == "/free"
    assert calls[-1][1] == {"unload_models": True, "free_memory": True}
