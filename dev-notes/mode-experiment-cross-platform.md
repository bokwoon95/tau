# Stripped-down cross-platform protocol experiment

The manual experiment now runs on Windows as well as POSIX hosts. This is a
portability change to the standalone experiment, not a new Tau harness layer.
No changes were needed to the provider, OAuth implementation, or agent harness.

## What changed and why

- Removed descriptor-relative filesystem confinement. Read/write/edit now use
  ordinary files. Relative paths start at the workspace; absolute paths,
  traversal, symlinks, and hardlinks are allowed. Exact-match edit validation and
  newline preservation remain, but writes are no longer crash-atomic.
- POSIX input still uses asyncio readers and termios. Windows console input uses
  cancellable msvcrt polling, including hidden OAuth input. Redirected Windows
  stdin uses a daemon reader so cancellation cannot hold process shutdown open.
  Prompt input keeps Enter (CR) for submission and Ctrl+J (LF) for a newline.
  POSIX interactive input disables canonical mode and CR/LF translation, with
  terminal attributes restored on return or cancellation. No input dependency
  was added; Shift+Enter is not separately bound. OAuth input remains single-line.
- Windows SIGINT dispatches onto the asyncio loop; POSIX keeps its signal handler.
- POSIX workers keep selectors and process groups. Windows pipes use two bounded
  reader threads and a small queue; completion is still the explicit status file,
  not a prompt or output-silence heuristic.
- Process cleanup is best effort: POSIX PGIDs, Windows taskkill.exe /T /F /PID.
  No Job Objects, native filesystem security wrappers, or sandbox framework.
- Windows retains OS environment variables required for launching runtimes while
  still excluding inherited provider credentials. Native Git Bash is supported;
  the System32 WSL launcher is not a native runtime for this experiment.
- Trace files use private POSIX permissions or inherited Windows directory ACLs.
- The experiment's Windows HTTP client uses `ssl.create_default_context()` to
  include Windows system CA roots instead of certifi-only trust. A credential-free
  GET reproduced a certificate-chain failure with the default httpx client and
  succeeded at TLS with system trust. TLS verification remains enabled; no
  `pip-system-certs` dependency or process-wide SSL patch is required. Transport
  errors are classified without printing potentially sensitive exception text.

## Headerless Codex streams

A successful Codex `/responses` stream can omit `Content-Type`. The provider
already parses its SSE, but the experiment's trace originally classified it as a
plain body and therefore missed completion evidence. The trace now treats a
successful `/responses` response with no content type as SSE too. It still
requires an actual successful terminal event, never just HTTP 200. Offline tests
cover both declared and headerless SSE, including disconnects without completion.
The opaque `x-codex-turn-state` header is also redacted.

## Usage and checks

```text
uv run python -m mode_experiment login
uv run python -m mode_experiment run --protocol modes --allow-unsafe-local
uv run pytest tests/mode_experiment
uv run mypy src/mode_experiment
uv run ruff check src/mode_experiment tests/mode_experiment
```

Offline tests cover file access outside the workspace, hardlinks, persistent
Python/Bash/PowerShell state, bounded output, cancellation, descendant cleanup,
terminal secret input, and a fake OAuth login through the CLI entrypoint. Real
browser OAuth and interactive terminal smoke testing remain user-initiated.

This experiment is deliberately unsandboxed. Process cleanup does not contain
arbitrary code or reliably recover detached children after their parent exits.
Future sandboxing can be implemented separately rather than disguised as file
path validation here.
