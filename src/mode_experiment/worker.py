"""Process supervisor with a sanitized environment; source executes locally.

The host speaks JSON lines to this supervisor, NOT to the model. Completion is
an explicit out-of-band status file, never prompt matching or output silence.
Only bounded output is held in memory. Child interpreter state is never restarted.
"""

import json
import os
import queue
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import suppress
from pathlib import Path

PYTHON_CHILD = r"""
import json, os, sys, traceback
scope = {"__name__": "__main__"}
control = sys.argv[1]
for line in sys.stdin:
    request = json.loads(line)
    status = 0
    try:
        exec(compile(request["code"], "<tau-action>", "exec"), scope, scope)
    except Exception:
        traceback.print_exc()
        status = 1
    sys.stdout.flush()
    sys.stderr.flush()
    with open(control, "w") as f:
        f.write(str(status))
"""


def main() -> None:
    language, limit = sys.argv[1], int(sys.argv[2])
    name = {"bash": "bash", "powershell": "pwsh", "python": "python3"}[language]
    executable = sys.executable if language == "python" else shutil.which(name)
    if language == "bash" and sys.platform == "win32":
        # System32/bash.exe launches WSL, not a native Windows interpreter.
        if executable and Path(executable).parent.name.lower() == "system32":
            git_bash = Path("C:/Program Files/Git/bin/bash.exe")
            executable = str(git_bash) if git_bash.is_file() else None
    if not executable:
        guidance = (
            "PowerShell (pwsh)" if language == "powershell" else "Bash (Git for Windows on Windows)"
        )
        print(
            json.dumps(
                {
                    "error": f"{language} unavailable: install {guidance} locally",
                    "unavailable": True,
                }
            ),
            flush=True,
        )
        return
    with tempfile.TemporaryDirectory(prefix="tau-worker-") as directory:
        control = (Path(directory) / "done").as_posix()
        source = (
            Path(directory) / ("action.ps1" if language == "powershell" else "action")
        ).as_posix()
        if language == "python":
            argv = [executable, "-u", "-c", PYTHON_CHILD, control]
        elif language == "bash":
            argv = [executable, "--noprofile", "--norc"]
        else:
            argv = [executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", "-"]
        child = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=os.name == "posix",
        )

        assert child.stdin is not None and child.stdout is not None and child.stderr is not None

        def cleanup(*_: object) -> None:
            if sys.platform == "win32":
                if child.poll() is None:
                    subprocess.run(
                        ["taskkill.exe", "/T", "/F", "/PID", str(child.pid)],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                    )
            else:
                # Kill the group even when its original leader has already exited.
                with suppress(ProcessLookupError):
                    os.killpg(child.pid, signal.SIGKILL)
            child.wait()

        def terminate(*_: object) -> None:
            cleanup()
            raise SystemExit(0)

        signal.signal(signal.SIGTERM, terminate)
        signal.signal(signal.SIGINT, terminate)
        selector = selectors.DefaultSelector() if os.name == "posix" else None
        chunks: queue.Queue[tuple[str, bytes]] = queue.Queue(maxsize=16)
        for name, stream in (("stdout", child.stdout), ("stderr", child.stderr)):
            if selector is not None:
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)
            else:

                def read_pipe(name: str = name, fd: int = stream.fileno()) -> None:
                    while True:
                        data = os.read(fd, 8192)
                        if not data:
                            break
                        chunks.put((name, data))

                threading.Thread(target=read_pipe, daemon=True).start()
        print(json.dumps({"ready": True}), flush=True)
        try:
            for line in sys.stdin:
                request = json.loads(line)
                code = request["code"]
                if Path(control).exists():
                    Path(control).unlink()
                Path(source).write_bytes(code.encode("utf-8"))
                if language == "python":
                    command = json.dumps({"code": code}) + "\n"
                elif language == "bash":
                    # Dot-sourcing runs in the shell itself, not a subshell.
                    quoted_source = source.replace("'", "'\"'\"'")
                    quoted_control = control.replace("'", "'\"'\"'")
                    command = f". '{quoted_source}'\nprintf '%s' \"$?\" > '{quoted_control}'\n"
                else:
                    # Dot-sourcing and try do not introduce a PowerShell scope.
                    quoted_source = source.replace("'", "''")
                    quoted_control = control.replace("'", "''")
                    command = (
                        "$global:LASTEXITCODE = 0; $tau_status = 0; "
                        f"try {{ . '{quoted_source}'; if (-not $?) {{ $tau_status = 1 }}; "
                        "if ($LASTEXITCODE) { $tau_status = $LASTEXITCODE } } "
                        "catch { [Console]::Error.WriteLine($_.ToString()); $tau_status = 1 }; "
                        f"[IO.File]::WriteAllText('{quoted_control}', [string]$tau_status)\n"
                    )
                child.stdin.write(command.encode("utf-8"))
                child.stdin.flush()
                outputs = {"stdout": bytearray(), "stderr": bytearray()}
                truncated = False
                deadline = time.monotonic() + request["timeout"]

                def drain(timeout: float, outputs: dict[str, bytearray] = outputs) -> None:
                    nonlocal truncated
                    if selector is not None:
                        batch = []
                        for key, _ in selector.select(timeout):
                            data = os.read(key.fd, 8192)
                            if not data:
                                selector.unregister(key.fileobj)
                            else:
                                batch.append((key.data, data))
                    else:
                        batch = []
                        with suppress(queue.Empty):
                            batch.append(chunks.get(timeout=timeout))
                        for _ in range(chunks.qsize()):
                            batch.append(chunks.get_nowait())
                    for name, data in batch:
                        target = outputs[name]
                        remaining = max(0, limit - len(target))
                        target.extend(data[:remaining])
                        truncated |= len(data) > remaining

                result: dict[str, object]
                while True:
                    drain(0.02)
                    if child.poll() is not None:
                        drain(0)
                        result = {
                            "error": f"interpreter died (exit {child.returncode}); state lost",
                            "state_lost": True,
                        }
                        break
                    if Path(control).exists() and Path(control).stat().st_size:
                        # Explicit completion after writes; consume queued output.
                        if selector is not None:
                            while selector.select(0):
                                drain(0)
                        else:
                            # Allow pipe-reader threads to deliver the final writes.
                            drain(0.02)
                            while not chunks.empty():
                                drain(0.02)
                        status = int(Path(control).read_text())
                        result = {"success": status == 0, "exit_code": status}
                        break
                    if time.monotonic() >= deadline:
                        cleanup()
                        result = {
                            "error": "action timeout; interpreter destroyed; state lost",
                            "state_lost": True,
                        }
                        break
                result.update(
                    {
                        name: bytes(data).decode("utf-8", errors="replace")
                        for name, data in outputs.items()
                    }
                )
                result["truncated"] = truncated
                print(json.dumps(result), flush=True)
                if result.get("state_lost"):
                    break
        finally:
            cleanup()
            if selector is not None:
                selector.close()


if __name__ == "__main__":
    main()
