"""Instrument packs for the board, cut out of a SoundFont (.sf2).

The board's own voices are sums of sine waves and a plucked-string loop; to the
owner's ear they were a whistle and an egg slicer. A digital piano does not
compute its sound, it replays recordings — and so can a microcontroller, if the
recordings are small enough. This reads a SoundFont on the PC and writes, per
instrument, one `.bin` the board loads: a JSON line (the zones: root pitch,
length, loop) followed by the samples, mono, 16 bit, 16 kHz.

    python tools/elecrow_samples.py GeneralUser-GS.sf2 out_dir

Nothing here is specific to one SoundFont; INSTRUMENTS names General MIDI
programs, which every GM bank has.
"""
from __future__ import annotations

import array
import json
import struct
import sys
import warnings
from pathlib import Path

with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    import audioop

RATE = 16000
#: name on the board -> (General MIDI program, how many zones, seconds kept)
INSTRUMENTS = {
    "warmpad": (89, 5, 3.0),
    "halo": (94, 5, 3.0),
    "choir": (52, 5, 2.5),
    "strings": (48, 5, 2.5),
    "harp": (46, 6, 2.5),
    "vibes": (11, 5, 3.0),
    "musicbox": (10, 5, 2.5),
    "piano": (0, 6, 3.0),
}
KEYS = (48, 86)                 # C3..D6: what the little speaker carries
PEAK = 20000


def _chunks(data: bytes, start: int, end: int):
    pos = start
    while pos + 8 <= end:
        tag, size = data[pos:pos + 4], struct.unpack_from("<I", data, pos + 4)[0]
        yield tag, pos + 8, size
        pos += 8 + size + (size & 1)


def load(path: Path) -> dict:
    """The parts of a SoundFont needed to find an instrument's samples."""
    data = path.read_bytes()
    if data[:4] != b"RIFF" or data[8:12] != b"sfbk":
        raise ValueError("not a SoundFont (.sf2)")
    found: dict = {}
    for tag, pos, size in _chunks(data, 12, len(data)):
        if tag != b"LIST":
            continue
        for sub, spos, ssize in _chunks(data, pos + 4, pos + size):
            found[sub] = (spos, ssize)
    def rows(tag, fmt):
        pos, size = found[tag]
        step = struct.calcsize(fmt)
        return [struct.unpack_from(fmt, data, pos + i) for i in range(0, size - step + 1, step)]
    return {
        "data": data, "smpl": found[b"smpl"][0],
        "phdr": rows(b"phdr", "<20sHHHIII"), "pbag": rows(b"pbag", "<HH"),
        "pgen": rows(b"pgen", "<HH"), "inst": rows(b"inst", "<20sH"),
        "ibag": rows(b"ibag", "<HH"), "igen": rows(b"igen", "<HH"),
        "shdr": rows(b"shdr", "<20sIIIIIBbHH"),
    }


def _zones(sf: dict, program: int, bank: int = 0) -> list[dict]:
    """Every sample an instrument plays: root key, key range, loop."""
    phdr = sf["phdr"]
    index = next(i for i, p in enumerate(phdr[:-1]) if p[1] == program and p[2] == bank)
    instruments = []
    for bag in range(phdr[index][3], phdr[index + 1][3]):
        for g in range(sf["pbag"][bag][0], sf["pbag"][bag + 1][0]):
            oper, amount = sf["pgen"][g]
            if oper == 41:
                instruments.append(amount)
    zones = []
    for inst in instruments:
        general: dict = {}
        for bag in range(sf["inst"][inst][1], sf["inst"][inst + 1][1]):
            gens = dict(sf["igen"][g] for g in range(sf["ibag"][bag][0], sf["ibag"][bag + 1][0]))
            if 53 not in gens:
                general = gens                      # the instrument's defaults
                continue
            gens = {**general, **gens}
            name, start, end, ls, le, rate, pitch, cents, _link, kind = sf["shdr"][gens[53]]
            if kind not in (1, 4) or end - start < 256:     # mono or left only
                continue
            keys = gens.get(43, 0x7F00)
            root = gens.get(58, pitch)
            if root > 127:
                root = pitch
            zones.append({
                "sample": gens[53], "lo": keys & 0xFF, "hi": keys >> 8, "root": root,
                "cents": cents, "start": start, "end": end, "rate": rate,
                "loop": (ls - start, le - start) if gens.get(54, 0) & 1 and le > ls else None,
                "name": name.split(b"\0")[0].decode("latin-1")})
    return zones


def _pick(zones: list[dict], count: int) -> list[dict]:
    """`count` zones spread across the keys the board plays, one per sample."""
    seen, kept = set(), []
    for z in sorted(zones, key=lambda z: z["root"]):
        if z["sample"] in seen or z["hi"] < KEYS[0] or z["lo"] > KEYS[1]:
            continue
        seen.add(z["sample"])
        kept.append(z)
    if len(kept) <= count:
        return kept
    step = (len(kept) - 1) / (count - 1)
    return [kept[round(i * step)] for i in range(count)]


def _cut(sf: dict, zone: dict, seconds: float) -> tuple[bytes, list[int] | None, float]:
    """(samples at 16 kHz, loop or None, root pitch in Hz) for one zone."""
    base = sf["smpl"] + zone["start"] * 2
    raw = sf["data"][base:base + (zone["end"] - zone["start"]) * 2]
    ratio = RATE / zone["rate"]
    out, _ = audioop.ratecv(raw, 2, 1, zone["rate"], RATE, None)
    pcm = array.array("h", out)
    limit = int(seconds * RATE)
    loop = None
    if zone["loop"]:
        ls, le = (int(x * ratio) for x in zone["loop"])
        le = min(le, len(pcm))
        if le - ls >= 32:
            if le > limit:                          # keep what leads into the loop's end
                cut = le - limit
                pcm, ls, le = pcm[cut:], max(0, ls - cut), le - cut
                for i in range(min(800, len(pcm))):         # and let it swell in
                    pcm[i] = pcm[i] * i // 800
            pcm = pcm[:le]
            fade = min(96, ls)
            for i in range(fade):                   # make the jump back a seamless one
                t = i / fade
                a, b = le - fade + i, ls - fade + i
                pcm[a] = int(pcm[a] * (1 - t) + pcm[b] * t)
            loop = [ls, le]
    if loop is None:
        pcm = pcm[:limit]
        fade = min(2400, len(pcm))
        for i in range(fade):
            pcm[len(pcm) - fade + i] = pcm[len(pcm) - fade + i] * (fade - i) // fade
    peak = max(1, max(abs(x) for x in pcm))
    pcm = array.array("h", (x * PEAK // peak for x in pcm))
    hertz = 440.0 * 2 ** ((zone["root"] - 69) / 12) * 2 ** (-zone["cents"] / 1200)
    return pcm.tobytes(), loop, hertz


def pack(sf: dict, name: str) -> bytes:
    """One instrument as the board loads it: a JSON line, then the samples."""
    program, count, seconds = INSTRUMENTS[name]
    head, body = [], b""
    for zone in _pick(_zones(sf, program), count):
        pcm, loop, hertz = _cut(sf, zone, seconds)
        head.append({"hz": round(hertz, 2), "n": len(pcm) // 2, "loop": loop})
        body += pcm
    if not head:
        raise ValueError(f"{name}: no usable samples in program {program}")
    return json.dumps({"name": name, "rate": RATE, "zones": head}).encode() + b"\n" + body


def main() -> None:
    source, out = Path(sys.argv[1]), Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    sf = load(source)
    for name in INSTRUMENTS:
        try:
            data = pack(sf, name)
        except (StopIteration, ValueError) as exc:
            print(f"{name}: skipped ({exc})")
            continue
        (out / f"{name}.bin").write_bytes(data)
        zones = json.loads(data.split(b"\n", 1)[0])["zones"]
        print(f"{name}: {len(data) // 1024} KB, {len(zones)} zones, roots "
              + " ".join(f"{z['hz']:.0f}{'~' if z['loop'] else ''}" for z in zones))


if __name__ == "__main__":
    main()
