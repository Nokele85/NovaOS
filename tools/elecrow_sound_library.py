"""Recorded sound catalogue and guarded scene calls (SPEC §26.4)."""
from __future__ import annotations

import json
import re
from pathlib import Path

NAME = re.compile(r"^[a-z0-9_]+\.pcm$")
MAX_BYTES = 3_500_000


def catalogue(root: Path, query: str = "", limit: int = 40) -> dict:
    path = root / "installed.json"
    if not path.is_file():
        return {"ok": False, "status": "no_library", "sounds": []}
    entries = json.loads(path.read_text(encoding="utf-8"))["sounds"]
    words = query.lower().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").split()
    selected = [e for e in entries if all(w in (
        e["file"] + " " + e.get("description", "") + " " + " ".join(e.get("tags", []))
    ).lower() for w in words)]
    shown = selected[:max(1, min(100, int(limit)))]
    result = {"ok": True, "total": len(entries), "matches": len(selected),
              "sounds": shown}
    # SPEC §7.3: never let the host cut a filename catalogue mid-JSON.
    while len(json.dumps(result)) > 8000 and shown:
        shown.pop()
    result["shown"] = len(shown)
    result["more"] = len(shown) < len(selected)
    return result


def scene_call(root: Path, layers: list[dict], level: int, minutes: float,
               title: str) -> tuple[dict, str]:
    if not layers:
        return {"ok": True, "stopped": True}, (
            "import json\nfrom nova import music\nprint(json.dumps(music.ambience('off')))\n")
    if len(layers) > 6:
        return {"ok": False, "error": "at most six layers"}, ""
    data = catalogue(root, limit=100)
    if not data["ok"]:
        return data, ""
    all_entries = json.loads((root / "installed.json").read_text(encoding="utf-8"))["sounds"]
    known = {e["file"]: e for e in all_entries}
    cleaned, used = [], set()
    try:
        for layer in layers:
            if set(layer) - {"files", "gain", "gap_min", "gap_max"}:
                raise ValueError("layer fields: files, gain, gap_min, gap_max")
            files = layer.get("files")
            if not isinstance(files, list) or not 1 <= len(files) <= 4:
                raise ValueError("each layer needs one to four catalogue filenames in files")
            modes = set()
            for name in files:
                if not isinstance(name, str) or not NAME.fullmatch(name) or name not in known:
                    raise ValueError(f"unknown sound {name!r}; use device_sound_library first")
                modes.add(known[name]["mode"])
                used.add(name)
            if len(modes) != 1:
                raise ValueError("do not mix backgrounds and events in one layer")
            gain = int(layer.get("gain", 50))
            low, high = float(layer.get("gap_min", 4)), float(layer.get("gap_max", 15))
            if not 0 <= gain <= 100 or not 0.25 <= low <= high <= 600:
                raise ValueError("gain 0..100; event gaps 0.25..600 seconds, min <= max")
            cleaned.append({"files": files, "mode": next(iter(modes)), "gain": gain,
                            "gap_min": low, "gap_max": high})
        size = sum(known[n]["bytes"] for n in used)
        if size > MAX_BYTES:
            raise ValueError("scene is too large for board RAM; select fewer/shorter variants")
        if not 0 <= int(level) <= 100 or not 0.5 <= float(minutes) <= 480:
            raise ValueError("level 0..100; minutes 0.5..480")
    except (ValueError, TypeError) as exc:
        return {"ok": False, "error": str(exc)}, ""
    code = ("import json\nfrom nova import music\n"
            f"print(json.dumps(music.soundscape({cleaned!r}, level={int(level)}, "
            f"minutes={float(minutes)!r}, title={str(title)[:60]!r})))\n")
    return {"ok": True, "layers": len(cleaned), "sample_bytes": size,
            "files": sorted(used), "source": "recordings"}, code
