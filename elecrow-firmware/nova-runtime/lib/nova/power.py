# Power: how long she lasts on a battery, and how she comes back from sleep.
#
# The board has no pin on the battery and none on the charger (Elecrow's data
# sheet: the CHG lamp hangs on the TP4059 itself), so nothing here can know
# that a battery is attached or that it is charging. What it can do is draw
# less, and sleep. Three states:
#
#   awake   everything on.
#   doze    after a while without a touch, a press or a call from the host:
#           the backlight dims and then goes out, the processor slows down,
#           the Wi-Fi radio naps between beacons. She stays reachable; any
#           touch, press or call brings everything back.
#   sleep   deep sleep, on request (or after `sleep_after_min` if set). The
#           radio is off, so nobody can call her. She comes back on the
#           button, and on a timer: every `checkin_min` minutes she wakes
#           without the screen, joins the Wi-Fi, listens `listen_s` seconds
#           for a call and sleeps again if none comes. That window is how the
#           host wakes her over the air. Over the cable the host resets her.
#
# Waking from deep sleep is a reboot: `nova.start()` runs again and joins the
# Wi-Fi the way it does at power-up, so there is nothing to "reconnect".
#
# Status: written from the data sheet and from MicroPython's documented
# behaviour on the ESP32-S3; NOT yet measured on the board. The numbers in
# docs/elecrow-power.md are estimates until a meter has been on the cable.
import json
import sys
import time

CONFIG = "/nova_power.json"
DEFAULTS = {
    "dim_after_s": 30,        # no touch, press or call for this long: dim
    "dim_percent": 20,
    "off_after_s": 120,       # ...and for this long: backlight out, doze
    "idle_mhz": 80,           # processor clock while dozing
    "sleep_after_min": 0,     # deep sleep after this much doze; 0 = never
    "checkin_min": 5,         # in deep sleep, wake this often to listen; 0 = never
    "listen_s": 25,           # how long one check-in listens for a call
    "touch_wakes": False,     # keep the panel powered so a touch ends deep sleep
    "low_battery_percent": 5,     # with a fuel gauge fitted: sleep below this
}
GAUGE_ADDR = 0x36             # MAX17048 on the shared I2C bus, if one is fitted

try:
    _ticks, _diff = time.ticks_ms, time.ticks_diff
except AttributeError:        # CPython: the host's tests load this file
    def _ticks():
        return int(time.monotonic() * 1000)

    def _diff(a, b):
        return a - b

_cfg = None
_last = 0                     # the last touch, press or call
_mode = "awake"               # awake | dim | doze
_bright = 100                 # what "on" means for the backlight
_busy_hz = None               # the clock she booted with
_timer = None
_woke = None                  # None | "timer" | "button" | "touch"
_checkin = False              # this boot is a check-in nobody has answered yet
_restart_at = None            # an answered check-in: an ordinary boot follows
_sleep_at = None              # when a sleep was requested
_sleep_delay = 0
_sleep_args = None
_net_tries = 0
_gauge = None                 # None = not looked yet, False = none fitted
_held = 0                     # seconds the button has been down
_run = None                   # the runtime log, see runtime()
_run_ticks = 0

RUNLOG = "/nova_runtime.json"     # on the flash: the SD card is off while she sleeps
HOLD_S = 5                    # the button held this long sends her to sleep
RUN_KEYS = ("awake_s", "dim_s", "doze_s", "checkin_s", "sleep_s")


def _nova():
    import nova
    return nova


def config():
    """Her power settings: the defaults, changed by /nova_power.json."""
    global _cfg
    if _cfg is None:
        _cfg = dict(DEFAULTS)
        try:
            with open(CONFIG) as f:
                for key, value in json.load(f).items():
                    if key in DEFAULTS:
                        _cfg[key] = value
        except (OSError, ValueError):
            pass
    return _cfg


def configure(**changes):
    """Change power settings and keep them across reboots. Unknown names are
    refused, so a typo cannot quietly do nothing."""
    cfg = config()
    for key in changes:
        if key not in DEFAULTS:
            raise ValueError("no power setting called %r; there are: %s"
                             % (key, ", ".join(sorted(DEFAULTS))))
    for key, value in changes.items():
        cfg[key] = bool(value) if isinstance(DEFAULTS[key], bool) else max(0, int(value))
    with open(CONFIG, "w") as f:
        json.dump(cfg, f)
    return cfg


def woke():
    """What ended the last deep sleep: "timer", "button", "touch", or None
    after an ordinary power-up or reset."""
    return _woke


def _read_wake():
    try:
        import machine
        if machine.reset_cause() != machine.DEEPSLEEP_RESET:
            return None
        reason = machine.wake_reason()
        if reason == getattr(machine, "TIMER_WAKE", 4):
            return "timer"
        if reason == getattr(machine, "EXT0_WAKE", 2):
            return "button"
        if reason == getattr(machine, "EXT1_WAKE", 3):
            return "touch"
        return "button"       # a deep sleep ended and it was not the clock
    except Exception:
        return None


def begin():
    """Called once by nova.start(): note why she is awake and start watching
    for idleness. Returns woke()."""
    global _woke, _last, _busy_hz, _timer, _checkin
    _woke = _read_wake()
    _checkin = _woke == "timer"
    _last = _ticks()
    try:
        _run_begin()
    except Exception as e:
        _nova().log("power: no runtime log: %r" % (e,))
    try:
        import machine
        _busy_hz = machine.freq()
        _timer = machine.Timer(1)
        _timer.init(mode=machine.Timer.PERIODIC, period=1000, callback=_tick)
    except Exception as e:
        _nova().log("power: no idle watch: %r" % (e,))
    return _woke


def poke():
    """Something happened - a touch, a press, a call from the host. Wakes her
    from doze. During a check-in it answers "restarting": she boots properly
    and is back about ten seconds later."""
    global _last, _checkin, _restart_at
    _last = _ticks()
    if _checkin:
        # Somebody answered the check-in. What it left off - the panel supply,
        # and with it the SD card and her apps - comes back through an ordinary
        # boot, a moment from now. Switching the panel on here, with the radio
        # already running, browns the chip out (measured 2026-10-12: a
        # power-on reset fifteen seconds into the check-in).
        _checkin = False
        _restart_at = _ticks()
        return "restarting"
    if _restart_at is not None:
        return "restarting"
    if _mode != "awake":
        _wake()
    return _mode


def poke_from_press():
    """The button: True when the press woke her from doze (and so should not
    also turn the volume), False when she was awake already."""
    was = _mode
    poke()
    return was == "doze"


def brightness(percent=None):
    """The backlight while she is awake, 5-100. Without a number: what it is."""
    global _bright
    if percent is not None:
        _bright = max(5, min(100, int(percent)))
        if _mode == "awake":
            _light(_bright)
    return _bright


def _light(percent):
    nova = _nova()
    if nova.display_if_ready() is None:
        return                # no screen was started: leave the panel dark
    chip = nova.io()
    if chip is not None:
        chip.backlight(percent)


def _clock(hz):
    try:
        import machine
        if hz and machine.freq() != hz:
            machine.freq(hz)
    except Exception:
        pass


def _radio_nap(on):
    try:
        from nova import net
        net.power_save(on)
    except Exception:
        pass


def _busy():
    """True while she is playing: music needs the full clock, and a piece that
    runs is a reason not to fall asleep."""
    music = sys.modules.get("nova.music")
    if music is None:
        return False
    try:
        return bool(music._now.get("playing")) or music._amb is not None
    except Exception:
        return False


def _wake():
    global _mode
    _clock(_busy_hz)
    _radio_nap(False)
    _nova().io()              # after a check-in the panel supply is still off
    _light(_bright)
    _mode = "awake"


def _touched():
    try:
        return _nova().touch().read() is not None
    except Exception:
        return False


# --- the runtime log ---------------------------------------------------------
#
# The board cannot measure its battery, so the only way to learn how long one
# lasts is to run it empty and read afterwards how long that took. One "run" is
# the time between two power-ups: seconds spent awake, dimmed, dozing, in
# check-ins and asleep. Deep sleep does not end a run; losing power does.

def _run_load():
    try:
        with open(RUNLOG) as f:
            data = json.load(f)
        if isinstance(data.get("current"), dict) and isinstance(data.get("previous"), list):
            return data
    except (OSError, ValueError):
        pass
    return {"current": None, "previous": []}


def _run_save():
    if _run is None:
        return
    try:
        with open(RUNLOG, "w") as f:
            json.dump(_run, f)
    except OSError:
        pass


def _run_begin():
    global _run
    data = _run_load()
    cur = data["current"]
    now = time.time()
    if _woke is not None and cur:
        # back from deep sleep: the same run. The clock kept going, so the time
        # asleep is simply what is missing.
        if cur.get("slept_at") is not None:
            cur["sleep_s"] += max(0, now - cur["slept_at"])
            cur["slept_at"] = None
        cur["checkins" if _woke == "timer" else "wakes"] += 1
    else:
        if cur:
            data["previous"] = [cur] + data["previous"][:3]
        cur = {"started": now, "last": now, "slept_at": None,
               "sleeps": 0, "checkins": 0, "wakes": 0}
        for key in RUN_KEYS:
            cur[key] = 0
        data["current"] = cur
    _run = data
    _run_save()


def _run_count():
    """One second into the state she is in; written out once a minute."""
    global _run_ticks
    if _run is None:
        return
    cur = _run["current"]
    key = "checkin_s" if (_checkin or _restart_at is not None) else _mode + "_s"
    cur[key] += 1
    cur["last"] = time.time()
    _run_ticks += 1
    if _run_ticks >= 60:
        _run_ticks = 0
        _run_save()


def _run_view(run):
    total = sum(run[key] for key in RUN_KEYS)
    out = {"total_min": round(total / 60, 1)}
    for key in RUN_KEYS:
        out[key[:-2] + "_min"] = round(run[key] / 60, 1)
    out["sleeps"] = run["sleeps"]
    out["checkins"] = run["checkins"]
    return out


def runtime():
    """How long she has been running since power came on, split by state, and
    the runs before this one. Run a battery empty, plug her in again, and the
    first entry under "previous" is how long that battery lasted."""
    if _run is None:
        return {"current": None, "previous": []}
    return {"current": _run_view(_run["current"]),
            "previous": [_run_view(r) for r in _run["previous"]]}


def runtime_reset():
    """Forget the earlier runs and start counting this one from now."""
    global _run
    cur = {"started": time.time(), "last": time.time(), "slept_at": None,
           "sleeps": 0, "checkins": 0, "wakes": 0}
    for key in RUN_KEYS:
        cur[key] = 0
    _run = {"current": cur, "previous": []}
    _run_save()
    return runtime()


# --- the button, held --------------------------------------------------------

def _button_down():
    try:
        import machine
        # no mode given: this reads the pin the volume knob already owns
        return machine.Pin(_nova().board.BUTTON).value() == 0
    except Exception:
        return False


def _hold():
    """The button held HOLD_S seconds sends her to sleep. The press that began
    the hold also turned the volume one step; that is taken back."""
    global _held
    if not _button_down():
        _held = 0
        return False
    _held += 1
    if _held < HOLD_S:
        return False
    _held = 0
    music = sys.modules.get("nova.music")
    if music is not None:
        try:
            if music._before is not None:
                music.volume(music._before)
        except Exception:
            pass
    nova = _nova()
    d = nova.display_if_ready()
    if d is not None:
        try:
            from nova.screen import WHITE, rgb
            _light(_bright)
            d.fill(rgb(6, 4, 16))
            d.text("Schlaf", 8, 100, WHITE, scale=3)
            d.text("Taste loslassen", 8, 140, WHITE)
            d.text("Taste weckt wieder", 8, 156, WHITE)
            d.show()
        except Exception:
            pass
    sleep(delay_s=2)
    return True


def _tick(_t=None):
    try:
        _step()
    except Exception as e:    # a fault here must never stop the board
        try:
            _nova().log("power: %r" % (e,))
        except Exception:
            pass


def _step():
    global _mode, _sleep_at, _net_tries, _last
    cfg = config()
    now = _ticks()
    _run_count()
    if _restart_at is not None:
        if _diff(now, _restart_at) >= 1500:    # the call that woke her has its answer
            import machine
            _nova().log("power: check-in answered, booting")
            machine.reset()
        return
    if _sleep_at is not None:
        if _diff(now, _sleep_at) >= _sleep_delay:
            _sleep_at = None
            _go(*_sleep_args)
        return
    if _hold():
        return
    if _mode != "awake" and _touched():
        poke()
        return
    busy = _busy()
    idle = _diff(now, _last) // 1000
    if _checkin:
        if idle >= cfg["listen_s"]:
            _go(cfg["checkin_min"], "nobody called")
        return
    if _mode == "awake" and idle >= cfg["dim_after_s"]:
        _light(cfg["dim_percent"])
        _mode = "dim"
    if _mode == "dim" and idle >= cfg["off_after_s"]:
        _light(0)
        _radio_nap(True)
        _mode = "doze"
    _net_tries += 1
    if _net_tries >= 60 and not busy:
        # once a minute, in every state: is the Wi-Fi still there? A boot that
        # found no network used to stay offline until the next power-up.
        _net_tries = 0
        try:
            from nova import net
            net.ensure()
        except Exception:
            pass
    if _mode == "doze":
        _clock(_busy_hz if busy else cfg["idle_mhz"] * 1000000)
        if not busy:
            level = battery()
            if level and level["percent"] <= cfg["low_battery_percent"]:
                _go(0, "battery at %d %%" % level["percent"])
            elif cfg["sleep_after_min"] and idle >= cfg["sleep_after_min"] * 60:
                _go(cfg["checkin_min"], "idle %d min" % (idle // 60))


def sleep(checkin_min=None, delay_s=2):
    """Deep sleep. She wakes on the button, and every `checkin_min` minutes to
    listen for a call (0 = the button only; None = the configured value).
    The sleep starts `delay_s` seconds from now, so this call still answers."""
    global _sleep_at, _sleep_args, _sleep_delay
    cfg = config()
    minutes = cfg["checkin_min"] if checkin_min is None else max(0, int(checkin_min))
    _sleep_args = (minutes, "asked")
    _sleep_at = _ticks()
    _sleep_delay = max(1, int(delay_s)) * 1000
    wakes = ["button"]
    if cfg["touch_wakes"]:
        wakes.append("touch")
    if minutes:
        wakes.append("check-in every %d min, listening %d s" % (minutes, cfg["listen_s"]))
    return {"sleeping_in_s": max(1, int(delay_s)), "wakes_on": wakes}


def charge(checkin_min=15):
    """Sleep so the charger can finish: with her drawing nothing the charge
    lamp can turn green. Wakes on the button, or at a check-in."""
    nova = _nova()
    d = nova.display_if_ready()
    if d is not None:
        try:
            from nova.screen import WHITE, rgb
            d.fill(rgb(6, 4, 16))
            d.text("Laden", 8, 100, WHITE, scale=3)
            d.text("Taste weckt", 8, 140, WHITE)
            d.show()
        except Exception:
            pass
    return sleep(checkin_min)


def _go(checkin_min, why):
    """Into deep sleep, now. Everything that draws current goes first."""
    import machine
    nova = _nova()
    cfg = config()
    nova.log("power: sleep (%s), check-in %s min" % (why, checkin_min))
    if _timer is not None:
        _timer.deinit()
    music = sys.modules.get("nova.music")
    if music is not None:
        try:
            music.stop()
            music.ambience("off")
        except Exception:
            pass
    cam = sys.modules.get("nova.cam")
    if cam is not None:
        try:
            cam.close()
        except Exception:
            pass
    B = nova.board
    chip = nova.io()
    if chip is not None:
        try:
            chip.mute(True)
            chip.flash(False)
            chip.rgb(0, 0, 0)
            chip.backlight(0)
            if not cfg["touch_wakes"]:
                # the AW9523 keeps its outputs through the ESP32's sleep, so
                # the panel supply (and the red PWR lamp on it) stays off
                chip._bit(B.AW_POWER, False)
        except Exception:
            pass
    try:
        from nova import net
        net.off()
    except Exception:
        pass
    try:
        import esp32
        esp32.wake_on_ext0(pin=machine.Pin(B.BUTTON, machine.Pin.IN, machine.Pin.PULL_UP),
                           level=esp32.WAKEUP_ALL_LOW)
        if cfg["touch_wakes"]:
            esp32.wake_on_ext1(pins=(machine.Pin(B.TOUCH_INT, machine.Pin.IN,
                                                 machine.Pin.PULL_UP),),
                               level=esp32.WAKEUP_ALL_LOW)
    except Exception as e:
        nova.log("power: button wake not armed: %r" % (e,))
    # a finger still on the button would wake her the instant she slept
    for _ in range(200):
        if not _button_down():
            break
        time.sleep(0.05)
    if _run is not None:
        _run["current"]["sleeps"] += 1
        _run["current"]["slept_at"] = time.time()
        _run_save()
    if checkin_min:
        machine.deepsleep(int(checkin_min) * 60000)
    else:
        machine.deepsleep()


def battery():
    """{"volts", "percent"} from a MAX17048 fuel gauge on the I2C pads, or
    None: the board itself cannot measure its battery."""
    global _gauge
    if _gauge is False:
        return None
    try:
        bus = _nova().i2c()
        v = bus.readfrom_mem(GAUGE_ADDR, 0x02, 2)
        s = bus.readfrom_mem(GAUGE_ADDR, 0x04, 2)
    except Exception:
        if _gauge is None:
            _gauge = False    # none answered at the first look: stop asking
        return None
    _gauge = True
    return {"volts": round(((v[0] << 8) | v[1]) * 78.125e-6, 3),
            "percent": min(100, int(s[0] + s[1] / 256 + 0.5))}


def status():
    """Her power state in one look: mode, idle seconds, clock, why she woke,
    the settings, and the battery when a fuel gauge is fitted."""
    mhz = None
    try:
        import machine
        mhz = machine.freq() // 1000000
    except Exception:
        pass
    return {"mode": "check-in" if _checkin else "restarting" if _restart_at is not None else _mode,
            "idle_s": _diff(_ticks(), _last) // 1000, "cpu_mhz": mhz,
            "brightness": _bright, "playing": _busy(), "woke": _woke,
            "battery": battery(), "runtime": runtime(), "config": config()}
