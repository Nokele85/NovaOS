# Wi-Fi from /nova_secrets.json, and clock sync.
#
# The secrets file is written by the host's device_install_runtime from its
# own secret store; nothing here prints or returns a network name or
# password. `status()` says whether she is online, not where.
#
# Rook's rule (README, "Zeit"): no timestamp before NTP. `synced` says whether
# the clock can be trusted; an app that records time must check it.
import json
import time

import network

SECRETS = "/nova_secrets.json"

_wlan = None
_synced = False
_index = None


def _networks():
    try:
        with open(SECRETS) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    return [n for n in data.get("wifi", []) if n.get("ssid")]


def connect(timeout_ms=12000):
    """Try each configured network in order; True once one is up."""
    global _wlan, _index
    _wlan = network.WLAN(network.STA_IF)
    _wlan.active(True)
    if _wlan.isconnected():
        # already up after a soft reset: the clock still has to be set, or
        # everything she writes carries a 1970 timestamp (Rook's rule)
        if not _synced:
            sync_clock()
        return True
    for i, n in enumerate(_networks()):
        try:
            _wlan.connect(n["ssid"], n.get("password", ""))
        except OSError:
            continue
        start = time.ticks_ms()
        while time.ticks_diff(time.ticks_ms(), start) < timeout_ms:
            if _wlan.isconnected():
                _index = i
                sync_clock()
                return True
            time.sleep_ms(200)
        _wlan.disconnect()
    return False


def power_save(on):
    """Let the radio nap between beacons (she stays reachable, a call takes a
    moment longer), or run it flat out. False when this firmware cannot."""
    if _wlan is None:
        return False
    try:
        _wlan.config(pm=_wlan.PM_POWERSAVE if on else _wlan.PM_PERFORMANCE)
        return True
    except (AttributeError, ValueError, OSError):
        return False


def ensure(timeout_ms=6000):
    """Join again if the connection was lost; True while she is online."""
    if _wlan is not None and _wlan.isconnected():
        return True
    return connect(timeout_ms)


def off():
    """Radio off, for deep sleep. connect() turns it back on."""
    if _wlan is None:
        return
    try:
        _wlan.disconnect()
    except OSError:
        pass
    _wlan.active(False)


def sync_clock():
    global _synced
    try:
        import ntptime
        ntptime.settime()            # UTC
        _synced = True
    except Exception:
        _synced = False
    return _synced


def online():
    return bool(_wlan and _wlan.isconnected())


def status():
    up = bool(_wlan and _wlan.isconnected())
    out = {"configured": len(_networks()), "connected": up,
           "network": (_index + 1) if up and _index is not None else None,
           "clock_synced": _synced}
    if up:
        out["ip"] = _wlan.ifconfig()[0]
        try:
            out["rssi"] = _wlan.status("rssi")
        except Exception:
            pass
    return out
