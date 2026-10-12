"""Owner-only operations on the Elecrow board - never exposed to Nova.

    python tools/elecrow_device_admin.py import-secrets
    python tools/elecrow_device_admin.py backup
    python tools/elecrow_device_admin.py flash-micropython <firmware.bin> --yes
    python tools/elecrow_device_admin.py install-runtime --yes
    python tools/elecrow_device_admin.py autostart <app|none>
    python tools/elecrow_device_admin.py sd-inventory
    python tools/elecrow_device_admin.py sd-backup
    python tools/elecrow_device_admin.py sd-format --yes

`import-secrets` copies only the two Wi-Fi networks out of the Rook firmware's
secrets.h into secrets/elecrow_device.json (gitignored) and prints key names,
never values. The Rook API token stays where it is: the board does not need it.

`flash-micropython` refuses without a full flash backup from the last 24 h:
the image it replaces is the only copy of whatever the board runs now.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))

from elecrow_device import RawRepl, Redactor, board_port, upload_code  # noqa: E402

WORKSPACE = REPO / "elecrow-firmware"
RUNTIME = WORKSPACE / "nova-runtime"
BACKUPS = WORKSPACE / "backups"
SECRETS = REPO / "secrets" / "elecrow_device.json"
ROOK_SECRETS = REPO.parent / "rook-os" / "clients" / "rook-elecrow" / "rook_elecrow" / "secrets.h"
ESPTOOL = Path.home() / ".platformio" / "packages" / "tool-esptoolpy" / "esptool.py"
FLASH_SIZE = 0x1000000          # 16 MB (W25Q128)
BACKUP_MAX_AGE_S = 24 * 3600


def _esptool(*args: str, port: str, baud: int = 460800, quiet: bool = False) -> None:
    cmd = [sys.executable, str(ESPTOOL), "--chip", "esp32s3", "--port", port,
           "--baud", str(baud), *args]
    if not quiet:
        print("$", " ".join(cmd))
    subprocess.run(cmd, check=True,
                   stdout=subprocess.DEVNULL if quiet else None)


# The board's CH340K drops bytes on long reads at 460800 baud ("Corrupt data,
# expected 0x1000 bytes but received 0xd96", twice at ~6 %, 2026-09-11). esptool
# notices and aborts, so nothing wrong is ever saved - but a 16 MB read then
# never finishes. Read in 1 MB pieces at 230400, each retried on its own.
BACKUP_BAUD = 230400
BACKUP_PIECE = 0x100000
BACKUP_TRIES = 3


def import_secrets(_args) -> None:
    text = ROOK_SECRETS.read_text(encoding="utf-8", errors="replace")
    values = dict(re.findall(r'#define\s+(\w+)\s+"([^"]*)"', text))
    wifi = []
    for ssid_key, pw_key in (("ROOK_WIFI_SSID", "ROOK_WIFI_PASSWORD"),
                             ("ROOK_WIFI_SSID2", "ROOK_WIFI_PASSWORD2")):
        if values.get(ssid_key):
            wifi.append({"ssid": values[ssid_key], "password": values.get(pw_key, "")})
    SECRETS.parent.mkdir(exist_ok=True)
    SECRETS.write_text(json.dumps({"wifi": wifi}, indent=1), encoding="utf-8")
    print(f"wrote {SECRETS} with {len(wifi)} Wi-Fi network(s); values not shown")


def backup(args) -> Path:
    port = board_port(args.port)
    BACKUPS.mkdir(parents=True, exist_ok=True)
    # An interrupted run (a power cut, 2026-09-11) resumes: its finished pieces
    # are still true as long as nothing was written to the board since - and
    # only this script writes, and only after a backup exists.
    unfinished = [p for p in BACKUPS.glob("elecrow_*_16mb.parts")
                  if time.time() - p.stat().st_mtime < BACKUP_MAX_AGE_S]
    if unfinished:
        parts = max(unfinished, key=lambda p: p.stat().st_mtime)
        target = parts.with_suffix(".bin")
        print(f"resuming {parts.name}")
    else:
        target = BACKUPS / time.strftime("elecrow_%Y%m%d-%H%M%S_16mb.bin")
        parts = target.with_suffix(".parts")
    parts.mkdir(exist_ok=True)
    image = bytearray()
    for offset in range(0, FLASH_SIZE, BACKUP_PIECE):
        piece = parts / f"{offset:08x}.bin"
        if piece.is_file() and piece.stat().st_size == BACKUP_PIECE:
            image += piece.read_bytes()
            print(f"  {offset + BACKUP_PIECE:#010x} / {FLASH_SIZE:#x} kept from before")
            continue
        for attempt in range(1, BACKUP_TRIES + 1):
            try:
                _esptool("read_flash", hex(offset), hex(BACKUP_PIECE), str(piece),
                         port=port, baud=BACKUP_BAUD, quiet=True)
                if piece.stat().st_size == BACKUP_PIECE:
                    break
            except subprocess.CalledProcessError:
                pass
            print(f"  {offset:#010x}: attempt {attempt} failed")
        else:
            raise SystemExit(f"could not read {offset:#x} after {BACKUP_TRIES} attempts")
        image += piece.read_bytes()
        print(f"  {offset + BACKUP_PIECE:#010x} / {FLASH_SIZE:#x} read")
    target.write_bytes(image)
    for piece in parts.iterdir():
        piece.unlink()
    parts.rmdir()
    if target.stat().st_size != FLASH_SIZE:
        raise SystemExit("backup has the wrong size")
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    target.with_suffix(".sha256").write_text(f"{digest}  {target.name}\n", encoding="ascii")
    print(f"backup {target} sha256 {digest}")
    return target


def _fresh_backup() -> Path | None:
    now = time.time()
    fresh = [p for p in BACKUPS.glob("elecrow_*_16mb.bin")
             if p.stat().st_size == FLASH_SIZE and now - p.stat().st_mtime < BACKUP_MAX_AGE_S]
    return max(fresh, key=lambda p: p.stat().st_mtime) if fresh else None


def flash_micropython(args) -> None:
    firmware = Path(args.firmware).resolve()
    if not firmware.is_file():
        raise SystemExit(f"no such file: {firmware}")
    saved = _fresh_backup()
    if saved is None:
        raise SystemExit("no full backup from the last 24 h - run `backup` first")
    if not args.yes:
        raise SystemExit("this erases the board; add --yes after checking the port")
    print(f"backup present: {saved.name}")
    print(f"firmware sha256 {hashlib.sha256(firmware.read_bytes()).hexdigest()}")
    port = board_port(args.port)
    _esptool("erase_flash", port=port)
    _esptool("write_flash", "0", str(firmware), port=port)
    print("flashed; next: install-runtime --yes")


def install_runtime(args) -> None:
    if not args.yes:
        raise SystemExit("this replaces the board's start-up code; add --yes")
    files = sorted(p for p in RUNTIME.rglob("*.py"))
    port = board_port(args.port)
    redact = Redactor.from_file(SECRETS)
    with RawRepl(port) as repl:
        repl.exec("import os\nfor d in ('/lib', '/lib/nova', '/apps'):\n"
                  "    try:\n        os.mkdir(d)\n    except OSError:\n        pass\n")
        for path in files:
            target = "/" + path.relative_to(RUNTIME).as_posix()
            for code in upload_code(target, path.read_bytes()):
                result = repl.exec(code, timeout=10)
                if not result["ok"]:
                    raise SystemExit(f"{target}: {redact(result['stderr'])}")
            # every chunk answered "ok" on 2026-10-12 and __init__.py was still
            # empty on the board, which then booted without Wi-Fi: count the bytes
            size = repl.exec(f"import os\nprint(os.stat({target!r})[6])\n", timeout=10)
            if size["stdout"].strip() != str(len(path.read_bytes())):
                raise SystemExit(f"{target}: {size['stdout'].strip() or '?'} bytes on the "
                                 f"board, {len(path.read_bytes())} expected - run it again")
            print("installed", target)
        if SECRETS.is_file():
            for code in upload_code("/nova_secrets.json", SECRETS.read_bytes()):
                repl.exec(code, timeout=10)
            print("installed /nova_secrets.json (values not shown)")
        else:
            print("no secrets/elecrow_device.json - run import-secrets for Wi-Fi")
        # drop what an earlier runtime left behind (a renamed module that still
        # imports is worse than a missing one - `nova.display` was both a
        # module and an accessor once, and the module won)
        wanted = sorted("/" + p.relative_to(RUNTIME).as_posix() for p in files)
        stale = repl.exec(
            "import os, json\n"
            f"keep = {wanted!r}\n"
            "gone = []\n"
            "for d in ('/lib/nova', '/lib', '/'):\n"
            "    for f in os.listdir(d):\n"
            "        p = (d + '/' + f).replace('//', '/')\n"
            "        if p.endswith('.py') and p not in keep and p != '/lib/sdcard.py':\n"
            "            os.remove(p); gone.append(p)\n"
            "print(json.dumps(gone))\n", timeout=10)
        if stale["ok"] and stale["stdout"].strip() not in ("[]", ""):
            print("removed stale:", stale["stdout"].strip())
        if not (RUNTIME / "lib" / "sdcard.py").is_file():
            print("note: lib/sdcard.py (micropython-lib) missing - apps stay in flash")
    print("done; press RESET or power-cycle the board")


SD_BACKUP_MAX_BYTES = 64 * 1024 * 1024     # beyond this, a card reader is faster

_SD_WALK = (
    "import os, json, nova.sd as S\n"
    "st = S.mount()\n"
    "out = {'status': st, 'files': []}\n"
    "if st == 'mounted':\n"
    "    out['unreadable'] = []\n"
    "    def walk(p):\n"
    "        try:\n"
    "            entries = list(os.ilistdir(p))\n"
    "        except OSError as e:\n"
    "            out['unreadable'].append([p + '/', repr(e)]); return\n"
    "        for e in entries:\n"
    "            q = p + '/' + e[0]\n"
    "            if e[1] == 0x4000:\n"
    "                out['files'].append([q + '/', -1]); walk(q)\n"
    "            else:\n"
    "                try:\n"
    "                    out['files'].append([q, os.stat(q)[6]])\n"
    "                except OSError as err:\n"
    "                    out['unreadable'].append([q, repr(err)])\n"
    "    walk('/sd')\n"
    "    v = os.statvfs('/sd'); out['total'] = v[0] * v[2]; out['free'] = v[0] * v[3]\n"
    "print(json.dumps(out))\n")


def _sd_inventory(repl) -> dict:
    # a Raspberry Pi boot card has hundreds of overlay files; at SoftSPI's
    # 66 KB/s walking them takes minutes, not seconds
    result = repl.exec(_SD_WALK, timeout=600)
    if not result["ok"]:
        raise SystemExit(f"inventory failed: {result['stderr'][-400:]}")
    return json.loads(result["stdout"].strip().splitlines()[-1])


def sd_inventory(args) -> dict:
    redact = Redactor.from_file(SECRETS)
    with RawRepl(board_port(args.port)) as repl:
        inv = _sd_inventory(repl)
    files = [f for f in inv["files"] if f[1] >= 0]
    print(f"card: {inv['status']}")
    if inv["status"] == "mounted":
        print(f"size {inv['total'] / 1e9:.2f} GB, free {inv['free'] / 1e9:.2f} GB, "
              f"{len(files)} files, {sum(f[1] for f in files) / 1e6:.2f} MB used by files")
        for path, size in inv["files"][:200]:
            print(f"  {'<dir>' if size < 0 else size:>10}  {redact(path)}")
        if len(inv["files"]) > 200:
            print(f"  ... and {len(inv['files']) - 200} more")
    return inv


def sd_backup(args) -> Path:
    import base64
    redact = Redactor.from_file(SECRETS)
    target = BACKUPS / time.strftime("sd_%Y%m%d-%H%M%S")
    with RawRepl(board_port(args.port)) as repl:
        inv = _sd_inventory(repl)
        if inv["status"] != "mounted":
            raise SystemExit(f"nothing to back up: card {inv['status']}")
        files = [(p, s) for p, s in inv["files"] if s >= 0]
        total = sum(s for _, s in files)
        if total > SD_BACKUP_MAX_BYTES:
            raise SystemExit(f"{total / 1e6:.0f} MB on the card - too slow over USB serial; "
                             "copy it with a card reader instead")
        manifest = []
        for path, size in files:
            code = (f"import binascii, hashlib\nf = open({path!r}, 'rb')\nh = hashlib.sha256()\n"
                    "while True:\n    b = f.read(3072)\n    if not b:\n        break\n"
                    "    h.update(b)\n    print(binascii.b2a_base64(b).decode().strip())\n"
                    "f.close()\nprint('SHA', binascii.hexlify(h.digest()).decode())\n")
            result = repl.exec(code, timeout=30 + size / 4000)
            lines = result["stdout"].strip().splitlines()
            if not result["ok"] or not lines or not lines[-1].startswith("SHA "):
                raise SystemExit(f"{redact(path)}: {redact(result['stderr'][-300:])}")
            data = b"".join(base64.b64decode(line) for line in lines[:-1])
            digest = hashlib.sha256(data).hexdigest()
            if len(data) != size or digest != lines[-1].split()[1]:
                raise SystemExit(f"{redact(path)}: copy does not match the card")
            local = target / path.removeprefix("/sd/")
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_bytes(data)
            manifest.append({"path": path, "bytes": size, "sha256": digest})
            print(f"  saved {size:>9}  {redact(path)}")
    target.mkdir(parents=True, exist_ok=True)
    (target / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    print(f"backup {target}: {len(manifest)} files, {total} bytes, each checked by sha256")
    return target


def _fresh_sd_backup() -> Path | None:
    now = time.time()
    done = [p for p in BACKUPS.glob("sd_*/manifest.json") if now - p.stat().st_mtime < BACKUP_MAX_AGE_S]
    return max(done, key=lambda p: p.stat().st_mtime).parent if done else None


def sd_format(args) -> None:
    if not args.yes:
        raise SystemExit("this erases the TF card; add --yes")
    with RawRepl(board_port(args.port)) as repl:
        inv = _sd_inventory(repl)
        has_files = inv["status"] == "mounted" and any(s >= 0 for _, s in inv["files"])
        if has_files and _fresh_sd_backup() is None:
            if not args.without_backup:
                raise SystemExit("the card holds files and there is no sd backup from the last "
                                 "24 h - run `sd-backup` first (or --without-backup, if the "
                                 "owner has said the contents may go)")
            # the owner waived the copy; what is being erased is still written down
            BACKUPS.mkdir(parents=True, exist_ok=True)
            record = BACKUPS / time.strftime("sd_erased_%Y%m%d-%H%M%S.json")
            record.write_text(json.dumps(inv, indent=1), encoding="utf-8")
            print(f"no copy (owner's decision); inventory of what is erased: {record}")
        if inv["status"].startswith(("driver_missing", "failed", "not_tried")):
            raise SystemExit(f"card not usable: {inv['status']}")
        result = repl.exec(
            "import os, json, nova.sd as S\n"
            "c = S.card()\n"
            "try:\n    os.umount('/sd')\nexcept OSError:\n    pass\n"
            "os.VfsFat.mkfs(c)\n"
            "os.mount(os.VfsFat(c), '/sd')\n"
            "S._status = 'mounted'\n"
            "for d in ('/sd/apps', '/sd/apps/_lib'):\n    os.mkdir(d)\n"
            "f = open('/sd/README.txt', 'w')\n"
            "f.write('Novas Apps: /sd/apps/<name>/main.py - gemeinsame Module: /sd/apps/_lib/\\n')\n"
            "f.close()\n"
            "back = open('/sd/README.txt').read()\n"
            "v = os.statvfs('/sd')\n"
            "print(json.dumps({'total': v[0] * v[2], 'free': v[0] * v[3], 'root': os.listdir('/sd'),\n"
            "                  'write_read_back': back.startswith('Novas Apps'), 'retries': S.retries()}))\n",
            timeout=120)
    if not result["ok"]:
        raise SystemExit(f"format failed: {result['stderr'][-400:]}")
    print("formatted and prepared:", result["stdout"].strip())


def autostart(args) -> None:
    name = None if args.app == "none" else args.app
    port = board_port(args.port)
    with RawRepl(port) as repl:
        result = repl.exec("import nova, json\ns = nova.state()\n"
                           f"s['autostart'] = {name!r}; s['boot_fails'] = 0\n"
                           "nova.save_state(s)\nprint(json.dumps(s))\n")
    print(result["stdout"] or result["stderr"])


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--port", default="auto")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("import-secrets").set_defaults(fn=import_secrets)
    sub.add_parser("backup").set_defaults(fn=backup)
    f = sub.add_parser("flash-micropython")
    f.add_argument("firmware")
    f.add_argument("--yes", action="store_true")
    f.set_defaults(fn=flash_micropython)
    i = sub.add_parser("install-runtime")
    i.add_argument("--yes", action="store_true")
    i.set_defaults(fn=install_runtime)
    sub.add_parser("sd-inventory").set_defaults(fn=sd_inventory)
    sub.add_parser("sd-backup").set_defaults(fn=sd_backup)
    s = sub.add_parser("sd-format")
    s.add_argument("--yes", action="store_true")
    s.add_argument("--without-backup", action="store_true",
                   help="only when the owner said the card's contents may go")
    s.set_defaults(fn=sd_format)
    a = sub.add_parser("autostart")
    a.add_argument("app")
    a.set_defaults(fn=autostart)
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
