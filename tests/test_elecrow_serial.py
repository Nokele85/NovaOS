"""The serial line contract between the Elecrow hand and its probe firmware.

The firmware is generated from a raw string, so a doubled backslash there is a
literal backslash in C++: the heartbeat printed `\\n` instead of ending its
line, and `readStringUntil('\\n')` compiled to a multi-character constant. The
MCP side sent a literal backslash-n too. Both halves are pinned here."""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

pytest.importorskip("mcp")
from tools import elecrow_mcp_server as elecrow                   # noqa: E402

FIRMWARE_SRC = Path(__file__).resolve().parents[1] / \
    "elecrow-firmware" / "nova-device" / "src" / "main.cpp"


@pytest.mark.parametrize("source", [elecrow.FIRMWARE, FIRMWARE_SRC.read_text(encoding="utf-8")],
                         ids=["generator", "checked-in"])
def test_the_firmware_ends_lines_and_reads_commands_on_a_real_newline(source):
    assert r'%lu\n"' in source and r'%lu\\n"' not in source
    assert r"readStringUntil('\n')" in source
    assert r"'\\n'" not in source


def test_a_serial_command_is_sent_with_a_real_line_feed(monkeypatch):
    written: list[bytes] = []

    class FakeSerial:
        def __init__(self, *a, **kw):
            self._lines = [b"NOVA_PONG\n"]

        def open(self):
            # the port must open with DTR/RTS released, or the board reboots
            assert self.dtr is False and self.rts is False

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def reset_input_buffer(self):
            pass

        def write(self, data):
            written.append(data)

        def flush(self):
            pass

        def readline(self):
            return self._lines.pop(0) if self._lines else b""

    monkeypatch.setitem(sys.modules, "serial", types.SimpleNamespace(Serial=FakeSerial))
    server = elecrow.build_server()
    tool = server._tool_manager.get_tool("elecrow_serial_command")
    result = tool.fn(port="COM8", command="ping", seconds=1)
    assert written == [b"ping\n"]
    assert result["ok"] and result["lines"][0] == "NOVA_PONG"
