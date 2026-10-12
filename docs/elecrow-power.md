# The board on a battery

How Nova's board saves power, how it sleeps, and how it comes back. The code is
`elecrow-firmware/nova-runtime/lib/nova/power.py` on the board and three tools in
`tools/elecrow_mcp_server.py` on the host.

**Status: written and tested off the board, not yet measured on it.** Every current
and every runtime below is an estimate until a USB meter has been on the cable.

## What the hardware gives us

From Elecrow's data sheet for the board (SAD00006D):

| | |
|---|---|
| Battery connector | JST-SH, 1.0 mm pitch, 2 pins, for one 3.7 V lithium cell |
| Charger | TP4059, fed from USB-C; the CHG lamp is red while charging, green when full |
| Draw at 5 V, camera off | 123 mA (0.6 W) with Elecrow's own firmware |
| Draw at 5 V, camera on | 134 mA, 158 mA with the fill light |
| Extra draw while charging | 86 to 137 mA |

Two things the board cannot do:

- **It cannot measure its battery.** No pin is connected to the cell or to the charger.
  It does not know how full the battery is, or whether it is charging.
- **It cannot set the charge current.** That is a resistor on the board.

So the software can do exactly two things for the battery: draw less, and sleep.

## Three states

| State | What happens | Reachable? | Estimated draw |
|---|---|---|---|
| **awake** | everything on | yes | 120–160 mA |
| **doze** | after 30 s without a touch, a press or a call the backlight dims; after 2 min it goes out, the processor drops to 80 MHz and the Wi-Fi radio naps between beacons | yes, a call takes a moment longer | 50–70 mA |
| **sleep** | deep sleep: radio off, panel off | only at a check-in, or by the button | a few mA |

Music keeps the processor at full speed for as long as it plays; the screen may still
go dark underneath it. Nothing sends her into deep sleep on its own unless
`sleep_after_min` is set.

Any touch, any press of the button and any call from the host ends a doze. A press
that wakes her does not also turn the volume.

## Coming back from deep sleep

Deep sleep ends in a reboot. `nova.start()` runs again and joins the Wi-Fi exactly as
it does at power-up, so there is no separate "reconnect".

| What wakes her | How fast | What she does |
|---|---|---|
| The button | at once | an ordinary boot, with the screen |
| The host, over the cable | at once | the host resets her through the cable (`device_wake`) |
| The host, over Wi-Fi | at her next check-in | see below |
| A touch | at once, if `touch_wakes` is on | costs power: the panel stays powered while she sleeps |

**The check-in** is how she can be woken over the air. Asleep, her radio is off and
nobody can call her. So every `checkin_min` minutes (5 by default) a timer wakes her
with the screen left dark. She joins the Wi-Fi and listens for `listen_s` seconds (25).
If the host calls in that window she stays awake. If nobody calls, she sleeps again.

`device_wake` does the calling: it knocks every three seconds in the background for up
to six minutes, which is long enough to catch one check-in.

A check-in costs roughly half a minute awake out of every five. With the defaults that
is an average of about 10 mA, so a 310 mAh cell lasts on the order of a day asleep, and
a 2000 mAh cell about a week. Longer intervals last longer and answer later.

## Charging

Because the board cannot see the charger, `charge()` is simply a sleep with a note on
the screen. With her drawing nothing, the charger can finish and the lamp can turn
green. The button wakes her; so does a check-in, every 15 minutes.

Before trusting the charger with a small cell, measure it once: a USB meter between
supply and board, once with the battery and once without. The difference is the charge
current. It should be no more than the cell's capacity in mA (310 mA for a 310 mAh
cell); half that is kinder.

## A fuel gauge, if you add one

A MAX17048 on the reserved I2C pads (address `0x36`) is found on its own. Then
`power.battery()` returns volts and percent, `device_power` shows them, and below
`low_battery_percent` (5) she puts herself to sleep without check-ins so the cell is
not drained flat.

## Settings

`/nova_power.json` on the board; change it with `power.configure(...)`.

| Setting | Default | Meaning |
|---|---|---|
| `dim_after_s` | 30 | idle seconds until the backlight dims |
| `dim_percent` | 20 | how dim |
| `off_after_s` | 120 | idle seconds until the backlight goes out and she dozes |
| `idle_mhz` | 80 | processor clock while dozing |
| `sleep_after_min` | 0 | minutes of doze until deep sleep; 0 = never on her own |
| `checkin_min` | 5 | minutes between check-ins while asleep; 0 = the button only |
| `listen_s` | 25 | how long one check-in listens |
| `touch_wakes` | false | keep the panel powered so a touch ends deep sleep |
| `low_battery_percent` | 5 | with a fuel gauge: sleep below this |

## On the board

```python
from nova import power
power.status()             # mode, idle seconds, clock, why she woke, battery
power.brightness(60)       # the backlight while awake
power.sleep()              # deep sleep with check-ins
power.sleep(0)             # deep sleep, the button only
power.charge()             # sleep so the charger can finish
power.configure(sleep_after_min=15)
```

## From the host

| Tool | What it does |
|---|---|
| `device_power` | her power state; looking does not wake her |
| `device_sleep` | deep sleep; `charging=true` for the charge mode; needs `confirm=true` |
| `device_wake` | bring her back: a reset over the cable, or knocking over Wi-Fi |

Every other call to the board counts as activity: it ends a doze and turns a check-in
into staying awake.

## Installing it

The runtime on the board has to be replaced over the cable, and the board server on
the PC restarted so that it knows the new tools:

```bash
python tools/elecrow_device_admin.py install-runtime --yes
```

## What still has to be checked on the board

- the real draw in each state, with a meter;
- that the button ends deep sleep and she boots her program, not the bootloader;
- that the panel supply and the PWR lamp really stay off while she sleeps;
- that a call over Wi-Fi gets through while the radio naps;
- that a check-in is reached by `device_wake` within its window;
- the charge current with this cell.
