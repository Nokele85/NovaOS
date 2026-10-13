"""An add-on for Nova, as small as one can be and still be complete.

Copy this folder, rename it, and replace the three example tools with your own.
An add-on is its own program: a small tool server Nova reaches through one
entry in the MCP_SERVERS file. It imports nothing of hers, so it can be
written, tested and thrown away without touching the core.

What the example shows, because every real add-on needs the same things:

    notebook_status    a tool that only looks       -> safe to call any time
    notebook_add       a tool that changes something -> asks for confirm=true
    notebook_list      a tool that reads back what the add-on keeps

Run it by hand to see it start (it then waits for a client on stdin):
    python tools/addon_template/server.py
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP

#: Shown to Nova as the server's name; keep it short and lower-case.
NAME = "notebook"

#: Results travel into a prompt. Past a few hundred characters the house cuts
#: them, so an add-on that answers briefly is an add-on she can read.
MAX_ITEMS = 20
MAX_TEXT = 200


def data_dir() -> Path:
    """Where this add-on keeps its own files. Never inside the repository:
    what an add-on stores is personal, and the repository is public."""
    root = Path(os.environ.get("ADDON_DATA", str(Path.home() / ".nova-addons" / NAME)))
    root.mkdir(parents=True, exist_ok=True)
    return root


def _load() -> list[dict]:
    try:
        return json.loads((data_dir() / "notes.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def _save(notes: list[dict]) -> None:
    (data_dir() / "notes.json").write_text(json.dumps(notes, ensure_ascii=False, indent=1),
                                          encoding="utf-8")


def build_server() -> FastMCP:
    mcp = FastMCP(NAME)

    # The first line of a docstring is what Nova reads when she decides
    # whether to use the tool. Say what it does and when, in plain words.

    @mcp.tool()
    def notebook_status() -> dict:
        """How many notes the notebook holds and when the last one was added.
        Looks only; changes nothing."""
        notes = _load()
        return {"ok": True, "notes": len(notes),
                "last_added": notes[-1]["at"] if notes else None}

    @mcp.tool()
    def notebook_add(text: str, confirm: bool = False) -> dict:
        """Add one short note to the notebook. It stays until someone removes
        it, so this needs confirm=true."""
        text = " ".join(str(text).split())
        if not text:
            # Say what was wrong and how to do it right: she reads this and
            # tries again, and a bare "invalid" teaches her nothing.
            return {"ok": False, "error": "text is empty; pass the note as text"}
        if len(text) > MAX_TEXT:
            return {"ok": False,
                    "error": f"text has {len(text)} characters; a note holds {MAX_TEXT}"}
        if not confirm:
            return {"ok": False, "status": "confirm",
                    "error": "this writes a note that stays; call again with confirm=true"}
        notes = _load()
        notes.append({"text": text, "at": time.strftime("%Y-%m-%d %H:%M")})
        _save(notes)
        return {"ok": True, "notes": len(notes)}

    @mcp.tool()
    def notebook_list(limit: int = 5) -> dict:
        """The newest notes in the notebook, newest first."""
        limit = max(1, min(MAX_ITEMS, int(limit)))
        notes = _load()
        return {"ok": True, "total": len(notes), "notes": notes[::-1][:limit]}

    return mcp


if __name__ == "__main__":
    build_server().run()
