# FORK.md — how this installation is built

**Every agent reads this before touching anything.** `AGENTS.md` points here in its first lines;
Claude Code reaches it through `CLAUDE.md`'s `@AGENTS.md` import, Codex and the other CLIs through
`AGENTS.md` directly. It binds all of them equally, and it is this fork's file: `AGENTS.md`,
`SPEC.md` and `docs/` stay as close to upstream as they can be, so that an upstream change merges
instead of colliding.

This installation is a fork of **[yuri-os/YuriOS](https://github.com/yuri-os/YuriOS)**, which is a working
system and stays the pattern for everything added here. The owner's standing instruction, in his
words: *everything we build from now on must be the way the template's docs require. No special
cases* ("keine Sonderlocken"). Read it before touching anything, and follow it rather than the
shortest path to a working feature.

The template, in the shape it has upstream — read the section that covers what you are about to
build, and build it that way:

| Upstream | What it settles |
|---|---|
| [SPEC.md](https://github.com/yuri-os/YuriOS/blob/main/SPEC.md) | the normative contract; §7 is her hands, §21 the night, §26 autonomous tool use |
| [AGENTS.md](https://github.com/yuri-os/YuriOS/blob/main/AGENTS.md) | this guide's parent: the gate, the layering, the commit convention |
| [CLAUDE.md](https://github.com/yuri-os/YuriOS/blob/main/CLAUDE.md) | the map, and the named extension points |
| [docs/architecture.md](https://github.com/yuri-os/YuriOS/blob/main/docs/architecture.md) | how the packages fit and what may not cross |
| [docs/tools.md](https://github.com/yuri-os/YuriOS/blob/main/docs/tools.md) | her hands, the guard, the audit log, a foreign MCP server |
| [docs/mind.md](https://github.com/yuri-os/YuriOS/blob/main/docs/mind.md) | the tick loop, the activity ladder, DREAM |
| [docs/configuration.md](https://github.com/yuri-os/YuriOS/blob/main/docs/configuration.md) | every setting and where it is read |

**Build at the named extension point, not beside it.** A new image provider is one `ImageBackend`
in `forge/backends/__init__.py`; a new medium is one `Channel`; a new intention is a function in
`mind/acts.py` plus one line in `_act`; a new dream job is a class plus a name in `BUILTIN_JOBS`;
a tool setting goes through `world/tools/spawn_env.py` and nowhere else; a foreign MCP server is an
entry in `MCP_SERVERS`, not a bespoke client. If a change does not fit any of them, that is the
signal to ask, not to invent a new place for it.

**A new capability is not finished until all six are done**, in the same change:

1. `SPEC.md` says it — a new section appended, or a row in the table it belongs to (§7.1 lists her
   hands and says *exposes exactly*, so a tool that is not there contradicts the spec);
2. the code carries `SPEC §n` citations at the lines that implement the rules;
3. tests run offline — no model, no GPU, no API key, no live service (`tests/conftest.py`'s fakes);
4. `docs/` is updated in the same change, so it never describes a build that no longer runs;
5. `python scripts/spec_map.py` regenerated (`--check` green);
6. `./scripts/check.sh` green — ruff, mypy, pytest, web.

**Two rules that override convenience:**

- **Nothing gains autonomy without the owner's explicit yes.** `mind/hands.py`'s `HANDS` is the
  vocabulary `MIND_TOOL_ALLOWLIST` is written in; adding a name there, or to that variable, means
  she may act unasked. Propose it, never do it in passing. SPEC §26 leaves code execution and a
  shell out of that set deliberately.
- **The running host is fed commits, not copied files.** It is a clone like this one. A file
  dropped onto it with `scp` leaves a machine whose state matches no revision — it took a
  216-file hash comparison to prove one such deployment was even correct (2026-09-13).

**Known debt from before this rule — do not take it as precedent.** `tell_story` is a real hand
that is missing from SPEC §7.1; `world/stories.py` and `world/qwenvoice.py` carry no `SPEC §`
citation while `world/selfies.py` carries six; `tools/` and `elecrow-firmware/` are untracked even
though `docs/tools.md` says the repository includes them. Fix these when you touch them; never
copy their shape into something new.

