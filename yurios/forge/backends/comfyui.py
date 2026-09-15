"""Remote ComfyUI backend for YuriOS' camera.

The GPU stays on the gaming PC. YuriOS only submits a Krea-compatible graph,
waits for its result, downloads the PNG, and releases ComfyUI's models in a
``finally`` block. This deliberately keeps the Rook integration mechanics
behind YuriOS' own ImageBackend seam; no Rook runtime or database is imported.
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
import uuid
from typing import Any

from ..types import Capabilities, GenRequest, ImageResult
from .base import ImageBackend


class ComfyUIBackend(ImageBackend):
    name = "comfyui-krea2"
    #: Renders on a LAN GPU that other remote services share (the story TTS);
    #: the selfie lab queues it behind `world.vram.remote_gpu_lock()`.
    REMOTE_GPU = True

    def __init__(self, *, host: str = "192.168.0.104", port: int = 8188,
                 timeout: int = 300, release_after_render: bool = True,
                 unet_name: str = "fineporn_v4_fp8.safetensors",
                 clip_name: str = "qwen3vl_4b_fp8_scaled.safetensors",
                 vae_name: str = "qwen_image_vae.safetensors",
                 lora_name: str = "", lora_strength: float = 0.6,
                 prompt_trigger: str = "") -> None:
        self.host, self.port = host, int(port)
        self.timeout = int(timeout)
        self.release_after_render = bool(release_after_render)
        self.unet_name, self.clip_name, self.vae_name = unet_name, clip_name, vae_name
        self.lora_name, self.lora_strength = lora_name, float(lora_strength)
        self.prompt_trigger = prompt_trigger.strip()

    @property
    def base(self) -> str:
        return f"http://{self.host}:{self.port}"

    def _request(self, path: str, payload: dict[str, Any] | None = None,
                 timeout: int = 15) -> Any:
        body = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            self.base + path, data=body,
            headers={"Content-Type": "application/json"} if body else {})
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read()
        return json.loads(raw) if raw else {}

    def health(self) -> bool:
        try:
            self._request("/system_stats", timeout=4)
            return True
        except Exception:
            return False

    def capabilities(self) -> Capabilities:
        return Capabilities(
            name=self.name, supports_generate=True, supports_lora=bool(self.lora_name),
            uncensored=None, notes="Remote ComfyUI Krea2 graph; models released after render.")

    def _workflow(self, req: GenRequest, seed: int) -> dict[str, Any]:
        positive = " ".join(x for x in (self.prompt_trigger, req.prompt) if x)
        model_input: list[Any] = ["1", 0]
        graph: dict[str, Any] = {
            "1": {"class_type": "UNETLoader", "inputs": {
                "unet_name": self.unet_name, "weight_dtype": "fp8_e4m3fn"}},
            "2": {"class_type": "CLIPLoader", "inputs": {
                "clip_name": self.clip_name, "type": "krea2"}},
            "3": {"class_type": "VAELoader", "inputs": {"vae_name": self.vae_name}},
            "10": {"class_type": "TextEncodeQwenImageEditPlus", "inputs": {
                "clip": ["2", 0], "prompt": positive, "vae": ["3", 0]}},
            "11": {"class_type": "TextEncodeQwenImageEditPlus", "inputs": {
                "clip": ["2", 0], "prompt": req.negative_prompt, "vae": ["3", 0]}},
            "12": {"class_type": "ReferenceLatent", "inputs": {"conditioning": ["10", 0]}},
            "20": {"class_type": "EmptySD3LatentImage", "inputs": {
                "width": req.width, "height": req.height, "batch_size": 1}},
            "30": {"class_type": "KSampler", "inputs": {
                "model": model_input, "positive": ["12", 0], "negative": ["11", 0],
                "latent_image": ["20", 0], "seed": seed,
                "steps": req.steps or 12, "cfg": req.cfg if req.cfg is not None else 1.0,
                "sampler_name": "euler", "scheduler": "beta", "denoise": 1.0}},
            "40": {"class_type": "VAEDecode", "inputs": {"samples": ["30", 0], "vae": ["3", 0]}},
            "50": {"class_type": "SaveImage", "inputs": {
                "images": ["40", 0], "filename_prefix": "yurios/selfie"}},
        }
        if self.lora_name:
            graph["70"] = {"class_type": "LoraLoaderModelOnly", "inputs": {
                "model": ["1", 0], "lora_name": self.lora_name,
                "strength_model": self.lora_strength}}
            graph["30"]["inputs"]["model"] = ["70", 0]
        return graph

    def _release(self) -> None:
        if not self.release_after_render:
            return
        try:
            self._request("/free", {"unload_models": True, "free_memory": True}, timeout=15)
        except Exception:
            # Cleanup is best effort and must not hide a successfully downloaded image.
            pass

    def generate(self, req: GenRequest) -> ImageResult:
        if not self.health():
            raise RuntimeError("ComfyUI is unavailable")
        seed = int(req.seed if req.seed is not None else uuid.uuid4().int % (2**32))
        try:
            queued = self._request("/prompt", {"prompt": self._workflow(req, seed)}, timeout=20)
            if queued.get("node_errors") or not queued.get("prompt_id"):
                raise RuntimeError("ComfyUI rejected the Krea2 workflow")
            prompt_id = str(queued["prompt_id"])
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                time.sleep(2)
                try:
                    history = self._request("/history/" + prompt_id, timeout=15)
                except Exception:
                    continue
                entry = history.get(prompt_id)
                if not entry:
                    continue
                if entry.get("status", {}).get("status_str") == "error":
                    raise RuntimeError("ComfyUI generation failed")
                for output in entry.get("outputs", {}).values():
                    for image in output.get("images", []):
                        query = urllib.parse.urlencode({
                            "filename": image["filename"],
                            "subfolder": image.get("subfolder", ""),
                            "type": image.get("type", "output"),
                        })
                        with urllib.request.urlopen(self.base + "/view?" + query, timeout=60) as response:
                            data = response.read()
                        if data:
                            return ImageResult.new(
                                data, self.name, model=self.unet_name, seed=seed,
                                prompt=req.prompt, negative_prompt=req.negative_prompt,
                                comfy_prompt_id=prompt_id, release_after_render=self.release_after_render)
                if entry.get("status", {}).get("completed"):
                    raise RuntimeError("ComfyUI completed without an image")
            raise TimeoutError("ComfyUI render timed out")
        finally:
            self._release()
