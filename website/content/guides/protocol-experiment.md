---
title: Manual protocol experiment
description: Explore native Codex tools and raw-text modes in a terminal or expandable web chat.
---

Tau includes an experimental CLI for exploring native tool calls versus raw
assistant-text modes with the same OpenAI Codex subscription model. It is
separate from the production coding agent: no TUI, slash commands, extensions,
saved conversations, benchmarks, or graders.

## Run from a checkout

Requires `uv` on Windows, macOS, or Linux. Bash and PowerShell are optional
installed runtimes (`bash` and `pwsh`); Python uses the project environment.
On Windows use Git for Windows Bash, not the System32 WSL launcher:

```bash
uv sync
uv run python -m mode_experiment login
uv run python -m mode_experiment run --protocol tools --allow-unsafe-local
uv run python -m mode_experiment run --protocol modes --allow-unsafe-local
```

Login uses Tau's existing browser OAuth and `openai-codex` credential, including
refresh/persistence and manual redirect fallback. It does not change unrelated
provider preferences. Default model/reasoning is `gpt-6.1-sol` / `medium`.
Windows HTTPS uses system CA trust with certificate verification enabled; browser
callback success alone does not confirm that Python's token exchange succeeded.

**Execution is local and unsandboxed.** Generated source can access host files,
secrets, and the network. Direct read/write/edit operations are also unrestricted:
relative paths use the workspace root, but absolute paths, traversal, and links
are allowed. The workspace is a default directory, not an access boundary;
the agent may use user-requested files, dependencies, and binaries elsewhere
on the host without changing workspaces. The explicit opt-in and warning are
not a security boundary.
There is no Docker setup; use trusted prompts and disposable data.

Startup displays a fresh empty temporary workspace by default. Retain it with
`--keep-workspace`, or modify a dedicated existing directory in place with
`--workspace /absolute/path`. A final answer returns to input; EOF ends the session.
Enter submits; Ctrl+J inserts a newline. Shift+Enter has no separate binding.
Ctrl-C interrupts the active turn, or exits at idle input. Process cleanup uses
POSIX process groups or Windows `taskkill.exe /T /F /PID`, with no containment
guarantee. Windows trace files inherit directory ACLs; use a private directory.
Each language retains its own variables/functions/imports/cwd across actions
and prompts. Only new action output is returned, but model requests still replay
the whole conversation. There is no compaction or conversation persistence.

## Web chat with expandable traces

For a less cluttered view, start the local FastAPI server:

```bash
uv run --system-certs python -m mode_experiment web --protocol tools --allow-unsafe-local
uv run --system-certs python -m mode_experiment web --protocol modes --allow-unsafe-local
```

Choose one command, then open the private URL printed in the terminal. All text
is monospace. Enter sends a prompt; Shift+Enter inserts a newline. User and
assistant messages appear as chat bubbles. HTTP requests/responses, individual
SSE frames, tool calls/results, raw mode text, and harness diagnostics appear in
order as collapsed `<details>` rows with short summaries. SSE events are nested
inside one collapsed group per HTTP request (for example, `SSE #12`), showing
an event count, range, and latest event type. Expand the group, then expand any
individual event to inspect its multiline, pretty-printed JSON. Other rows
expand directly to their JSON or source text. Answers appear after a
completed response, just like the CLI; transport deltas stay in the trace.
Each chat bubble and diagnostic row has a **Copy** button for its full displayed
payload, even when collapsed. SSE groups have **Copy group**. **Copy All** copies
all records loaded in the page, including collapsed payloads, in chronological
order with labels and separators. It does not include the private URL token;
prompts/code/workspace data remain sensitive. After a reload, the replay limit
below still applies.

Ctrl-click (Cmd-click on macOS) toggles a bubble/event's highlighted selection.
Shift-click adds the visible range from the last toggle-click. Modifier-clicking
a summary selects it without expanding it; normal clicks still expand/collapse.
**Copy Selected**, on the right of the Send/Interrupt row, is enabled when records
are selected and copies only those records in chronological order. Selecting an
SSE group includes all its events, including newly arriving events; overlapping
group/event selections are copied only once. Ctrl-click a selected item again
to deselect it. Clipboard actions do not clear selection.

**Interrupt** cancels the current turn and preserves conversation/workspace/mode
using the same recovery behavior as CLI Ctrl-C.

### Token/cache status bar

The composer includes a compact live status bar, for example:

```text
↑123k ↓21k R2.8M CH99.6% ~24.2%/272k
```

`↑` is cumulative **fresh** input (excluding cache reads/writes); `↓` is output,
including reasoning tokens reported as output. `R` counts cache-read tokens;
`W` appears when cache writes are reported. `CH` is the session's weighted cache
hit rate: cache reads divided by all input tokens (fresh + reads + writes).
These are totals across accepted completed model responses, not just the last
user turn. Missing usage is not counted: `?` means none was reported, and `≥`
marks partial totals. Hover over the bar for exact counts and reporting coverage.

The context percentage is **current**, not cumulative. It uses the latest
provider-reported context plus estimates for subsequent user/tool/feedback
messages; without provider usage, it estimates the system prompt, history, and
tool schemas from their text sizes. `~` marks this estimate. The bar turns orange
at 80% and red at 95%. There is no cost estimate or automatic compaction.

Window size comes from an exact-model match in the account's cached/discovered
Codex catalog, then bundled model metadata. Unknown windows show `?`, not a
made-up default. Use `--discover-models` to query the authenticated catalog, or
set an explicit window for the selected serving model, for example
`--context-window 272000`. This flag overrides metadata and only controls display;
it does not change provider limits. Server-owned totals survive browser reloads,
even when older trace records have expired. They reset when the server restarts.

The server defaults to `127.0.0.1:8000`; use `--port 8001` to change the port.
It only accepts loopback hosts and requires the private URL token. Do not expose
or tunnel this unsandboxed execution service, and keep the URL secret. Login is
still performed through the existing `login` command, not the web UI.

All run configuration flags also work with `web`, including `--workspace`,
`--keep-workspace`, `--no-trace`, `--trace-file`, reasoning, and turn limits.
One server is one in-memory conversation shared by all connected tabs, with
only one active turn at a time. Reloading replays the latest 2,000 records;
older records expire from server memory. Use a private `--trace-file` for a
full diagnostic transcript. There is no conversation persistence across server
restarts. Stop the server with Ctrl-C to clean up its workspace and processes.

## What to watch

Native tools use schemas and associated tool results. Modes use no native tool
definitions; the system prompt teaches the grammar. For example, a single
completed response can select Python and execute source without an extra request:

```text
@@tau mode python
x = 40
print(x + 2)
```

Ordinary subsequent text is another action in that mode. Whole `@@tau exit`
returns to control; `@@tau final` plus a newline and answer finishes a turn.
`@@tau literal` plus a newline escapes control-looking payloads in active modes.
Partial/failed responses never execute. Both protocols use the same backend.

Live HTTP/SSE tracing is enabled by default; use `--no-trace` to disable it or
`--trace-file /tmp/tau.trace` for a private diagnostic transcript. This is a
reconstructed application-level trace, not a packet capture. A POST carries
JSON; its SSE frames are events within its response, not new HTTP requests.
Successful Codex streams may omit `Content-Type`; the experiment still observes
SSE and requires a successful terminal event rather than trusting HTTP 200 alone.
Text-only modes still use a JSON transport envelope—the model generates raw
source text, not JSON tool argument strings.

Auth fields are redacted, but prompts/code/workspace contents remain sensitive;
do not publish traces casually. Browser-internal traffic and low-level TLS/socket
details are outside scope. Counters distinguish requests, actions, transitions,
and failures; provider usage is not labeled free merely because cost is unknown.

Try setting Python `x` in one action and using it in another, then in your next
prompt. Try writing/reading/editing a file, or recovering after division by zero.
These are manual usage examples, not scored tasks. Real-provider smoke testing
must be initiated explicitly by you.

See `src/mode_experiment/README.md` in the checkout for complete escaping/file
syntax, configuration flags, limits, Ctrl-C/state-loss behavior and offline checks.
