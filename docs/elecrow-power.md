# The board on a battery

How Nova's board saves power, how it sleeps, and how it comes back. The code is
`elecrow-firmware/nova-runtime/lib/nova/power.py` on the board and three tools in
`tools/elecrow_mcp_server.py` on the host.

**Status: running on the board since 12 October 2026.** Dozing, deep sleep, the check-in
and waking over Wi-Fi were tried on the board itself (see "What was checked"). Every
current and every runtime below is still an estimate: no meter has been on the cable.

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
| **doze** | after 30 s without a touch, a press or a call the backlight dims; after 2 min it goes out, the processor drops from 240 to 80 MHz and the Wi-Fi radio naps between beacons | yes, a call takes a moment longer | 50–70 mA |
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
If the host calls in that window she answers "restarting" and boots properly, which
takes about ten seconds. If nobody calls, she sleeps again.

Why a boot and not simply staying up: a check-in leaves the panel supply off, and the SD
card with her apps hangs on that supply. Switching it on with the radio already running
browns the chip out (seen on the board: a power-on reset fifteen seconds into the
check-in). An ordinary boot brings the panel up first and the radio after it.

`device_wake` does the calling: it knocks every three seconds in the background for up
to six minutes, which is long enough to catch one check-in. With a one-minute check-in
the board answered 71 to 88 seconds after it was sent to sleep.

A check-in costs roughly half a minute awake out of every five. With the defaults that
is an average of about 10 mA, so a 310 mAh cell lasts on the order of a day asleep, and
a 2000 mAh cell about a week. Longer intervals last longer and answer later.

## The button

One button does three things:

| Press | What it does |
|---|---|
| short | turns the volume one step |
| short, while she dozes | wakes her, and leaves the volume alone |
| held for 5 seconds | sends her to sleep; the screen says so, and she sleeps once the button is let go |
| while she sleeps | wakes her |

That is the AI button (BOOT on the board). RESET restarts her from any state.

## How long a battery lasts: the runtime log

The board cannot measure its battery, so the way to find out is to run one empty and
read afterwards how long that took. `power.runtime()` keeps the count: minutes awake,
dimmed, dozing, in check-ins and asleep since power came on. Deep sleep does not end a
run; losing power does. After a battery has run empty and she is plugged in again, the
first entry under `previous` is how long it lasted. The log is written to the flash
once a minute, so the last minute before the end can be missing.

`power.runtime_reset()` starts the count afresh, for a clean test.

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
power.status()             # mode, idle seconds, clock, why she woke, battery, runtime
power.runtime()            # minutes by state, this run and the ones before
power.brightness(60)       # the backlight while awake
power.sleep()              # deep sleep with check-ins
power.sleep(0)             # deep sleep, the button only
power.charge()             # sleep so the charger can finish
power.configure(sleep_after_min=15)
```

## From the host

| Tool | What it does |
|---|---|
| `device_power` | her power state and the runtime log; `reset_runtime=true` starts the log afresh; looking does not wake her |
| `device_sleep` | deep sleep; `charging=true` for the charge mode; needs `confirm=true` |
| `device_wake` | bring her back: a reset over the cable, or knocking over Wi-Fi |

Every other call to the board counts as activity, and every session begins by saying
so. That is not only a courtesy: dozing, she runs at a third of her clock, and a file
upload at that speed lost bytes.

## Installing it

The runtime on the board has to be replaced over the cable, and the board server on
the PC restarted so that it knows the new tools:

```bash
python tools/elecrow_device_admin.py install-runtime --yes
```

The installer counts the bytes of every file on the board afterwards. It did not always:
once every chunk answered "ok" and `nova/__init__.py` was still empty, and the board then
booted without Wi-Fi.

## What was checked on the board

On 12 October 2026, on USB power, without a battery attached:

| | Result |
|---|---|
| Boot with the new runtime | Wi-Fi up after 7 s, SD card mounted, 240 MHz |
| Dim, then doze | at the configured seconds; the clock reads 80 MHz in doze |
| A call over Wi-Fi while dozing | gets through; the call wakes her |
| Deep sleep | no answer on the cable and none on Wi-Fi |
| Check-in after one minute | answered a knock over Wi-Fi 71 s after falling asleep |
| After the answered check-in | ordinary boot, SD card and apps back, Wi-Fi up |
| From the server, the way Nova calls | `device_power` answers over Wi-Fi |

## What still has to be checked

- the real draw in each state, with a meter;
- that the button ends deep sleep and she boots her program, not the bootloader;
- that the PWR lamp really stays off while she sleeps;
- everything above again on the battery, where the supply is weaker than USB;
- the charge current with this cell.

## When she is not reachable over Wi-Fi

- **Is she asleep?** Then only `device_wake`, the cable or the button brings her back.
- **Did she boot without a network?** The old runtime tried the Wi-Fi once per boot and
  stayed offline if that failed. Now she tries again every minute.
- **Is the supply weak?** A boot that finds neither the SD card nor the network points at
  power, not at the Wi-Fi.
