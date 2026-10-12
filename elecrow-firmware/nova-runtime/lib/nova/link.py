# Her line out: the board dials the relay and keeps the line open.
#
# Nothing can call into a phone's hotspot, so away from home the board has to
# be the one that calls. It opens a WebSocket to the relay (tools/board_relay.py
# on the always-on host), proves it holds its secret and checks that the relay
# holds it too, and then hangs that socket where the USB cable's REPL hangs
# (os.dupterm). From there on the bytes are her ordinary raw REPL, and every
# tool on the host works as it does over the cable.
#
# /nova_link.json, written by the host's installer and never in source:
#   {"enabled": true, "id": "nova", "secret": "...",
#    "urls": ["ws://<relay on the home network>/board/link",
#             "wss://<public name>/board/link"]}
# The addresses are tried in order: at home the first answers at once; away it
# times out and the public one is used. That one is TLS with the certificate
# checked against nova/roots.py, which needs her clock to be right.
#
# Only one console can hang on the REPL besides the cable. While the link is
# up the LAN console (nova/remote.py) refuses callers; it stays as the way in
# when the relay cannot be reached.
import io
import json
import os
import time

CONFIG = "/nova_link.json"
RETRY_S = 30                  # after a failed call, wait this long
CONNECT_S = 4                 # per address
HELLO_S = 8

try:
    _ticks, _diff, _sleep_ms = time.ticks_ms, time.ticks_diff, time.sleep_ms
except AttributeError:        # CPython: the host's tests load this file
    def _ticks():
        return int(time.monotonic() * 1000)

    def _diff(a, b):
        return a - b

    def _sleep_ms(ms):
        time.sleep(ms / 1000)

_cfg = None
_line = None                  # the open Line, or None
_last_try = None
_last_error = None
_via = None


def config():
    global _cfg
    if _cfg is None:
        try:
            with open(CONFIG) as f:
                _cfg = json.load(f)
        except (OSError, ValueError):
            _cfg = {}
    return _cfg


def enabled():
    cfg = config()
    return bool(cfg.get("enabled") and cfg.get("secret") and cfg.get("urls"))


def _hmac(key, msg):
    """HMAC-SHA256 as hex; this firmware has hashlib but no hmac."""
    import hashlib
    import binascii
    key = key.encode() if isinstance(key, str) else key
    msg = msg.encode() if isinstance(msg, str) else msg
    if len(key) > 64:
        key = hashlib.sha256(key).digest()
    key = key + bytes(64 - len(key))
    inner = hashlib.sha256(bytes(b ^ 0x36 for b in key) + msg).digest()
    outer = hashlib.sha256(bytes(b ^ 0x5C for b in key) + inner).digest()
    return binascii.hexlify(outer).decode()


def _parse(url):
    scheme, rest = url.split("://", 1)
    hostport, _, path = rest.partition("/")
    host, _, port = hostport.partition(":")
    return scheme, host, int(port or (443 if scheme == "wss" else 80)), "/" + path


def frame(payload, opcode=2):
    """One WebSocket frame from a client. Client frames must carry a mask; a
    mask of zeros is one, and leaves the payload as it is."""
    n = len(payload)
    if n < 126:
        head = bytes([0x80 | opcode, 0x80 | n])
    else:
        head = bytes([0x80 | opcode, 0x80 | 126, n >> 8, n & 0xFF])
    return head + b"\x00\x00\x00\x00" + payload


class Line(io.IOBase):
    """The open line, as the stream os.dupterm wants: `readinto` hands the
    REPL the bytes the host sent, `write` sends what the REPL prints."""

    def __init__(self, sock):
        self.sock = sock
        self.open = True
        self.host = False         # is a session of the board server attached?
        self._in = bytearray()    # bytes off the socket, not yet whole frames
        self._rx = bytearray()    # REPL bytes for readinto
        self._rp = 0
        self.bye = None

    # --- towards the REPL ---------------------------------------------------

    def readinto(self, buf):
        if self._rp >= len(self._rx):
            self._rx = bytearray()
            self._rp = 0
            self._pump()
            if not self._rx:
                return None if self.open else 0
        n = min(len(buf), len(self._rx) - self._rp)
        buf[:n] = self._rx[self._rp:self._rp + n]
        self._rp += n
        return n

    def _pump(self):
        """Take what the socket has and sort it into REPL bytes and words."""
        if not self.open:
            return
        try:
            data = self.sock.read(512)
        except OSError:
            data = None           # nothing there yet
        if data == b"":
            self.open = False     # the other end hung up
            return
        if data:
            self._in.extend(data)
        while self._frame():
            pass

    def _frame(self):
        b = self._in
        if len(b) < 2:
            return False
        n = b[1] & 0x7F
        at = 2
        if n == 126:
            if len(b) < 4:
                return False
            n = (b[2] << 8) | b[3]
            at = 4
        elif n == 127:
            self.open = False     # nobody sends her frames that large
            return False
        masked = b[1] & 0x80
        if masked:
            at += 4
        if len(b) < at + n:
            return False
        payload = bytes(b[at:at + n])
        if masked:
            key = b[at - 4:at]
            payload = bytes(c ^ key[i & 3] for i, c in enumerate(payload))
        opcode = b[0] & 0x0F
        self._in = bytearray(b[at + n:])
        if opcode in (0, 2):
            self._rx.extend(payload)
        elif opcode == 1:
            self._word(payload)
        elif opcode == 9:
            self._send(frame(payload, 10))        # ping -> pong
        elif opcode == 8:
            self.open = False
        return True

    def _word(self, payload):
        try:
            word = json.loads(payload)
        except ValueError:
            return
        if "host" in word:
            self.host = bool(word["host"])

    # --- from the REPL ------------------------------------------------------

    def write(self, buf):
        # What an app prints while nobody is attached stays here: sending it
        # would keep the radio busy for a listener that does not exist.
        if self.open and self.host and len(buf):
            for i in range(0, len(buf), 1024):
                self._send(frame(bytes(buf[i:i + 1024])))
        return len(buf)

    def _send(self, data):
        if not self.open:
            return
        view = memoryview(data)
        sent, waited = 0, 0
        while sent < len(data):
            try:
                n = self.sock.write(view[sent:])
            except OSError:
                n = None
            if n:
                sent += n
                waited = 0
                continue
            waited += 5
            if waited > 3000:     # three seconds without a byte going out
                self.open = False
                return
            _sleep_ms(5)

    def say(self, word):
        """A word about herself to the relay (not REPL bytes)."""
        self._send(frame(json.dumps(word).encode(), 1))

    def close(self):
        if self.open:
            try:
                self._send(frame(b"", 8))
            except Exception:
                pass
        self.open = False
        try:
            self.sock.close()
        except Exception:
            pass


# --- dialling ---------------------------------------------------------------

def _exact(sock, n):
    out = b""
    while len(out) < n:
        part = sock.read(n - len(out))
        if not part:
            raise OSError("line closed")
        out += part
    return out


def _recv_text(sock):
    """One text frame, blocking - only used while the line is being set up."""
    head = _exact(sock, 2)
    n = head[1] & 0x7F
    if n == 126:
        ext = _exact(sock, 2)
        n = (ext[0] << 8) | ext[1]
    elif n == 127:
        raise OSError("frame too large")
    key = _exact(sock, 4) if head[1] & 0x80 else None
    payload = _exact(sock, n)
    if key:
        payload = bytes(c ^ key[i & 3] for i, c in enumerate(payload))
    if head[0] & 0x0F == 8:
        raise OSError("refused")
    return json.loads(payload)


def _dial(url, cfg, hello):
    """Open one address and do the whole handshake; (Line, relay's answer)."""
    import binascii
    import socket
    scheme, host, port, path = _parse(url)
    addr = socket.getaddrinfo(host, port)[0][-1]
    raw = socket.socket()
    raw.settimeout(CONNECT_S)
    try:
        raw.connect(addr)
        raw.settimeout(HELLO_S)
        sock = raw
        if scheme == "wss":
            import ssl
            from . import roots
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.verify_mode = ssl.CERT_REQUIRED
            ctx.load_verify_locations(cadata=roots.PEM)
            sock = ctx.wrap_socket(raw, server_hostname=host)
        key = binascii.b2a_base64(os.urandom(16)).strip().decode()
        sock.write(("GET %s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\n"
                    "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
                    "Sec-WebSocket-Version: 13\r\n\r\n" % (path, host, key)).encode())
        head = b""
        while not head.endswith(b"\r\n\r\n"):
            head += _exact(sock, 1)
            if len(head) > 2048:
                raise OSError("no upgrade")
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise OSError("no upgrade")
        # each end proves it holds the secret before any REPL byte moves
        mine = binascii.hexlify(os.urandom(16)).decode()
        said = dict(hello)
        said["id"] = cfg.get("id", "nova")
        said["nonce"] = mine
        sock.write(frame(json.dumps(said).encode(), 1))
        theirs = _recv_text(sock)
        if theirs.get("proof") != _hmac(cfg["secret"], "relay:" + mine):
            raise OSError("the relay does not know the secret")
        sock.write(frame(json.dumps(
            {"proof": _hmac(cfg["secret"], "board:" + str(theirs.get("nonce")))}).encode(), 1))
        answer = _recv_text(sock)
        if not answer.get("ok"):
            raise OSError("refused")
        raw.setblocking(False)
        try:                      # tell the REPL machinery when bytes arrive, so
            raw.setsockopt(socket.SOL_SOCKET, 20, os.dupterm_notify)   # Ctrl-C lands
        except Exception:
            pass
        line = Line(sock)
        line.host = bool(answer.get("host"))
        return line, answer
    except Exception:
        try:
            raw.close()
        except Exception:
            pass
        raise


def _attach(line):
    """Hang the line on the REPL, unless somebody is on the LAN console."""
    prev = os.dupterm(None)
    if prev is not None and prev is not _line:
        os.dupterm(prev)          # a console session is open: leave it alone
        return False
    os.dupterm(line)
    return True


def connect(hello=None):
    """Dial the relay. The relay's answer ({"ok", "stay", ...}) once the line
    is up and hung on the REPL; None when no address answered."""
    global _line, _last_try, _last_error, _via
    if not enabled():
        return None
    cfg = config()
    _last_try = _ticks()
    drop()
    for url in cfg["urls"]:
        try:
            line, answer = _dial(url, cfg, hello or {})
        except Exception as e:
            _last_error = "%s: %r" % (url.split("://")[0], e)
            continue
        if not _attach(line):
            line.close()
            _last_error = "console busy"
            return None
        _line, _via, _last_error = line, url.split("://")[0], None
        return answer
    return None


def alive():
    return _line is not None and _line.open


def drop():
    """Hang up, if a line is open."""
    global _line
    line, _line = _line, None
    if line is None:
        return
    try:
        prev = os.dupterm(None)
        if prev is not None and prev is not line:
            os.dupterm(prev)
    except Exception:
        pass
    line.close()


def bye(reason, checkin_min=0):
    """Tell the relay why she is leaving (deep sleep: when she checks in
    again), then hang up."""
    if alive():
        try:
            _line.say({"bye": reason, "checkin_min": checkin_min})
            _sleep_ms(150)        # let it leave before the radio goes
        except Exception:
            pass
    drop()


def tell(state):
    """Pass a word about herself to the relay: mode, battery."""
    if alive():
        try:
            _line.say({"state": state})
        except Exception:
            pass


def keep(online):
    """Called once a second by nova.power: redial when the line is down, not
    more often than every RETRY_S. True while the line is up."""
    if alive():
        return True
    if _line is not None:
        drop()                    # it died: free the REPL slot
    if not enabled() or not online:
        return False
    if _last_try is not None and _diff(_ticks(), _last_try) < RETRY_S * 1000:
        return False
    return connect() is not None


def status():
    """{"enabled", "connected", "via", "host", "error"}: is her line out up,
    over which kind of address, and is the board server attached right now."""
    return {"enabled": enabled(), "connected": alive(), "via": _via if alive() else None,
            "host": bool(alive() and _line.host), "error": _last_error}
