# Recorded ambience for the existing speaker mixer. SPEC §26.4.
import os
import random

RATE = 16000
MAX_BYTES = 3500000
CROSS = 4000                         # 250 ms between recorded backgrounds


def _random(low, high):
    return low + random.getrandbits(24) % max(1, high - low + 1)


class Layer:
    def __init__(self, spec, samples):
        self.samples = [samples[n] for n in spec["files"]]
        self.loop = spec["mode"] == "background"
        self.gain = int(spec.get("gain", 50))
        self.low = int(spec.get("gap_min", 4) * RATE)
        self.high = int(spec.get("gap_max", 15) * RATE)
        self.pos = 0
        self.wait = 0 if self.loop else _random(0, self.low)
        self.current = self.samples[_random(0, len(self.samples) - 1)]
        self.following = self.current
        self.strength = self.gain

    def render(self, out, count, mix):
        offset = 0
        while offset < count:
            if self.wait:
                skip = min(self.wait, count - offset)
                self.wait -= skip
                offset += skip
                continue
            length = len(self.current) // 2
            cross = min(CROSS, length // 4) if self.loop else 0
            left = length - self.pos
            n = min(left, count - offset)
            target = memoryview(out)[offset * 2:(offset + n) * 2]
            # Keep chunks on the crossfade boundary.
            if cross and self.pos < length - cross:
                n = min(n, length - cross - self.pos)
                target = target[:n * 2]
                mix(target, self.current, self.pos, n, self.strength * 256 // 100)
            elif cross:
                progress = self.pos - (length - cross)
                a = (cross - progress - n // 2) * self.strength * 256 // (cross * 100)
                b = (progress + n // 2) * self.strength * 256 // (cross * 100)
                mix(target, self.current, self.pos, n, max(0, a))
                mix(target, self.following, progress, n, max(0, b))
            else:
                mix(target, self.current, self.pos, n, self.strength * 256 // 100)
            self.pos += n
            offset += n
            if self.pos >= length:
                if self.loop:
                    self.current = self.following
                    self.pos = cross
                else:
                    self.pos = 0
                    self.current = self.samples[_random(0, len(self.samples) - 1)]
                    self.wait = _random(self.low, self.high)
                self.following = self.samples[_random(0, len(self.samples) - 1)]
                self.strength = self.gain * _random(85, 100) // 100


def load(specs):
    import gc
    if not 1 <= len(specs) <= 6:
        raise ValueError("one to six recorded layers")
    names = set(n for s in specs for n in s["files"])
    sizes = {}
    for name in names:
        if not name.endswith(".pcm") or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789_." for c in name) or ".." in name:
            raise ValueError("invalid sound filename")
        sizes[name] = os.stat("/sd/sounds/" + name)[6]
        if sizes[name] < 4 or sizes[name] % 2:
            raise ValueError("invalid PCM length")
    if sum(sizes.values()) > MAX_BYTES:
        raise ValueError("selected samples exceed board memory budget")
    gc.collect()
    if sum(sizes.values()) + 200000 > gc.mem_free():
        raise ValueError("not enough free RAM; select fewer sample variants")
    samples = {}
    for name in names:
        with open("/sd/sounds/" + name, "rb") as f:
            samples[name] = bytearray(f.read())
    return [Layer(s, samples) for s in specs]
