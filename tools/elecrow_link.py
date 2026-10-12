"""The board server's side of the board relay (tools/board_relay.py).

The board dials out to the relay and keeps a line open; this module is how the
board server reaches the board through it. `RelayStream` is serial-like, so
`RawRepl` runs over it unchanged: the bytes are the board's own raw REPL.

Standard library only, like elecrow_wifi. The relay is on the local network;
the token never enters an error message.
"""
import base64
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from elecrow_wifi import WebReplStream


def config_path():
    return Path(os.environ.get("ELECROW_LINK_CONFIG",
        str(Path(__file__).resolve().parent.parent / "secrets" / "elecrow_link.json")))


def configuration():
    """{"enabled", "relay": "http://host:port", "host_token", "board_id", ...}
    or {} when the link is not set up."""
    try:
        return json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _ask(cfg: dict, path: str, timeout: float = 3.0) -> dict:
    board = cfg.get("board_id", "nova")
    sep = "&" if "?" in path else "?"
    request = urllib.request.Request(
        cfg["relay"].rstrip("/") + path + f"{sep}board={board}",
        headers={"Authorization": "Bearer " + cfg["host_token"]})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError):
        return {"relay": "unreachable", "online": False}


def status(cfg: dict) -> dict:
    """How the relay sees the board: on the line or not, asleep until when."""
    return _ask(cfg, "/board/status")


def wake(cfg: dict, minutes: float = 6) -> dict:
    """Ask for the board to stay up at its next check-in from deep sleep."""
    return _ask(cfg, f"/board/wake?minutes={float(minutes):g}")


def describe(state: dict) -> str:
    """One sentence about where the board is, for whoever asked for it."""
    if state.get("relay") == "unreachable":
        return "The relay does not answer, so nothing is known about the board."
    if state.get("online"):
        return "The board is on the line."
    if state.get("asleep"):
        due = state.get("next_checkin_s")
        if due is None:
            return "The board is in deep sleep with check-ins off: only its button wakes it."
        if due <= 0:
            return "The board is in deep sleep and its check-in is due any moment."
        return f"The board is in deep sleep; it checks in again in about {max(1, round(due / 60))} min."
    seen = state.get("last_seen_s")
    if seen is None:
        return "The board has not called in since the relay started. Is it switched on and online?"
    return (f"The board is not on the line; it was last heard {max(1, round(seen / 60))} min ago. "
            "It may be without power or without a network.")


class RelayStream(WebReplStream):
    """Serial-like bytes to the board through the relay. Opening fails with
    OSError when the board is not on the line or another session has it."""

    def __init__(self, cfg: dict, timeout: float = 5.0):
        import socket
        url = urlsplit(cfg["relay"])
        host, port = url.hostname, url.port or 80
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.timeout = 0.1
        self.buffer = bytearray()
        try:
            key = base64.b64encode(os.urandom(16)).decode()
            board = cfg.get("board_id", "nova")
            request = (f"GET /board/host?board={board} HTTP/1.1\r\nHost: {host}:{port}\r\n"
                       "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                       f"Authorization: Bearer {cfg['host_token']}\r\n"
                       f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
            self.sock.sendall(request.encode())
            response = bytearray()
            while not response.endswith(b"\r\n\r\n") and len(response) < 8192:
                response.extend(self._exact(1))
            if b" 101 " not in response.split(b"\r\n")[0]:
                raise OSError("refused")
            self.sock.settimeout(timeout)
        except Exception:
            self.close()
            raise OSError("the relay refused the session") from None

    def write(self, data):
        self._send(data, 2)            # REPL bytes travel as binary frames
        return len(data)
