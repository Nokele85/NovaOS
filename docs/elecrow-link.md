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
        └──── all on the always-on host ────┘              (anywhere)
```

The board server runs next to her as `nova-board-server.service`, in an environment of
its own, with the sound library and her learned music beside it. The PC is not part of
the path: it can be off.

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
  between beacons; the relay pings every 15 seconds. Not measured yet.
- **Data:** what an app prints while nobody is attached is not sent. An idle line is a
  ping every 15 seconds.
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

On the always-on host, for the board server: `secrets/elecrow_link.json` with `enabled`,
`relay` (`http://127.0.0.1:8770`), `board_id` and `host_token`, and the service
`tools/systemd/nova-board-server.service` (port 8769, local only).

On the PC, for installing: `secrets/elecrow_link.json`

```json
{"enabled": true, "relay": "http://<always-on host>:8770", "board_id": "nova",
 "host_token": "...", "board_secret": "...",
 "urls": ["ws://<always-on host>:8770/board/link", "wss://<public name>/board/link"]}
```

Then install the runtime; the installer writes `/nova_link.json` onto the board. With
`--port link` it does so through the relay, wherever the board is; with `--port wifi`
over the console on the home network, which only answers while the line is down:

```bash
python tools/elecrow_device_admin.py install-runtime --yes
python tools/elecrow_device_admin.py --port link install-runtime --yes
python tools/elecrow_device_admin.py --port wifi install-runtime --yes
```

## What was checked on the board

On 12 October 2026, on battery, at home:

| | Result |
|---|---|
| Boot | dials the relay on the home network, on the line 7 s after power-up |
| A call through the relay | answered in 0.6 s |
| Installing the whole runtime through the relay | every file arrived complete |
| Forced onto the public address | TLS with the checked certificate; on the line as `public` |
| A call over the public line | 0.7 s; 3000 bytes back and 1500 bytes up, complete |
| Back to both addresses | on the line as `lan` again |

Not checked yet: a real hotspot, and how much battery an open line costs.

## Limits

- **The cable stays at the PC.** Flashing firmware and installing over USB are done
  there, with `tools/elecrow_device_admin.py`. Over the line the installer works from
  the PC too (`--port link`).
- **New sounds and learned music live on the always-on host.** Its copies under
  `elecrow-firmware/sound-library` and `music-library` are the ones in use; a library
  built on the PC has to be copied over.
- **The board needs a Wi-Fi it knows.** It joins the networks in its own list, nothing
  else. A hotspot has to be in that list.
- **No Wi-Fi, no line.** The board has no mobile radio of its own.
