# Nova runtime for the Elecrow board - the fixed layer under Nova's apps.
#
# Boot order (Rook's, board_io.h): AW9523 (panel power) -> display -> SD ->
# Wi-Fi -> safe-mode decision -> autostart app. Everything below the apps is
# installed by the host's device_install_runtime and is not Nova's to change.
#
# Safe mode - no autostart - when any of these holds:
#   * a finger is on the screen at power-up,
#   * the file /safe exists,
#   * the autostart app failed to get past its first 20 s three boots running.
import json
import os
import sys
import time

from machine import I2C, Pin

from . import board as B
from . import sd

VERSION = "0.1.0"
STATE = "/nova_state.json"
MAX_BOOT_FAILS = 3
LOG_MAX = 32 * 1024

safe_mode = False
safe_reason = None

_i2c = None
_io = None
_display = None
_touch = None
_timer = None


def i2c():
    global _i2c
    if _i2c is None:
        _i2c = I2C(0, scl=Pin(B.I2C_SCL), sda=Pin(B.I2C_SDA), freq=B.I2C_HZ)
    return _i2c


def io():
    """The AW9523, powered up; None when it does not answer."""
    global _io
    if _io is None:
        from .aw9523 import AW9523
        chip = AW9523(i2c())
        if chip.begin():
            _io = chip
            time.sleep_ms(50)          # let the panel supply settle (Rook)
    return _io


def display():
    """Her screen. The module behind it is `screen.py`, not `display.py`: a
    submodule of the same name as an accessor *replaces* that accessor the
    moment it is imported, and `nova.display()` then raises "'module' object
    isn't callable" (which is what ate the boot animation on 2026-09-12)."""
    global _display
    if _display is None:
        if io() is None:
            raise OSError("AW9523 not found - the panel has no power")
        from .screen import Display
        _display = Display()
        _io.backlight(100)
    return _display


def display_if_ready():
    return _display


def touch():
    global _touch
    if _touch is None:
        from .touchpad import Touch
        _touch = Touch(i2c())
    return _touch


def _log_path():
    return (sd.MOUNT if sd.status() == "mounted" else "") + "/nova.log"


def log(msg):
    path = _log_path()
    try:
        if os.stat(path)[6] > LOG_MAX:
            try:
                os.remove(path + ".1")
            except OSError:
                pass
            os.rename(path, path + ".1")
    except OSError:
        pass
    try:
        with open(path, "a") as f:
            f.write("%d %s\n" % (time.time(), msg))
    except OSError:
        pass


def state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"autostart": None, "boot_fails": 0}


def save_state(data):
    with open(STATE, "w") as f:
        json.dump(data, f)


def _clear_fails(_t=None):
    s = state()
    if s.get("boot_fails"):
        s["boot_fails"] = 0
        save_state(s)


def info():
    from . import net
    s = state()
    return {"runtime": VERSION, "safe_mode": safe_mode, "safe_reason": safe_reason,
            "autostart": s.get("autostart"), "boot_fails": s.get("boot_fails", 0),
            "sd": sd.status(), "sd_retries": sd.retries(), "io": _io is not None,
            "display": _display is not None, "net": net.status(),
            "power": _power_brief()}


def _power_brief():
    try:
        from . import power
        s = power.status()
        return {"mode": s["mode"], "woke": s["woke"], "battery": s["battery"]}
    except Exception:
        return None


def _splash(lines):
    """Her start-up: the animation, then the same status lines under it.

    The animation is hers to look at and never load-bearing: if anything in it
    raises, the status still gets drawn and the boot carries on."""
    try:
        d = display()
    except OSError:
        return
    from .screen import WHITE, rgb
    try:
        from . import boot_anim
        boot_anim.play(d, io(), touch())
    except Exception as e:                 # an animation may not cost her a boot
        log("boot animation failed: %r" % e)
        d.fill(rgb(6, 4, 16))
        d.text("Nova", 8, 10, rgb(255, 120, 200), scale=3)
    for i, line in enumerate(lines):
        d.text(line, 8, 242 + i * 10, WHITE)
    d.show()


def _checkin():
    """A timer woke her from deep sleep: join the Wi-Fi with the screen left
    dark and listen for a call. `power` sends her back to sleep if none comes;
    a call (it pokes) turns this into being awake. No splash, no autostart.

    The panel supply stays off, and the SD card hangs on it (measured
    2026-10-12: mounting here times out). An answered check-in ends in an
    ordinary boot (power.poke), which brings both back in the right order."""
    from . import net
    up = net.connect()
    if up:
        try:
            from . import remote
            remote.start()
        except Exception:
            pass
    log("check-in net=%s" % up)
    if not up:                         # no network, nobody can call: back to sleep
        from . import power
        power.sleep(delay_s=1)


def start():
    global safe_mode, safe_reason, _timer
    from . import power
    if power.begin() == "timer":
        return _checkin()
    io()
    sd.mount()
    from . import apps, net
    apps.ensure_root()
    s = state()
    if touch().read() is not None:
        safe_mode, safe_reason = True, "touch held at boot"
    else:
        try:
            os.stat("/safe")
            safe_mode, safe_reason = True, "/safe exists"
        except OSError:
            if s.get("boot_fails", 0) >= MAX_BOOT_FAILS:
                safe_mode, safe_reason = True, "autostart failed %d boots" % s["boot_fails"]
    _splash(["runtime " + VERSION,
             "SAFE MODE: " + safe_reason if safe_mode else "ok",
             "apps: " + apps.root(),
             "sd: " + sd.status()])
    net.connect()
    try:
        from . import remote
        remote.start()
    except Exception:
        log("WLAN console unavailable; USB remains available")
    log("boot runtime=%s safe=%s sd=%s net=%s" % (VERSION, safe_mode, sd.status(),
                                                  net.status()["connected"]))
    try:                               # the button is the volume knob
        from . import music
        music.knob()
    except Exception as e:
        log("volume knob not wired: %r" % (e,))
    name = s.get("autostart")
    if safe_mode or not name:
        return
    s["boot_fails"] = s.get("boot_fails", 0) + 1
    save_state(s)
    try:
        from machine import Timer
        _timer = Timer(0)
        _timer.init(mode=Timer.ONE_SHOT, period=20000, callback=_clear_fails)
    except Exception:
        pass
    try:
        apps.run(name)
        _clear_fails()                 # it finished on its own: not a crash
    except Exception as e:
        _clear_fails()                 # an exception is logged, not a boot loop
        log("autostart %s failed: %r" % (name, e))
        sys.print_exception(e)
