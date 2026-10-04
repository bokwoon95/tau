---
title: Manual protocol experiment
description: Explore native Codex tools and raw-text operation modes in a plain terminal.
---

Tau includes an experimental CLI for exploring native tool calls versus raw
assistant-text modes with the same OpenAI Codex subscription model. It is
separate from the production coding agent: no TUI, slash commands, extensions,
saved conversations, benchmarks, or graders.

## Run from a checkout

Requires `uv`, a POSIX host, and installed language runtimes (`bash`, optionally
`pwsh`; Python uses the project environment):

```bash
uv sync
uv run python -m mode_experiment login
uv run python -m mode_experiment run --protocol tools --allow-unsafe-local
uv run python -m mode_experiment run --protocol modes --allow-unsafe-local
```

Login uses Tau's existing browser OAuth and `openai-codex` credential, including
refresh/persistence and manual redirect fallback. It does not change unrelated
provider preferences. Default model/reasoning is `gpt-5.4` / `medium`.

**Execution is local and unsandboxed.** Generated source can access host files,
secrets, and the network. Only direct read/write/edit operations are confined to
the workspace. The explicit opt-in and warning are not a security boundary.
There is no Docker setup; use trusted prompts and disposable data.

Startup displays a fresh empty temporary workspace by default. Retain it with
`--keep-workspace`, or modify a dedicated existing directory in place with
`--workspace /absolute/path`. A final answer returns to input; EOF ends the session.
Each language retains its own variables/functions/imports/cwd across actions
and prompts. Only new action output is returned, but model requests still replay
the whole conversation. There is no compaction or conversation persistence.

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
