import json

import pytest

pytest.importorskip("mcp")
from mcp.shared.memory import create_connected_server_and_client_session

from tools import elecrow_mcp_server as elecrow


async def test_elecrow_contract_is_safe_and_scaffolded(tmp_path, monkeypatch):
    monkeypatch.setattr(elecrow, "DEFAULT_ROOT", tmp_path / "elecrow")
    async with create_connected_server_and_client_session(
            elecrow.build_server()._mcp_server) as session:
        names = sorted(t.name for t in (await session.list_tools()).tools
                       if t.name.startswith("elecrow_"))
        assert names == ["elecrow_build", "elecrow_debug", "elecrow_flash",
                         "elecrow_scaffold", "elecrow_serial_command",
                         "elecrow_serial_log", "elecrow_status"]
        out = await session.call_tool("elecrow_scaffold", {})
        data = json.loads(out.content[0].text)
        assert data["ok"]
        assert (tmp_path / "elecrow" / "nova-device" / "src" / "main.cpp").is_file()
        refused = await session.call_tool("elecrow_flash", {})
        assert "confirmation_required" in refused.content[0].text


async def test_elecrow_rejects_path_escape(tmp_path, monkeypatch):
    monkeypatch.setattr(elecrow, "DEFAULT_ROOT", tmp_path / "elecrow")
    async with create_connected_server_and_client_session(
            elecrow.build_server()._mcp_server) as session:
        result = await session.call_tool("elecrow_status", {"project_name": "../bad"})
    assert result.isError
