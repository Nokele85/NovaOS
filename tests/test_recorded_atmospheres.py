"""Recorded scene contracts without a board, model or network. SPEC §26.4."""
import importlib.util
import json
from pathlib import Path

import pytest

from tools import elecrow_sound_library as library

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def sounds(tmp_path):
    entries = [
        {"file": "stadt__gespraeche__fern__hintergrund__001.pcm", "mode": "background",
         "description": "distant crowd conversations", "tags": ["stadt", "fern"], "bytes": 100},
        {"file": "stadt__gespraeche__fern__hintergrund__002.pcm", "mode": "background",
         "description": "distant crowd conversations", "tags": ["stadt", "fern"], "bytes": 100},
        {"file": "dorf__hund__ereignis__001.pcm", "mode": "event",
         "description": "dog barking", "tags": ["dorf"], "bytes": 50},
    ]
    (tmp_path / "installed.json").write_text(json.dumps({"sounds": entries}))
    return tmp_path, entries


def test_catalogue_search_returns_exact_descriptive_names(sounds):
    root, entries = sounds
    result = library.catalogue(root, "gespraeche fern", 1)
    assert result["matches"] == 2 and len(result["sounds"]) == 1
    assert result["sounds"][0]["file"] == entries[0]["file"]
    assert library.catalogue(root, "unknown")["matches"] == 0


def test_uninstalled_prepared_catalogue_is_not_offered(tmp_path):
    (tmp_path / "catalog.json").write_text('{"sounds": []}')
    assert library.catalogue(tmp_path)["status"] == "no_library"


def test_scene_variants_memory_and_generated_call(sounds):
    root, entries = sounds
    layers = [{"files": [e["file"] for e in entries[:2]], "gain": 35}]
    facts, code = library.scene_call(root, layers, 40, 10, "Stadt")
    assert facts["sample_bytes"] == 200 and facts["source"] == "recordings"
    assert "music.soundscape" in code and "'mode': 'background'" in code
    compile(code, "<scene>", "exec")
    assert library.scene_call(root, [], 40, 10, "")[0]["stopped"]


@pytest.mark.parametrize("layer", [
    {"files": ["../secret.pcm"]}, {"files": ["invented.pcm"]},
    {"files": []}, {"files": "not-a-list"},
    {"files": ["dorf__hund__ereignis__001.pcm"], "enabled": True},
    {"files": ["dorf__hund__ereignis__001.pcm"], "gap_min": 10, "gap_max": 2},
    {"files": ["dorf__hund__ereignis__001.pcm"], "gain": -1},
])
def test_refuses_bad_names_arguments_and_timing_before_sending(sounds, layer):
    facts, code = library.scene_call(sounds[0], [layer], 35, 10, "")
    assert not facts["ok"] and not code


def test_refuses_mixed_modes_and_excess_memory(sounds):
    root, entries = sounds
    assert not library.scene_call(root, [{"files": [e["file"] for e in entries]}], 35, 10, "")[1]
    entries[0]["bytes"] = library.MAX_BYTES + 1
    (root / "installed.json").write_text(json.dumps({"sounds": entries}))
    assert not library.scene_call(root, [{"files": [entries[0]["file"]]}], 35, 10, "")[1]


def runtime():
    path = ROOT / "elecrow-firmware/nova-runtime/lib/nova/soundscape.py"
    spec = importlib.util.spec_from_file_location("board_soundscape", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_background_crossfades_and_never_stops():
    import array
    module = runtime()
    samples = {"a": array.array("h", [1000] * 16000).tobytes(),
               "b": array.array("h", [2000] * 16000).tobytes()}
    layer = module.Layer({"files": ["a", "b"], "mode": "background", "gain": 50}, samples)
    calls = []
    def mix(out, pcm, pos, count, gain):
        assert pos + count <= len(pcm) // 2
        assert count > 0 and 0 <= gain <= 256
        calls.append((pos, count))
    for _ in range(40):
        layer.render(bytearray(4000), 2000, mix)
    assert len(calls) > 40                  # overlap mixes two recordings
    assert layer.wait == 0


def test_event_waits_between_repetitions():
    module = runtime()
    samples = {"bark": b"\x00\x01" * 1600}
    layer = module.Layer({"files": ["bark"], "mode": "event", "gain": 40,
                          "gap_min": 1, "gap_max": 2}, samples)
    layer.wait = 0
    calls = []
    for _ in range(24):
        layer.render(bytearray(4000), 2000, lambda *args: calls.append(1))
    assert 1 < len(calls) < 12             # no continuous repeated bark
