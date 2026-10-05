# Manual Codex protocol experiment

Compare native tools with raw assistant-text modes interactively. This is not
Tau's full coding agent: no TUI, slash commands, plugins, saved conversations,
benchmarks, or graders. A final answer returns to the prompt, not out of the CLI.

## Start

From a Tau checkout, with `uv` on Windows, macOS, or Linux:

```bash
uv sync
uv run python -m mode_experiment login
uv run python -m mode_experiment run --protocol tools --allow-unsafe-local
uv run python -m mode_experiment run --protocol modes --allow-unsafe-local
```

Login reuses Tau's browser OAuth, localhost callback on port 1455, PKCE/state
validation, and manual redirect/code fallback. Paste fallback input when needed;
it is not echoed on a terminal. Use `login --no-browser` to open the displayed
link yourself. Login saves only `openai-codex` in Tau's existing
`~/.tau/credentials.json` (or `$TAU_HOME/credentials.json`). Existing credentials
are reused, expired credentials are refreshed and persisted, and unrelated
provider settings/credentials are not rewritten. No API-key setup is supported.
On Windows, the experiment's HTTP client uses system CA trust (including locally
installed CA roots), with TLS verification enabled. A browser callback success
page confirms receipt of the code, not a successful HTTPS token exchange.

**Execution is local and UNSANDBOXED.** The opt-in is required every run.
Generated source can access host files, secrets, and the network. A dedicated
workspace and sanitized interpreter environment are not a security boundary.
There is no Docker/container setup. Use trusted prompts and disposable data.

Bash and PowerShell require `bash` and `pwsh` on `PATH`. On Windows use native
Bash from Git for Windows, not the System32 WSL launcher (the standard Git install
is detected as a fallback). Python uses the project's Python executable. Missing runtimes produce installation guidance; other
operations still work. Install missing tools yourself, for example on macOS:

```bash
brew install bash
brew install --cask powershell
```

## Web UI

For monospace chat with collapsed, individually expandable diagnostics:

```bash
uv run --system-certs python -m mode_experiment web --protocol tools --allow-unsafe-local
uv run --system-certs python -m mode_experiment web --protocol modes --allow-unsafe-local
```

Run one command and open the private URL printed by the server. Enter submits;
Shift+Enter inserts a newline. Only user/assistant conversation is shown as chat
bubbles. HTTP, SSE, tool calls/results, raw mode text, and harness diagnostics
are ordered collapsed `<details>` with short summaries; expanded JSON is
pretty-printed and mode source keeps its newlines. SSE frames are additionally
grouped inside a collapsed row per request (such as `SSE #12`), with an event
count/range and latest event type. Expand that group to see individually
expandable frames such as `SSE #12.1` and `SSE #12.2`. Copy buttons copy each
bubble/diagnostic payload without expanding it; Copy group copies an SSE group.
Copy All copies every record loaded in the page, including collapsed payloads,
in chronological order with labels. Copies retain display redaction/truncation
and the replay limit still applies after reload. Treat copied data as sensitive.
Ctrl-click (Cmd-click on macOS) toggles highlighted bubbles/events; Shift-click
adds a visible range from the last toggle-click. Modifier-clicks do not expand
or collapse details. Copy Selected sits on the right of Send/Interrupt and copies
selected records chronologically, including collapsed payloads. Selecting an SSE
group includes its events (also newly arriving ones), without duplicating any
individually selected events. Ctrl-click again to deselect; copying keeps selection.
Answers are displayed only
when complete, like the CLI. Interrupt cancels the active turn.

A live composer status bar shows session totals and current context, for example:

```text
↑123k ↓21k R2.8M CH99.6% ~24.2%/272k
```

Input (`↑`) counts fresh tokens, excluding cache reads/writes; output (`↓`)
includes provider-reported reasoning output. `R` is cache reads, with `W` shown
when cache writes are reported. `CH` is weighted cache reads / total input.
Missing response usage is excluded; `?` indicates none reported and `≥` marks
partial totals. Hover for exact counts/coverage. Context uses the latest provider
usage plus estimated trailing messages, or text/schema estimates before any
usage is available. `~` marks the estimate. Orange/red indicate 80%/95% filled.
There is no cost estimate or automatic compaction.

The window uses exact-model cached/discovered Codex limits, then bundled metadata.
Use `--discover-models` to query live metadata or `--context-window 272000` to
override the display window. Unknown limits show `?`; the override does not change
provider limits. Totals survive browser reload/replay expiration but not server
restarts.

The server uses FastAPI with vanilla HTML/CSS/JS. Web handling lives in `web.py`;
the frontend is `web.html`. It reuses the same runner, redaction, credential
refresh, execution backend, limits, and flags as `run`. Login remains a CLI
operation. Default bind is `127.0.0.1:8000`; change the port with `--port`.
Only loopback hosts are supported. The private URL token is required for access;
keep it secret and **never expose or tunnel this unsandboxed service**.

All tabs share one conversation and one active turn. Reload replays the latest
2,000 records from memory. Use a private `--trace-file` for a full diagnostic
transcript. Restarting loses the conversation. Ctrl-C stops the server and
cleans up execution processes and the temporary workspace unless retained.

## Workspace, model, and limits

The default workspace is a new, empty temporary directory, never the repository
or implicit current directory. It is a default directory, not an access boundary:
user-requested operations may use files, dependencies, and binaries elsewhere
on the host without changing workspaces. Startup displays its path and all configuration.
Use `--keep-workspace` to retain it after exit. An explicit **existing** workspace
is modified in place and never automatically removed:

```bash
mkdir -p /tmp/tau-manual-workspace
uv run python -m mode_experiment run --protocol modes --allow-unsafe-local \
  --workspace /tmp/tau-manual-workspace --model gpt-6.1-sol \
  --reasoning medium --reasoning-summary auto --trace-file /tmp/tau-manual.trace
```

Defaults: model `gpt-6.1-sol`, reasoning `medium`, summary `auto`, 24 model requests,
24 actions, 4 protocol errors, 300 seconds per user turn, 30 seconds per action,
16,384 output characters for file reads / bytes per interpreter output channel,
and 65,536 characters per diagnostic record/frame. Configure with
`--max-requests`, `--max-actions`, `--max-recoveries`, `--wall-time`,
`--action-timeout`, `--output-limit`, and `--trace-limit`.

`--reasoning default` leaves the provider's reasoning configuration unset;
other offered values are sent as their literal effort. `--reasoning-summary`
applies when an effort is supplied. Unsupported model/effort combinations are
provider errors, not silently repaired. `--discover-models` displays the
account's Codex catalog before prompting, using Tau's adapter and bundled
Codex compatibility version. `run --help` lists all flags.

Counters show model invocations (excluding HTTP retries), executed/attempted
actions, failures, transitions, and protocol errors. HTTP tracing numbers each
actual request/retry separately. Usage is provider-reported when available;
Tau's default zero cost does **not** mean free usage.

## Operations and persistence

Both protocols use exactly the same backend:

- `read(path)`: bounded UTF-8 file contents.
- `write(path, content)`: create/overwrite, creating parent directories.
- `edit(path, old_text, new_text)`: exactly one nonempty match, including
  overlapping-match checks; rejected changes leave the original file intact.
- `bash(code)`, `powershell(code)`, `python(code)`: multiline source in one
  lazy, persistent process per language, not a fresh interpreter per action.

Relative file paths use the workspace root; absolute paths, parent traversal,
symlinks, and hardlinks are allowed. Read/write/edit are ordinary host file operations,
with no confinement or sandboxing. Files retain LF/CRLF exactly. Edits validate the
match before writing, but writes are not crash-atomic.

Variables, functions, imports, and interpreter cwd persist across actions, mode
exits/reentries, and user prompts. Languages and independent CLI sessions have
separate state. **Only each action's new output is returned**, not a cumulative
REPL dump or serialized variable state. Model requests replay the conversation,
including earlier actions/results and original provider metadata. There is no
history window/compaction; restart the CLI for a fresh conversation.

## Protocols

`tools` supplies six native schemas; implementations are not uploaded. Results
retain call associations. Codex currently sets `parallel_tool_calls=true`; the
experiment does not secretly change that setting and executes calls serially
in response order. Text alongside calls is displayed but is not a final answer.
Unsupported native tools are provider failures; there is no text-JSON fallback.

`modes` passes `tools=[]` and teaches the complete grammar in the system prompt.
Both arms also reuse Tau's general coding guidance and date/workspace prompt
assembly, without production-only resource loading.

Start in **control**. Names are lowercase/case-sensitive. A single completed
assistant response can select a mode AND perform an action, with no
acknowledgement request in between:

```text
@@tau mode python
x = 40
print(x + 2)
```

In an active mode, ordinary text is one complete action. Executable text is raw
source: no JSON wrapper, fence removal, or code repair. Mode switching/repeated
selection preserves processes. Header-only `@@tau mode NAME` optionally selects
without executing. Whole `@@tau exit` returns to control; only in control,
`@@tau final` plus LF/CRLF and answer text finishes the user turn.

Whole commands accept exactly zero or one final LF/CRLF, not arbitrary trimming.
First-line headers are recognized only at the beginning, never inside later
lines/strings. Other first lines beginning `@@tau` are reserved protocol errors.
`print("@@tau exit")` and `.mode csv` are ordinary active-mode payloads.

In an active mode, `@@tau literal` plus LF/CRLF removes exactly that header and
passes the remainder literally, including empty text. Prefix twice to represent
a payload starting with that header. A combined mode-selection remainder is
already literal: it is never reclassified or given a second prefix removal.
Thinking is never source. Unexpected native calls are rejected. Partial,
truncated, failed, or cancelled responses never execute or change mode state.

### File payloads

- `read`: entire relative path; one optional terminal LF/CRLF is structural.
- `write`: path on the first line, LF/CRLF, then exact content (possibly empty).
- `edit`: path, then `@@tau old`, `@@tau new`, `@@tau end` delimiter lines:

```text
@@tau mode edit
notes.txt
@@tau old
old line
@@tau new
new line
@@tau end
```

Section newlines belong to the replacement text. Escape section lines beginning
`@@tau` followed by a space, or backslash, with one additional backslash. A
special `\n` line (literal backslash plus lowercase n) immediately before a
section delimiter removes exactly one preceding LF/CRLF; this represents a
section without a terminal newline. Escape a literal `\n` line as `\\n`.
An empty new section deletes. Nothing may follow the end delimiter except its
one optional terminal LF/CRLF. Outer literal-prefix escaping does not replace
this section escaping. File content is never whitespace-trimmed.

## Live tracing

HTTP/SSE tracing is on by default; disable it with `--no-trace`. `--trace-file`
overwrites a private diagnostic transcript, not a saved conversation. Logs
redact auth headers, tokens, cookies, account identifiers, authorization codes,
PKCE values, and OAuth state before console/file output. The necessary browser
authorization link is shown only on the console, not copied to the transcript.
**Prompts, generated code, and workspace data are still sensitive. Do not submit
secrets or assume a trace is safe to publish.**

A model request is an HTTP POST with a JSON body. Normally its response is a
long-lived `text/event-stream`, carrying SSE frames with JSON `data` fields.
Codex can omit `Content-Type`; successful `/responses` responses without that
header are still parsed as SSE. HTTP 200 alone does not establish completion.
An SSE frame is **not** a new request or connection. Requests, retries,
connection reuse, and stream events are different things. Even text-only modes
use the provider's JSON HTTP envelope: the model generates raw source text;
the client, not the model, serializes the transport.

The HTTP/1-style layout is a reconstructed application-level trace, **not a
packet capture**; the actual HTTP version is reported when available. HTTP
headers/bodies, retries, optional discovery, and OAuth exchange/refresh from
injected clients are observed. Complete raw SSE frames are shown immediately,
before Tau filters them, including argument deltas, comments, ignored events,
and terminal fields. There are no invented DNS/TLS/connect requests.
Browser-internal traffic and low-level sockets/TLS are outside scope.

Display truncation is marked; oversized/incomplete frames are withheld rather
than partly exposing auth data. Execution payloads are never display-truncated.
A transport without a verified successful terminal event is rejected (increase
`--trace-limit` if a terminal frame exceeded its buffer). Completed answers,
parsed actions, execution results, and harness annotations are labeled; ordinary
rendering does not duplicate every streamed delta.

## Interruptions and manual smoke prompts

Enter submits a prompt; Ctrl+J inserts a newline without submitting. Shift+Enter
has no separate binding. OAuth fallback input stays hidden and single-line.

EOF (Ctrl-D on POSIX; Ctrl-Z at empty input on Windows) or Ctrl-C at idle input
ends the session and cleans up processes. Ctrl-C during streaming interrupts only that turn; partial
output is not executed. During execution it destroys the affected interpreter,
explicitly reports state loss, and returns to input. Timeout/process death also
makes that language unavailable for the rest of the session: no silent restart.
Other live processes, workspace, conversation, and authoritative mode remain.
Ordinary execution errors allow recovery within the limits. Detached/daemonized
children can escape cleanup: there is no containment guarantee. POSIX cleanup uses
process groups; Windows uses `taskkill.exe /T /F /PID` (best effort, not Job Objects).
Windows transcript files inherit directory ACLs; store traces in a private directory.

After explicitly signing in, manually run **each** invocation above and try:

1. “Write notes.txt containing two lines, read it, then edit exactly one line.”
2. “Set Python x = 40 in one action, print x + 2 in a separate action.”
3. In the next user prompt: “Print x again without reassigning it.”
4. “Set a Bash variable/function, switch to Python, return to Bash and use it.”
5. “Cause a Python division-by-zero error, then recover using the retained x.”

Watch SSE live and compare request/action/transition counts. These are usage
examples, not automated tasks or a grader. Real-provider smoke runs must be
initiated by you; none are claimed by the implementation's offline checks.

Offline checks (no credentials/network required):

```bash
uv run pytest tests/mode_experiment tests/test_oauth.py tests/test_provider_runtime.py
uv run mypy src/mode_experiment
uv run ruff check src/mode_experiment tests/mode_experiment
```
