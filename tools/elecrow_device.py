"""Host side of Nova's playground on the Elecrow board (MicroPython runtime).

The MCP server (`elecrow_mcp_server.py`) is the contract; this module is the
wire and the rules behind it:

  * `RawRepl` - MicroPython's raw REPL over the USB serial port: code in,
    stdout and traceback out, with a hard time limit and Ctrl-C when it runs over.
  * `Redactor` - every string that comes back from the board passes it. The
    Wi-Fi credentials live on the board (`/nova_secrets.json`), and an app
    Nova writes can read that file; what it prints must still never reach a
    chat, an audit log or a model.
  * `check_app_name` / `check_file_name` - a model-written string is the
    only thing between the tool call and the board's filesystem.
  * `screenshot_png` / `describe_frame` - the display's framebuffer as a PNG
    for a person and for the vision model, and as plain numbers for when
    no vision model answers.

Nothing here imports YuriOS; it runs on the Windows PC that owns the cable.
"""
from __future__ import annotations

import base64
import json
import re
import struct
import time
import zlib
from pathlib import Path

CH340_VID = 0x1A86
READ_CHUNK = 256
WRITE_CHUNK = 256
WRITE_PAUSE_S = 0.01
UPLOAD_B64_CHUNK = 4096        # one raw-REPL round trip per 3 KB of file

APP_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
FILE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]{0,39}\.(py|txt|json|csv)$")
MAX_FILE_BYTES = 48 * 1024
MAX_APP_BYTES = 256 * 1024
MAX_FILES = 16
LIB_APP = "_lib"


class DeviceError(RuntimeError):
    """The board did not answer the way the raw REPL promises."""


def check_app_name(name: str, *, allow_lib: bool = True) -> str:
    name = (name or "").strip()
    if allow_lib and name == LIB_APP:
        return name
    if not APP_RE.match(name):
        raise ValueError("app name: lowercase letters, digits, '-' and '_', "
                         "max 32, starting with a letter or digit")
    return name


def check_file_name(name: str) -> str:
    name = (name or "").strip()
    if not FILE_RE.match(name):
        raise ValueError("file name: letters, digits, '-' and '_', ending in "
                         ".py/.txt/.json/.csv; no folders")
    return name


# ---- secrets -----------------------------------------------------------------

class Redactor:
    """Replace every known secret value in board output."""

    MARK = "«redacted»"

    def __init__(self, values=()):
        # longest first, so a password containing the SSID is caught whole
        self.values = sorted({v for v in values if v and len(v) >= 4},
                             key=len, reverse=True)

    @classmethod
    def from_file(cls, path: Path) -> "Redactor":
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        values = []
        values += [data.get("webrepl_password", "")]
        for net in data.get("wifi", []):
            values += [net.get("ssid", ""), net.get("password", "")]
        return cls(values)

    def __call__(self, text: str) -> str:
        for v in self.values:
            text = text.replace(v, self.MARK)
        return text


# ---- the raw REPL ------------------------------------------------------------

def _ports():
    from serial.tools import list_ports
    return list(list_ports.comports())


def board_port(port: str = "auto") -> str:
    """The Elecrow's CH340 port; `auto` refuses to guess between several."""
    if port and port != "auto":
        return port
    found = [p.device for p in _ports() if p.vid == CH340_VID]
    if len(found) != 1:
        raise DeviceError(f"expected one CH340 board, found {found or 'none'}; "
                          "pass the COM port explicitly")
    return found[0]


#: Sent at the start of a session: a session is activity. It ends her doze
#: and turns a check-in from deep sleep into staying awake (nova/power.py).
#: It is not a courtesy: dozing she runs at a third of her clock, and a file
#: upload at that speed loses bytes (seen 2026-10-12, "incorrect padding").
#: A runtime from before power.py has no such module and the line does nothing.
POKE = ("try:\n    import nova.power as _p\n    _p.poke()\n"
        "except Exception:\n    pass\n")


def reset_pulse(port: str, hold_s: float = 0.12) -> None:
    """Reset the board through the CH340's auto-reset circuit: RTS pulls EN
    low, DTR stays released so GPIO0 is high and she boots her program, not the
    bootloader. This is how a board in deep sleep is woken over the cable -
    asleep, she answers nothing on the serial line. It kills whatever runs."""
    import serial
    ser = serial.Serial()
    ser.port, ser.baudrate, ser.timeout = port, 115200, 0.1
    ser.dtr = False
    ser.rts = False
    ser.open()
    try:
        ser.rts = True
        time.sleep(hold_s)
        ser.rts = False
    finally:
        ser.close()


class RawRepl:
    """One raw-REPL session: `with RawRepl(port) as repl: repl.exec(code)`.

    DTR/RTS stay released on open: the board's CH340 auto-reset circuit turns
    a toggle into a reset (or into download mode), which would kill the app
    she is looking at.
    """

    def __init__(self, port: str, baud: int = 115200, serial_factory=None,
                 poke: bool = True):
        self.port = port
        self.baud = baud
        self._factory = serial_factory
        self._poke = poke
        self.ser = None

    def __enter__(self) -> "RawRepl":
        if self._factory is not None:
            self.ser = self._factory()
        else:
            import serial
            ser = serial.Serial()
            ser.port, ser.baudrate, ser.timeout = self.port, self.baud, 0.1
            ser.dtr = False
            ser.rts = False
            ser.open()
            self.ser = ser
        self.enter()
        if self._poke:
            self.exec(POKE, timeout=3)
        return self

    def __exit__(self, *exc) -> None:
        try:
            self.ser.write(b"\x02")            # back to the friendly REPL
        finally:
            self.ser.close()

    def _read_until(self, marker: bytes, timeout: float) -> tuple[bytes, bool]:
        data = b""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            chunk = self.ser.read(READ_CHUNK)
            if chunk:
                data += chunk
                if marker in data:
                    return data, True
        return data, False

    def _read_more(self, buf: bytes, eots: int, timeout: float) -> bytes:
        """Keep reading until `buf` holds `eots` end-of-text markers."""
        end = time.monotonic() + timeout
        while buf.count(b"\x04") < eots and time.monotonic() < end:
            buf += self.ser.read(READ_CHUNK)
        return buf

    def enter(self) -> None:
        # Ctrl-C twice stops whatever runs (an autostart app included)
        self.ser.write(b"\r\x03\x03")
        time.sleep(0.2)
        self.ser.reset_input_buffer()
        self.ser.write(b"\r\x01")
        _, ok = self._read_until(b"raw REPL; CTRL-B to exit\r\n>", 5)
        if not ok:
            raise DeviceError("board did not enter the raw REPL - is the "
                              "MicroPython runtime installed?")

    def exec(self, code: str, timeout: float = 10.0) -> dict:
        """Run `code`; {"ok", "stdout", "stderr", "timed_out", "seconds"}."""
        started = time.monotonic()
        payload = code.encode("utf-8")
        for i in range(0, len(payload), WRITE_CHUNK):
            self.ser.write(payload[i:i + WRITE_CHUNK])
            time.sleep(WRITE_PAUSE_S)
        self.ser.write(b"\x04")
        head, ok = self._read_until(b"OK", 5)
        if not ok:
            raise DeviceError(f"raw REPL refused the code: {head[-200:]!r}")
        # the reply is  OK <stdout> \x04 <traceback> \x04 >  - and any of it may
        # already have arrived with the OK
        buf = head[head.index(b"OK") + 2:]
        buf = self._read_more(buf, 2, timeout)
        timed_out = buf.count(b"\x04") < 2
        if timed_out:
            self.ser.write(b"\x03")            # KeyboardInterrupt on the board
            buf = self._read_more(buf, 2, 5)
        stdout, _, rest = buf.partition(b"\x04")
        stderr, _, tail = rest.partition(b"\x04")
        if b">" not in tail:
            self._read_until(b">", 2)
        return {"ok": not stderr.strip() and not timed_out,
                "stdout": stdout.decode("utf-8", "replace"),
                "stderr": stderr.decode("utf-8", "replace"),
                "timed_out": timed_out,
                "seconds": round(time.monotonic() - started, 2)}


def upload_code(path: str, data: bytes) -> list[str]:
    """Code snippets that write `data` to `path` on the board, chunk by chunk."""
    b64 = base64.b64encode(data).decode("ascii")
    parts = [f"f=open({path!r},'wb')\nf.close()\n"]
    for i in range(0, len(b64), UPLOAD_B64_CHUNK):
        # each chunk stands alone: no reliance on what an earlier one imported
        parts.append(f"import binascii\nf=open({path!r},'ab')\n"
                     f"f.write(binascii.a2b_base64({b64[i:i + UPLOAD_B64_CHUNK]!r}))\n"
                     "f.close()\n")
    return parts


# ---- screenshots -------------------------------------------------------------

def rgb565be_to_rgb(raw: bytes) -> bytes:
    out = bytearray(len(raw) // 2 * 3)
    for i in range(0, len(raw) - 1, 2):
        c = (raw[i] << 8) | raw[i + 1]
        o = i // 2 * 3
        out[o] = ((c >> 11) & 0x1F) * 255 // 31
        out[o + 1] = ((c >> 5) & 0x3F) * 255 // 63
        out[o + 2] = (c & 0x1F) * 255 // 31
    return bytes(out)


def png_bytes(rgb: bytes, width: int, height: int) -> bytes:
    rows = b"".join(b"\x00" + rgb[y * width * 3:(y + 1) * width * 3]
                    for y in range(height))

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (struct.pack(">I", len(body)) + kind + body
                + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 6)) + chunk(b"IEND", b""))


_NAMED = {"black": (0, 0, 0), "white": (255, 255, 255), "red": (255, 0, 0),
          "green": (0, 255, 0), "blue": (0, 0, 255), "yellow": (255, 255, 0),
          "cyan": (0, 255, 255), "magenta": (255, 0, 255), "pink": (255, 120, 200),
          "orange": (255, 140, 0), "grey": (128, 128, 128), "purple": (128, 0, 160)}


def _name(rgb) -> str:
    return min(_NAMED, key=lambda n: sum((a - b) ** 2 for a, b in zip(_NAMED[n], rgb)))


def describe_frame(rgb: bytes, width: int, height: int) -> dict:
    """What can be said about a picture without looking at it: coverage, where the
    content is, and which colours dominate."""
    px = [tuple(rgb[i:i + 3]) for i in range(0, len(rgb), 3)]
    background = max(set(px[::7]), key=px[::7].count) if px else (0, 0, 0)
    lit = [(i % width, i // width) for i, p in enumerate(px)
           if sum(abs(a - b) for a, b in zip(p, background)) > 60]
    counts: dict[str, int] = {}
    for p in px[::3]:
        counts[_name(p)] = counts.get(_name(p), 0) + 1
    total = max(1, sum(counts.values()))
    colours = sorted(counts.items(), key=lambda kv: -kv[1])[:4]
    out = {"size": [width, height], "background": _name(background),
           "content_ratio": round(len(lit) / max(1, len(px)), 3),
           "colours": {k: round(v / total, 3) for k, v in colours}}
    if lit:
        xs, ys = [p[0] for p in lit], [p[1] for p in lit]
        out["content_box"] = [min(xs), min(ys), max(xs), max(ys)]
    return out
