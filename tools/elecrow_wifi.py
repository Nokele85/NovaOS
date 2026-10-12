"""WebREPL byte stream for the existing raw REPL (SPEC §26.6).

Standard-library-only, authenticated on the local network. No command replay:
the caller may choose USB only before a command has been sent.
"""
import base64
import hashlib
import json
import os
import socket
import struct
import time
from pathlib import Path


def config_path():
    return Path(os.environ.get("ELECROW_NETWORK_CONFIG",
        str(Path(__file__).resolve().parent.parent / "secrets" / "elecrow_network.json")))


def configuration():
    try:
        return json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


class WebReplStream:
    """Serial-like bytes over RFC6455; credentials never enter an error."""

    def __init__(self, host, password, port=8266):
        self.sock = socket.create_connection((host, port), timeout=3)
        self.timeout = 0.1
        self.buffer = bytearray()
        try:
            key = base64.b64encode(os.urandom(16)).decode()
            request = (f"GET / HTTP/1.1\r\nHost: {host}:{port}\r\n"
                       "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                       f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
            self.sock.sendall(request.encode())
            response = bytearray()
            while not response.endswith(b"\r\n\r\n") and len(response) < 8192:
                response.extend(self._exact(1))
            accept = base64.b64encode(hashlib.sha1(
                (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
            if b" 101 " not in response.split(b"\r\n")[0] or accept not in response:
                raise OSError("invalid WebREPL handshake")
            self._until(b"Password: ", 4)
            self.write((password + "\r").encode())
            self._until(b"WebREPL connected", 4)
            # A peer that sends only half a frame must not defeat deadlines.
            self.sock.settimeout(3)
        except Exception:
            self.close()
            raise OSError("WebREPL connection or authentication failed") from None

    def _exact(self, count):
        result = bytearray()
        while len(result) < count:
            part = self.sock.recv(count - len(result))
            if not part:
                raise OSError("WebREPL disconnected")
            result.extend(part)
        return bytes(result)

    def _send(self, payload, opcode=2):
        mask = os.urandom(4)
        size = len(payload)
        header = bytes([0x80 | opcode, 0x80 | size]) if size < 126 else (
            bytes([0x80 | opcode, 0x80 | 126]) + struct.pack(">H", size))
        self.sock.sendall(header + mask + bytes(
            value ^ mask[i % 4] for i, value in enumerate(payload)))

    def _frame(self):
        first, second = self._exact(2)
        size = second & 127
        if size == 126:
            size = struct.unpack(">H", self._exact(2))[0]
        elif size == 127:
            size = struct.unpack(">Q", self._exact(8))[0]
        if size > 1024 * 1024:
            raise OSError("oversized WebREPL frame")
        mask = self._exact(4) if second & 128 else None
        payload = self._exact(size)
        if mask:
            payload = bytes(v ^ mask[i % 4] for i, v in enumerate(payload))
        opcode = first & 15
        if opcode == 8:
            raise OSError("WebREPL closed")
        if opcode == 9:
            self._send(payload, 10)
            return b""
        return payload if opcode in (0, 1, 2) else b""

    def read(self, count):
        import select
        if not self.buffer:
            if not select.select([self.sock], [], [], self.timeout)[0]:
                return b""
            self.buffer.extend(self._frame())
        result = bytes(self.buffer[:count])
        del self.buffer[:count]
        return result

    def _until(self, marker, timeout):
        data = bytearray()
        end = time.monotonic() + timeout
        while marker not in data:
            if time.monotonic() >= end:
                raise OSError("WebREPL login timed out")
            data.extend(self.read(256))

    def write(self, data):
        # WebREPL reserves binary frames for its file-transfer protocol;
        # terminal input (including raw-REPL control bytes) uses text frames.
        self._send(data, 1)
        return len(data)

    def reset_input_buffer(self):
        self.buffer.clear()
        import select
        while select.select([self.sock], [], [], 0)[0]:
            self._frame()

    def close(self):
        self.sock.close()
