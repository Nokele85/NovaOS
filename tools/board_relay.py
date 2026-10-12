"""The board relay: where Nova's board calls in from wherever it is.

At home the board server could simply call the board. On a phone's hotspot it
cannot: nothing reaches into a hotspot. So the board dials out, to this relay,
and keeps the line open. The board server then talks to the relay, and the
relay hands the bytes through. The bytes are the board's own raw REPL, exactly
what goes over the USB cable, so every tool works the same over this line.

Three kinds of caller, one port:

    /board/link    the board. A WebSocket, open to the internet through the
                   public proxy, so it trusts nobody: both ends prove they
                   hold the board's secret before a single REPL byte moves.
    /board/host    the board server. A WebSocket, local network only.
    /board/status  the board server asks how the board is. Local only.
    /board/wake    the board server asks for the board to stay up at its next
                   check-in from deep sleep. Local only.

A board in deep sleep is not connected. It wakes on a timer, calls in, and is
told in the first answer whether anybody asked for it ("stay"). If not, it is
asleep again a few seconds later.

Runs on the always-on host, next to her. It imports nothing of hers.
    python tools/board_relay.py --port 8770 --secrets secrets/board_relay.json
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

log = logging.getLogger("board-relay")

HANDSHAKE_S = 8             # a caller that has not proved itself by then is cut
WAKE_MAX_S = 30 * 60
HELLO_MAX = 2048            # bytes of JSON a stranger may send before it has proved anything


def proof(secret: str, who: str, nonce: str) -> str:
    """What each end sends to show it holds the board's secret. `who` keeps a
    proof made by one side from being played back as the other's."""
    return hmac.new(secret.encode(), f"{who}:{nonce}".encode(), hashlib.sha256).hexdigest()


@dataclass
class Board:
    """What the relay knows of one board."""
    link: object = None             # its open line, or None
    host: object = None             # the board server talking to it right now
    via: str = ""                   # "lan" or "public"
    since: float = 0.0              # when the line came up
    last_seen: float = 0.0
    last_bye: dict = field(default_factory=dict)    # what it said before it left
    hello: dict = field(default_factory=dict)       # what it said when it came
    wake_until: float = 0.0         # somebody wants it up at its next check-in
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class Relay:
    def __init__(self, secrets: dict, clock=time.time):
        self.boards_secret: dict = dict(secrets.get("boards") or {})
        self.host_token: str = str(secrets.get("host_token") or "")
        self.boards: dict[str, Board] = {name: Board() for name in self.boards_secret}
        self.clock = clock

    # --- what the board server may ask -------------------------------------

    def status(self, name: str) -> dict:
        board = self.boards.get(name)
        if board is None:
            return {"known": False, "online": False}
        now = self.clock()
        out = {"known": True, "online": board.link is not None,
               "via": board.via if board.link is not None else None,
               "busy": board.host is not None,
               "last_seen_s": round(now - board.last_seen) if board.last_seen else None,
               "wake_pending": now < board.wake_until,
               "state": {k: board.hello.get(k) for k in ("mode", "woke", "battery")
                         if k in board.hello}}
        bye = board.last_bye
        if board.link is None and bye.get("bye") == "sleep":
            minutes = int(bye.get("checkin_min") or 0)
            out["asleep"] = True
            if minutes:
                due = bye["at"] + minutes * 60 - now
                # a check-in that is overdue is still coming: it boots first
                out["next_checkin_s"] = max(0, round(due))
            else:
                out["next_checkin_s"] = None    # the button only
        return out

    def wake(self, name: str, seconds: float) -> dict:
        board = self.boards.get(name)
        if board is None:
            return {"known": False}
        board.wake_until = self.clock() + max(30, min(WAKE_MAX_S, seconds))
        return self.status(name)

    # --- the two lines ------------------------------------------------------

    async def board_line(self, ws, via: str) -> None:
        """A board calling in. Nothing is believed until both proofs are done."""
        try:
            raw = await asyncio.wait_for(ws.recv(), HANDSHAKE_S)
            if not isinstance(raw, str) or len(raw) > HELLO_MAX:
                raise ValueError("not a hello")
            hello = json.loads(raw)
            name, theirs = str(hello.get("id", "")), str(hello.get("nonce", ""))
            secret = self.boards_secret.get(name)
            if not secret or not 16 <= len(theirs) <= 64:
                raise ValueError("unknown board")
            mine = os.urandom(16).hex()
            await ws.send(json.dumps({"nonce": mine, "proof": proof(secret, "relay", theirs)}))
            answer = json.loads(await asyncio.wait_for(ws.recv(), HANDSHAKE_S))
            if not hmac.compare_digest(str(answer.get("proof", "")), proof(secret, "board", mine)):
                raise ValueError("wrong proof")
        except (ValueError, KeyError, TypeError, asyncio.TimeoutError, ConnectionClosed) as exc:
            log.warning("a caller on /board/link was turned away (%s, via %s)",
                        type(exc).__name__, via)
            await ws.close(4401, "no")
            return

        board = self.boards[name]
        now = self.clock()
        stay = True
        if hello.get("checkin"):
            stay = now < board.wake_until
        if stay:
            board.wake_until = 0.0
        old = board.link
        board.link, board.via, board.since, board.last_seen = ws, via, now, now
        board.hello = {k: hello.get(k) for k in ("mode", "woke", "battery", "checkin")}
        board.last_bye = {}
        if old is not None:                      # it called again before the old line died
            await old.close(4000, "replaced")
        await ws.send(json.dumps({"ok": True, "stay": stay, "host": board.host is not None}))
        log.info("%s is on the line via %s%s", name, via,
                 "" if stay else " (a check-in nobody asked for)")
        try:
            async for message in ws:
                board.last_seen = self.clock()
                if isinstance(message, bytes):
                    host = board.host
                    if host is not None:
                        try:
                            await host.send(message)
                        except ConnectionClosed:
                            pass
                else:                            # a word about itself, not REPL bytes
                    try:
                        word = json.loads(message)
                    except ValueError:
                        continue
                    if "bye" in word:
                        board.last_bye = {"bye": word.get("bye"),
                                          "checkin_min": word.get("checkin_min"),
                                          "at": self.clock()}
                    elif "state" in word and isinstance(word["state"], dict):
                        board.hello.update(word["state"])
        except ConnectionClosed:
            pass
        finally:
            if board.link is ws:
                board.link = None
                host = board.host
                if host is not None:
                    await host.close(4410, "the board left")
                log.info("%s is off the line%s", name,
                         " (asleep)" if board.last_bye.get("bye") == "sleep" else "")

    async def host_line(self, ws, name: str) -> None:
        """The board server, for the length of one session of raw-REPL bytes."""
        board = self.boards.get(name)
        if board is None or board.link is None:
            await ws.close(4404, "the board is not on the line")
            return
        if board.lock.locked():
            await ws.close(4409, "another session has the board")
            return
        async with board.lock:
            link = board.link
            board.host = ws
            try:
                await link.send(json.dumps({"host": True}))
                async for message in ws:
                    if isinstance(message, str):
                        message = message.encode()
                    await link.send(message)
            except ConnectionClosed:
                pass
            finally:
                board.host = None
                if board.link is link:
                    try:
                        await link.send(json.dumps({"host": False}))
                    except ConnectionClosed:
                        pass

    # --- one port, three kinds of caller -----------------------------------

    def _local(self, request) -> bool:
        """Did this come from the local network, with the board server's token?
        Anything the public proxy passed on carries its forwarding header."""
        if "X-Forwarded-For" in request.headers or "Forwarded" in request.headers:
            return False
        given = request.headers.get("Authorization", "")
        return bool(self.host_token) and hmac.compare_digest(
            given, "Bearer " + self.host_token)

    def process_request(self, connection, request):
        url = urlsplit(request.path)
        query = parse_qs(url.query)
        name = (query.get("board") or ["nova"])[0]
        if url.path == "/board/link":
            return None                          # a WebSocket; it proves itself inside
        if url.path in ("/board/host", "/board/status", "/board/wake"):
            if not self._local(request):
                return connection.respond(HTTPStatus.FORBIDDEN, "no\n")
            if url.path == "/board/host":
                return None
            if url.path == "/board/status":
                body = self.status(name)
            else:
                minutes = float((query.get("minutes") or ["6"])[0])
                body = self.wake(name, minutes * 60)
            response = connection.respond(HTTPStatus.OK, json.dumps(body) + "\n")
            response.headers["Content-Type"] = "application/json"
            return response
        return connection.respond(HTTPStatus.NOT_FOUND, "no\n")

    async def handler(self, ws) -> None:
        request = ws.request
        url = urlsplit(request.path)
        if url.path == "/board/link":
            via = "public" if "X-Forwarded-For" in request.headers else "lan"
            await self.board_line(ws, via)
        elif url.path == "/board/host":
            name = (parse_qs(url.query).get("board") or ["nova"])[0]
            await self.host_line(ws, name)
        else:
            await ws.close(4404, "no")


def load_secrets(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not data.get("boards") or not data.get("host_token"):
        raise SystemExit(f"{path}: needs \"boards\" and \"host_token\"")
    return data


async def run(relay: Relay, host: str, port: int) -> None:
    # A board that loses power says no goodbye: only an unanswered ping shows
    # it, so the relay asks often. The board answers from inside its REPL
    # machinery, and a dozing one answers late, hence the patience.
    async with serve(relay.handler, host, port, process_request=relay.process_request,
                     ping_interval=15, ping_timeout=25, max_size=1 << 20):
        log.info("board relay listening on %s:%s for %s", host, port,
                 ", ".join(sorted(relay.boards)) or "nobody")
        await asyncio.Future()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--secrets", default=str(
        Path(__file__).resolve().parent.parent / "secrets" / "board_relay.json"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s: %(message)s")
    asyncio.run(run(Relay(load_secrets(Path(args.secrets))), args.host, args.port))


if __name__ == "__main__":
    main()
