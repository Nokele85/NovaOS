"""The board's power module and the host's three power tools, without a board.

`nova/power.py` is loaded here as plain Python against a fake `machine`, a fake
`esp32` and a fake `nova`. That proves its decisions - when she dims, when a
check-in goes back to sleep, what a call from the host changes - and nothing
about milliamps: those need a meter on the cable.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "elecrow-firmware" / "nova-runtime" / "lib" / "nova"
sys.path.insert(0, str(ROOT / "tools"))


class Slept(Exception):
    """machine.deepsleep() never returns on the board; here it raises."""

    def __init__(self, ms):
        self.ms = ms


class Chip:
    def __init__(self):
        self.light, self.muted, self.panel = None, None, True
        self.lamp = None

    def backlight(self, percent):
        self.light = percent

    def mute(self, on):
        self.muted = on

    def flash(self, on):
        pass

    def rgb(self, r, g, b):
        self.lamp = (r, g, b)

    def _bit(self, pin, on):
        self.panel = on


class Bus:
    def __init__(self, gauge=None):
        self.gauge = gauge

    def readfrom_mem(self, addr, reg, n):
        if self.gauge is None:
            raise OSError("no ack")
        return self.gauge[reg]


@pytest.fixture
def rig(monkeypatch, tmp_path):
    """power.py with a clock that only moves when the test moves it."""
    spec = importlib.util.spec_from_file_location("board_power", LIB / "power.py")
    power = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(power)
    spec_b = importlib.util.spec_from_file_location("board_pins", LIB / "board.py")
    pins = importlib.util.module_from_spec(spec_b)
    spec_b.loader.exec_module(pins)

    state = types.SimpleNamespace(now=0, hz=160_000_000, reset=1, wake=0, armed=[],
                                  touch=None, radio=[], log=[], chip=Chip(),
                                  bus=Bus(), screen=object())

    def deepsleep(ms=None):
        raise Slept(ms)

    def freq(hz=None):
        if hz is None:
            return state.hz
        state.hz = hz

    class Timer:
        PERIODIC = 1

        def __init__(self, n):
            pass

        def init(self, **kw):
            pass

        def deinit(self):
            pass

    class Pin:
        IN, PULL_UP = 0, 1

        def __init__(self, n, *a):
            self.n = n

    machine = types.SimpleNamespace(
        DEEPSLEEP_RESET=4, TIMER_WAKE=4, EXT0_WAKE=2, EXT1_WAKE=3, Timer=Timer, Pin=Pin,
        reset_cause=lambda: state.reset, wake_reason=lambda: state.wake,
        freq=freq, deepsleep=deepsleep)
    esp32 = types.SimpleNamespace(
        WAKEUP_ALL_LOW=0,
        wake_on_ext0=lambda pin, level: state.armed.append(("button", pin.n)),
        wake_on_ext1=lambda pins, level: state.armed.append(("touch", pins[0].n)))
    net = types.SimpleNamespace(power_save=lambda on: state.radio.append(on),
                                ensure=lambda: True, off=lambda: state.radio.append("off"))
    nova = types.SimpleNamespace(
        board=pins, net=net, io=lambda: state.chip, i2c=lambda: state.bus,
        display_if_ready=lambda: state.screen, log=state.log.append,
        touch=lambda: types.SimpleNamespace(read=lambda: state.touch))
    for name, module in (("machine", machine), ("esp32", esp32), ("nova", nova),
                         ("nova.net", net)):
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.delitem(sys.modules, "nova.music", raising=False)
    monkeypatch.delitem(sys.modules, "nova.cam", raising=False)
    monkeypatch.setattr(power, "_ticks", lambda: state.now)
    monkeypatch.setattr(power, "CONFIG", str(tmp_path / "nova_power.json"))

    def run(seconds):
        """Let that many seconds pass, one tick each."""
        for _ in range(seconds):
            state.now += 1000
            power._step()

    return types.SimpleNamespace(power=power, state=state, run=run)


def test_she_dims_then_dozes_and_a_call_brings_her_back(rig):
    power, state = rig.power, rig.state
    assert power.begin() is None
    rig.run(29)
    assert state.chip.light is None and power.status()["mode"] == "awake"
    rig.run(1)
    assert state.chip.light == 20 and power.status()["mode"] == "dim"
    rig.run(91)
    assert state.chip.light == 0 and power.status()["mode"] == "doze"
    assert state.hz == 80_000_000 and state.radio == [True]
    assert power.poke() == "awake"
    assert state.chip.light == 100 and state.hz == 160_000_000
    assert state.radio == [True, False]


def test_a_touch_ends_the_doze(rig):
    rig.power.begin()
    rig.run(130)
    assert rig.power.status()["mode"] == "doze"
    rig.state.touch = (10, 10)
    rig.run(1)
    assert rig.power.status()["mode"] == "awake" and rig.state.chip.light == 100


def test_music_keeps_the_clock_up_while_the_screen_sleeps(rig, monkeypatch):
    monkeypatch.setitem(sys.modules, "nova.music",
                        types.SimpleNamespace(_now={"playing": True}, _amb=None))
    rig.power.begin()
    rig.run(130)
    assert rig.state.chip.light == 0                # the screen may sleep
    assert rig.state.hz == 160_000_000              # the piece keeps its clock


def test_she_never_sleeps_on_her_own_unless_told_to(rig):
    rig.power.begin()
    rig.run(3 * 3600)                               # three hours, nobody there
    assert rig.power.status()["mode"] == "doze"


def test_with_sleep_after_set_she_goes_and_arms_the_button(rig):
    rig.power.begin()
    rig.power.configure(sleep_after_min=10, checkin_min=5)
    with pytest.raises(Slept) as slept:
        rig.run(11 * 60)
    assert slept.value.ms == 5 * 60000
    assert rig.state.armed == [("button", 0)]
    chip = rig.state.chip
    assert chip.light == 0 and chip.muted is True and chip.panel is False
    assert rig.state.radio[-1] == "off"


def test_an_asked_sleep_answers_first(rig):
    rig.power.begin()
    out = rig.power.sleep(0)
    assert out["sleeping_in_s"] == 2 and out["wakes_on"] == ["button"]
    rig.run(1)                                      # the call has returned by now
    with pytest.raises(Slept) as slept:
        rig.run(2)
    assert slept.value.ms is None                   # no check-in: the button only


def test_touch_wake_keeps_the_panel_powered(rig):
    rig.power.begin()
    rig.power.configure(touch_wakes=True)
    rig.power.sleep(5, delay_s=1)
    with pytest.raises(Slept):
        rig.run(2)
    assert rig.state.chip.panel is True
    assert ("touch", 11) in rig.state.armed


def test_a_check_in_nobody_answers_goes_back_to_sleep(rig):
    rig.state.reset, rig.state.wake = 4, 4          # deep sleep, ended by the timer
    assert rig.power.begin() == "timer"
    assert rig.power.status()["mode"] == "check-in"
    with pytest.raises(Slept) as slept:
        rig.run(26)
    assert slept.value.ms == 5 * 60000
    assert rig.state.chip.light is None or rig.state.chip.light == 0


def test_a_call_during_the_check_in_keeps_her_awake(rig):
    rig.state.reset, rig.state.wake = 4, 4
    rig.power.begin()
    rig.run(10)
    rig.power.poke()                                # the host's call
    rig.run(60)                                     # no Slept: she stays
    assert rig.power.status()["mode"] in ("awake", "dim")
    assert rig.power.woke() == "timer"


def test_the_button_wake_is_an_ordinary_boot(rig):
    rig.state.reset, rig.state.wake = 4, 2
    assert rig.power.begin() == "button"
    assert rig.power.status()["mode"] == "awake"


def test_a_press_that_wakes_her_does_not_also_turn_the_volume(rig):
    rig.power.begin()
    assert rig.power.poke_from_press() is False     # awake: the press is a press
    rig.run(130)
    assert rig.power.poke_from_press() is True      # dozing: it only woke her


def test_unknown_settings_are_refused(rig):
    with pytest.raises(ValueError, match="no power setting"):
        rig.power.configure(sleep_after=10)


def test_no_fuel_gauge_means_no_battery_reading(rig):
    assert rig.power.battery() is None
    assert rig.power.status()["battery"] is None


def test_a_fuel_gauge_is_read_and_a_low_battery_puts_her_to_sleep(rig):
    # MAX17048: VCELL 0xC000 * 78.125 uV = 3.84 V; SOC 0x0380 = 3.5 %
    rig.state.bus = Bus({0x02: bytes([0xC0, 0x00]), 0x04: bytes([0x03, 0x80])})
    assert rig.power.battery() == {"volts": 3.84, "percent": 4}
    rig.power.begin()
    with pytest.raises(Slept) as slept:
        rig.run(130)
    assert slept.value.ms is None                   # empty: no check-ins either
    assert "battery at 4 %" in rig.state.log[-1]


# --- the host ---------------------------------------------------------------

@pytest.fixture
def server(monkeypatch, tmp_path):
    import elecrow_mcp_server as srv
    monkeypatch.setenv("ELECROW_NETWORK_CONFIG", str(tmp_path / "none.json"))
    return srv, srv.build_server()


def test_every_call_pokes_her_first(monkeypatch, server):
    srv, _ = server
    import elecrow_device
    ran = []

    class Repl:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

        def exec(self, code, timeout=0):
            ran.append(code)
            return {"ok": True, "timed_out": False, "stdout": "", "stderr": ""}

    monkeypatch.setattr(elecrow_device, "RawRepl", Repl)
    monkeypatch.setattr(elecrow_device, "board_port", lambda port: "COM9")
    srv._device_call("COM9", ["print(1)\n"])
    assert ran == [srv.POKE, "print(1)\n"]
    ran.clear()
    srv._device_call("COM9", ["print(1)\n"], poke=False)
    assert ran == ["print(1)\n"]


def test_looking_at_her_power_does_not_wake_her(monkeypatch, server):
    srv, mcp = server
    seen = {}

    def call(port, snippets, **kw):
        seen.update(kw)
        return {"ok": True, "stdout": '{"mode": "doze"}'}

    monkeypatch.setattr(srv, "_device_call", call)
    out = mcp._tool_manager.get_tool("device_power").fn()
    assert out["data"] == {"mode": "doze"} and seen["poke"] is False


def test_sleep_wants_a_confirmation(monkeypatch, server):
    srv, mcp = server
    monkeypatch.setattr(srv, "_device_call", lambda *a, **k: pytest.fail("touched the board"))
    out = mcp._tool_manager.get_tool("device_sleep").fn()
    assert out["status"] == "confirm"


def test_sleep_and_charge_send_the_right_call(monkeypatch, server):
    srv, mcp = server
    sent = []
    monkeypatch.setattr(srv, "_device_call",
                        lambda port, snippets, **k: sent.append(snippets[0]) or {
                            "ok": True, "stdout": '{"sleeping_in_s": 2}'})
    tool = mcp._tool_manager.get_tool("device_sleep").fn
    tool(checkin_minutes=10, confirm=True)
    tool(charging=True, confirm=True)
    assert "P.sleep(10)" in sent[0] and "P.charge(5)" in sent[1]


def test_wake_resets_over_the_cable(monkeypatch, server):
    srv, mcp = server
    import elecrow_device
    pulsed = []
    monkeypatch.setattr(srv, "_device_call", lambda *a, **k: {"ok": False, "status": "serial"})
    monkeypatch.setattr(elecrow_device, "board_port", lambda port: "COM9")
    monkeypatch.setattr(elecrow_device, "reset_pulse", pulsed.append)
    monkeypatch.setattr(srv, "_knock", lambda *a: pytest.fail("knocked with a cable there"))
    out = mcp._tool_manager.get_tool("device_wake").fn()
    assert out["status"] == "reset" and pulsed == ["COM9"]


def test_wake_knocks_when_there_is_no_cable(monkeypatch, server):
    srv, mcp = server
    import elecrow_device

    def no_port(port):
        raise elecrow_device.DeviceError("no board on a cable")

    knocks = []
    monkeypatch.setattr(srv, "_device_call", lambda *a, **k: {"ok": False, "status": "serial"})
    monkeypatch.setattr(elecrow_device, "board_port", no_port)
    monkeypatch.setattr(srv, "_knock", lambda port, seconds: knocks.append(seconds) or True)
    out = mcp._tool_manager.get_tool("device_wake").fn(wait_minutes=4)
    assert out["status"] == "knocking" and knocks == [240]


def test_wake_says_so_when_she_is_already_awake(monkeypatch, server):
    srv, mcp = server
    monkeypatch.setattr(srv, "_device_call", lambda *a, **k: {"ok": True, "port": "COM9"})
    assert mcp._tool_manager.get_tool("device_wake").fn()["status"] == "awake"


def test_the_knocker_stops_when_she_answers(monkeypatch, server):
    srv, _ = server
    answers = iter([{"ok": False}, {"ok": False}, {"ok": True}])
    monkeypatch.setattr(srv, "_device_call", lambda *a, **k: next(answers))
    monkeypatch.setattr(srv, "KNOCK_EVERY_S", 0)
    assert srv._knock("wifi", 30) is True
    srv._KNOCK["thread"].join(timeout=5)
    assert srv._KNOCK["woke"] is not None and not srv._KNOCK["thread"].is_alive()


def test_reset_pulse_leaves_dtr_released(monkeypatch):
    """DTR low with RTS would hold GPIO0 down: the bootloader, not her."""
    import elecrow_device
    lines = []

    class Port:
        def __setattr__(self, name, value):
            if name in ("dtr", "rts"):
                lines.append((name, value))
            object.__setattr__(self, name, value)

        def open(self):
            lines.append(("open", None))

        def close(self):
            lines.append(("close", None))

    monkeypatch.setitem(sys.modules, "serial", types.SimpleNamespace(Serial=Port))
    elecrow_device.reset_pulse("COM9", hold_s=0)
    assert lines == [("dtr", False), ("rts", False), ("open", None),
                     ("rts", True), ("rts", False), ("close", None)]
