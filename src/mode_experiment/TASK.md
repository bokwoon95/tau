# Manual experiment: native tools versus text-only modes

## Objective

Build a small CLI for manually exploring two ways for the same OpenAI Codex model to operate on a workspace:

1. **Tool protocol:** provider-native structured tool calls.
2. **Mode protocol:** ordinary assistant text, interpreted according to the harness's current mode. Executable payloads are raw source, not model-generated JSON argument strings.

The user should be able to type prompts, watch actual HTTP requests and streamed SSE events, inspect execution feedback, and continue the conversation to get a feel for both protocols.

Implement **OpenAI Codex subscription authentication only** initially. Do not add other providers, API-key setup, benchmark suites, task catalogs, graders, paired runs, repetitions, or aggregate comparison reports. Deterministic implementation tests are still required.

Do not build a full coding agent: no TUI, slash commands, plugins, compaction, or durable conversation/session storage. Credential storage and an optional diagnostic transcript are not conversation persistence.

The implementation lives in `src/mode_experiment/`. Reuse Tau's provider adapter, OAuth implementation, credential storage, and message/event contracts. Do not invent a second provider or HTTP stack. Run Python and tests through `uv`.

## Required deliverable and manual workflow

Provide a runnable CLI with these command shapes:

```text
uv run python -m mode_experiment login
uv run python -m mode_experiment run --protocol tools [options]
uv run python -m mode_experiment run --protocol modes [options]
```

`login` signs in to the OpenAI Codex subscription endpoint. `run` starts a plain terminal prompt loop, not a Textual application. Support model selection, reasoning settings, an explicit workspace, execution isolation, and configurable limits. Document defaults and exact runnable commands in a short README.

For each user prompt, the harness makes model requests and executes actions until the assistant finishes its answer or a limit/error interrupts the turn. Then return to the user prompt. Keep the conversation, workspace, active modal state, and interpreter processes for the lifetime of that CLI session. An assistant final answer finishes a turn, **not** the CLI session. EOF ends the session and cleans up resources. Document Ctrl-C behavior for idle input, model streaming, and execution.

Display the workspace path, protocol, model, reasoning configuration, execution boundary, and tracing configuration at startup. Start with an empty temporary workspace by default; allow an explicitly selected workspace without implicitly using the repository or current directory. Explain whether an explicit workspace is modified in place and how temporary artifacts can be retained for inspection.

Add example manual prompts for reading/writing/editing files, retaining language state over multiple actions, and recovering from an execution error. These are usage examples, not an automated task suite or success grader.

## Scope and architecture

Keep these concerns separate:

- Authentication/configuration: Codex login, stored credentials, refresh, and model settings.
- Provider adapter: Tau's existing Codex request and streaming implementation.
- Diagnostic tracing: observes HTTP and SSE without changing provider behavior.
- Protocol adapters: translate native calls or modal text into actions.
- Shared execution backend: the six operations below.
- Manual runner: prompt loop, conversation, action execution, feedback, and limits.

Use the same backend and operation feedback for both protocols. Hardcode the six operation definitions; no discovery or extension system is needed.

Inspect `tau_agent/messages.py`, `tools.py`, `provider.py`, and `provider_events.py`, plus `tau_ai/openai_codex.py`, `tau_coding/oauth.py`, `credentials.py`, and the existing Codex runtime credential/configuration helpers before implementing. Core application sessions and the TUI are unnecessary.

Keep changes inside the experiment where possible. A small optional HTTP-client/observer injection in existing helpers is acceptable if needed for complete tracing; preserve existing defaults and add regression tests. Do not fork OAuth/refresh logic or refactor the production harness to accommodate the experiment.

## Codex subscription login

Reuse Tau's existing browser OAuth flow, localhost callback, manual authorization-input fallback, PKCE/state validation, and credential store. Store/reuse the existing `openai-codex` credential rather than inventing an experimental token format. Do not rewrite unrelated Tau provider settings or credentials.

Use a `login` subcommand, **not** `/login` or any other slash-command system. Device-code login and additional auth methods are not required. If credentials are absent, `run` should give an actionable instruction to run `login`. Expired credentials should use Tau's existing refresh behavior and persist refreshed credentials correctly.

Authentication and provider requests run on the host. Never pass OAuth credentials into executable processes or mount the credential store inside their isolation environment.

## Live HTTP and SSE tracing

Tracing is a primary feature of this experiment and should be enabled by default, with an explicit option to disable it. Show events immediately as they arrive, rather than waiting for the completed assistant message. An optional trace-file destination is useful, but no machine-readable episode-result/reporting system is required.

Explain the actual transport in the README:

- A model request is an HTTP POST with a JSON body.
- Its response is normally a long-lived `text/event-stream` response containing SSE frames, usually with JSON in their `data` fields.
- An SSE event is **not** a new HTTP request. Requests, retries, HTTP connection reuse, and SSE events are different things.
- The text-only protocol still uses the provider's JSON HTTP envelope. The model generates raw source as assistant text; transport serialization is handled by the provider/client, not by the model.

Print each actual HTTP request and response in a readable, HTTP/1-style diagnostic layout. Include a request number, purpose/model-turn association where available, timestamp, method, URL/path, headers, request body, response status, and response headers. Print non-streaming response bodies safely as well. Observe inference, model-discovery, token-exchange/refresh, redirects, and retry requests made by the experiment's HTTP clients. Every actual retry must have its own request record.

Label this as a reconstructed application-level trace, not a packet capture. Report the actual HTTP version when available; an HTTP/1-looking display must not falsely claim the connection uses HTTP/1. Do not invent extra "connect" requests for SSE events, DNS, or TLS handshakes. Browser-internal login traffic and low-level socket/TLS details are outside scope; explicitly state that boundary.

For streaming responses, print each complete SSE frame as soon as its frame boundary arrives, before the provider adapter filters or normalizes it. Preserve visible `event:`, `data:`, `id:`, `retry:`, comment/heartbeat, and terminal fields when present. Include a per-request event number and elapsed time. Show argument/text deltas even when Tau's normalized events only expose a completed tool call; do not substitute normalized agent events for the network trace.

A schematic display is:

```text
>>> request #3 [model turn 2, elapsed 0.000s]
POST /backend-api/codex/responses HTTP/1-style diagnostic
Authorization: <redacted>
Content-Type: application/json

{"model":"...","stream":true,"input":[...]}

<<< response #3 [actual version: HTTP/2, elapsed 0.210s]
200 OK
Content-Type: text/event-stream

<<< SSE #3.1 [elapsed 0.215s]
event: response.output_text.delta
data: {"type":"response.output_text.delta","delta":"print"}
```

Use a single ordered output path or otherwise serialize records so prompts, trace frames, and execution feedback do not corrupt each other. Clearly label harness annotations versus provider events. Avoid unnecessarily duplicating every delta as both raw trace and ordinary assistant rendering; always make the completed answer, parsed action, and execution result easy to identify.

### Trace correctness and redaction

Implement tracing using injected `httpx` clients/transport or stream wrappers around the existing adapter and OAuth helpers. Tee streamed bytes through an incremental frame decoder while delivering the original stream unchanged to Tau. Do not consume a response twice, buffer the whole response before displaying events, issue a second request just to inspect it, or change cancellation/retry behavior. HTTP chunk boundaries, token boundaries, and SSE frame boundaries are not interchangeable.

Redact before console/file output: bearer/API tokens, refresh/access/ID tokens, authorization codes, PKCE verifiers, OAuth state/callback secrets, credential-bearing cookies, and account identifiers. Handle JSON, form bodies, URL queries, headers, SSE data, and error paths. A required browser authorization link may be shown to the user for login, but do not copy sensitive authorization/callback values into diagnostic logs. Never add a flag that prints unredacted credentials.

Traces legitimately contain user prompts, generated code, and workspace data. Warn users not to submit secrets and not to assume a trace is safe to publish merely because auth fields were redacted. Mark any truncation; keep stream/framing buffers bounded and do not turn a display limit into silent loss of execution payloads.

## Required operations: both tools and modes

Implement `read`, `write`, `edit`, `bash`, `powershell`, and `python`.

- `read`: read a UTF-8 file relative to the workspace, with useful errors and bounded output.
- `write`: create or overwrite a UTF-8 file; create parent directories within the workspace.
- `edit`: exact-text replacement. Require exactly one match; validate against original content and reject ambiguous changes without modifying the file. One replacement per action is sufficient initially.
- `bash`: execute Bash source in the session's persistent Bash process.
- `powershell`: execute PowerShell source in the session's persistent `pwsh` process.
- `python`: execute Python source in the session's persistent Python process.

Define explicit native schemas such as `read(path)`, `write(path, content)`, `edit(path, old_text, new_text)`, and `bash/powershell/python(code)`. Schemas describe the interface; local implementations are not uploaded to the provider.

### Persistent processes are mandatory

Each manual session owns one reusable process per executable language, started lazily. Do **not** start a fresh interpreter per action. Bash/PowerShell variables, functions, and working directory, and Python imports, variables, and definitions must survive later actions, mode exits/reentries, and subsequent user prompts. The three languages have independent state.

Native tools must use exactly those same persistent-process semantics. Processes are not shared between CLI sessions or protocol variants.

Implement reliable action framing, completion detection, stdout/stderr capture, and error reporting for multiline source. Do not use prompt text or silence to detect completion. Internal process framing may use JSON; model-facing executable mode payloads must not require JSON wrappers.

Set action timeouts and output limits. On timeout, cancellation that destroys a process, or process death, report failure and state loss explicitly. Never silently recreate a process while claiming its state survived. Clean up processes and descendants on session completion and failure.

Detect missing Bash/PowerShell executables and report the affected operation as unavailable with installation guidance. Other available operations may still be used. Do not automatically install host software or pretend an unavailable action succeeded.

## Protocol A: native tool calling

Supply the six definitions through the Codex adapter's native tool interface. Execute returned calls and append correctly associated tool-result messages. Preserve provider-required IDs, reasoning/replay metadata, and signatures in the original Tau message representation; do not flatten history into text.

Execute stateful actions serially. Request serial calls where the adapter exposes that option; otherwise process multiple calls in response order and visibly log the condition. Do not change Codex defaults covertly or pretend a serial API setting was applied when it was not.

A normal final assistant answer finishes the current user turn and returns to terminal input. Text accompanying tool calls is displayed but does not end the turn while calls remain. Validate malformed arguments and return useful error feedback instead of guessing or silently repairing them.

If native tools are unsupported, say so explicitly. Do not replace this protocol with model-generated JSON parsed from ordinary text.

## Protocol B: text-only modes

Supply **no native tool definitions** (`tools=[]`). Explain the protocol in the system prompt, classify completed `AssistantMessage.text`, and keep the complete original assistant messages for provider replay. Provider-exposed thinking is not executable source. Unexpected native calls in this protocol are an explicit violation, not executable actions.

Use a small explicit state machine with a control state and the six operation modes. Begin in control state. The namespaced markers below are ordinary text conventions, not proprietary special tokens.

### Control commands

- In control state, a whole response `@@tau mode NAME` enters one of the six modes. Names are lowercase and case-sensitive. Acknowledge the transition and request mode-specific content on the next model turn.
- In an active mode, a whole response `@@tau exit` returns to control. Exiting a mode does not destroy its language process.
- In control state, first line `@@tau final` followed by a line ending and answer text completes the current user turn. Remain in control for the next user prompt.
- Other control-state responses, unknown modes/commands, and control commands in the wrong state produce actionable protocol-error feedback with bounded recovery.

Mode selection and action payloads are separate responses. Recognize whole-response commands only as the exact command with zero or one final LF/CRLF; do not use unrestricted whitespace trimming. The final-answer form is explicitly a first-line header. Do not extract commands from arbitrary source lines, strings, Markdown, or fenced blocks.

### Action payloads and literal escaping

In an active mode, an ordinary assistant response is one action payload and the mode stays active afterward. For Bash, PowerShell, and Python, use the entire completed text as source, with no JSON wrapper, Markdown fence, or mandatory payload sentinel. The API response boundary delimits the action.

No reserved marker can be guaranteed absent from arbitrary literal content. Use an optional literal-prefix escape in active modes:

```text
@@tau literal
PAYLOAD
```

Recognize the exact `@@tau literal` first line followed by LF or CRLF. Remove exactly that header and its separator, then pass the entire remainder to the mode's payload parser **without reclassifying it as a control command**. Preserve all remaining characters, including leading/trailing whitespace and line endings. Empty remainder is valid escape syntax; the operation can independently reject an empty payload.

Use this escape when payloads collide with reserved control forms or begin with the literal-prefix header themselves. Prefix twice to represent a payload that itself begins with `@@tau literal`; remove only the outer prefix. Reject malformed/control-looking messages rather than guessing their intended meaning, and document the exact reserved forms with parser tests.

Examples:

- SQLite input `.mode csv` would be ordinary payload in a future SQLite mode, not a harness command. SQLite is not an operation to implement in this phase.
- Python `print("@@tau exit")` is ordinary source, not an exit.
- To submit literal payload `@@tau exit`, send `@@tau literal` on its own first line followed by that payload.

Never execute partial streamed text. Displaying raw SSE deltas is diagnostic observation, not incremental code execution. Only classify and execute after a successful completed response; do not execute truncated, cancelled, or failed responses.

After transitions and actions, report the authoritative current mode and remind the model of the allowed next response. The harness owns mode state; do not infer it from the language the generated code appears to use.

### File-mode payloads

Use simple non-JSON syntax:

- `read`: the entire payload is the relative path.
- `write`: first line is the relative path; the separator is structural and all remaining text is exact file content. A path plus separator and empty remainder writes an empty file.
- `edit`: first line is the relative path, followed by explicitly delimited old/new sections. Choose and document a reversible delimiter-escape rule; cover multiline replacements, empty replacement text, and delimiter collisions.

Do not trim file contents or replacements. Specify line-ending behavior and whether/when terminal line endings on path-only payloads are structural. Literal escaping is an outer layer, applied before file parsing; it does not replace file-section delimiter escaping. Do not make executable-language payloads follow file-mode syntax.

Protocol errors must be visible and distinguishable from execution errors. Never silently remove fences, repair commands, change modes, or rewrite code to hide a model mistake. Keep mode transitions and their additional model requests visible.

## Workspace and execution safety

Constrain direct file operations to the explicitly displayed workspace, including traversal and symlink handling. A workspace directory alone is **not a sandbox**: arbitrary Bash, PowerShell, and Python source can access the host.

Run real model-generated source only in an explicitly configured disposable isolation environment, such as a container with:

- No provider credentials or host secrets mounted.
- No host mounts beyond the selected experiment workspace.
- Network disabled by default.
- Resource limits and process/descendant cleanup.

Maintain the persistent language processes inside that boundary; provider requests remain on the host. An unsafe local-development execution option may be provided, but require explicit opt-in and a prominent warning every session. Never present it as sandboxed or choose it automatically when isolation setup fails.

Document isolation prerequisites and exact setup/run commands. Missing runtimes or isolation infrastructure must be actionable failures, not hidden fallback behavior.

## Limits and feedback

Configure model-request, execution-action, and protocol-recovery limits per user turn, plus wall-time, action-timeout, and output limits. Distinguish a completed answer from exhaustion, cancellation, protocol failure, provider failure, and execution-infrastructure failure.

Use comparable operation feedback in both protocols: operation, success/failure, stdout/stderr or file result, relevant errors, and truncation. Execution errors should normally give the model a chance to recover within the limits. Explain the conversation/process state retained after an interrupted turn before accepting another prompt.

Show lightweight counters for model requests, actions, failures, and modal transitions so the user can see the cost of entry/exit turns. Show provider-reported usage when available; do not call Tau's default zero cost free usage. No aggregate metrics, comparative scorecards, or benchmark machinery are required.

## Tests and acceptance criteria

Use fake/scripted providers, mock HTTP streams, and safe deterministic fixtures; offline tests must not require real credentials.

Cover:

- Browser-login callback/manual-input handling, credential persistence, refresh, and missing/failed authentication without touching the user's real credentials.
- Native tool calls/result association, malformed arguments, mixed text/calls, and serial action ordering.
- Mode entry/action/exit/final handling, wrong-state and unknown commands, bounded recovery, and continuation across user prompts.
- Literal-prefix round trips, nested literal headers, marker strings inside code, SQLite-like `.mode` text, file newline preservation, delimiter collisions, and atomic edit failure.
- No native declarations in the mode arm, preserved replay metadata, and no execution of partial/failed/cancelled/truncated responses.
- Persistence in installed runtimes, multiline source, stdout/stderr, exceptions, timeout, process death/state loss, cancellation, and descendant cleanup. Missing PowerShell must be clearly reported; test full behavior when `pwsh` is installed.
- Workspace confinement and separation between independent sessions.
- Incremental HTTP/SSE display, arbitrary byte/chunk splits and split UTF-8, LF/CRLF and multiline frames, ignored/terminal/heartbeat events, non-SSE bodies, retries, and disconnects. Assert trace output appears before the response finishes and the adapter receives unchanged data.
- Redaction across headers, URLs, JSON/form bodies, SSE, and errors; diagnostic tracing must not leak credentials or add provider calls.

Provide opt-in real-provider smoke instructions: sign in, run a manual conversation in each protocol, observe live SSE, and demonstrate a persistent variable in separate actions. Real-provider smoke runs must be initiated explicitly by the user and use configured isolation or the explicit unsafe-local opt-in. Do not claim they were run without credentials and an actual run.

The README must explain login, both manual invocation forms, model/reasoning configuration, protocol/escaping rules, tracing and its limitations, execution safety/prerequisites, missing-runtime behavior, limits/cancellation, and exact offline test commands. Add beginner-friendly implementation notes under `dev-notes/` explaining reuse of Tau's Codex provider and why JSON HTTP transport is separate from model-generated JSON tool arguments.

## Suggested implementation order

1. Inspect existing contracts and Codex OAuth/provider helpers; define actions, results, and the manual runner state.
2. Implement `login`, credential reuse/refresh, and redacted incremental HTTP/SSE tracing with mock-network tests.
3. Implement shared workspace operations and isolated persistent processes; test them independently.
4. Implement and test the modal state machine and literal escaping with scripted responses.
5. Implement the native-tool protocol with scripted responses using the same backend.
6. Wire the terminal prompt loop, document commands, and make both protocols ready for user-driven Codex smoke runs.

Keep this experimental subsystem small. Benchmarks and additional providers can be considered later, after manual exploration.
