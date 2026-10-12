"""Nova's playground on the Elecrow board - everything testable without it.

The wire (MicroPython's raw REPL) is scripted by a fake serial port; the rules
(names, sizes, secrets) are exercised directly; the board-side runtime is at
least compiled, because a syntax error there is found by the owner at 23:00
with a dark screen otherwise."""
from __future__ import annotations

import asyncio
import base64
import json
import re
import sys
import zlib
from pathlib import Path

import pytest

pytest.importorskip("mcp")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import elecrow_device as dev                                    # noqa: E402

RUNTIME = ROOT / "elecrow-firmware" / "nova-runtime"


# ---- names ---------------------------------------------------------------

@pytest.mark.parametrize("name", ["clock", "mood-face", "a1", "_lib"])
def test_app_names_she_may_use(name):
    assert dev.check_app_name(name) == name


@pytest.mark.parametrize("name", ["", "../x", "Clock", "a/b", ".hidden", "x" * 33, "a b"])
def test_app_names_that_would_leave_her_folder(name):
    with pytest.raises(ValueError):
        dev.check_app_name(name)


@pytest.mark.parametrize("name", ["main.py", "Face_2.json", "notes.txt"])
def test_file_names_ok(name):
    assert dev.check_file_name(name) == name


@pytest.mark.parametrize("name", ["../boot.py", "lib/x.py", ".env", "main.mpy", "x.sh", "main"])
def test_file_names_refused(name):
    with pytest.raises(ValueError):
        dev.check_file_name(name)


# ---- secrets -------------------------------------------------------------

def test_redactor_hides_every_known_value(tmp_path):
    f = tmp_path / "s.json"
    f.write_text(json.dumps({"wifi": [{"ssid": "HomeNet", "password": "HomeNet-pw-123"}]}))
    r = dev.Redactor.from_file(f)
    out = r("ssid=HomeNet pw=HomeNet-pw-123")
    assert "HomeNet" not in out and out.count(dev.Redactor.MARK) == 2


def test_redactor_without_secrets_is_a_no_op(tmp_path):
    assert dev.Redactor.from_file(tmp_path / "missing.json")("abc") == "abc"


# ---- the raw REPL --------------------------------------------------------

class FakeBoard:
    """MicroPython's raw REPL, scripted: code arrives, ^D runs it."""

    BANNER = b"raw REPL; CTRL-B to exit\r\n>"

    def __init__(self, run):
        self.run = run                  # code -> (stdout, stderr) or None = hangs
        self.pending = b""
        self.code = b""
        self.hanging = False
        self.written = []

    def write(self, data):
        self.written.append(data)
        if data == b"\r\x01":
            self.pending += self.BANNER
        elif data == b"\x04":
            reply = self.run(self.code.decode())
            self.code = b""
            if reply is None:
                self.pending += b"OK"
                self.hanging = True
            else:
                out, err = reply
                self.pending += b"OK" + out.encode() + b"\x04" + err.encode() + b"\x04>"
        elif data == b"\x03" and self.hanging:
            self.hanging = False
            self.pending += b"\x04Traceback\r\nKeyboardInterrupt: \r\n\x04>"
        elif data not in (b"\r\x03\x03", b"\x02"):
            self.code += data

    def read(self, n):
        chunk, self.pending = self.pending[:n], self.pending[n:]
        return chunk

    def reset_input_buffer(self):
        self.pending = b""

    def close(self):
        pass


def test_raw_repl_returns_stdout_and_traceback():
    board = FakeBoard(lambda code: ("hello\r\n", "") if "print" in code
                      else ("", "Traceback\r\nNameError: x\r\n"))
    with dev.RawRepl("COM8", serial_factory=lambda: board) as repl:
        ok = repl.exec("print('hello')")
        bad = repl.exec("x")
    assert ok["ok"] and ok["stdout"] == "hello\r\n"
    assert not bad["ok"] and "NameError" in bad["stderr"]


def test_a_runaway_app_is_interrupted_not_waited_for(monkeypatch):
    board = FakeBoard(lambda code: None)
    with dev.RawRepl("COM8", serial_factory=lambda: board) as repl:
        result = repl.exec("while True: pass", timeout=0.2)
    assert result["timed_out"] and "KeyboardInterrupt" in result["stderr"]
    assert b"\x03" in board.written


def test_upload_snippets_rebuild_the_file_exactly(tmp_path):
    data = bytes(range(256)) * 50
    target = tmp_path / "blob.bin"
    for code in dev.upload_code(str(target), data):
        exec(code, {})
    assert target.read_bytes() == data


# ---- screenshots ---------------------------------------------------------

def _frame(w, h, box, colour_be):
    raw = bytearray(w * h * 2)
    x0, y0, x1, y1 = box
    for y in range(y0, y1 + 1):
        for x in range(x0, x1 + 1):
            i = (y * w + x) * 2
            raw[i:i + 2] = colour_be
    return bytes(raw)


def test_a_red_box_is_found_and_named():
    raw = _frame(40, 30, (10, 5, 19, 14), b"\xf8\x00")        # pure red, big-endian
    rgb = dev.rgb565be_to_rgb(raw)
    assert rgb[(5 * 40 + 10) * 3:(5 * 40 + 10) * 3 + 3] == bytes([255, 0, 0])
    info = dev.describe_frame(rgb, 40, 30)
    assert info["background"] == "black"
    assert info["content_box"] == [10, 5, 19, 14]
    assert "red" in info["colours"]


def test_png_is_a_valid_image():
    png = dev.png_bytes(bytes(3 * 4 * 2), 4, 2)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    idat = png.index(b"IDAT")
    size = int.from_bytes(png[idat - 4:idat], "big")
    assert zlib.decompress(png[idat + 4:idat + 4 + size]) == (b"\x00" + bytes(12)) * 2


# ---- the MCP surface -----------------------------------------------------

def _server():
    import elecrow_mcp_server as srv
    return srv, srv.build_server()


def test_the_playground_tools_are_advertised():
    _, mcp = _server()
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert {"device_info", "device_exec", "device_write_app", "device_run_app",
            "device_read_app", "device_delete_app", "device_list_apps",
            "device_screenshot", "device_log", "device_set_autostart"} <= names
    # firmware, runtime and secrets stay with the owner's CLI
    assert not {n for n in names if "install" in n or "secret" in n}


def test_a_bad_app_name_never_reaches_the_board(monkeypatch):
    srv, mcp = _server()
    monkeypatch.setattr(srv, "_device_call", lambda *a, **k: pytest.fail("touched the board"))
    tool = mcp._tool_manager.get_tool("device_write_app")
    with pytest.raises(ValueError):
        tool.fn(name="../boot", files={"main.py": "x"})


def test_autostart_needs_the_users_yes(monkeypatch):
    srv, mcp = _server()
    monkeypatch.setattr(srv, "_device_call", lambda *a, **k: pytest.fail("touched the board"))
    out = mcp._tool_manager.get_tool("device_set_autostart").fn(name="clock")
    assert out["status"] == "confirmation_required"


def test_autostart_without_a_name_is_refused_not_cleared(monkeypatch):
    """She sent app="warm_pad", confirm=true three times: the unknown argument
    was dropped, name fell back to "", and "" cleared the autostart — each call
    answering ok. A missing name is now a refusal that names the parameter."""
    srv, mcp = _server()
    monkeypatch.setattr(srv, "_device_call", lambda *a, **k: pytest.fail("touched the board"))
    out = mcp._tool_manager.get_tool("device_set_autostart").fn(confirm=True)
    assert out["ok"] is False and out["status"] == "name_required"
    assert "name" in out["message"] and "app" in out["message"]


def test_clearing_the_autostart_is_asked_for_by_name(monkeypatch):
    srv, mcp = _server()
    sent = []
    monkeypatch.setattr(srv, "_device_call", lambda port, code, **k: sent.append(code) or {
        "ok": True, "stdout": '{"autostart": null}'})
    mcp._tool_manager.get_tool("device_set_autostart").fn(name="", confirm=True)
    assert sent and "s['autostart'] = None" in sent[0][0]


def test_board_output_is_redacted_before_it_leaves(monkeypatch, tmp_path):
    srv, _ = _server()
    secrets = tmp_path / "s.json"
    secrets.write_text(json.dumps({"wifi": [{"ssid": "HomeNet", "password": "sesame-4711"}]}))
    monkeypatch.setenv("ELECROW_DEVICE_SECRETS", str(secrets))
    # this machine's own WLAN setup must not send the call over the air
    monkeypatch.setenv("ELECROW_NETWORK_CONFIG", str(tmp_path / "none.json"))
    monkeypatch.setenv("ELECROW_LINK_CONFIG", str(tmp_path / "no-link.json"))
    monkeypatch.setattr(dev, "board_port", lambda port: "COM8")
    board = FakeBoard(lambda code: ("pw is sesame-4711\r\n", ""))
    real = dev.RawRepl
    monkeypatch.setattr(dev, "RawRepl",
                        lambda port, **kw: real(port, serial_factory=lambda: board, **kw))
    out = srv._device_call("auto", ["print(open('/nova_secrets.json').read())"])
    assert "sesame-4711" not in json.dumps(out)
    assert dev.Redactor.MARK in out["stdout"]


# ---- the proxy on Benniclaw ----------------------------------------------

def test_the_proxy_never_hands_base64_to_the_model(monkeypatch, tmp_path):
    import elecrow_mcp_stdio_proxy as proxy
    monkeypatch.setattr(proxy, "SCREENS", tmp_path)

    async def fake_describe(png):
        return "Ein rotes Quadrat auf Schwarz."

    monkeypatch.setattr(proxy, "_describe", fake_describe)
    png = dev.png_bytes(bytes(12), 2, 2)
    data = asyncio.run(proxy._post_process(
        {"ok": True, "texts": [], "png_b64": base64.b64encode(png).decode()}))
    assert "png_b64" not in data
    assert data["vision"] == "Ein rotes Quadrat auf Schwarz."
    assert Path(data["saved_on_benniclaw"]).read_bytes() == png


def test_without_a_vision_model_the_words_stay_the_boards(monkeypatch, tmp_path):
    import elecrow_mcp_stdio_proxy as proxy
    monkeypatch.setattr(proxy, "SCREENS", tmp_path)
    monkeypatch.setattr(proxy, "_env", lambda name: "")
    png = dev.png_bytes(bytes(12), 2, 2)
    data = asyncio.run(proxy._post_process(
        {"ok": True, "picture": {"background": "black"},
         "png_b64": base64.b64encode(png).decode()}))
    assert "vision" not in data and data["picture"] == {"background": "black"}


def test_the_vision_request_turns_thinking_off():
    """deepseek-flash thinks by default and answered an empty string at this
    budget (measured 2026-09-11) - the request must say reasoning_effort none,
    and carry the picture as a data URL in a user message."""
    import elecrow_mcp_stdio_proxy as proxy
    body = proxy._vision_body("deepseek-flash", b"\x89PNG")
    assert body["reasoning_effort"] == "none"
    (msg,) = body["messages"]
    assert msg["role"] == "user"
    image = [c for c in msg["content"] if c["type"] == "image_url"][0]
    assert image["image_url"]["url"].startswith("data:image/png;base64,")


def test_a_deepseek_route_gets_deepseeks_vision_model(monkeypatch):
    import elecrow_mcp_stdio_proxy as proxy
    monkeypatch.setattr(proxy, "_env", lambda name: "")
    assert proxy._vision_model("https://api.deepseek.com/v1") == "deepseek-flash"
    assert proxy._vision_model("http://localhost:1234/v1") == ""
    monkeypatch.setattr(proxy, "_env", lambda name: "some-vl" if name == "ELECROW_VISION_MODEL" else "")
    assert proxy._vision_model("https://api.deepseek.com/v1") == "some-vl"


def test_an_empty_vision_answer_is_reported_not_passed_off(monkeypatch):
    import elecrow_mcp_stdio_proxy as proxy
    env = {"OPENAI_API_BASE": "https://api.deepseek.com/v1", "OPENAI_API_KEY": "k"}
    monkeypatch.setattr(proxy, "_env", lambda name: env.get(name, ""))

    class Reply:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": ""}, "finish_reason": "length"}]}

    class Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *a, **kw):
            return Reply()

    monkeypatch.setattr(proxy.httpx, "AsyncClient", Client)
    said = asyncio.run(proxy._describe(b"\x89PNG"))
    assert said.startswith("(Bildbeschreibung nicht verfügbar")


# ---- the board-side runtime ----------------------------------------------

@pytest.mark.parametrize("path", sorted(RUNTIME.rglob("*.py")), ids=lambda p: p.name)
def test_runtime_files_compile(path):
    compile(path.read_text(encoding="utf-8"), str(path), "exec")


def test_no_runtime_module_shadows_an_accessor():
    """`nova.display()` and `nova/display.py` cannot both exist: importing the
    submodule replaces the function on the package, and the next call raises
    "'module' object isn't callable" - which is how the boot animation died
    on 2026-09-12."""
    nova = RUNTIME / "lib" / "nova"
    functions = set(re.findall(r"^def (\w+)\(", (nova / "__init__.py").read_text(encoding="utf-8"),
                               re.M))
    modules = {p.stem for p in nova.glob("*.py") if p.stem != "__init__"}
    assert not functions & modules, f"module shadows accessor: {sorted(functions & modules)}"


def test_runtime_uses_rooks_board_facts():
    board = (RUNTIME / "lib" / "nova" / "board.py").read_text(encoding="utf-8")
    for fact in ("AW9523_ADDR = 0x5A", "AW_POWER = 8", "AW_FLASH = 9", "AW_MUTE = 12",
                 "LCD_W = 240", "LCD_H = 284", "SD_SCK = 7", "MIC_CLK = 1"):
        assert fact in board
    io = (RUNTIME / "lib" / "nova" / "aw9523.py").read_text(encoding="utf-8")
    assert "0xFF & ~" in io and "REG_OUTPUT1, 0xFF" not in io   # never fire the flash
