"""What her board really has, read off the runtime's own source — and a check
that holds an app against it before the app is written.

She writes MicroPython for a board whose library (`nova`) exists nowhere but on
that board and in `elecrow-firmware/nova-runtime/lib/nova/`. A model guesses the
rest: on 2026-10-07 her `warm_pad` called `nova.speaker.tone(220, 1200)` (the
module is never imported, and the first argument is the io chip) and
`nova.display.clear()` (`display` is a function, and there is no `clear`). It
would have slept until the full hour and died on the tone. Twice before that she
had written CircuitPython (`board`, `displayio`) for it.

Nothing here is hand-kept. The reference and the check are both built from the
library's source with `ast`, so they are right for as long as the source is.
Host-side and pure Python: no board, no model, no network.
"""
from __future__ import annotations

import ast
from pathlib import Path

#: Modules a model reaches for that this board does not have. CircuitPython's
#: are here by name because they are the ones she actually wrote.
NOT_ON_THE_BOARD = {
    "board": "that is CircuitPython; pins are in `nova.board` (constants) and `machine.Pin`",
    "displayio": "that is CircuitPython; her screen is `nova.display()`",
    "terminalio": "that is CircuitPython; `nova.display().text(...)` draws text",
    "digitalio": "that is CircuitPython; use `machine.Pin`",
    "busio": "that is CircuitPython; use `machine.I2C` / `machine.SPI`",
    "analogio": "that is CircuitPython; use `machine.ADC`",
    "pwmio": "that is CircuitPython; use `machine.PWM`",
    "neopixel_write": "that is CircuitPython",
    "supervisor": "that is CircuitPython",
    "microcontroller": "that is CircuitPython; use `machine`",
    "audiocore": "that is CircuitPython; sound is `nova.speaker`",
    "audiobusio": "that is CircuitPython; sound is `nova.speaker`",
    "numpy": "there is no numpy on a microcontroller; use `math` and plain lists",
    "requests": "not installed; the network is `nova.net` and `socket`",
    "threading": "not on this board; use `time.sleep` or `asyncio`",
    "subprocess": "this is a microcontroller, not a computer with a shell",
    "tkinter": "there is no desktop; her screen is `nova.display()`",
    "pygame": "there is no desktop; her screen is `nova.display()`",
    "PIL": "not on this board",
    "datetime": "not on this board; use `time.localtime()` (the clock is UTC)",
}

#: What `nova`'s accessors hand back, so a method called on the result can be
#: checked. (module, class) — read from the accessors' own bodies.
ACCESSOR_RETURNS = {"display": ("screen", "Display"), "io": ("aw9523", "AW9523")}


def _signature(fn: ast.FunctionDef, *, method: bool = False) -> tuple[str, list[str], int, bool]:
    """(pretty signature, parameter names, how many are required, takes *args/**kw)."""
    args = fn.args
    names = [a.arg for a in args.posonlyargs + args.args]
    if method and names:
        names = names[1:]                          # self
    defaults = len(args.defaults)
    required = max(0, len(names) - defaults)
    shown = []
    all_args = (args.posonlyargs + args.args)[1 if method else 0:]
    first_default = len(all_args) - defaults
    for i, a in enumerate(all_args):
        if i >= first_default:
            shown.append(f"{a.arg}={ast.unparse(args.defaults[i - first_default])}")
        else:
            shown.append(a.arg)
    loose = bool(args.vararg or args.kwarg)
    return f"{fn.name}({', '.join(shown)})", names, required, loose


def load_api(lib_dir: str | Path) -> dict:
    """The `nova` package as data: per module its functions, classes and constants."""
    lib = Path(lib_dir)
    api: dict = {}
    for path in sorted(lib.glob("*.py")):
        mod = "nova" if path.stem == "__init__" else f"nova.{path.stem}"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        entry = {"functions": {}, "classes": {}, "constants": [], "doc": ""}
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and not node.name.startswith("_"):
                sig, names, required, loose = _signature(node)
                entry["functions"][node.name] = {
                    "sig": sig, "params": names, "required": required, "loose": loose,
                    "doc": (ast.get_docstring(node) or "").split("\n\n")[0].replace("\n", " ")}
            elif isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
                methods = {}
                for item in node.body:
                    if isinstance(item, ast.FunctionDef) and not item.name.startswith("_"):
                        sig, names, required, loose = _signature(item, method=True)
                        methods[item.name] = {"sig": sig, "params": names,
                                              "required": required, "loose": loose}
                entry["classes"][node.name] = methods
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id.isupper():
                        entry["constants"].append(t.id)
        api[mod] = entry
    return api


def api_card(api: dict, module: str = "") -> str:
    """The reference she reads: what exists, with the exact call shapes."""
    wanted = [m for m in api if not module or m == module or m == f"nova.{module}"]
    lines = ["Her board runs MicroPython with the `nova` library. Everything below exists; "
             "nothing else in `nova` does. Submodules must be imported before use "
             "(`from nova import speaker`). The clock is UTC. The screen is 240x284.", ""]
    for mod in wanted:
        e = api[mod]
        if mod in ("nova.boot_anim", "nova.apps", "nova.aw9523", "nova.sd") and not module:
            continue                                # the runtime's own plumbing
        lines.append(f"## {mod}" + ("" if mod == "nova" else f"   — `from nova import {mod.split('.')[1]}`"))
        for f in e["functions"].values():
            lines.append(f"- {mod}.{f['sig']}" + (f" — {f['doc'][:110]}" if f["doc"] else ""))
        for cls, methods in e["classes"].items():
            how = next((f"`nova.{acc}()`" for acc, (m, c) in ACCESSOR_RETURNS.items()
                        if f"nova.{m}" == mod and c == cls), f"`{cls}(...)`")
            lines.append(f"- class {cls} — get it with {how}; methods: "
                         + ", ".join(m["sig"] for m in methods.values()))
        if e["constants"] and mod in ("nova.screen", "nova.board"):
            lines.append("- constants: " + ", ".join(e["constants"][:24]))
        lines.append("")
    lines += ["## A sound and a line of text, written correctly",
              "```python",
              "import nova",
              "from nova import speaker",
              "speaker.tone(nova.io(), 220, 1200)      # io first, then Hz, then ms",
              "d = nova.display()                       # a function: call it",
              "d.fill()                                 # there is no clear()",
              "d.text('Nova war hier.', 8, 8)",
              "d.show()",
              "```"]
    return "\n".join(lines)


def _dotted(node: ast.AST) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return ""


def check_app(files: dict[str, str], api: dict) -> list[str]:
    """What will fail when this app runs, in words she can act on. [] = nothing found.

    Only the certain cases: a syntax error, a module the board does not have, a
    name `nova` does not have, a submodule used without importing it, a function
    used as an object, a method the object does not have, and arguments that
    cannot fit. An app this passes can still be wrong; one it refuses is.
    """
    problems: list[str] = []
    own = {Path(f).stem for f in files}
    submodules = {m.split(".", 1)[1] for m in api if m != "nova"}
    root = api.get("nova", {"functions": {}, "classes": {}, "constants": []})

    for fname, source in files.items():
        if not fname.endswith(".py"):
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError as e:
            problems.append(f"{fname} line {e.lineno}: syntax error — {e.msg}")
            continue

        imported_subs: set[str] = set()         # nova submodules this file imports
        alias: dict[str, str] = {}              # local name -> "nova.speaker"
        holds: dict[str, tuple[str, str]] = {}  # local name -> (module, class) it was given
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    top = a.name.split(".")[0]
                    if top in NOT_ON_THE_BOARD and top not in own:
                        problems.append(f"{fname} line {node.lineno}: `import {a.name}` — "
                                        f"{NOT_ON_THE_BOARD[top]}")
                    if a.name.startswith("nova."):
                        sub = a.name.split(".")[1]
                        if sub not in submodules:
                            problems.append(f"{fname} line {node.lineno}: there is no `{a.name}`; "
                                            f"nova has: {', '.join(sorted(submodules))}")
                        imported_subs.add(sub)
                        alias[a.asname or a.name] = a.name
            elif isinstance(node, ast.ImportFrom):
                top = (node.module or "").split(".")[0]
                if top in NOT_ON_THE_BOARD and top not in own:
                    problems.append(f"{fname} line {node.lineno}: `from {node.module} import …` — "
                                    f"{NOT_ON_THE_BOARD[top]}")
                if node.module == "nova":
                    for a in node.names:
                        if a.name in submodules:
                            imported_subs.add(a.name)
                            alias[a.asname or a.name] = f"nova.{a.name}"
                        elif (a.name not in root["functions"] and a.name not in root["constants"]
                              and a.name != "*"):
                            problems.append(f"{fname} line {node.lineno}: nova has no `{a.name}`")
            elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                made = _dotted(node.value.func)
                if made.startswith("nova.") and made.split(".")[1] in ACCESSOR_RETURNS:
                    for t in node.targets:
                        if isinstance(t, ast.Name):
                            holds[t.id] = ACCESSOR_RETURNS[made.split(".")[1]]

        def check_call(node: ast.Call, where: str, spec: dict) -> None:
            given = len(node.args)
            if not spec["loose"] and given > len(spec["params"]):
                problems.append(f"{fname} line {node.lineno}: {where} takes at most "
                                f"{len(spec['params'])} arguments — {spec['sig']}")
            named = {k.arg for k in node.keywords if k.arg}
            if not spec["loose"]:
                for k in sorted(named - set(spec["params"])):
                    problems.append(f"{fname} line {node.lineno}: {where} has no argument "
                                    f"`{k}` — {spec['sig']}")
            if given + len(named & set(spec["params"])) < spec["required"]:
                problems.append(f"{fname} line {node.lineno}: {where} needs "
                                f"{spec['required']} argument(s) — {spec['sig']}")
            # the io chip is the first argument of everything that makes a sound
            if spec["params"][:1] == ["io"] and node.args and isinstance(node.args[0], ast.Constant):
                problems.append(f"{fname} line {node.lineno}: {where} — the first argument is the "
                                f"io chip, not {node.args[0].value!r}: "
                                f"{where.rsplit('.', 1)[-1]}(nova.io(), …) — {spec['sig']}")

        for node in ast.walk(tree):
            if not isinstance(node, (ast.Attribute, ast.Call)):
                continue
            target = node.func if isinstance(node, ast.Call) else node
            path = _dotted(target)
            if not path:
                # nova.display().clear() — a method on what an accessor returns
                if (isinstance(node, ast.Call) and isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Call)):
                    made = _dotted(target.value.func)
                    if made.startswith("nova.") and made.split(".")[1] in ACCESSOR_RETURNS:
                        m, c = ACCESSOR_RETURNS[made.split(".")[1]]
                        methods = api.get(f"nova.{m}", {}).get("classes", {}).get(c, {})
                        if methods and target.attr not in methods:
                            problems.append(f"{fname} line {node.lineno}: {c} has no `{target.attr}()`; "
                                            f"it has: {', '.join(methods)}")
                continue
            parts = path.split(".")
            head = alias.get(parts[0], parts[0])
            full = (head.split(".") + parts[1:]) if head != parts[0] else parts
            if full[0] == "nova" and len(full) >= 2:
                second = full[1]
                if second in submodules:
                    if second not in imported_subs and isinstance(node, ast.Attribute) and len(full) == 2:
                        problems.append(f"{fname} line {node.lineno}: `nova.{second}` is a module that "
                                        f"`import nova` does not load — add `from nova import {second}`")
                    mod = api[f"nova.{second}"]
                    if len(full) >= 3 and isinstance(node, ast.Call):
                        name = full[2]
                        if name in mod["functions"]:
                            check_call(node, f"{second}.{name}", mod["functions"][name])
                        elif name not in mod["classes"] and name not in mod["constants"]:
                            problems.append(f"{fname} line {node.lineno}: nova.{second} has no "
                                            f"`{name}`; it has: {', '.join(list(mod['functions']) + list(mod['classes']))}")
                elif second in root["functions"]:
                    if len(full) >= 3:
                        how = ""
                        if second in ACCESSOR_RETURNS:
                            m, c = ACCESSOR_RETURNS[second]
                            methods = api.get(f"nova.{m}", {}).get("classes", {}).get(c, {})
                            how = f" — then use: {', '.join(methods)}"
                        problems.append(f"{fname} line {node.lineno}: `nova.{second}` is a function, "
                                        f"not an object: call it first, x = nova.{second}(){how}")
                    elif isinstance(node, ast.Call):
                        check_call(node, f"nova.{second}", root["functions"][second])
                elif second not in root["constants"] and len(full) == 2 and not second.startswith("_"):
                    problems.append(f"{fname} line {node.lineno}: nova has no `{second}`; it has: "
                                    f"{', '.join(sorted(list(root['functions']) + sorted(submodules)))}")
            elif parts[0] in holds and len(parts) == 2 and isinstance(node, ast.Call):
                m, c = holds[parts[0]]
                methods = api.get(f"nova.{m}", {}).get("classes", {}).get(c, {})
                if methods and parts[1] not in methods:
                    problems.append(f"{fname} line {node.lineno}: {c} has no `{parts[1]}()`; "
                                    f"it has: {', '.join(methods)}")
                elif methods:
                    check_call(node, f"{parts[0]}.{parts[1]}", methods[parts[1]])
    return list(dict.fromkeys(problems))          # each once, in order
