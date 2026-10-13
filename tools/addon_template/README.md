# Add-on template

A working add-on to copy from. It gives Nova a tiny notebook: three tools, one small
file of its own, nothing else. Replace the notebook with whatever the add-on is for.

## What an add-on is

A program of its own that Nova reaches as a tool server. It imports nothing from the
core, keeps its own files, and is switched on with one entry in a configuration file.
That is the whole point of the [add-on rule](../../README.md#the-add-on-rule): new
abilities dock beside the core instead of being cut into it.

## Make your own in five steps

1. **Copy the folder** and give it the add-on's name: `tools/weather/`, `tools/garden/`.
2. **Rename** `NAME` at the top of `server.py`, and the three tools. Start every tool's
   name with the add-on's name (`weather_today`, `weather_week`), so that in a list of
   fifty tools it is obvious where each one comes from.
3. **Write the tools.** Each is a plain function with a docstring. Nova reads the
   docstring to decide whether to use the tool, so write it for her: what it does and
   when to reach for it.
4. **Test it without her.** Copy `tests/test_addon_template.py`, point it at your folder
   and adapt the checks. The tests need no model, no network and no Nova.
5. **Switch it on.** Add an entry like the one in `mcp-servers.example.json` to the file
   `MCP_SERVERS` points at, and restart Nova. The tools are then offered to her in
   conversation.

## The four habits the example shows

- **Looking is free, changing asks.** A tool that only reads can be called any time. A
  tool that changes something that stays takes `confirm` and refuses without it.
- **Answer briefly.** Results go into a prompt, and the house cuts long ones. Return a
  few fields, cap the lists, and offer a `limit` instead of returning everything.
- **Explain refusals.** `{"ok": false, "error": "text has 240 characters; a note holds
  200"}` lets her try again correctly. `"invalid input"` does not.
- **Keep data out of the repository.** The repository is public. What an add-on stores
  goes where `ADDON_DATA` points, outside the checkout.

## What an add-on cannot do by itself

- **Act unasked.** Tools from an add-on are available when you talk to her. For her to
  use one on her own initiative it needs a row in `yurios/mind/hands.py` and its name in
  `MIND_TOOL_ALLOWLIST`. That is a deliberate decision by a human, never part of
  installing an add-on.
- **Replace something in the core.** If the add-on takes over a job the core already
  does, the core's version is switched off by its own setting. The add-on does not
  reach in and disable it.

## Which Python runs it

Any Python that has the `mcp` package. An add-on with heavy dependencies gets an
environment of its own, so that installing it can never break hers.

```bash
python -m venv ~/nova-addons-venv
~/nova-addons-venv/bin/pip install "mcp>=1.2,<2"
```

## Trying it by hand

```bash
python tools/addon_template/server.py        # starts and waits for a client; Ctrl-C ends it
python -m pytest tests/test_addon_template.py
```
