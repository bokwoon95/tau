"""Manual experiment CLI with terminal and local web frontends (no agent harness)."""

import argparse
import asyncio
import codecs
import os
import queue
import signal
import sys
import tempfile
import threading
from contextlib import suppress
from importlib import import_module
from pathlib import Path

from mode_experiment.authentication import credential_resolver, login
from mode_experiment.execution import Backend
from mode_experiment.runner import Limits, Runner
from mode_experiment.tracing import Output, Trace
from tau_ai.openai_codex import OpenAICodexConfig, OpenAICodexProvider
from tau_coding.credentials import FileCredentialStore


class Terminal:
    """Cancellable terminal input; redirected Windows input uses a daemon reader."""

    def __init__(self, output: Output) -> None:
        self.output = output
        self.buffer = bytearray()
        self.eof = False
        self.lines: queue.Queue[str] | None = None

    def input_key(self, chars: list[str], char: str, *, secret: bool) -> str | None:
        if char == "\x03":
            raise asyncio.CancelledError
        if char in ("\x04", "\x1a") and not chars:
            self.eof = True
            raise EOFError
        if char == "\r" or (char == "\n" and secret):
            sys.stdout.write("\n")
            sys.stdout.flush()
            return "".join(chars)
        if char in ("\b", "\x7f"):
            if chars:
                removed = chars.pop()
                if not secret:
                    if removed == "\n":
                        column = len("".join(chars).rsplit("\n", 1)[-1]) + 1
                        sys.stdout.write(f"\x1b[1A\x1b[{column}G")
                    else:
                        sys.stdout.write("\b \b")
        elif char == "\n" or char == "\t" or char.isprintable():
            chars.append(char)
            if not secret:
                sys.stdout.write(char)
        sys.stdout.flush()
        return None

    async def windows_readline(self, *, secret: bool) -> str:
        if not sys.stdin.isatty():
            if self.lines is None:
                self.lines = queue.Queue()

                def read_lines() -> None:
                    assert self.lines is not None
                    for line in sys.stdin:
                        self.lines.put(line)
                    self.lines.put("")

                threading.Thread(target=read_lines, daemon=True).start()
            while self.lines.empty():
                await asyncio.sleep(0.02)
            line = self.lines.get_nowait()
            if not line:
                self.eof = True
                raise EOFError
            return line.removesuffix("\n").removesuffix("\r")
        msvcrt = import_module("msvcrt")

        chars: list[str] = []
        extended = False
        while True:
            if not msvcrt.kbhit():
                await asyncio.sleep(0.02)
                continue
            char = msvcrt.getwch()
            if extended:
                extended = False
                continue
            if char in ("\x00", "\xe0"):
                extended = True
            else:
                result = self.input_key(chars, char, secret=secret)
                if result is not None:
                    return result

    async def readline(self, prompt: str, *, secret: bool = False) -> str:
        self.output.emit("[harness: input] " + prompt)
        if self.eof and not self.buffer:
            raise EOFError
        if sys.platform == "win32":
            return await self.windows_readline(secret=secret)
        termios = import_module("termios")
        loop = asyncio.get_running_loop()
        fd = sys.stdin.fileno()
        attributes = None
        interactive = os.isatty(fd)
        if interactive:
            attributes = termios.tcgetattr(fd)
            hidden = list(attributes)
            hidden[6] = list(attributes[6])
            # Preserve SIGINT, but distinguish Enter (CR) from Ctrl-J (LF).
            hidden[0] &= ~(termios.ICRNL | termios.INLCR | termios.IGNCR)
            hidden[3] &= ~(termios.ECHO | termios.ECHONL | termios.ICANON)
            hidden[6][termios.VMIN] = 1
            hidden[6][termios.VTIME] = 0
            termios.tcsetattr(fd, termios.TCSANOW, hidden)
        chars: list[str] = []
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        escape = ""
        try:
            while True:
                if interactive and self.buffer:
                    # Consume only through submission; keep typeahead for next input.
                    byte = bytes(self.buffer[:1])
                    del self.buffer[:1]
                    for char in decoder.decode(byte):
                        if char == "\x1b":
                            escape = char
                            continue
                        if escape:
                            escape += char
                            if (len(escape) > 2 and (char.isalpha() or char == "~")) or (
                                len(escape) == 2 and char not in "[O"
                            ):
                                escape = ""
                            continue
                        result = self.input_key(chars, char, secret=secret)
                        if result is not None:
                            return result
                    continue
                if not interactive and b"\n" in self.buffer:
                    break
                if self.eof:
                    if interactive:
                        if chars:
                            return "".join(chars)
                        raise EOFError
                    break
                ready = loop.create_future()

                def consume(ready: asyncio.Future[None] = ready) -> None:
                    try:
                        data = os.read(fd, 4096)
                        if data:
                            self.buffer.extend(data)
                        else:
                            self.eof = True
                        if not ready.done():
                            ready.set_result(None)
                    except OSError as exc:
                        if not ready.done():
                            ready.set_exception(exc)

                loop.add_reader(fd, consume)
                try:
                    await ready
                finally:
                    loop.remove_reader(fd)
            if not self.buffer and self.eof:
                raise EOFError
            line, _, rest = self.buffer.partition(b"\n")
            self.buffer = bytearray(rest)
            return line.removesuffix(b"\r").decode("utf-8", errors="replace")
        finally:
            if attributes is not None:
                termios.tcsetattr(fd, termios.TCSANOW, attributes)


def positive(value: str) -> float:
    number = float(value)
    if not 0 < number < float("inf"):
        raise argparse.ArgumentTypeError("must be finite and positive")
    return number


def count(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Manual Codex native tools versus text-only modes")
    commands = root.add_subparsers(dest="command", required=True)
    for name in ("login", "run", "web"):
        sub = commands.add_parser(name)
        sub.add_argument(
            "--no-trace", action="store_true", help="disable HTTP/SSE diagnostic display"
        )
        sub.add_argument(
            "--trace-file", type=Path, help="optional private diagnostic transcript (overwritten)"
        )
        sub.add_argument(
            "--trace-limit",
            type=count,
            default=65536,
            help="diagnostic record/frame characters (default: 65536)",
        )
        if name == "login":
            sub.add_argument("--no-browser", action="store_true")
            continue
        if name == "web":
            sub.add_argument(
                "--host", choices=("127.0.0.1", "localhost", "::1"), default="127.0.0.1"
            )
            sub.add_argument("--port", type=count, default=8000)
            sub.add_argument(
                "--context-window",
                type=count,
                help="context window tokens for the web status bar; overrides model metadata",
            )
        sub.add_argument("--protocol", required=True, choices=("tools", "modes"))
        sub.add_argument("--model", default="gpt-6.1-sol")
        sub.add_argument(
            "--reasoning",
            choices=("default", "none", "minimal", "low", "medium", "high", "xhigh"),
            default="medium",
        )
        sub.add_argument(
            "--reasoning-summary", choices=("auto", "concise", "detailed", "none"), default="auto"
        )
        sub.add_argument(
            "--workspace",
            type=Path,
            help="modify a dedicated existing directory in place; default: empty temp directory",
        )
        sub.add_argument(
            "--keep-workspace",
            action="store_true",
            help="retain default temporary workspace after exit",
        )
        sub.add_argument(
            "--allow-unsafe-local",
            action="store_true",
            help="explicitly opt in to unsandboxed host execution",
        )
        sub.add_argument(
            "--discover-models",
            action="store_true",
            help="display authenticated Codex model catalog before prompting",
        )
        sub.add_argument("--max-requests", type=count, default=24)
        sub.add_argument("--max-actions", type=count, default=24)
        sub.add_argument("--max-recoveries", type=count, default=4)
        sub.add_argument("--wall-time", type=positive, default=300)
        sub.add_argument("--action-timeout", type=positive, default=30)
        sub.add_argument("--output-limit", type=count, default=16384)
    return root


async def main_async(args: argparse.Namespace) -> int:
    store = FileCredentialStore()
    if args.trace_file is not None and args.trace_file.resolve() == store.path.resolve():
        sys.stderr.write("[harness: failure] Trace destination must not be the credential store.\n")
        return 1
    output = Output(sys.stdout, args.trace_file, args.trace_limit)
    trace = Trace(output, enabled=not args.no_trace, frame_limit=args.trace_limit)
    terminal = Terminal(output)
    current = asyncio.current_task()
    active = None

    def interrupt() -> None:
        # Idle input/login: exit. Active model/execution: interrupt only that turn.
        if active is not None:
            active.cancel()
        elif current is not None:
            current.cancel()

    loop = asyncio.get_running_loop()
    previous_sigint = signal.getsignal(signal.SIGINT)
    if os.name == "nt":
        signal.signal(signal.SIGINT, lambda *_: loop.call_soon_threadsafe(interrupt))
    else:
        loop.add_signal_handler(signal.SIGINT, interrupt)
    workspace = None
    temporary = None
    backend = None
    provider = None
    output.emit(
        "[harness] Reconstructed application-level trace, not a packet capture. "
        "Auth fields are redacted, but prompts/code/workspace data are NOT safe to publish. "
        "Do not submit secrets."
    )
    try:
        async with trace.client(timeout=60) as client:
            if args.command == "login":
                trace.association = "OAuth login/token exchange"
                await login(
                    store,
                    client,
                    output,
                    lambda prompt: terminal.readline(prompt, secret=True),
                    open_browser=not args.no_browser,
                )
                return 0
            if not args.allow_unsafe_local:
                raise RuntimeError(
                    "Local execution is UNSANDBOXED. "
                    "Pass --allow-unsafe-local to opt in explicitly."
                )
            resolve = credential_resolver(store, client, output)
            if args.workspace is None:
                temporary = tempfile.mkdtemp(prefix="tau-mode-experiment-")
                workspace = Path(temporary)
            else:
                workspace = args.workspace.expanduser().resolve(strict=True)
                if not workspace.is_dir():
                    raise RuntimeError("--workspace must be an existing directory")
            output.emit(
                "[harness: WARNING] UNSAFE LOCAL EXECUTION: generated code can read/modify "
                "the host, access host secrets, and use the network. NOT SANDBOXED."
            )
            disposition = "modified in place"
            if temporary:
                disposition = (
                    "temporary; retained" if args.keep_workspace else "temporary; removed at exit"
                )
            output.emit(
                f"[harness: startup]\nworkspace: {workspace} ({disposition})\n"
                f"protocol: {args.protocol}\nmodel: {args.model}\n"
                f"reasoning: {args.reasoning}; summary: {args.reasoning_summary}\n"
                "execution: local host, UNSANDBOXED; network: host/unrestricted\n"
                f"tracing: {'disabled' if args.no_trace else 'live HTTP/SSE'}; "
                f"transcript: {args.trace_file or 'none'}\n"
                f"limits per turn: requests={args.max_requests}, actions={args.max_actions}, "
                f"recoveries={args.max_recoveries}, wall={args.wall_time}s; "
                f"action={args.action_timeout}s; output={args.output_limit}\n"
                "Codex adapter serial API option: unavailable; parallel_tool_calls remains true. "
                "Local execution is serial."
            )
            backend = Backend(
                workspace,
                timeout=args.action_timeout,
                output_limit=args.output_limit,
            )
            config = OpenAICodexConfig(
                credential_resolver=resolve,
                reasoning_effort=None if args.reasoning == "default" else args.reasoning,
                reasoning_summary=args.reasoning_summary,
            )
            provider = OpenAICodexProvider(config, client=client)
            if args.discover_models:
                trace.association = "model discovery"
                catalog = await provider.discover_models()
                output.emit(
                    "[harness: discovered models] "
                    + ", ".join(model.id for model in catalog.models)
                )
            runner = Runner(
                provider,
                backend,
                output,
                protocol=args.protocol,
                model=args.model,
                limits=Limits(
                    args.max_requests, args.max_actions, args.max_recoveries, args.wall_time
                ),
                trace=trace,
                completed=trace.completed,
            )
            while True:
                prompt = await terminal.readline("Prompt (Enter sends; Ctrl+J newline; EOF exits):")
                if not prompt:
                    continue
                active = asyncio.create_task(runner.turn(prompt))
                try:
                    await active
                finally:
                    active = None
    except (EOFError, asyncio.CancelledError):
        output.emit(
            "[harness] Session ended (EOF/Ctrl-C at input); cleaning up processes and descendants."
        )
        return 0
    except Exception as exc:
        output.emit(f"[harness: failure] {exc}")
        return 1
    finally:
        if os.name != "nt":
            loop.remove_signal_handler(signal.SIGINT)
        signal.signal(signal.SIGINT, previous_sigint)
        if provider is not None:
            await provider.aclose()
        if backend is not None:
            await backend.close()
        if temporary is not None:
            if args.keep_workspace:
                output.emit(f"[harness] Retained workspace: {temporary}")
            else:
                import shutil

                shutil.rmtree(temporary)
        output.close()


def main() -> None:
    args = parser().parse_args()
    with suppress(KeyboardInterrupt):
        if args.command == "web":
            from mode_experiment.web import serve

            serve(args)
        else:
            raise SystemExit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
