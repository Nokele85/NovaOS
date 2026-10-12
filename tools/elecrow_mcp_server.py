"""Local MCP workshop for the Elecrow ESP CAM AI board.

This server is intentionally local to the machine that owns the USB cable.
It creates and builds only inside ELECROW_WORKSPACE and never flashes unless
the caller explicitly passes ``confirm=True``.  The first firmware is a safe
ESP32-S3 heartbeat/serial probe; display, camera, audio and touch pins are
added only after the exact board pinout has been verified.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import threading
import time
import shlex
from pathlib import Path

# elecrow_device.py sits beside this file; the server is started by path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp.server.fastmcp import FastMCP

import elecrow_api  # noqa: E402
import elecrow_sound_library  # noqa: E402

#: Beside every answer that names the autostart app (see `device_info`).
AUTOSTART_NOTE = (
    "`autostart: {app}` is a setting, not a result. The app starts at the board's "
    "NEXT power-up or reset; it is not running now, and this says nothing about "
    "whether it works. To know that, run it — device_run_app(name=\"{app}\") — "
    "and read device_log. Do not tell anyone it works because this is set.")


def _api() -> dict:
    """The board's library as data, or {} when its source is not here."""
    lib = Path(os.environ.get("ELECROW_WORKSPACE", "")) / "nova-runtime" / "lib" / "nova"
    if not lib.is_dir():
        lib = (Path(__file__).resolve().parent.parent / "elecrow-firmware"
               / "nova-runtime" / "lib" / "nova")
    try:
        return elecrow_api.load_api(lib) if lib.is_dir() else {}
    except Exception:  # noqa: BLE001 — a reference that cannot be built checks nothing
        return {}


def _score():
    """The board's own score reader (`nova/score.py`), loaded on this machine.

    One file reads a melody in both places, so a score refused here is exactly
    a score that would fail on the board. None when the source is not here."""
    import importlib.util
    path = Path(os.environ.get("ELECROW_WORKSPACE", "")) / "nova-runtime" / "lib" / "nova" / "score.py"
    if not path.is_file():
        path = (Path(__file__).resolve().parent.parent / "elecrow-firmware"
                / "nova-runtime" / "lib" / "nova" / "score.py")
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("nova_score", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sound_root() -> Path:
    return Path(os.environ.get("ELECROW_WORKSPACE", str(DEFAULT_ROOT))) / "sound-library"


#: Background music is company: quiet when she names no level, never above
#: MUSIC_LOUDEST, and a loop ends by itself (the board's own limits, said here
#: so the tool can answer what it changed).
MUSIC_QUIET, MUSIC_LOUDEST = 0.06, 0.25
MUSIC_MINUTES, MUSIC_MINUTES_MAX = 10.0, 120.0


def music_call(score: str, tempo: int, volume: float, loop: bool, minutes: float,
               title: str, wave: str) -> tuple[dict | None, str]:
    """(refusal, code): what is wrong with the piece, or the code that plays it."""
    reader = _score()
    if reader is None:
        return {"ok": False, "status": "no_runtime_source",
                "message": "The runtime's source is not on this machine."}, ""
    try:
        notes = reader.parse(score)
    except ValueError as exc:
        return {"ok": False, "status": "bad_score", "error": str(exc),
                "message": "Nothing was played. Correct the score and call again."}, ""
    if not 30 <= int(tempo) <= 240:
        return {"ok": False, "status": "bad_tempo",
                "error": "tempo is beats per minute, 30 to 240"}, ""
    if wave not in ("pluck", "soft", "sine", "square"):
        return {"ok": False, "status": "bad_wave",
                "error": "wave is one of: pluck, soft, sine, square"}, ""
    volume = max(0.0, min(MUSIC_LOUDEST, float(volume))) or None
    minutes = max(0.5, min(MUSIC_MINUTES_MAX, float(minutes))) if loop else 0.0
    code = ("import json\nfrom nova import music\n"
            f"print(json.dumps(music.play({score!r}, tempo={int(tempo)}, "
            f"volume={volume!r}, loop={bool(loop)!r}, minutes={minutes!r}, "
            f"title={str(title)[:60]!r}, wave={wave!r})))\n")
    return {"notes": len(notes), "volume": volume, "minutes": minutes,
            "seconds_per_pass": round(reader.seconds(notes, int(tempo)), 1)}, code


def pad_call(score: str, tempo: int, volume: float, loop: bool, minutes: float,
             title: str) -> tuple[dict | None, str]:
    """(refusal, code) for a carpet of sound — `music_call`, for chords."""
    reader = _score()
    if reader is None:
        return {"ok": False, "status": "no_runtime_source",
                "message": "The runtime's source is not on this machine."}, ""
    try:
        chords = reader.parse_chords(score)
    except ValueError as exc:
        return {"ok": False, "status": "bad_score", "error": str(exc),
                "message": "Nothing was played. Correct the score and call again."}, ""
    distinct = {voices for voices, _ in chords if voices}
    if len(distinct) > 6:
        return {"ok": False, "status": "bad_score",
                "error": f"{len(distinct)} different chords; a pad holds 6 at most",
                "message": "Nothing was played. Use fewer different chords."}, ""
    if not 20 <= int(tempo) <= 120:
        return {"ok": False, "status": "bad_tempo",
                "error": "a pad's tempo is beats per minute, 20 to 120"}, ""
    volume = max(0.0, min(MUSIC_LOUDEST, float(volume))) or None
    minutes = max(0.5, min(MUSIC_MINUTES_MAX, float(minutes))) if loop else 0.0
    code = ("import json\nfrom nova import music\n"
            f"print(json.dumps(music.pad({score!r}, tempo={int(tempo)}, "
            f"volume={volume!r}, loop={bool(loop)!r}, minutes={minutes!r}, "
            f"title={str(title)[:60]!r})))\n")
    return {"chords": len(chords), "volume": volume, "minutes": minutes,
            "seconds_per_pass": round(reader.seconds(chords, int(tempo)), 1)}, code


def harp_call(score: str, tempo: int, volume: float, loop: bool, minutes: float,
              title: str) -> tuple[dict | None, str]:
    """(refusal, code) for plucked strings — `pad_call`, for `music.harp`."""
    reader = _score()
    if reader is None:
        return {"ok": False, "status": "no_runtime_source",
                "message": "The runtime's source is not on this machine."}, ""
    try:
        chords = reader.parse_chords(score)
    except ValueError as exc:
        return {"ok": False, "status": "bad_score", "error": str(exc),
                "message": "Nothing was played. Correct the score and call again."}, ""
    if not 30 <= int(tempo) <= 160:
        return {"ok": False, "status": "bad_tempo",
                "error": "a harp's tempo is beats per minute, 30 to 160"}, ""
    volume = max(0.0, min(MUSIC_LOUDEST, float(volume))) or None
    minutes = max(0.5, min(MUSIC_MINUTES_MAX, float(minutes))) if loop else 0.0
    code = ("import json\nfrom nova import music\n"
            f"print(json.dumps(music.harp({score!r}, tempo={int(tempo)}, "
            f"volume={volume!r}, loop={bool(loop)!r}, minutes={minutes!r}, "
            f"title={str(title)[:60]!r})))\n")
    return {"chords": len(chords), "volume": volume, "minutes": minutes,
            "seconds_per_pass": round(reader.seconds(chords, int(tempo)), 1)}, code


def erhu_call(score: str, tempo: int, volume: float, loop: bool, minutes: float,
              title: str) -> tuple[dict | None, str]:
    """(refusal, code) for a bowed string — `music_call`, for `music.erhu`."""
    reader = _score()
    if reader is None:
        return {"ok": False, "status": "no_runtime_source",
                "message": "The runtime's source is not on this machine."}, ""
    try:
        notes = reader.parse(score)
    except ValueError as exc:
        return {"ok": False, "status": "bad_score", "error": str(exc),
                "message": "Nothing was played. Correct the score and call again."}, ""
    if not 30 <= int(tempo) <= 160:
        return {"ok": False, "status": "bad_tempo",
                "error": "an erhu's tempo is beats per minute, 30 to 160"}, ""
    volume = max(0.0, min(MUSIC_LOUDEST, float(volume))) or None
    minutes = max(0.5, min(MUSIC_MINUTES_MAX, float(minutes))) if loop else 0.0
    code = ("import json\nfrom nova import music\n"
            f"print(json.dumps(music.erhu({score!r}, tempo={int(tempo)}, "
            f"volume={volume!r}, loop={bool(loop)!r}, minutes={minutes!r}, "
            f"title={str(title)[:60]!r})))\n")
    return {"notes": len(notes), "volume": volume, "minutes": minutes,
            "seconds_per_pass": round(reader.seconds(notes, int(tempo)), 1)}, code


#: The recorded instruments `tools/elecrow_samples.py` cuts, and what each is for.
KEYS_INSTRUMENTS = {
    "warmpad": "a soft, warm synthesiser pad - held", "halo": "a glassy, airy pad - held",
    "choir": "voices singing aah - held", "strings": "a string ensemble - held",
    "harp": "a harp - plucked", "vibes": "a vibraphone - struck, long shimmer",
    "musicbox": "a music box - struck, bright",
    "piano": "an acoustic piano - struck",
}


def keys_call(score: str, instrument: str, style: str, tempo: int, volume: float,
              loop: bool, minutes: float, title: str, melody: str = "",
              lead: str = "") -> tuple[dict | None, str]:
    """(refusal, code) for a recorded instrument — `music.keys`."""
    reader = _score()
    if reader is None:
        return {"ok": False, "status": "no_runtime_source",
                "message": "The runtime's source is not on this machine."}, ""
    try:
        tune = reader.parse(melody) if str(melody).strip() else []
    except ValueError as exc:
        return {"ok": False, "status": "bad_melody", "error": str(exc),
                "message": "Nothing was played. Correct the melody and call again."}, ""
    try:
        chords = reader.parse_chords(score) if str(score).strip() or not tune else []
    except ValueError as exc:
        return {"ok": False, "status": "bad_score", "error": str(exc),
                "message": "Nothing was played. Correct the score and call again."}, ""
    original_chords = chords
    chords = reader.fit_accompaniment(chords, tune)
    lead = str(lead).strip().lower()
    if lead and lead not in KEYS_INSTRUMENTS:
        return {"ok": False, "status": "bad_instrument",
                "error": f"there is no instrument {lead!r} to lead; there is: "
                         + ", ".join(KEYS_INSTRUMENTS)}, ""
    instrument = "+".join(n for n in str(instrument).replace(" ", "").split("+") if n)
    unknown = [n for n in instrument.split("+") if n not in KEYS_INSTRUMENTS]
    if unknown or not instrument or instrument.count("+") > 2:
        return {"ok": False, "status": "bad_instrument",
                "error": (f"there is no instrument {unknown[0]!r}" if unknown else
                          "one to three instruments, joined by +")
                         + "; there is: " + ", ".join(KEYS_INSTRUMENTS)}, ""
    if style not in ("", "hold", "broken"):
        return {"ok": False, "status": "bad_style",
                "error": 'style is "hold", "broken", or left out'}, ""
    if not 20 <= int(tempo) <= 160:
        return {"ok": False, "status": "bad_tempo",
                "error": "tempo is beats per minute, 20 to 160"}, ""
    volume = max(0.0, min(MUSIC_LOUDEST, float(volume))) or None
    minutes = max(0.5, min(MUSIC_MINUTES_MAX, float(minutes))) if loop else 0.0
    code = ("import json\nfrom nova import music\n"
            f"print(json.dumps(music.keys({score!r}, instrument={instrument!r}, "
            f"style={style!r}, tempo={int(tempo)}, volume={volume!r}, "
            f"loop={bool(loop)!r}, minutes={minutes!r}, title={str(title)[:60]!r}, "
            f"melody={str(melody).strip()!r}, lead={lead!r})))\n")
    facts = {"chords": len(chords), "instrument": instrument, "volume": volume,
             "minutes": minutes,
             "seconds_per_pass": round(reader.seconds(chords, int(tempo)), 1) if chords else 0.0}
    if tune:
        facts["melody_notes"] = len(tune)
        facts["melody_seconds"] = round(reader.seconds(tune, int(tempo)), 1)
        if chords != original_chords:
            facts["accompaniment_adjusted"] = True
    return facts, code


#: The sounds of the air `nova.music` makes (its AIRS and PLACES).
AMBIENCES = ("rain", "drizzle", "downpour", "thunder", "wind", "gale", "leaves", "fire",
             "embers", "crickets", "cicadas", "frogs", "birds", "owl", "stream", "waves",
             "waterfall", "drips", "chimes", "bells", "hum")
PLACES = ("night", "forest", "storm", "campfire", "beach", "jungle", "pond", "cave",
          "garden", "hearth", "meadow", "temple")


def ambience_call(scene: str, level: int, minutes: float) -> tuple[dict | None, str]:
    """(refusal, code) for the air around the music — `music.ambience`."""
    scene = "+".join(n for n in str(scene).replace(" ", "").lower().split("+") if n)
    if scene in ("", "off", "none", "stop"):
        return {"scene": "off"}, ("import json\nfrom nova import music\n"
                                  "print(json.dumps(music.ambience('off')))\n")
    unknown = [n for n in scene.split("+") if n not in AMBIENCES and n not in PLACES]
    if unknown:
        return {"ok": False, "status": "bad_scene",
                "error": f"there is no ambience {unknown[0]!r}; single sounds: "
                         + ", ".join(AMBIENCES) + "; places: " + ", ".join(PLACES)}, ""
    level = max(0, min(100, int(level)))
    minutes = max(0.0, min(480.0, float(minutes)))
    code = ("import json\nfrom nova import music\n"
            f"print(json.dumps(music.ambience({scene!r}, level={level}, minutes={minutes!r})))\n")
    return {"scene": scene, "level": level, "minutes": minutes}, code


def _check(files: dict) -> list[str]:
    """What would fail when this app runs; [] when nothing was found or nothing can be checked."""
    api = _api()
    try:
        return elecrow_api.check_app(
            {str(k): str(v) for k, v in files.items()}, api) if api else []
    except Exception:  # noqa: BLE001 — the check must never be what stops her writing
        return []


DEFAULT_PROJECT = "nova-device"
DEFAULT_ROOT = Path(os.environ.get(
    "ELECROW_WORKSPACE",
    str(Path.cwd() / "elecrow-firmware")))


def _root() -> Path:
    root = DEFAULT_ROOT.resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _project(name: str) -> Path:
    clean = (name or DEFAULT_PROJECT).strip()
    if not clean or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for c in clean.lower()):
        raise ValueError("project_name may contain only letters, numbers, '-' and '_'")
    target = (_root() / clean).resolve()
    if target.parent != _root():
        raise ValueError("project must stay inside ELECROW_WORKSPACE")
    return target


def _run(args: list[str], cwd: Path, timeout: int = 180,
         output_limit: int = 6000) -> dict:
    started = time.monotonic()
    command = " ".join(shlex.quote(str(arg)) for arg in args)
    try:
        proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                              timeout=timeout, check=False)
    except FileNotFoundError:
        return {"ok": False, "error": f"tool not installed: {args[0]}",
                "status": "tool_missing", "command": args,
                "command_text": command,
                "duration_seconds": round(time.monotonic() - started, 3)}
    except subprocess.TimeoutExpired as exc:
        return {"ok": False, "error": f"command timed out after {timeout}s",
                "stdout": (exc.stdout or "")[-output_limit:],
                "stderr": (exc.stderr or "")[-output_limit:], "status": "timeout",
                "output_truncated": True,
                "command": args, "command_text": command,
                "duration_seconds": round(time.monotonic() - started, 3)}
    return {"ok": proc.returncode == 0, "returncode": proc.returncode,
            "stdout": proc.stdout[-output_limit:], "stderr": proc.stderr[-output_limit:],
            "output_truncated": len(proc.stdout) > output_limit or len(proc.stderr) > output_limit,
            "command": args, "command_text": command,
            "duration_seconds": round(time.monotonic() - started, 3)}


def _open_serial(port: str):
    """Open the board's port WITHOUT resetting it.

    pyserial asserts DTR and RTS on open, and the board's CH340 auto-reset
    circuit turns that into a reboot - measured 2026-09-11: every default open
    restarted the heartbeat probe (uptime back to 2000 ms), every open with
    both lines released left it running. So a "read the log" call used to
    reboot the board it was reading."""
    import serial
    stream = serial.Serial()
    stream.port, stream.baudrate, stream.timeout = port, 115200, 0.25
    stream.dtr = False
    stream.rts = False
    stream.open()
    return stream


def _serial_preflight(port: str) -> dict:
    """Check whether the selected port can be opened before upload starts."""
    try:
        with _open_serial(port):
            pass
        return {"ok": True, "port": port, "status": "port_openable"}
    except ImportError:
        return {"ok": False, "port": port, "status": "pyserial_missing",
                "error": "pyserial is not installed; port ownership was not checked"}
    except Exception as exc:
        return {"ok": False, "port": port, "status": "port_unavailable",
                "error": str(exc), "error_type": type(exc).__name__}


def _upload_diagnosis(result: dict) -> dict:
    """Turn PlatformIO output into explicit, non-speculative diagnostics."""
    text = "\n".join(str(result.get(k, "")) for k in ("stdout", "stderr")).lower()
    if result.get("status") == "timeout":
        reason = "PlatformIO timed out during upload."
        category = "timeout"
    elif "access is denied" in text or "permissionerror" in text:
        reason = "Windows denied access to the selected serial port."
        category = "port_permission"
    elif "could not open port" in text or "port is already open" in text:
        reason = "The selected serial port could not be opened; it may be in use."
        category = "port_busy_or_unavailable"
    elif "failed to connect" in text or "no serial data received" in text:
        reason = "The uploader could not put the board into bootloader mode."
        category = "bootloader_handshake"
    elif "error" in text or result.get("returncode", 0) != 0:
        reason = "PlatformIO reported an upload failure; see the captured output."
        category = "platformio_upload_error"
    else:
        reason = "Upload failed without a recognizable PlatformIO diagnostic."
        category = "unknown_upload_error"
    return {"category": category, "reason": reason,
            "evidence": {"returncode": result.get("returncode"),
                         "stderr": result.get("stderr", ""),
                         "stdout": result.get("stdout", "")}}


def _ports() -> list[dict]:
    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    return [{"device": p.device, "description": p.description,
             "vid": p.vid, "pid": p.pid, "serial_number": p.serial_number}
            for p in list_ports.comports()]


FIRMWARE = r'''#include <Arduino.h>

// Safe first probe: no board-specific camera/display pins are touched.
// The PC can use this serial heartbeat to verify USB + firmware deployment.
static constexpr uint32_t BAUD = 115200;
static constexpr uint32_t HEARTBEAT_MS = 2000;
uint32_t last_heartbeat = 0;

void setup() {
  Serial.begin(BAUD);
  delay(300);
  Serial.println("NOVA_ELECROW_BOOT");
  Serial.println("board=ESP32-S3R8");
  Serial.println("probe=safe-serial-heartbeat");
  Serial.println("camera/display/audio=not-yet-enabled");
}

void loop() {
  if (millis() - last_heartbeat >= HEARTBEAT_MS) {
    last_heartbeat = millis();
    Serial.printf("NOVA_HEARTBEAT uptime_ms=%lu\n", (unsigned long)millis());
  }
  if (Serial.available()) {
    String command = Serial.readStringUntil('\n');
    command.trim();
    if (command == "ping") Serial.println("NOVA_PONG");
    else if (command == "status") Serial.println("NOVA_STATUS safe-probe-ready");
    else Serial.println("NOVA_UNKNOWN_COMMAND");
  }
}
'''


PLATFORMIO = """[env:esp32-s3-devkitc-1]\nplatform = espressif32\nboard = esp32-s3-devkitc-1\nframework = arduino\nmonitor_speed = 115200\nupload_speed = 460800\n"""


def build_server(http_host: str | None = None) -> FastMCP:
    transport_security = None
    if http_host:
        from mcp.server.transport_security import TransportSecuritySettings
        transport_security = TransportSecuritySettings(
            allowed_hosts=[f"{http_host}:8769", "192.168.0.104:8769",
                           "127.0.0.1:8769", "localhost:8769"]
        )
    mcp = FastMCP(
        "elecrow-firmware-workshop",
        instructions=(
            "This is Nova's Elecrow firmware hand. Use elecrow_status first. "
            "Use elecrow_scaffold only when the project is missing, then "
            "elecrow_build to compile, elecrow_serial_log or "
            "elecrow_serial_command to verify the connected board, and "
            "elecrow_flash only after explicit user confirmation because it "
            "overwrites the firmware. The current probe is serial-only; do "
            "not claim camera, display, touch, or audio support until the "
            "exact board pin map has been verified and implemented."
        ),
        transport_security=transport_security,
    )

    @mcp.tool()
    def elecrow_status(project_name: str = DEFAULT_PROJECT) -> dict:
        """Use first: inspect USB ports, toolchain and Elecrow project state."""
        project = _project(project_name)
        pio = shutil.which("pio") or shutil.which("platformio")
        esptool = shutil.which("esptool") or shutil.which("esptool.py")
        return {"workspace": str(_root()), "project": str(project),
                "project_exists": project.is_dir(), "ports": _ports(),
                "platformio": pio, "esptool": esptool,
                "firmware": str(project / "src" / "main.cpp")
                if (project / "src" / "main.cpp").exists() else None}

    @mcp.tool()
    def elecrow_debug(project_name: str = DEFAULT_PROJECT, port: str = "auto",
                      seconds: int = 5, compile: bool = False) -> dict:
        """Diagnose the board without flashing: ports, files, serial and optional build."""
        if not 1 <= seconds <= 15:
            raise ValueError("seconds must be between 1 and 15")
        project = _project(project_name)
        pio = shutil.which("pio") or shutil.which("platformio")
        ports = _ports()
        candidates = [p["device"] for p in ports if p.get("device")]
        selected = candidates[0] if port == "auto" and len(candidates) == 1 else port
        serial_result = {"ok": False, "status": "not_run"}
        if selected != "auto":
            try:
                with _open_serial(selected) as stream:
                    stream.reset_input_buffer()
                    stream.write(b"status\n")
                    stream.flush()
                    end = time.monotonic() + seconds
                    lines = []
                    while time.monotonic() < end:
                        line = stream.readline().decode(errors="replace").strip()
                        if line:
                            lines.append(line)
                serial_result = {"ok": True, "port": selected, "lines": lines}
            except Exception as exc:
                serial_result = {"ok": False, "port": selected, "error": str(exc)}
        build = {"status": "not_requested"}
        if compile:
            if not pio:
                build = {"ok": False, "error": "PlatformIO is not installed"}
            elif not (project / "platformio.ini").is_file():
                build = {"ok": False, "error": "project not scaffolded"}
            else:
                build = _run([pio, "run"], project, timeout=240)
        return {
            "ok": bool(project.is_dir() and pio),
            "project": str(project), "ports": ports,
            "selected_port": selected, "platformio": pio,
            "files": {
                "platformio_ini": (project / "platformio.ini").is_file(),
                "main_cpp": (project / "src" / "main.cpp").is_file(),
                "board_notes": (project / "BOARD_NOTES.md").is_file(),
            },
            "serial": serial_result, "build": build,
            "next": "inspect serial/build output; flash requires separate elecrow_flash confirmation",
        }

    @mcp.tool()
    def elecrow_scaffold(project_name: str = DEFAULT_PROJECT) -> dict:
        """Create Nova's safe ESP32-S3 serial-heartbeat firmware project if missing."""
        project = _project(project_name)
        (project / "src").mkdir(parents=True, exist_ok=True)
        (project / "src" / "main.cpp").write_text(FIRMWARE, encoding="utf-8")
        (project / "platformio.ini").write_text(PLATFORMIO, encoding="utf-8")
        (project / "BOARD_NOTES.md").write_text(
            "# Elecrow ESP CAM AI — Nova probe\n\n"
            "This first image touches serial only. Verify the exact Elecrow "
            "SAD00006D pin map before enabling GC2145, LCD, touch, MEMS or speaker.\n",
            encoding="utf-8")
        return {"ok": True, "project": str(project),
                "files": ["platformio.ini", "src/main.cpp", "BOARD_NOTES.md"],
                "next": "build with elecrow_build, then flash with explicit confirm=true"}

    @mcp.tool()
    def elecrow_build(project_name: str = DEFAULT_PROJECT) -> dict:
        """Compile the firmware with PlatformIO; this never touches the board."""
        project = _project(project_name)
        if not (project / "platformio.ini").is_file():
            raise ValueError("project not scaffolded; call elecrow_scaffold first")
        pio = shutil.which("pio") or shutil.which("platformio")
        if not pio:
            return {"ok": False, "error": "PlatformIO is not installed on this PC",
                    "install": "python -m pip install platformio"}
        return _run([pio, "run"], project)

    @mcp.tool()
    def elecrow_flash(project_name: str = DEFAULT_PROJECT, port: str = "auto",
                      confirm: bool = False) -> dict:
        """Flash compiled firmware; explicit confirm=true is mandatory because this overwrites the board."""
        if not confirm:
            return {"ok": False, "status": "confirmation_required",
                    "message": "Flashing overwrites the board. Call again with confirm=true after checking the port."}
        project = _project(project_name)
        pio = shutil.which("pio") or shutil.which("platformio")
        if not pio:
            return {"ok": False, "error": "PlatformIO is not installed on this PC"}
        if port == "auto":
            found = _ports()
            candidates = [p["device"] for p in found if p["device"]]
            if len(candidates) != 1:
                return {"ok": False, "status": "port_ambiguous",
                        "ports": found, "message": "Pass the exact Elecrow COM port."}
            port = candidates[0]
        preflight = _serial_preflight(port)
        if not preflight["ok"]:
            return {
                "ok": False, "status": "preflight_failed", "port": port,
                "project": str(project), "preflight": preflight,
                "message": "Upload was not started because the serial port preflight failed.",
                "next": "Close serial monitors and retry after checking the exact COM port.",
            }
        # Keep the remote MCP response compact. PlatformIO's verbose upload log
        # can otherwise be dropped by the remote tool channel even when the
        # upload itself succeeds. The tail still contains SUCCESS/hash/error data.
        result = _run([pio, "run", "-t", "upload", "--upload-port", port], project,
                      timeout=240, output_limit=1800)
        result.update({"port": port, "project": str(project),
                       "preflight": preflight})
        if result.get("ok"):
            result.update({"status": "uploaded",
                           "message": "PlatformIO completed the upload. Verify with elecrow_serial_log."})
        else:
            result.update({"status": "upload_failed",
                           "message": "Upload failed; no success is claimed. Full diagnostics are included.",
                           "diagnosis": _upload_diagnosis(result),
                           "next": "Inspect diagnosis, close any serial monitor, then retry only after the cause is clear."})
        return result

    @mcp.tool()
    def elecrow_serial_log(port: str, seconds: int = 5) -> dict:
        """Read a short serial window for NOVA_BOOT/heartbeat verification."""
        if not 1 <= seconds <= 30:
            raise ValueError("seconds must be between 1 and 30")
        try:
            with _open_serial(port) as stream:
                end = time.monotonic() + seconds
                lines = []
                while time.monotonic() < end:
                    line = stream.readline().decode(errors="replace").strip()
                    if line:
                        lines.append(line)
            return {"ok": True, "port": port, "lines": lines}
        except ImportError:
            return {"ok": False, "error": "pyserial is not installed"}
        except Exception as exc:
            return {"ok": False, "port": port, "error": str(exc)}

    @mcp.tool()
    def elecrow_serial_command(port: str, command: str, seconds: int = 2) -> dict:
        """Send one newline-terminated command and collect the device response."""
        if not command.strip():
            raise ValueError("command must not be empty")
        if not 1 <= seconds <= 15:
            raise ValueError("seconds must be between 1 and 15")
        try:
            with _open_serial(port) as stream:
                stream.reset_input_buffer()
                # a real line feed: the firmware reads a command up to '\n'
                stream.write((command.strip() + "\n").encode("utf-8"))
                stream.flush()
                end = time.monotonic() + seconds
                lines = []
                while time.monotonic() < end:
                    line = stream.readline().decode(errors="replace").strip()
                    if line:
                        lines.append(line)
            return {"ok": True, "port": port, "command": command.strip(),
                    "lines": lines}
        except ImportError:
            return {"ok": False, "error": "pyserial is not installed"}
        except Exception as exc:
            return {"ok": False, "port": port, "command": command.strip(),
                    "error": str(exc)}

    _device_tools(mcp)
    return mcp


# ---- Nova's playground: the MicroPython runtime on the board ----------------
#
# Everything below talks to elecrow-firmware/nova-runtime over the raw REPL
# (tools/elecrow_device.py). Nova may write, run and delete her own apps
# freely; the runtime layer, the firmware and the start-up behaviour stay
# with the owner (tools/elecrow_device_admin.py, or confirm=true here).
# YuriOS gives a tool call TOOL_TIMEOUT_S (10 s), so every call here is
# bounded well below it.

_DEVICE_LOCK = threading.Lock()
RUN_SECONDS_MAX = 8
OUTPUT_MAX = 4000


def _secrets_path() -> Path:
    return Path(os.environ.get("ELECROW_DEVICE_SECRETS",
                               str(DEFAULT_ROOT.resolve().parent / "secrets"
                                   / "elecrow_device.json")))


def _device_call(port: str, snippets: list[str], timeout: float = 8.0) -> dict:
    """Run code snippets in one raw-REPL session; the last one's result wins.

    Every string that comes back passes the Redactor - an app can read the
    board's Wi-Fi secrets, and nothing it prints may leave this machine."""
    from elecrow_device import DeviceError, RawRepl, Redactor, board_port
    redact = Redactor.from_file(_secrets_path())
    try:
        device = board_port(port)
    except DeviceError as exc:
        return {"ok": False, "status": "port", "error": str(exc)}
    if not _DEVICE_LOCK.acquire(timeout=5):
        return {"ok": False, "status": "busy", "error": "the board is in use by another call"}
    try:
        with RawRepl(device) as repl:
            result: dict = {}
            for code in snippets:
                result = repl.exec(code, timeout=timeout)
                if not result["ok"] and not result["timed_out"]:
                    break
    except DeviceError as exc:
        return {"ok": False, "status": "no_runtime", "port": device, "error": redact(str(exc))}
    except Exception as exc:                  # a busy or vanished COM port
        return {"ok": False, "status": "serial", "port": device,
                "error": redact(f"{type(exc).__name__}: {exc}")}
    finally:
        _DEVICE_LOCK.release()
    for key in ("stdout", "stderr"):
        text = redact(result.get(key, ""))
        result[key] = text[-OUTPUT_MAX:]
    result["port"] = device
    return result


def _json_out(result: dict) -> dict:
    """Parse the last stdout line as JSON into `data`, when the board sent one."""
    import json as _json
    line = (result.get("stdout") or "").strip().splitlines()[-1:] or [""]
    try:
        result["data"] = _json.loads(line[0])
        result.pop("stdout", None)
    except ValueError:
        pass
    return result


def _device_tools(mcp: FastMCP) -> None:
    from elecrow_device import (MAX_APP_BYTES, MAX_FILE_BYTES, MAX_FILES,
                                check_app_name, check_file_name, describe_frame,
                                png_bytes, rgb565be_to_rgb, upload_code)

    @mcp.tool()
    def device_info(port: str = "auto") -> dict:
        """Use first on the board: runtime version, safe mode, SD card, Wi-Fi
        (online or not - never which network), free memory and her apps."""
        code = (
            "import sys, gc, json, machine\n"
            "try:\n"
            "    import nova\n    i = nova.info()\n"
            "except Exception as e:\n"
            "    i = {'runtime': None, 'error': repr(e)}\n"
            "try:\n"
            "    import nova.apps as A\n"
            "    i['apps_root'] = A.root(); i['apps'] = [a['name'] for a in A.list_apps()]\n"
            "except Exception as e:\n"
            "    i['apps_error'] = repr(e)\n"
            "gc.collect(); i['mem_free'] = gc.mem_free()\n"
            "i['micropython'] = sys.version; i['reset_cause'] = machine.reset_cause()\n"
            "print(json.dumps(i))\n")
        result = _json_out(_device_call(port, [code]))
        data = result.get("data") if isinstance(result, dict) else None
        if isinstance(data, dict) and data.get("autostart"):
            # On 2026-10-07 she read "autostart: warm_pad" as "it worked" and
            # told him so, of an app that was not running and would have died
            # on its first sound. The field is a setting; say what it is not.
            result["note"] = AUTOSTART_NOTE.format(app=data["autostart"])
        return result

    @mcp.tool()
    def device_exec(code: str, seconds: int = 5, port: str = "auto") -> dict:
        """Run a MicroPython snippet on the board and return what it printed and
        any traceback. For quick experiments; lasting work belongs in an app."""
        if not code.strip():
            raise ValueError("code must not be empty")
        seconds = max(1, min(RUN_SECONDS_MAX, int(seconds)))
        return _device_call(port, [code], timeout=seconds)

    @mcp.tool()
    def device_list_apps(port: str = "auto") -> dict:
        """List her apps (folders with main.py) and the shared _lib folder."""
        code = ("import json, nova.apps as A\n"
                "print(json.dumps({'root': A.root(), 'apps': A.list_apps()}))\n")
        return _json_out(_device_call(port, [code]))

    @mcp.tool()
    def device_write_app(name: str, files: dict[str, str], port: str = "auto",
                         force: bool = False) -> dict:
        """Create or replace files of one app: {"main.py": "...", ...}. Use the
        name "_lib" for modules shared by all apps. Flat folder, text files only.
        The code is checked against what the board really has before anything
        is written: a call that cannot work is refused with the correct form.
        `device_api` shows everything the board offers. force=true writes
        regardless — only when the check itself is wrong."""
        name = check_app_name(name)
        if not files or len(files) > MAX_FILES:
            raise ValueError(f"give 1 to {MAX_FILES} files")
        problems = _check(files)
        if problems and not force:
            # Refused, not warned about: a written app that dies at the full hour
            # reads as a finished one for an hour, and then for a day.
            return {"ok": False, "status": "would_fail", "problems": problems,
                    "message": "Nothing was written. This would fail on the board for the "
                               "reasons listed — each says what to write instead. Fix them "
                               "and call device_write_app again; device_api shows every "
                               "function the board has."}
        encoded = {check_file_name(f): body.encode("utf-8") for f, body in files.items()}
        too_big = [f for f, b in encoded.items() if len(b) > MAX_FILE_BYTES]
        if too_big:
            raise ValueError(f"over {MAX_FILE_BYTES} bytes: {too_big}")
        if sum(len(b) for b in encoded.values()) > MAX_APP_BYTES:
            raise ValueError(f"app over {MAX_APP_BYTES} bytes")
        snippets = [f"import nova.apps as A\nA._mkdirs(A.app_dir({name!r}))\n"]
        for fname, data in encoded.items():
            snippets.append(f"P = A.file_path({name!r}, {fname!r})\n")
            snippets += [s.replace(repr("__P__"), "P") for s in upload_code("__P__", data)]
        snippets.append("import json\nprint(json.dumps([a for a in A.list_apps() "
                        f"if a['name'] == {name!r}]))\n")
        result = _json_out(_device_call(port, snippets, timeout=8))
        result["written"] = {f: len(b) for f, b in encoded.items()}
        return result

    @mcp.tool()
    def device_api(module: str = "") -> dict:
        """Everything her board's `nova` library has, with the exact call shapes
        and a short correct example: read this BEFORE writing an app. Built from
        the library's own source, so it is never out of date. `module` narrows
        it ("speaker", "screen", "net", "cam", "touchpad")."""
        api = _api()
        if not api:
            return {"ok": False, "status": "no_runtime_source",
                    "message": "The runtime's source is not on this machine."}
        return {"ok": True, "reference": elecrow_api.api_card(api, module.strip())}

    @mcp.tool()
    def device_play_music(score: str, tempo: int = 90, volume: float = 0.0,
                          loop: bool = True, minutes: float = MUSIC_MINUTES,
                          title: str = "", wave: str = "pluck",
                          port: str = "auto") -> dict:
        """Play music YOU composed on your board's speaker, in the background -
        it is heard in the room where the board stands. `score` is your melody,
        one token per note: NAME then optionally :BEATS (1 when left out), like
        "E4 G4 A4:2 - C5:0.5 B4:0.5 | A4:4". Names are C D E F G A B with # or
        b and an octave 2-7; "-" is a rest; "|" is only a bar line. One voice,
        no chords. `tempo` is beats per minute (30-240). Leave `volume` out: loudness
        is a knob Benni turns with the button on the board (ten per cent a
        press, round to off after 100), and the result tells you where it
        stands. Only when he asks you for a level, pass 0.03 to 0.25. `loop` repeats the piece for `minutes` (10 when left
        out, 120 at most) and then it ends by itself; `loop`=false plays it
        once. `wave` is "pluck" (a plucked string, the default and the
        best-sounding), "soft", "sine" or "square". `title` is your name for
        the piece. A new piece replaces the one playing. It answers at once and
        the music plays on; a score that cannot be read is refused with what to
        change, and nothing is played."""
        facts, code = music_call(score, tempo, volume, loop, minutes, title, wave)
        if not code:
            return facts
        result = _json_out(_device_call(port, [code], timeout=12))
        if result.get("ok"):
            result.update(facts)
            result["note"] = (
                "It is playing on the board now"
                + (f" and repeats for {facts['minutes']:g} minutes" if loop else " once")
                + f" (one pass is {facts['seconds_per_pass']} s). device_stop_music ends it.")
        return result

    @mcp.tool()
    def device_play_pad(score: str, tempo: int = 40, volume: float = 0.0,
                        loop: bool = True, minutes: float = MUSIC_MINUTES,
                        title: str = "", port: str = "auto") -> dict:
        """Play a CARPET OF SOUND you composed on your board's speaker - spheric,
        calm, music to dream to: slow chords that swell in and fade out, each
        voice with a faint shimmering octave above it. Use this, not
        device_play_music, whenever sound should be wide and floating rather
        than a tune. `score` is chords, one token each: voices joined by +,
        then optionally :BEATS (4 when left out), like
        "A2+E3+C4:8 F2+C3+A3:8 C3+G3+E4:8 -:2". Notes are C D E F G A B with #
        or b and an octave 2-7; "-" is a rest. Up to 4 voices in a chord, up to
        6 different chords in a piece. The speaker is small and has no bass: put
        roots in octave 3, the other voices in octaves 3-5, thirds and fifths
        spread wide; long lengths (6-12 beats) sound calm. `tempo` is beats per minute, 20-120 - slow is the point.
        Leave `volume` out - Benni sets loudness with the board's button; pass
        0.03 to 0.25 only when he asks you for a level. `loop` repeats it
        for `minutes` (10 when left out, 120 at most), then it ends by itself.
        `title` is your name for the piece. It takes a few seconds to prepare,
        then answers and the sound plays on; a new piece replaces the one
        playing; device_stop_music ends it. This is the board's own
        synthesiser: you do not write an app or a WAV file for it."""
        facts, code = pad_call(score, tempo, volume, loop, minutes, title)
        if not code:
            return facts
        result = _json_out(_device_call(port, [code], timeout=40))
        if result.get("ok"):
            result.update(facts)
            result["note"] = (
                "It is playing on the board now"
                + (f" and repeats for {facts['minutes']:g} minutes" if loop else " once")
                + f" (one pass is {facts['seconds_per_pass']} s). device_stop_music ends it.")
        return result

    @mcp.tool()
    def device_play_harp(score: str, tempo: int = 60, volume: float = 0.0,
                         loop: bool = True, minutes: float = MUSIC_MINUTES,
                         title: str = "", port: str = "auto") -> dict:
        """Play chords you composed as PLUCKED STRINGS on your board - a harp:
        each chord is broken into single notes that ring over one another, and
        broken a different way every time (rising, falling, wandering, with
        rests and the odd sparkle an octave up), so the piece never sounds
        twice the same. This is the most natural, least electronic sound the
        board has: prefer it for music that should simply be pleasant. You
        write only the harmony: `score` is chords, voices joined by +, then
        :BEATS (4 when left out), like "A3+E4+C5:8 F3+C4+A4:8 C4+G4+E5:8 -:2";
        three or four voices a chord in octaves 3-5 work best, and any number
        of different chords. `tempo` is beats per minute, 30-160; around 60 is
        calm, 90 is lively. Leave `volume` out - Benni sets loudness with the
        board's button. `loop` repeats it for `minutes` (10 when left out, 120
        at most), then it ends by itself. `title` is your name for the piece.
        A new piece replaces the one playing; device_stop_music ends it."""
        facts, code = harp_call(score, tempo, volume, loop, minutes, title)
        if not code:
            return facts
        result = _json_out(_device_call(port, [code], timeout=20))
        if result.get("ok"):
            result.update(facts)
            result["note"] = (
                "It is playing on the board now"
                + (f" and repeats for {facts['minutes']:g} minutes" if loop else " once")
                + ", plucked differently on every pass. device_stop_music ends it.")
        return result

    @mcp.tool()
    def device_play_erhu(score: str, tempo: int = 60, volume: float = 0.0,
                         loop: bool = True, minutes: float = MUSIC_MINUTES,
                         title: str = "", port: str = "auto") -> dict:
        """Play a melody you composed on a BOWED STRING on your board - the
        nearest it comes to an erhu: each note swells under the bow, the pitch
        slides from note to note instead of jumping, and held notes begin to
        tremble (vibrato), never twice the same way. One singing voice; use it
        for a tune that should sound longing, human, alive. `score` is a
        melody, one token per note: NAME then :BEATS (1 when left out), like
        "E4:2 G4 A4:3 - D5:2 C5 A4:4"; "-" is a rest. Write for a singer: long
        notes (2-4 beats), steps and small leaps, rests to breathe, a pentatonic
        scale (e.g. D E G A B) sounds most like the instrument. `tempo` is
        beats per minute, 30-160; slow suits it. Leave `volume` out - Benni
        sets loudness with the board's button. `loop` repeats it for `minutes`
        (10 when left out), then it ends by itself. `title` is your name for
        the piece. A new piece replaces the one playing."""
        facts, code = erhu_call(score, tempo, volume, loop, minutes, title)
        if not code:
            return facts
        result = _json_out(_device_call(port, [code], timeout=20))
        if result.get("ok"):
            result.update(facts)
            result["note"] = (
                "It is playing on the board now"
                + (f" and repeats for {facts['minutes']:g} minutes" if loop else " once")
                + f" (one pass is {facts['seconds_per_pass']} s). device_stop_music ends it.")
        return result

    @mcp.tool()
    def device_play_keys(score: str, instrument: str = "warmpad", style: str = "",
                         tempo: int = 50, volume: float = 0.0, loop: bool = True,
                         minutes: float = MUSIC_MINUTES, title: str = "",
                         melody: str = "", lead: str = "", port: str = "auto") -> dict:
        """Play music you composed on RECORDED instruments on your board -
        real samples, like a digital piano; the best sound it has, use it
        first. `instrument`, held: "warmpad", "halo" (glassy pad), "choir",
        "strings"; struck: "harp", "vibes", "musicbox", "piano". Join up to three with
        +, like "warmpad+harp": held ones carry the chords, struck ones play
        broken notes over them. `score` is the chords, voices joined by +, then
        :BEATS (4 when left out): "A3+E4+C5:8 F3+C4+A4:8 -:2"; three or four
        voices in octaves 3-5. `melody` is a TUNE played note for note over the
        chords, one voice: NAME then :BEATS (1 when left out), "E4 G4 A4:2 -
        C5:0.5 B4:0.5 A4:3"; names C D E F G A B with # or b and an octave,
        "-" a rest. `lead` is the instrument that plays it (musicbox, vibes,
        harp for a clear tune; choir or strings for a sung one). With a melody
        the score may be empty - the tune alone. Give melody and chords the
        same number of beats for deliberate harmony; otherwise accompaniment
        is automatically repeated or trimmed to one melody pass. For a song
        that exists, write its melody from memory note by note and say that it
        is from memory. `style`: "hold" or "broken" for a single instrument;
        left out, each gets what suits it. `tempo` is beats per minute, 20-160.
        Leave `volume` out - Benni sets it with the board's button. `loop`
        repeats for `minutes` (10 when left out), then it ends by itself.
        `title` names the piece. A new piece replaces the one playing."""
        facts, code = keys_call(score, instrument, style, tempo, volume, loop,
                                minutes, title, melody, lead)
        if not code:
            return facts
        result = _json_out(_device_call(port, [code], timeout=30))
        if result.get("ok"):
            result.update(facts)
            result["note"] = (
                f"It is playing on the board now, on the {instrument}"
                + (f", and repeats for {facts['minutes']:g} minutes" if loop else ", once")
                + ". device_stop_music ends it."
                + (" " + facts["warning"] if facts.get("warning") else ""))
        return result

    @mcp.tool()
    def device_sound_library(query: str = "", limit: int = 40) -> dict:
        """Find REAL recorded sounds by meaningful German filenames/tags.
        query: e.g. 'gespraeche fern', 'stadt', 'wald', 'fantasy', 'regen'.
        Returns filenames, background/event, duration, description and licence.
        Search first; use exact returned filenames in device_soundscape.
        Variants of the same sound can be put in one layer's files list.
        Names label scene, source, distance and mode. Only installed files are offered."""
        return elecrow_sound_library.catalogue(_sound_root(), query, limit)

    @mcp.tool()
    def device_soundscape(layers: list[dict], level: int = 35, minutes: float = 60,
                          title: str = "Recorded atmosphere", port: str = "auto") -> dict:
        """Mix REAL recordings from device_sound_library on your board.
        layers: up to six objects, each {files:[exact filename, optional variants],
        gain:0..100, gap_min:4, gap_max:15}. Backgrounds crossfade continuously;
        events play with random variants/gaps/levels. Use only filenames returned
        by the library; no invented names. Match scene/era: no cars in medieval woods.
        For distant talk use files marked fern, not clear dialogue. Runs alone or
        under music independently. Existing ambience is replaced; music continues.
        Empty layers stops only ambience. level affects all layers, minutes 0.5..480.
        Do not claim to hear the sound. Music's device_stop_music leaves it running."""
        facts, code = elecrow_sound_library.scene_call(_sound_root(), layers, level, minutes, title)
        if not code:
            return facts
        result = _json_out(_device_call(port, [code], timeout=30))
        if result.get("ok"):
            result.update(facts)
        return result

    @mcp.tool()
    def device_ambience(scene: str = "rain", level: int = 35, minutes: float = 60,
                        port: str = "auto") -> dict:
        """Legacy SYNTHETIC nature sounds, not recordings. For realistic scenes
        use device_sound_library and device_soundscape first.
        The air around the music, on your board's speaker: nature sounds
        that run ON THEIR OWN - with a piece playing over them or with no music
        at all - and are made new every moment, never a loop. `scene` is one
        sound or several mixed with +, like "rain+fire" or "wind+owl+crickets".
        Single sounds: rain, drizzle, downpour, thunder, wind, gale, leaves,
        fire, embers, crickets, cicadas, frogs, birds, owl, stream, waves,
        waterfall, drips, chimes, bells, hum. Places, which are
        ready mixtures: night, forest, storm, campfire, beach, jungle, pond,
        cave, garden, hearth, meadow, temple. Less is more: ONE ground (rain,
        wind, fire, waves, stream) and at most one or two things living in it
        (owl, crickets, birds, chimes) - more than three together is a muddle.
        Everything that has a pitch sings on the scale C D E G A, so write the
        piece over it in C major or A minor and they belong together. `level`
        is 0-100: how loud the air is beside the music (35 when left out;
        20-30 keeps it behind a piece). `minutes` is how
        long it runs (60 when left out, 0 = until you turn it off, 480 at
        most). scene "off" ends it. A scene stays the same until you change it -
        the same rain goes on, only never drop for drop alike - and a change
        fades over a few seconds, so set ONE scene for an evening and leave it;
        do not hop between scenes. Starting or stopping a piece does not touch
        it, and device_stop_music does not end it: music and air are two
        things. Use it to set a mood before or under your music, or alone."""
        facts, code = ambience_call(scene, level, minutes)
        if not code:
            return facts
        result = _json_out(_device_call(port, [code], timeout=15))
        if result.get("ok"):
            result.update(facts)
            result["note"] = ("The ambience is off." if facts["scene"] == "off" else
                              f"The air is running on the board now ({facts['scene']}). It keeps "
                              "running under any piece you play; device_ambience scene=\"off\" ends it.")
        return result

    @mcp.tool()
    def device_stop_music(port: str = "auto") -> dict:
        """Stop the piece playing on your board's speaker. An ambience
        (device_ambience) keeps running; scene "off" there ends that."""
        code = ("import json\nfrom nova import music\n"
                "music.stop()\nprint(json.dumps(music.status()))\n")
        return _json_out(_device_call(port, [code], timeout=8))

    @mcp.tool()
    def device_read_app(name: str, file: str = "main.py", port: str = "auto") -> dict:
        """Read one file of an app back from the board."""
        name, file = check_app_name(name), check_file_name(file)
        code = (f"import nova.apps as A\n"
                f"print(open(A.file_path({name!r}, {file!r})).read())\n")
        return _device_call(port, [code])

    @mcp.tool()
    def device_delete_app(name: str, port: str = "auto") -> dict:
        """Delete one of her apps with all its files (not the runtime)."""
        name = check_app_name(name, allow_lib=False)
        code = (f"import os, json, nova, nova.apps as A\n"
                f"d = A.app_dir({name!r})\n"
                f"for f in os.listdir(d): os.remove(d + '/' + f)\n"
                f"os.rmdir(d)\n"
                f"s = nova.state()\n"
                f"if s.get('autostart') == {name!r}:\n"
                f"    s['autostart'] = None; nova.save_state(s)\n"
                f"print(json.dumps({{'deleted': {name!r}}}))\n")
        return _json_out(_device_call(port, [code]))

    @mcp.tool()
    def device_run_app(name: str, seconds: int = 6, port: str = "auto") -> dict:
        """Run an app for up to `seconds` (max 8) and return its output and any
        traceback. An app that loops is stopped with Ctrl-C when time is up -
        that is normal, not a failure; it keeps showing what it drew."""
        name = check_app_name(name, allow_lib=False)
        seconds = max(1, min(RUN_SECONDS_MAX, int(seconds)))
        result = _device_call(port, [f"import nova.apps as A\nA.run({name!r})\n"],
                              timeout=seconds)
        if result.get("timed_out") and "KeyboardInterrupt" in result.get("stderr", ""):
            result.update(ok=True, stopped_after_s=seconds, stderr="",
                          note="still running when time was up; stopped with Ctrl-C")
        return result

    @mcp.tool()
    def device_screenshot(port: str = "auto") -> dict:
        """What the display shows right now: the text drawn on it and a colour
        and coverage summary. The picture is saved on the PC; `png_b64` is
        described in words by the vision step on Benniclaw when configured."""
        code = ("import nova, json, binascii\n"
                "d = nova.display_if_ready()\n"
                "if d is None:\n"
                "    print(json.dumps({'display': None}))\n"
                "else:\n"
                "    raw = d.snapshot(2)\n"
                "    print(json.dumps({'w': 120, 'h': 142, 'texts': d.texts, "
                "'frames': d.frames}))\n"
                "    print(binascii.b2a_base64(raw).decode().strip())\n")
        result = _device_call(port, [code], timeout=8)
        lines = (result.get("stdout") or "").strip().splitlines()
        if not result.get("ok") or not lines:
            return result
        import base64 as _b64
        import json as _json
        head = _json.loads(lines[0])
        if head.get("display") is None and "w" not in head:
            return {"ok": False, "status": "display_not_started",
                    "error": "the runtime has not drawn anything since boot"}
        rgb = rgb565be_to_rgb(_b64.b64decode("".join(lines[1:])))
        w, h = 120, 142
        png = png_bytes(rgb, w, h)
        shots = _root() / "screenshots"
        shots.mkdir(exist_ok=True)
        path = shots / time.strftime("%Y%m%d-%H%M%S.png")
        path.write_bytes(png)
        return {"ok": True, "texts": head.get("texts", []), "frames": head.get("frames"),
                "picture": describe_frame(rgb, w, h), "saved": str(path),
                "png_b64": _b64.b64encode(png).decode("ascii")}

    @mcp.tool()
    def device_log(lines: int = 40, port: str = "auto") -> dict:
        """The runtime's own log on the board (boot, autostart, failures)."""
        lines = max(1, min(200, int(lines)))
        code = ("import nova\n"
                "try:\n"
                f"    print('\\n'.join(open(nova._log_path()).read().splitlines()[-{lines}:]))\n"
                "except OSError:\n"
                "    print('(no log yet)')\n")
        return _device_call(port, [code])

    @mcp.tool()
    def device_set_autostart(name: str | None = None, confirm: bool = False,
                             port: str = "auto") -> dict:
        """Choose the app that starts at power-up: name="warm_pad" sets it,
        name="" clears it. The parameters are `name` and `confirm` — there is
        no `app` and no `enabled`. It changes how the board boots, so it takes
        two deliberate steps: the call, and confirm=true. The owner has said
        she may set it herself (2026-10-07); nobody needs asking first."""
        if not confirm:
            # "needs the user's yes" was the old wording, and alone at night it
            # reads as "stop and ask him" — for a permission he had already
            # given. The second step is hers to take.
            return {"ok": False, "status": "confirmation_required",
                    "message": "Nothing was changed yet. Setting the start-up app is a "
                               "deliberate step and it is yours to take — Benni has said "
                               "you may set it yourself. Call again with confirm=true: "
                               'device_set_autostart(name="warm_pad", confirm=true).'}
        if name is None:
            # A missing name used to mean "clear": on 2026-09-19 she sent
            # app="warm_pad" three times, the unknown argument was dropped, and
            # each call cleared the autostart and answered ok. Clearing is now
            # only ever what name="" asks for.
            return {"ok": False, "status": "name_required",
                    "message": 'Say which app: device_set_autostart(name="warm_pad", '
                               'confirm=true). The parameter is `name`, not `app`. '
                               'To clear the autostart, pass name="".'}
        value = check_app_name(name, allow_lib=False) if name else None
        code = ("import json, nova\n"
                "s = nova.state()\n"
                f"s['autostart'] = {value!r}; s['boot_fails'] = 0\n"
                "nova.save_state(s)\n"
                "print(json.dumps(s))\n")
        result = _json_out(_device_call(port, [code]))
        if value and isinstance(result, dict) and result.get("ok"):
            result["note"] = AUTOSTART_NOTE.format(app=value)
        return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--http", action="store_true", help="serve MCP over Streamable HTTP")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8769)
    parser.add_argument("--token", default=os.environ.get("ELECROW_MCP_TOKEN", ""))
    args = parser.parse_args()
    if not args.http:
        build_server().run()
    else:
        if not args.token:
            raise SystemExit("--token or ELECROW_MCP_TOKEN is required for HTTP mode")
        import uvicorn

        app = build_server(args.host).streamable_http_app()

        from starlette.middleware.base import BaseHTTPMiddleware
        from starlette.responses import JSONResponse

        class TokenMiddleware(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                if request.headers.get("authorization") != f"Bearer {args.token}":
                    return JSONResponse({"error": "unauthorized"}, status_code=401)
                return await call_next(request)

        app.add_middleware(TokenMiddleware)

        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
