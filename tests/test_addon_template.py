"""The add-on template is itself an add-on: it has to work, or every add-on
copied from it starts out broken. No model, no network, no Nova."""
from __future__ import annotations

import ast
import importlib.util
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")

ROOT = Path(__file__).resolve().parents[1]
FOLDER = ROOT / "tools" / "addon_template"


@pytest.fixture
def addon(monkeypatch, tmp_path):
    monkeypatch.setenv("ADDON_DATA", str(tmp_path / "data"))
    spec = importlib.util.spec_from_file_location("addon_template_server", FOLDER / "server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    server = module.build_server()

    def call(name, **args):
        return server._tool_manager.get_tool(name).fn(**args)

    return module, server, call


def test_it_offers_its_tools_under_its_own_name(addon):
    module, server, _ = addon
    names = sorted(t.name for t in server._tool_manager.list_tools())
    assert names == ["notebook_add", "notebook_list", "notebook_status"]
    assert all(name.startswith(module.NAME + "_") for name in names)


def test_every_tool_says_what_it_does(addon):
    _, server, _ = addon
    for tool in server._tool_manager.list_tools():
        assert len((tool.description or "").split()) >= 6, tool.name


def test_looking_changes_nothing(addon, tmp_path):
    _, _, call = addon
    assert call("notebook_status") == {"ok": True, "notes": 0, "last_added": None}
    assert call("notebook_list")["notes"] == []
    assert not (tmp_path / "data" / "notes.json").exists()


def test_changing_something_asks_first(addon):
    _, _, call = addon
    out = call("notebook_add", text="buy firewood")
    assert out["ok"] is False and out["status"] == "confirm"
    assert call("notebook_status")["notes"] == 0


def test_a_confirmed_note_is_kept_and_read_back(addon):
    _, _, call = addon
    assert call("notebook_add", text="  buy   firewood ", confirm=True) == {"ok": True, "notes": 1}
    call("notebook_add", text="feed the cat", confirm=True)
    listed = call("notebook_list", limit=1)
    assert listed["total"] == 2 and [n["text"] for n in listed["notes"]] == ["feed the cat"]
    assert call("notebook_status")["last_added"] is not None


def test_a_refusal_says_how_to_do_it_right(addon):
    module, _, call = addon
    assert "pass the note as text" in call("notebook_add", text="   ", confirm=True)["error"]
    long = call("notebook_add", text="x" * (module.MAX_TEXT + 40), confirm=True)
    assert long["ok"] is False and str(module.MAX_TEXT) in long["error"]


def test_a_list_is_capped_however_much_is_asked_for(addon):
    module, _, call = addon
    for i in range(module.MAX_ITEMS + 5):
        call("notebook_add", text=f"note {i}", confirm=True)
    assert len(call("notebook_list", limit=1000)["notes"]) == module.MAX_ITEMS


def test_it_keeps_its_data_where_it_is_told_to(addon, tmp_path):
    _, _, call = addon
    call("notebook_add", text="outside the repository", confirm=True)
    stored = json.loads((tmp_path / "data" / "notes.json").read_text(encoding="utf-8"))
    assert stored[0]["text"] == "outside the repository"


def test_it_imports_nothing_of_the_core():
    """An add-on docks beside the core. One import of it and it is a part of it."""
    tree = ast.parse((FOLDER / "server.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "yurios" not in imported
    assert imported <= {"__future__", "json", "os", "time", "pathlib", "mcp"} | set(sys.stdlib_module_names)


def test_the_example_entry_is_valid_and_points_at_the_server():
    entry = json.loads((FOLDER / "mcp-servers.example.json").read_text(encoding="utf-8"))
    server = entry["mcpServers"]["notebook"]
    assert server["args"][0].endswith("tools/addon_template/server.py")
    assert "ADDON_DATA" in server["env"] and isinstance(server["rate"], int)
