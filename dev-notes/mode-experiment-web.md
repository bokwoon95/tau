# Manual experiment: web trace viewer

## What changed

`python -m mode_experiment web --protocol tools|modes --allow-unsafe-local`
starts a loopback FastAPI/uvicorn server. `src/mode_experiment/web.py` owns all
web handling; `src/mode_experiment/web.html` holds vanilla HTML, CSS, and JS
with two-space indentation. No frontend build tools or external assets are used.

The UI shows completed user/assistant messages as monospace chat bubbles. Every
HTTP record, SSE frame, execution action/result, mode source, and harness record
gets a collapsed details/summary row. SSE frames are further nested in one
collapsed group per request number, with a live event count/range and latest
event type. Grouping is frontend-only, so server replay and trace files keep
the original event order and payloads. Both group and individual frames start
collapsed. JSON is indented, source keeps its line breaks, and summaries
highlight methods/statuses, SSE types/deltas, and actions.

Copy buttons are available on bubbles, diagnostic summaries, and SSE groups.
Individual copies contain the displayed payload; group and Copy All exports
include labels and separators. Copy All reads a chronological frontend record
list, not rendered DOM text, so collapsed content is included and SSE grouping
does not reorder or duplicate events. Copies use already-redacted display data,
not raw transport data or the private URL. Clipboard API failures fall back to
a temporary selected textarea, and copy controls report success/failure without
toggling details. The existing replay/truncation limits also apply to copies.

Selection is frontend-only: Ctrl/Cmd-click toggles a selectable bubble/event/group;
Shift-click adds a range of visible selectable nodes in DOM order. Modifier-clicks
suppress native summary toggling and text range selection. A blue outline/header
highlights explicitly selected nodes. Copy Selected sits at the right of the
composer controls and is disabled for an empty selection. It unions selected
records and filters the chronological record list, so selected groups include
all their frames (even later arrivals), while overlapping selections cannot
produce duplicate copies. Selection survives copying and live updates; normal
clicks retain their expansion behavior.
Enter sends, Shift+Enter inserts a newline, and Interrupt cancels the active
turn. The timeline preserves the ordered output path used by the CLI.

## Why this boundary

The manual experiment remains independent of the production Tau agent/TUI.
It reuses `Runner`, `Trace`, `Backend`, and Codex authentication instead of
forking protocol behavior. The runner's optional `Output.event()` seam provides
structured actions and raw mode text; ordinary terminal output stays intact
apart from an additional raw mode-text diagnostic. `WebOutput` applies the
existing redaction/file-output path before publishing browser records. Trace
formatting operates on diagnostics, not on HTTP transport bytes.

This follows the project's brain/environment/frontend separation: FastAPI is an
adapter, not a dependency imported by `tau_agent` or the experiment runner.
The CLI imports the web adapter only for the `web` command.

## Token/cache status bar

`Runner.statistics()` sums normalized usage from accepted assistant responses
and calls the existing provider-anchored context estimator on the active
transcript/system/tools. `Output.statistics()` is an optional frontend seam
(no additional CLI output); updates occur after user/feedback insertion,
completed responses, and native tool results. `WebOutput` adds the known window
and publishes absolute snapshots as status-only journal records. The browser
renders them in a compact composer bar, not as extra timeline rows. Snapshots
are also included in `/state`, so reloads do not reconstruct session totals
from the bounded trace journal or double-count replayed responses.

The bar shows cumulative fresh input/output, cache reads, optional writes,
weighted hit rate, and estimated current-context percentage/window. It excludes
costs and does not pretend this experiment supports automatic compaction.
Tooltips give full counts, missing-usage coverage, and context provenance.
Unknown usage displays `?` and partial totals display `≥`; unknown limits stay
unknown. Window resolution uses `--context-window`, then an exact-model match
in discovered/account-scoped cached Codex limits (effective usable window),
then bundled metadata. No new network request is made unless the existing
`--discover-models` flag is supplied. The override is display-only.

Runner tests cover weighted cache hits, cache writes, missing usage, cumulative
versus current tokens, and tool-result contributions to context; no web-adapter
tests were added.

## Session and safety

One server owns one conversation, workspace, backend, and provider. Prompt
submission starts a background turn; concurrent submissions return HTTP 409.
A browser SSE connection replays ordered journal records and follows live
updates, with event IDs for reconnects and heartbeat comments. A bounded 2,000
record journal limits server memory; use `--trace-file` for a full trace. There
is no durable web conversation storage. Shutdown cancels the active turn,
closes provider/backend/client, and removes the default temporary workspace.

Execution is still UNSANDBOXED and requires explicit opt-in. The server only
binds loopback, authenticates its routes with a random private URL token,
rejects foreign Origin/Host headers, disables access logs (which would leak
the token), and sends no-store/no-referrer headers. Browser display uses
`textContent`, never HTML interpretation of agent/workspace content. These
precautions do not sandbox generated code. Do not tunnel/expose the server or
publish its URL, prompts, source, or trace files.

## Use and validation

```bash
uv sync --system-certs
uv run --system-certs python -m mode_experiment login
uv run --system-certs python -m mode_experiment web --protocol modes --allow-unsafe-local
uv run --system-certs pytest tests/mode_experiment
uv run --system-certs ruff check src/mode_experiment
```

Open the printed URL; optionally use `--port 8001`. Run with `--protocol tools`
to compare native calls. Existing CLI/runner/backend/trace tests cover shared
behavior. Per user request, no web-adapter test suite was added. Validation: existing experiment tests passed (125 passed, one POSIX-only test
skipped on Windows); Ruff lint/format checks passed. Local startup, HTML/state
serving, SSE replay, missing-token/foreign-Origin rejection, and temporary
workspace cleanup were smoke-checked without submitting a real-provider prompt.

## Workspace guidance correction

The shared experiment system prompt previously said to operate only within the
workspace, contradicting the unrestricted execution backend. That instruction
has been removed for both protocols and frontends. The workspace is now explicitly
a default working directory, not an access boundary; user-requested host files,
dependencies, and binaries can be used in place. The instruction not to access
host secrets remains. Parameterized prompt tests cover both protocols. Restart
the CLI/server to rebuild the system prompt; existing runners keep their startup
prompt and conversation until restarted.
