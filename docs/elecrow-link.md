# The board away from home

How Nova reaches her board when it is not on the home network: on a phone's hotspot, in
a bag, anywhere with Wi-Fi and internet. The code is `nova/link.py` on the board,
`tools/board_relay.py` on the always-on host and `tools/elecrow_link.py` in the board
server.

## The problem

At home the board server calls the board. Away from home it cannot. A phone's hotspot
lets connections out and none in, and the board has a different address there anyway.

## The answer: the board calls

The board dials out to a **relay** and keeps that line open. The board server talks to
the relay, and the relay passes the bytes through. Those bytes are the board's own raw
REPL, the same that travels over the USB cable, so every tool works over the line as it
does over the cable: apps, music, screenshots, sleep.

```
Nova ── board server ── relay ══ line the board opened ══ board
        (PC)            (always-on host)                   (anywhere)
```

The board knows two addresses for the relay and tries them in order:

1. the relay's address on the home network. At home this answers at once.
2. the public name. Away from home the first address times out after four seconds and
   this one is used. It is TLS, and the board checks the certificate against the two
   Let's Encrypt roots it carries (`nova/roots.py`). That check needs the board's clock
   to be right, which it is once it has been online.

## Who is allowed

The line is reachable from the internet, so it trusts nobody:

- The board and the relay each prove they hold the board's secret, with a fresh
  challenge in each direction, before a single REPL byte moves. A caller without the
  secret is cut off. A server that merely answers gets no REPL either.
- Only `/board/link` is open to the internet. The board server's side of the relay
  (`/board/host`, `/board/status`, `/board/wake`) answers on the local network only, wants
  the board server's token, and refuses anything the public proxy passed on.
- The secrets live in `secrets/` on the machines that need them and on the board in
  `/nova_link.json`. They are never in the repository.

## Deep sleep

A board in deep sleep is not on the line. It wakes on its timer, joins whatever Wi-Fi
it finds, calls the relay and is told in the first answer whether anybody asked for it:

- **Nobody did:** it is asleep again a few seconds later. Without the relay a check-in
  had to stay up and listen for 25 seconds; this is shorter and saves battery.
- **Somebody did** (`device_wake`): it stays up, and the line is ready.

The relay remembers what the board said when it left, so a call to a sleeping board is
answered with what is true: asleep, and due back in about so many minutes.

## What Nova sees

| | |
|---|---|
| The board is on the line | every tool works; `port` in the answer says `link` |
| It is in deep sleep | `status: asleep`, when it checks in next, and a hint to use `device_wake` |
| `device_wake` | asks the relay to keep it up at its next check-in; the answer says when that is |
| It is gone | `status: offline` and how long ago it was last heard |
| `device_power` | adds a `line` entry: where the board is, and over which kind of address |

When the relay does not know the board, the board server still tries the console on the
home network and then the cable, as before.

## One console at a time

Besides the cable, the board's REPL has one more seat. While the line is up it sits
there, and the console on the home network refuses callers. That console stays as the
way in when the relay cannot be reached. A session that is already open on it is left
alone: the line waits.

## What it costs

- **Power:** an open line keeps the radio associated. In doze the radio still naps
  between beacons; the relay pings every 30 seconds. Not measured yet.
- **Data:** what an app prints while nobody is attached is not sent. An idle line is a
  ping every 30 seconds.
- **Speed:** away from home every byte goes over the mobile network and back. Fine for
  commands; slow for uploading large sound files.

## Setting it up

On the always-on host: `secrets/board_relay.json`

```json
{"boards": {"nova": "<the board's secret>"}, "host_token": "<the board server's token>"}
```

and the service `tools/systemd/nova-board-relay.service` (port 8770).

On the public proxy, one path of her site goes to the relay; everything else stays:

```
handle /board/link {
	reverse_proxy 127.0.0.1:8767
}
```

with a second forward in the reverse tunnel (`-R 127.0.0.1:8767:127.0.0.1:8770`).

On the PC: `secrets/elecrow_link.json`

```json
{"enabled": true, "relay": "http://<always-on host>:8770", "board_id": "nova",
 "host_token": "...", "board_secret": "...",
 "urls": ["ws://<always-on host>:8770/board/link", "wss://<public name>/board/link"]}
```

Then install the runtime; the installer writes `/nova_link.json` onto the board. With
`--port wifi` it does so over the home network, for a board that is running on its
battery:

```bash
python tools/elecrow_device_admin.py install-runtime --yes
python tools/elecrow_device_admin.py --port wifi install-runtime --yes
```

## Limits

- **The board server still runs on the PC.** With the PC off, Nova cannot reach the
  board, wherever it is. Moving the board server to the always-on host removes that.
- **The board needs a Wi-Fi it knows.** It joins the networks in its own list, nothing
  else. A hotspot has to be in that list.
- **No Wi-Fi, no line.** The board has no mobile radio of its own.
