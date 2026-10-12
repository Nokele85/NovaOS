"""The board relay, the board's line out and the board server's side of it.

Everything here runs on one machine and one loopback port: the relay for real,
the board's own `nova/link.py` loaded as plain Python over a socket that speaks
MicroPython's read/write, and the board server's `RelayStream`. It proves the
protocol end to end. It proves nothing about TLS or about the radio.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import socket
import sys
import threading
import time
import types
from pathlib import Path

import pytest

pytest.importorskip("websockets")

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "elecrow-firmware" / "nova-runtime" / "lib" / "nova"
sys.path.insert(0, str(ROOT / "tools"))

import board_relay  # noqa: E402
import elecrow_link  # noqa: E402

SECRET = "0123456789abcdef0123456789abcdef"
TOKEN = "host-token-for-tests"


@pytest.fixture
def relay():
    """The relay on a free loopback port, in its own thread."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    instance = board_relay.Relay({"boards": {"nova": SECRET}, "host_token": TOKEN})
    loop = asyncio.new_event_loop()
    ready = threading.Event()

    async def main():
        from websockets.asyncio.server import serve
        async with serve(instance.handler, "127.0.0.1", port,
                         process_request=instance.process_request):
            ready.set()
            await stop

    stop = loop.create_future()
    thread = threading.Thread(target=lambda: loop.run_until_complete(main()), daemon=True)
    thread.start()
    assert ready.wait(5)
    cfg = {"enabled": True, "relay": f"http://127.0.0.1:{port}", "host_token": TOKEN,
           "board_id": "nova"}
    yield types.SimpleNamespace(relay=instance, port=port, cfg=cfg)
    loop.call_soon_threadsafe(stop.set_result, None)
    thread.join(timeout=5)


class MicroSocket:
    """A CPython socket with the methods the board's firmware calls."""

    def __init__(self):
        self.sock = socket.socket()

    def settimeout(self, seconds):
        self.sock.settimeout(seconds)

    def setblocking(self, on):
        self.sock.setblocking(on)

    def setsockopt(self, *a):
        pass                            # the dupterm notify hook; nothing to hook here

    def connect(self, addr):
        self.sock.connect(addr)

    def read(self, n):
        try:
            return self.sock.recv(n)
        except (BlockingIOError, socket.timeout):
            return None

    def write(self, data):
        try:
            return self.sock.send(bytes(data))
        except BlockingIOError:
            return None

    def close(self):
        self.sock.close()


@pytest.fixture
def board(monkeypatch, tmp_path, relay):
    """The board's link module, pointed at the relay under test."""
    spec = importlib.util.spec_from_file_location("board_link", LIB / "link.py")
    link = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(link)
    seat = {"stream": None}

    def dupterm(stream, *a):
        previous, seat["stream"] = seat["stream"], stream
        return previous

    import os as real_os
    monkeypatch.setattr(link, "os", types.SimpleNamespace(
        urandom=real_os.urandom, dupterm=dupterm, dupterm_notify=lambda *a: None))
    # the board's `import socket` gets MicroPython's shape; everything else in
    # this process (the relay, the board server's side) keeps the real module
    fake_socket = types.ModuleType("socket")
    fake_socket.__dict__.update(socket.__dict__)
    fake_socket.socket = MicroSocket
    fake_socket.getaddrinfo = lambda host, port: [(0, 0, 0, "", (host, port))]
    monkeypatch.setitem(sys.modules, "socket", fake_socket)
    config = tmp_path / "nova_link.json"
    config.write_text(json.dumps({
        "enabled": True, "id": "nova", "secret": SECRET,
        "urls": [f"ws://127.0.0.1:{relay.port}/board/link"]}))
    monkeypatch.setattr(link, "CONFIG", str(config))
    yield types.SimpleNamespace(link=link, seat=seat, config=config)
    link.drop()


def wait_for(check, seconds=3.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.02)
    return False


def read_all(line, want, seconds=3.0):
    """What the REPL would have read from the line, one byte at a time."""
    got = bytearray()
    one = bytearray(1)
    end = time.monotonic() + seconds
    while len(got) < want and time.monotonic() < end:
        n = line.readinto(one)
        if n:
            got += one[:n]
        else:
            time.sleep(0.01)
    return bytes(got)


# --- the proof ---------------------------------------------------------------

def test_the_board_computes_the_same_proof_as_the_relay(board):
    for who, nonce in (("relay", "a" * 32), ("board", "0f" * 16)):
        assert board.link._hmac(SECRET, f"{who}:{nonce}") == board_relay.proof(SECRET, who, nonce)


# --- the line ----------------------------------------------------------------

def test_the_board_calls_in_and_hangs_the_line_on_its_repl(relay, board):
    answer = board.link.connect({"mode": "awake"})
    assert answer["ok"] is True and answer["stay"] is True
    assert board.seat["stream"] is board.link._line          # the REPL's second seat
    assert board.link.status()["connected"] is True and board.link.status()["via"] == "ws"
    seen = elecrow_link.status(relay.cfg)
    assert seen["online"] is True and seen["via"] == "lan" and seen["state"]["mode"] == "awake"


def test_raw_repl_bytes_cross_in_both_directions(relay, board):
    board.link.connect()
    line = board.link._line
    host = elecrow_link.RelayStream(relay.cfg)
    try:
        assert wait_for(lambda: (line._pump(), line.host)[1])   # told a session is attached
        host.write(b"\r\x03\x03")                                # what RawRepl sends first
        assert read_all(line, 3) == b"\r\x03\x03"
        line.write(b"raw REPL; CTRL-B to exit\r\n>")
        got = b""
        end = time.monotonic() + 3
        while b">" not in got and time.monotonic() < end:
            got += host.read(256)
        assert got == b"raw REPL; CTRL-B to exit\r\n>"
    finally:
        host.close()
    assert wait_for(lambda: (line._pump(), not line.host)[1])   # ...and that it left


def test_a_whole_raw_repl_session_runs_over_the_line(relay, board):
    """RawRepl, unchanged, against a board that answers like MicroPython."""
    import elecrow_device
    board.link.connect()
    line = board.link._line
    done = threading.Event()

    def micropython():
        code = b""
        one = bytearray(1)
        while not done.is_set():
            if not line.readinto(one):
                time.sleep(0.005)
                continue
            c = bytes(one)
            if c == b"\x01":
                line.write(b"raw REPL; CTRL-B to exit\r\n>")
            elif c == b"\x04":
                ran, code = code, b""
                line.write(b"OK" + (b"4\r\n" if b"2 + 2" in ran else b"") + b"\x04\x04>")
            elif c not in (b"\r", b"\x03", b"\x02"):
                code += c

    thread = threading.Thread(target=micropython, daemon=True)
    thread.start()
    try:
        with elecrow_device.RawRepl(
                "link", serial_factory=lambda: elecrow_link.RelayStream(relay.cfg)) as repl:
            out = repl.exec("print(2 + 2)\n", timeout=5)
    finally:
        done.set()
        thread.join(timeout=2)
    assert out["ok"] is True and out["stdout"].strip() == "4"


def test_what_an_app_prints_with_nobody_attached_is_not_sent(relay, board):
    board.link.connect()
    line = board.link._line
    sent = []
    real = line._send
    line._send = lambda data: sent.append(data) or real(data)
    assert line.write(b"tick\r\n" * 50) == 300      # the REPL is told it all went
    assert sent == []                               # ...and the radio stayed quiet


def test_a_stranger_is_turned_away(relay, board):
    board.config.write_text(json.dumps({
        "enabled": True, "id": "nova", "secret": "f" * 32,
        "urls": [f"ws://127.0.0.1:{relay.port}/board/link"]}))
    board.link._cfg = None
    assert board.link.connect() is None
    assert board.seat["stream"] is None
    assert elecrow_link.status(relay.cfg)["online"] is False


def test_a_relay_that_does_not_know_the_secret_is_not_trusted(board, monkeypatch):
    """The other direction: a server that merely answers gets no REPL."""
    def liar(sock):
        return {"nonce": "ab" * 16, "proof": "00" * 32}

    monkeypatch.setattr(board.link, "_recv_text", liar)
    assert board.link.connect() is None
    assert "does not know the secret" in board.link.status()["error"]


def test_the_lan_console_is_left_alone_when_somebody_is_on_it(relay, board):
    console = object()
    board.seat["stream"] = console                  # a WebREPL session is open
    assert board.link.connect() is None
    assert board.seat["stream"] is console
    assert board.link.status()["error"] == "console busy"


def test_the_line_is_redialled_when_it_drops_but_not_in_a_hurry(relay, board):
    link = board.link
    link.connect()
    link._line.open = False                         # it died
    assert link.keep(online=True) is False          # too soon after the last call
    assert board.seat["stream"] is None             # ...but the seat is free again
    link._last_try -= (link.RETRY_S + 1) * 1000
    assert link.keep(online=True) is True
    assert link.keep(online=False) is True          # up is up


# --- deep sleep ---------------------------------------------------------------

def test_a_check_in_nobody_asked_for_is_told_to_sleep(relay, board):
    answer = board.link.connect({"checkin": True})
    assert answer["stay"] is False


def test_a_wake_request_keeps_her_up_at_the_next_check_in_once(relay, board):
    board.link.connect()
    board.link.bye("sleep", 5)
    assert wait_for(lambda: elecrow_link.status(relay.cfg).get("asleep"))
    seen = elecrow_link.status(relay.cfg)
    assert seen["online"] is False and 280 <= seen["next_checkin_s"] <= 300
    assert "checks in again in about 5 min" in elecrow_link.describe(seen)

    asked = elecrow_link.wake(relay.cfg, minutes=6)
    assert asked["wake_pending"] is True
    assert board.link.connect({"checkin": True})["stay"] is True
    assert elecrow_link.status(relay.cfg)["wake_pending"] is False
    board.link.bye("sleep", 5)
    assert wait_for(lambda: elecrow_link.status(relay.cfg).get("asleep"))
    assert board.link.connect({"checkin": True})["stay"] is False     # asked once, kept once


def test_sleep_with_check_ins_off_says_only_the_button_wakes_her(relay, board):
    board.link.connect()
    board.link.bye("sleep", 0)
    assert wait_for(lambda: elecrow_link.status(relay.cfg).get("asleep"))
    seen = elecrow_link.status(relay.cfg)
    assert seen["next_checkin_s"] is None
    assert "only its button" in elecrow_link.describe(seen)


# --- the board server's side ---------------------------------------------------

def test_no_session_when_the_board_is_not_on_the_line(relay):
    stream = elecrow_link.RelayStream(relay.cfg)
    with pytest.raises(OSError):
        end = time.monotonic() + 3
        while time.monotonic() < end:
            stream.read(1)
    stream.close()


def test_the_local_endpoints_want_the_token(relay):
    bad = dict(relay.cfg, host_token="wrong")
    assert elecrow_link.status(bad) == {"relay": "unreachable", "online": False}
    with pytest.raises(OSError):
        elecrow_link.RelayStream(bad)


def test_nothing_the_public_proxy_passed_on_reaches_the_local_endpoints(relay):
    import urllib.error
    import urllib.request
    request = urllib.request.Request(
        relay.cfg["relay"] + "/board/status?board=nova",
        headers={"Authorization": "Bearer " + TOKEN, "X-Forwarded-For": "203.0.113.9"})
    with pytest.raises(urllib.error.HTTPError) as refused:
        urllib.request.urlopen(request, timeout=3)
    assert refused.value.code == 403


def test_describe_says_where_she_is():
    describe = elecrow_link.describe
    assert "on the line" in describe({"online": True})
    assert "due any moment" in describe({"asleep": True, "next_checkin_s": 0})
    assert "not called in since" in describe({"online": False, "last_seen_s": None})
    assert "without power or without a network" in describe({"online": False, "last_seen_s": 600})
    assert "relay does not answer" in describe({"relay": "unreachable"})


# --- the tools -----------------------------------------------------------------

@pytest.fixture
def server(monkeypatch, tmp_path, relay):
    import elecrow_mcp_server as srv
    link_file = tmp_path / "elecrow_link.json"
    link_file.write_text(json.dumps(relay.cfg))
    monkeypatch.setenv("ELECROW_LINK_CONFIG", str(link_file))
    monkeypatch.setenv("ELECROW_NETWORK_CONFIG", str(tmp_path / "none.json"))
    return srv, srv.build_server()


def test_a_call_to_a_sleeping_board_says_when_it_checks_in(relay, board, server):
    srv, _ = server
    board.link.connect()
    board.link.bye("sleep", 5)
    assert wait_for(lambda: elecrow_link.status(relay.cfg).get("asleep"))
    out = srv._device_call("link", ["print(1)" + chr(10)])
    assert out["ok"] is False and out["status"] == "asleep"
    assert "about 5 min" in out["error"] and "device_wake" in out["hint"]


def test_wake_asks_the_relay_instead_of_knocking(relay, board, server, monkeypatch):
    srv, mcp = server
    board.link.connect()
    board.link.bye("sleep", 5)
    assert wait_for(lambda: elecrow_link.status(relay.cfg).get("asleep"))
    monkeypatch.setattr(srv, "_knock", lambda *a: pytest.fail("knocked although a line exists"))
    out = mcp._tool_manager.get_tool("device_wake").fn(port="link")
    assert out["status"] == "asked" and "asked to stay up" in out["note"]
    assert board.link.connect({"checkin": True})["stay"] is True
