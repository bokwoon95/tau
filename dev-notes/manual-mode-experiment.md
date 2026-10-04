# Manual native-tools versus text-modes experiment

This is a deliberately separate terminal experiment under `src/mode_experiment/`,
not another Tau coding session or a new provider. It follows Tau's architectural
separation: portable message/provider contracts stay unchanged; the experiment
owns its environment and frontend. It has no Textual dependency, session files,
resource discovery, extensions, benchmark tasks, or success grader.

## Components

- `authentication.py`: calls Tau's browser OAuth and credential store. Refresh is
  handled by the existing `OpenAICodexCredentialResolver`, including its lock,
  token rotation, and persistence. Its only production change is an optional
  HTTP-client injection; callers without it retain their existing behavior.
- `tracing.py`: uses injected httpx hooks and a byte-stream tee. It observes each
  actual request/retry and complete SSE frame before Tau normalizes provider
  events. Separate content decoding handles compressed responses without
  changing the adapter's original bytes. Redaction precedes output; diagnostic
  buffers are bounded and truncation/withholding is visible.
- `protocols.py`: a completed-message grammar, not a streaming code parser.
  Combined mode selection/action produces one action and one feedback message;
  standalone transitions genuinely cost another model invocation. Literal and
  edit-section escapes are independent reversible layers.
- `execution.py` / `worker.py`: one backend for six hardcoded operations. Direct
  files use POSIX descriptor-relative, no-follow operations and atomic replacement.
  Each executable language has one lazy reusable interpreter. A supervisor sends
  new source, collects bounded stdout/stderr, and checks an explicit completion
  status—not prompts, silence, or token boundaries. It never silently replaces a
  dead interpreter. Internal JSON framing is invisible to the model.
- `prompts.py`: reuses Tau's coding guidelines and prompt assembly for runtime
  date/cwd, then explicitly teaches the selected protocol. Production-only
  skills, extension, and installed host-documentation hints are deliberately absent.
- `runner.py` / `__main__.py`: keep original Tau assistant blocks, tool IDs and
  replay signatures in memory; execute serially; distinguish answers from limits,
  cancellation and failures; and return to terminal input after each turn.

## Two different meanings of JSON

Codex requests are HTTP POSTs whose bodies are JSON. Responses normally stream
SSE frames containing JSON data. Neither protocol removes that transport envelope.

Native tools ask the model to generate structured arguments, such as
`{"code":"print(42)"}`. Modes ask for ordinary assistant text, such as
`@@tau mode python` followed by literal Python source. Tau/httpx serialize the
outer request/response transport; the model need not escape source into a JSON
argument string in the modes arm. Tool schemas describe interfaces, not uploaded
local implementations.

Only each action's new output is returned from its interpreter. Full conversation
history (including earlier results) is still replayed to Codex; interpreter
variables are not serialized. Context trimming/compaction is intentionally absent.

## Local execution revision

At the user's request, isolation/Docker requirements were removed from `TASK.md`
and implementation. All executable source runs locally with an explicit
`--allow-unsafe-local` opt-in and a warning every session. Sanitizing inherited
environment variables is **not** a sandbox. Code can access host files, secrets,
and the network; detached children can escape process-group cleanup. Use trusted
prompts and disposable data. No real-provider smoke run was performed.

See `src/mode_experiment/README.md` for exact login/run commands, escaping rules,
manual prompts, interruption semantics and offline checks. Existing scripted
provider, mock-network and local-runtime tests are under `tests/mode_experiment/`;
further test expansion was stopped at the user's request.
