# Tools — her hands

Her hands are reached over a real **MCP** connection: an in-repo MCP server
(`yurios/world/tools/server.py`, FastMCP over stdio) that the brain connects to as a genuine MCP
client, discovering the calls with `list_tools` rather than hardcoding them. There are nineteen
built-in calls when every optional group is enabled, plus any third-party MCP calls you mount.
Disabled groups are not advertised at all.

```ini
TOOLS_BACKEND=mcp                 # mcp | fake | off
```

| Value | What it means |
|---|---|
| `mcp` *(default)* | the real in-repo MCP server over stdio |
| `fake` | deterministic offline results — tests, and a no-deps demo |
| `off` | no hands: she talks about doing things instead of doing them |

If the SDK or the server fails, the build degrades to tools-off and keeps talking. `/api/health`
reports the truth (`"mcp"` / `"fake"` / `"off"` / `"failed: …"`).

## The hands

| Tool | Arguments | Effect | Available when |
|---|---|---|---|
| `set_timer` | `minutes` (0 < m <= `TIMER_MAX_MINUTES`), `label?` | schedules a spoken announcement | always |
| `play_music` | `action`, `track?`, `volume?` | drives browser-side synthesized ambience | always |
| `create_goal` | `text`, `kind?` | adds or deduplicates a standing goal and returns its exact ID | the mind is on |
| `take_selfie` | `look?`, `scene?`, `framing?`, `lighting?`, `mood?`, `wardrobe?`, `avoid?` | starts a render off-turn | `SELFIE_BACKEND` is not `off` |
| `show_picture` | `subject`, `avoid?` | starts a render of something other than Yuri | `SELFIE_BACKEND` is not `off` |
| `draw_comic` | `panels` (2–6 of `{picture, text?}`), `title?`, `cast?`, `layout?`, `style?`, `avoid?` | starts one comic page: every panel rendered, framed, her words set under each, sent as one image | `SELFIE_BACKEND` is not `off` |
| `web_search` | `query`, `k?` | returns titles, links, and snippets from SearXNG | `SEARCH_BACKEND` is not `off` |
| `read_page` | `url` | reads one page and shelves it | `SEARCH_BACKEND` is not `off` |
| `research` | `topic`, `depth?` | reads several pages off-turn and posts what she found | `SEARCH_BACKEND` is not `off` |
| `list_notes` | `folder?` | lists files on Yuri's workspace desk | `WORKSPACE_ENABLED=true` |
| `read_note` | `path`, `start_line?`, `end_line?` | reads all or an inclusive line range | `WORKSPACE_ENABLED=true` |
| `count_note_lines` | `path` | counts the lines in a workspace note | `WORKSPACE_ENABLED=true` |
| `write_note` | `path`, `text` | replaces a workspace note | `WORKSPACE_ENABLED=true` |
| `append_note` | `path`, `text` | adds text to a workspace note | `WORKSPACE_ENABLED=true` |
| `edit_note` | `path`, `old_text`, `new_text` | replaces one exact passage | `WORKSPACE_ENABLED=true` |
| `delete_note` | `path` | removes a workspace note | `WORKSPACE_ENABLED=true` |
| `read_skill` | `name` | reads a skill's full instructions | `SKILLS_ENABLED=true` |
| `write_skill` | `name`, `description`, `instructions` | saves or replaces a skill | `SKILLS_ENABLED=true` |
| `delete_skill` | `name` | removes a skill | `SKILLS_ENABLED=true` |
| `tell_story` | `theme`, `mood?`, `length_minutes?` | starts an asynchronous complete story, voices it through the Rook TTS bridge and delivers the finished audio to Telegram | story tool enabled |
| `propose_edit` | `surface`, `content`, `reason` | queues a change to one of her soul files for your approval | the mind is on |

Third-party calls configured through `MCP_SERVERS` are discovered and admitted through the same
guard, with `TOOL_RATE_EXTERNAL` as their default rate. The surface doesn't grow a shell. Heavy
sandboxed hands are a named later rung, not an omission to be patched around.

### set_timer

The MCP server is the *contract and audit point* — it validates and records — but the **host**
schedules the wake, because only the host owns her voice. The board is written to
`<vault>/state/timers.json` on every change and reloaded at boot; `due` is a wall epoch, so a
restart does not drop a pending timer, and one that came due while she was down is simply already
due. Past a day (`STALE_AFTER_S`) a restored timer is leftover, not a promise still owed, and is
dropped rather than announced.

When a timer elapses she announces it through the ambient seam if a voice socket or a chat
viewer is there (the text room and the terminal never open `/ws/voice` while muted, so the line
lands in the transcript as `proactive`). If nobody is in the room at all, the line still lands,
stamped `unheard`, so the inbox and the doorbell deliver it instead of re-queuing "nobody to
tell" every tick. Timer announcements are delivered by the mind loop, so they are not announced
while `MIND_ENABLED=false` or while no model is configured.

```ini
TIMER_MAX_MINUTES=1440
TOOL_RATE_TIMER=6                 # calls per minute
```

### create_goal

Explicit requests such as "set a goal to…" use `create_goal`, which writes through the host-owned
standing `GoalStore` and returns the exact `g-…` ID. A file under `workspace/goals/` is only a
working note and never becomes a goal by resembling one. The tool is absent when the mind is off.
Working one of her goals, she can use it too: the mind files that goal itself, as `goal:<id>` —
one open per goal, and not from a goal filed that way (see [mind.md](mind.md#goals-she-files-herself)).

```ini
TOOL_RATE_GOAL=6
```

### play_music

Drives a generative ambient pad synthesized in the browser — not a media library. The seam is the
point: it proves an effector that reaches the frontend, and it costs nothing to run.

Because it is a synthesizer and not a library, `track` is a closed set — `warm_pad` and
`night_piano` are the two generators `web/js/music.js` actually implements. It is annotated as a
`Literal`, so the catalog reaches the model as an `enum` in the tool schema rather than as prose in
the description; a description alone gets a made-up track name back. This is the opposite of
`take_selfie`, where the library is deliberately a starting point and anything off-menu passes
through — the difference is that a selfie prompt is free text all the way down, and a track name
has to match a generator that exists.

```ini
TOOL_RATE_MUSIC=6
```

### Workspace notes and skills

The desk calls operate only inside `vault/workspace/`: relative paths only, no `..`, dotfiles, or
paths outside the desk. `write_note` replaces a whole file; use `append_note` for a running log.
Read an existing note before changing it: `edit_note` replaces one unique passage and refuses a
no-op; set `new_text` to an empty string to remove one unique obsolete or duplicate passage. It
can also replace or delete a 1-based inclusive line range using `start_line` and `end_line`.
When that exact passage appears more than once, an empty replacement removes the later copy and
preserves the first.
`append_note` is only for text that belongs at the end. `list_notes` returns names and sizes,
with `count` first so a clipped listing still says how many files there were; a folder that
is not on the desk says so rather than looking empty. It lists `workspace/` only — kept
memory is `memory/semantic/facts.md`, written by the night, not a desk folder.
`read_note` returns one page — whole lines, up to 4,000 characters of them — with where it is
first: `start_line`, `end_line`, `line_count`, and `next_start_line`, the line to read on from
(null at the end). Reading a long note is reading again from `next_start_line`. Notes are scratch space, so they are not
versioned in the Vault.

Skills live in `vault/skills/`. `read_skill` loads the instructions for one named skill, whole up to about 8,000 characters;
`write_skill` stores a lowercase-hyphenated name, a short description that says when to use it,
and its full instructions; `delete_skill` removes it. Skills are versioned. See [The mind](mind.md#her-desk-and-her-skills)
for the storage layout and prompt behavior.

```ini
WORKSPACE_ENABLED=true             # false = no note calls or desk context
SKILLS_ENABLED=true                # false = no skill calls or catalog context
TOOL_RATE_DESK=20                  # calls per minute across notes and skills
```

### propose_edit

The one hand that reaches at her identity. The MCP server validates the surface and returns
"asked", not "done"; the host queues the proposal for you. `CONSTITUTION.md` is not on the
list and never will be. See [The mind → self-edits](mind.md#self-edits).

```ini
TOOL_RATE_SELFEDIT=1
```

### web_search, read_page and research

Her three web hands. They arrive together or not at all — searching with no way to read what you
found is half a capability, and `research` is the two of them in sequence — so one knob turns all
three on:

```ini
SEARCH_BACKEND=off                # searxng | fake | off
SEARXNG_URL=http://localhost:8080
SEARCH_RESULTS=5                  # rows per web_search
SEARCH_LANGUAGE=en
SEARCH_SAFESEARCH=1               # 0 none | 1 moderate | 2 strict
FETCH_TIMEOUT_S=8
FETCH_MAX_BYTES=2000000
RESEARCH_MAX_PAGES=5
RESEARCH_MAX_CALLS=100
TOOL_RATE_SEARCH=6
TOOL_RATE_READ=6
TOOL_RATE_RESEARCH=2
```

With `SEARCH_BACKEND=off` — the default — `list_tools` doesn't mention them at all. No hand, not a
dead one, the same rule as `SELFIE_BACKEND=off`.

> **Experimental — and this is the hand that can cost you money.** The web hands are new and still
> moving, and `research` is the one thing here that keeps spending after it has answered: it
> returns to the conversation in milliseconds and then searches, fetches and *reads*, for as long
> as that takes. A long page is a model call per passage plus an embedding per chunk — dozens to
> hundreds of calls, over half an hour, from one sentence you said in passing. On a local model
> that is fan noise. On a metered API it is a bill, and nothing in the loop stops to ask you first.
>
> `MIND_DAILY_TOKENS` is a governor, not a spend cap: it is an estimate, it never gates
> conversation, it does not abort a read already in flight, and it does not stand between a tool
> call and the run it starts. `RESEARCH_MAX_CALLS` limits the estimated model calls admitted to
> each run before a page is read. Pages that do not fit are held unread on the shelf for you to
> resume. `RESEARCH_MAX_PAGES` also bounds the number of pages and `TOOL_RATE_RESEARCH` limits
> starts per minute. The **inner life** tab shows every run and the document
> being read right now with its model-call count, and the stop button there loses nothing. If she
> is pointed at a paid API, set a spend limit with the provider as well.

**Why SearXNG.** SearXNG is the search backend because it is keyless *and* third-party-less. You run the instance, so the record of what
she searched for is a file on your own machine. That is the local-first argument applied to the
one capability that usually hands your curiosity to somebody else.

#### Setting it up

The installer asks, and does the whole thing if you say yes — pulls the image, writes a settings
file with the JSON format enabled, starts the container, and flips `SEARCH_BACKEND` in `.env`:

```bash
./install.sh --web-search      # or answer y when it asks
```

It needs Docker. If Docker isn't usable the installer says so and carries on with web search
off — an optional capability never costs you the rest of the install.

After that the container is looked after for you:

| | |
|---|---|
| `yurios start` | brings `yurios-searxng` up alongside her if it's stopped |
| `yurios doctor` | says whether she can actually search *right now* |
| `data/searxng/settings.yml` | yours to edit — engines, safesearch, languages |

The settings file is written once and never overwritten, so a rerun of the installer won't revert
your edits. It's mounted read-only, which keeps it yours: SearXNG's entrypoint chowns
`/etc/searxng` to its own uid when it can, and that quietly takes your own config file away from
you.

> **The trap this saves you from.** SearXNG ships with its JSON output format **disabled**. An
> instance that answers a browser perfectly will answer *this* with `403` until `json` is in
> `search.formats`. The installer's settings file has it on from the start; if you point
> `SEARXNG_URL` at an instance you already run, `yurios doctor` names this specific cause, because
> a bare 403 sends you looking at authentication instead.
>
> ```bash
> curl 'http://localhost:8080/search?q=test&format=json'
> ```

**Pointing her at an instance you already have** is fine — set `SEARXNG_URL` and leave the
container alone. Anything that isn't loopback is never started, stopped or recreated by YuriOS,
and a working instance on loopback that we didn't create is reported as "not ours" rather than
claimed.

**Search then read is exactly two calls,** which is `TOOL_MAX_CALLS_PER_TURN`. That is deliberate:
the budget was sized for a hand that finds a thing and a hand that opens it. Raising the cap to fit
a third call raises it for every other tool too.

#### What she reads, she keeps

This is the part that makes the web hands more than a slower lookup. A tool result is
normally 600 characters that expire when the turn ends. Instead, every page — whether `research`
fetched it or she opened it herself with `read_page` — is ingested into her
[knowledge store](mind.md): chunked, situated, embedded, and indexed with a doc and character span
she can cite. The document carries the URL it came from in its header, so a citation survives the
round trip back to a source.

```bash
ls vault/knowledge/reference/          # web-on-tea-1754557200.md, …
```

And it comes back. Every turn she assembles searches the shelf for whatever you just said and
puts the best few chunks into the system prompt as **WHAT YOU'VE READ**, each with its
`doc (chars a-b)` citation, so she can answer from a page she read last week and tell you where
it came from. That block is deliberately not the memory block: reading is not remembering, and
a page she found must never come back as something you told her. `KNOWLEDGE_K` sets how many
chunks; `KNOWLEDGE_K=0` turns the slot off and leaves the shelf as a pure archive.

The model never sees the whole page. `read_page` returns a ~400-character `gist` for her to speak
to and the full text alongside it; the brain truncates only the copy that reaches the model and the
audit log, and hands the untruncated one to the host for shelving. Two audiences, one call.

With no mind running (`MIND_ENABLED=false`, or no model chosen yet) there is no shelf, so nothing
is kept — she still searches, still reads, still tells you what she found, and the message says
plainly that it wasn't filed.

#### research is start-don't-await

A search plus three fetches plus three embedding passes is twenty-odd seconds and `TOOL_TIMEOUT_S`
is ten. So `research` follows the camera's rule (§7.6): the server validates the ask and answers
`{"status": "started"}` immediately, the turn finishes with no dead air, and the reading happens
off-turn in `yurios/world/research.py`. What she found arrives in the chat as a message a while
later, routed back to the channel that asked, followed by one soft spoken line if she's free.

`depth` controls how many pages a run reads. When Yuri leaves it out, it defaults to
`RESEARCH_MAX_PAGES`; raise that setting to allow and default to a larger run.

A page that won't open — a paywall, a 404, a PDF — is skipped, not fatal. A run where nothing
opens still ends in her saying so.

#### read_page will not read this machine

`url` is the first tool argument that comes from a language model rather than from a person, and
her own API is on `localhost:8768` with the settings panel's keys behind it. So before every
request — and again on every redirect hop, which is why redirects are followed by hand —
`yurios/world/tools/fetch.py` rejects any scheme that isn't http/https, resolves the host, and
refuses private, loopback, link-local, reserved and multicast addresses. Non-text responses are
refused too, and the body stops at `FETCH_MAX_BYTES`.

### take_selfie and show_picture

Her camera has its own page: [Selfies](selfies.md). Two hands share it — `take_selfie` for a
picture of her, `show_picture` for a picture of anything else — and with `SELFIE_BACKEND=off`
neither is advertised at all.

### tell_story

`tell_story` is a long-running host-side job (SPEC §7.8), not a text shortcut. The MCP server
validates the request and answers at once with `{"status":"started","story_id":"…"}`;
`yurios/world/stories.py` does the work off-turn, one story at a time.

**The brief is hers.** An explicit request in a text turn ("erzähl mir eine Geschichte …") is caught
before the model can answer it with a paragraph, and she writes the brief from the conversation:
`title`, `theme`, `mood`, `register` (how explicit — it outranks the writer's style rules), `form`
(who narrates, in which tense), `characters` (with pronouns), `avoid`, and per scene `scenes`,
`scene_lengths` (kurz / normal / lang), `scene_talk` and `scene_moods` (how the scene should sound).
When she brings scenes, no planner re-plans them.

How a story is made:

1. The form is fixed before the first word — hers, or the house form: Nova narrates in the first
   person, the user is "du", present tense.
2. Each scene is written with the whole story so far, told where it belongs and which scene comes
   next, so it continues instead of setting up again.
3. A cheap check after each scene repairs one fault only: a scene that skipped its own opening.
4. Below `STORY_MIN_WORDS`, bounded extension scenes are added, and only then a separate finale;
   otherwise the last scene is the ending.
5. One editor reading, in the order a German fiction editor works — plot, characters, transitions,
   perspective, tense, language, direct speech. Corrections inside one sentence are applied under
   guards; a sentence is struck only if it repeats one just before it; a perspective finding is
   logged, never patched a sentence at a time.
   Then the author revises, as in a publishing house: for each plot, character or transition note
   (and each repeat too far back to strike) the writing model in her persona may change only the
   sentences the note names and their neighbours, and the editor checks the result. A revision is
   kept only if the note is fixed and nothing new broke; otherwise the text stays and the note stays
   in the log. At most eight notes per story, about 1 cent each.
6. `outline.json` and `story.md` land in `vault/stories/<story_id>/`, and a compact entry goes into
   the journal and the story index. Story prose never becomes a fact about the user — not after the
   turn, and not at night (SPEC §21).
7. A director casts and directs every scene. Narration and quoted speech are split by rule, so not a
   letter changes; the director reads the whole story, writes down what it understands of the scene
   (situation, tension, what each character feels, the physical circumstances) and derives one
   English instruction per line from it — only what is audible: pitch, speed, volume, emotion,
   tone, texture. Narration and Nova's lines are her voice, the user's lines `STORY_VOICE_USER`,
   anyone else `STORY_VOICE_MAN` or `STORY_VOICE_WOMAN`. The direction is saved as `regie.json`; a
   scene whose direction fails or runs past five minutes is read the old way, in her voice and scene
   mood.
8. The voice reads every line with its own speaker and instruction; the WAV is compressed to MP3 and
   delivered to the channel it was asked from and to Telegram.

Measured on the same story, the editor replaced three earlier passes: 10 calls and 5.34 cents became
one call and about 2–3 cents. The director costs about 0.3–0.6 cents a scene (one call with a short
reasoning pass); a whole story costs roughly 6–8 cents on DeepSeek V4.1-Flash.

Why the director is built this way, from the hearing test of 2026-09-14 on one scene: a prototype that
described roles ("Marek: gruff and wary") wrote studio talk like "off-mic" that the voice cannot use;
a director that first analysed the situation noticed that the guide calls through stone and that the
two of them have just squeezed through a gap, and turned both into audible instructions. That
matches the Qwen3-TTS Technical Report (InstructTTSEval: CustomVoice follows acoustic descriptions at
77 %, role-play at 64 %) and MultiActor-Audiobook (arXiv 2505.13082).

```ini
STORY_ENABLED=true
TOOL_RATE_STORY=1
STORY_TTS_URL=http://192.168.0.104:8091
STORY_TTS_SUPERVISOR_URL=http://192.168.0.104:8092
STORY_TTS_SPEAKER=sohee
STORY_DIRECTOR=true
STORY_VOICE_USER=dylan
STORY_VOICE_MAN=ryan
STORY_VOICE_WOMAN=serena
STORY_TTS_LANGUAGE=German
STORY_MIN_WORDS=1500
STORY_SCENES=8
```

The voice is Qwen3-TTS **CustomVoice** — one of the model's own speakers — because that variant
honours a per-request instruction; the Base cloning model ignores instructions and read a 23-minute
story in one register. Passages are split between sentences only, never inside one and never after
an abbreviation such as "Dr.".

The second service is the point of the pair. The voice is loaded before the first passage
(`POST /an`, about 110 s) and unloaded in a `finally` (`POST /aus`, which gave back 7 336 MiB when
measured), both inside the LAN GPU lock, so a selfie waits for the voice instead of starving beside
it. Shutdown unloads it as well, because a killed job never reaches its own `finally`.

If the voice or the delivery fails, the written artifacts stay and she says the recording did not
happen; she never claims a delivery that did not occur. The always-on mind does not start stories:
`tell_story` is not one of her autonomous hands.

## How a tool call actually happens

A `## TOOLS` block is appended to her system prompt, built from the *discovered* schemas. It tells
the model: speak a short lead-in sentence first, then emit a marker.

```
"let me write that down — [[write_note {"path": "research/you.md", "text": "…"}]]"
```

The streaming parser strips markers from her speech, tolerates markers split across token
boundaries, and silently drops unknown or oversized ones. On a closed marker: guard check → MCP
call → a **continuation stream** — the original messages plus her partial reply plus a
`((tool result: …))` cue — which she finishes as the same turn. So she *speaks to* what her hands
found, rather than reciting a payload.

**First audio never waits on a tool:** the lead-in sentence reaches TTS before the call runs.
Barge-in cancels the continuation, and a barged-in tool turn persists nothing.

### Reading the marker the model actually writes

A 12B local model *will* emit a broken one, and the interesting part is *how*. Driven through
every desk tool, the live model got the closer wrong every single time:

```
[[read_note {"path": "research/probe.md"}] ]
```

A space between the brackets. `endswith("]]")` never fires on that, so the marker stayed open and
swallowed everything after it — her next sentences and her next marker — until some accidental
`]]` came along. One space cost the whole turn. The prose tools are worse: asked to serialize a
paragraph it is still composing, the model leaves literal newlines and bare `"` inside the JSON
string and sometimes stops a bracket short.

So the parser reads what arrives rather than what the directive asked for:

| what she writes | what happens |
|---|---|
| `}]]`, `}] ]`, `}]\n]` | all close the marker; a stray bracket left in the body is trimmed |
| a literal newline or bare `"` inside a prose argument | the object is re-read leniently — `json` still owns every scalar, list and nested object |
| `}]` and then end-of-stream | salvaged, if the body is otherwise complete |
| `read_note{"path": "…"}` (no space) | the name is the leading identifier; the object still parses |
| `read_note("path")`, `read_note({"path": "…"})` | parenthesized call: JSON object unwrapped, or positional values zipped onto schema order |
| anything still unreadable | audit line (`dropped: malformed marker`), marked unrun in the verbatim record, and **one** re-emit pass |

Every recovery is self-validating: it can only ever produce a call that parses, so junk still
drops. That last row matters as much as the others — a silently dropped marker stays in the
transcript, so next turn she reads her own broken call back as evidence and tells you the note
exists.

**She does not say her lead-in twice.** "Continue from where you left off" reads to the model as
"say it again, then continue", so the continuation's echo of the previous pass is matched and
dropped. The match *holds* rather than swallows, so a continuation that merely opens the same way
("I'll check." → "I'll check the other one too.") is released whole.

## The guard

Every call passes `yurios/world/tools/guard.py` first:

- an **allowlist** — exactly the discovered tools; anything else is denied,
- **one-per-turn dedupe** — the same hand with byte-identical arguments, twice in one reply, is
  the model re-emitting a marker it already spent, not a second thing she meant. A
  `status: "started"` result reads as "nothing happened" and invites exactly that; the repeat is
  denied before the bucket, so it costs her no budget,
- **per-tool rate limits** (token buckets on the injected clock),
- a **per-turn call cap**,
- a **per-call timeout**, and
- **result truncation**.

```ini
TOOL_MAX_CALLS_PER_TURN=2
TOOL_TIMEOUT_S=10
TOOL_LOG_DIR=./tool-logs
```

She can be *asked* anything; the guard decides what her hands actually do.

## The audit log

Every call — allowed **or denied** — appends one JSONL line to `TOOL_LOG_DIR`
(`data/characters/<id>/tool-logs/calls.jsonl`):

```json
{"ts": …, "tool": "write_note", "args": {…}, "verdict": "allow", "duration_ms": 12, "result": "…"}
```

```bash
tail -f data/characters/yuri/tool-logs/calls.jsonl
```

That file is the answer to "what did she actually do?", and it's the same file the switchboard's
log tab reads.

## Somebody else's MCP server

The client was always a genuine MCP client — point it at a different command line and she has
different hands. `MCP_SERVERS` relaxes that to more than one at a time:

```ini
MCP_SERVERS=./mcp-servers.json
TOOL_RATE_EXTERNAL=4              # default bucket for a discovered tool
```

```json
{
  "mcpServers": {
    "fetch": {"command": "uvx", "args": ["mcp-server-fetch"], "env": {}, "rate": 4}
  }
}
```

It is the same `{"mcpServers": {...}}` shape everything else uses, so a config you already have
pastes straight in. `rate` is the one addition — calls per minute for every tool that server
offers; without it they get `TOOL_RATE_EXTERNAL`.

The repository includes a local Elecrow firmware hand at
`tools/mcp-servers.json`. It is configured for the Windows PC that owns the
board's USB connection. Nova discovers its calls at boot; the server
instructions tell her to inspect first, build before flashing, and require
explicit confirmation for the firmware overwrite.

Unset (the default) means her own server alone, byte for byte as before. With servers configured:

- her server is mounted **first**, so a third-party server advertising a name she already has is
  ignored rather than intercepting hers — the log says so;
- tool names stay **unprefixed**, because the model reads them, the audit log records them and the
  host dispatches on them;
- a server that **won't start is skipped, not fatal**. A typo in a third-party entry costs her that
  server, not her timers and her desk. `/api/health` and the boot board report how many of each
  came up.

Discovery is the allowlist for these (§7.3): nobody here can hardcode a rate for a tool whose name
isn't known until `list_tools` answers, so whatever a mounted server offers is admitted at its
configured rate. Her own hands are *not* admitted this way — their rates encode decisions discovery
can't see, like `SELFIE_BACKEND=off` leaving the camera out of the buckets.

**Mounting a server gives her its hands, rate-limited but not reviewed.** That is the trade the
knob exists to make; make it deliberately.

## Autonomous Elecrow hands

Two sets, and the difference matters. The firmware hands — status, debug, serial
log and commands, build — describe a board whose firmware is compiled on the PC.
The playground hands below describe what the board became once the MicroPython
runtime went on it: a small computer she writes Python for.

| autonomous | hands |
|---|---|
| look | `elecrow_status`, `elecrow_debug`, `elecrow_serial_log`, `device_info`, `device_list_apps`, `device_read_app`, `device_log`, `device_screenshot`, `device_api` |
| act | `elecrow_serial_command`, `elecrow_build`, `device_write_app`, `device_run_app`, `device_exec`, `device_set_autostart`, `device_play_music`, `device_play_pad`, `device_play_harp`, `device_stop_music` |
| never unasked | `elecrow_flash`, `device_delete_app` |

`device_exec` and `device_set_autostart` moved up on 2026-10-07, by the owner's explicit yes: her
goal to arm her own app at power-up could not be reached without the second, and trying a line of
MicroPython is how an app gets fixed. Both act on the board only — a microcontroller on a USB
cable — and a reset undoes either. The four playground hands that act are *cheap*: they cost no
model call and no card, and an expensive hand's twelve hours between identical calls made
write-run-fix impossible.

`mind/hands.py`'s `HANDS` is only the vocabulary `MIND_TOOL_ALLOWLIST` may be
written in; a name there is not yet a hand she has. Calls are recorded in the
mind tool audit and can be steps of an open goal.

Why the playground half exists at all: told only the firmware five, she filed a
goal of her own — *"eine kleine, eigene Anzeige auf dem Elecrow-Board bauen, die
nur mir gehört"* (2026-09-13) — and had no hand that could reach it. Her whole
audit log holds one `device_info` call and not a single `device_write_app`. A
capability she is never told about in the words of what it is, is one she does
not have.

Firmware flashing stays out of autonomous hands. `elecrow_flash` also asks for
`confirm=true` — that is a deliberate second step, not proof that a person
agreed: the flag is set by whoever calls the tool, and the log for 2026-09-11
shows a refused `confirm=false` call followed seconds later by the same call
with the flag flipped. Treat it as the speed bump it is.

## Her playground on the Elecrow board

Once the board runs the MicroPython runtime (`elecrow-firmware/nova-runtime/`,
installed by the owner with `tools/elecrow_device_admin.py`), Nova writes and
runs her own Python apps on it — no compile, no flash:

| Tool | Effect | Gate |
|---|---|---|
| `device_info` | runtime, safe mode, SD card, Wi-Fi online (never which network), memory, apps | none |
| `device_list_apps`, `device_read_app` | what she has written | none |
| `device_write_app` | create/replace files of one app, or of `_lib` (shared modules) | names and sizes checked on both sides; the code is checked against the board's library first and a call that cannot work is refused with the correct form (`force=true` overrides) |
| `device_api` | everything the board's `nova` library has, with exact call shapes and a short correct example | none — built from the library's source by `tools/elecrow_api.py`, never hand-kept |
| `device_play_music`, `device_stop_music` | play a melody she composed (`"E4 G4 A4:2 - C5:0.5"`, one voice) on the board's speaker in the background; a loop ends by itself after `minutes` | the score is read by the board's own `nova/score.py` on the PC first and refused in words; volume is quiet by default and capped at 0.25; a loop runs 120 minutes at most |
| `device_play_harp` | chords she composed as plucked strings (Karplus-Strong), broken into single notes a different way on every pass | as `device_play_music` |
| `device_play_pad` | a carpet of sound she composed: slow chords (`"A2+E3+C4:8 F2+C3+A3:8"`, up to 4 voices, up to 6 different chords) that swell and fade, each voice with a drifting octave above it | as `device_play_music`: read by `nova/score.py` first, quiet by default, capped, a loop ends by itself |
| `device_run_app` | run an app ≤ 8 s, return output and traceback; a looping app is stopped with Ctrl-C | none |
| `device_exec` | one MicroPython snippet ≤ 8 s | none |
| `device_screenshot` | the display's framebuffer: texts drawn, colours, coverage — and, on Benniclaw, a description by `ELECROW_VISION_MODEL` | none |
| `device_delete_app`, `device_log` | tidy up, read the runtime's log | none |
| `device_set_autostart` | the app that starts at power-up | `confirm=true` after the user's yes |

Every limit sits below YuriOS' `TOOL_TIMEOUT_S` (10 s). Everything the board
prints passes a redactor on the PC that removes the Wi-Fi credentials stored on
the board. Firmware, runtime and secrets are never tools: the owner's CLI does
them, and `flash-micropython` refuses without a full flash backup from the last
24 hours.

The serial port is opened with DTR and RTS released. pyserial's default
asserts both, and the board's auto-reset circuit turns that into a reboot:
before 2026-09-11 every `elecrow_serial_log` restarted the board it read.

On Benniclaw, `tools/elecrow_mcp_stdio_proxy.py` forwards the Windows server's
tool list rather than a copy of it, opens a fresh connection per call (a
restarted Windows server no longer leaves Nova with dead hands until YuriOS
restarts), and never hands a screenshot's base64 to the model.

## Her hands in the loop

The mind can reach for these same hands as a step of an open goal — never free-floating, at
most one call per tick. The house defaults are on: `MIND_TOOLS_ENABLED=true` and
`MIND_TOOL_ALLOWLIST=*`; a character's `hands` toggle is also on by default. All three
gates must admit a call. Change the house allowlist to narrow which tools can be used, or
turn off the house or character switch. Cheap
hands (the desk, `set_timer`) are a goal step; expensive ones (`research`, `read_page`,
`web_search`, the cameras) take the whole tick and are simply not offered over
`MIND_TOOL_PRESSURE_CEILING`. `MIND_TOOL_CALLS_PER_DAY` is a cap, checked before the call.

Nothing she makes this way is sent to you. It goes on her shelf, in her gallery, or on her
desk. `tool-logs/calls.jsonl` marks every one of them `mind_tool`. The remaining omission is
the *workshop* — code execution and a shell — not mind-initiated tools. See
[The mind → her hands in the loop](mind.md#her-hands-in-the-loop) and
[the cost note](README.md#experimental--and-it-can-spend).
