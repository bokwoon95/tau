"""Process supervisor with a sanitized environment; source executes locally.

The host speaks JSON lines to this supervisor, NOT to the model. Completion is
an explicit out-of-band status file, never prompt matching or output silence.
Only bounded output is held in memory. Child interpreter state is never restarted.
"""

import json
import os
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
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
    if not executable:
        guidance = "PowerShell (pwsh)" if language == "powershell" else "Bash"
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
        control = str(Path(directory) / "done")
        source = str(Path(directory) / ("action.ps1" if language == "powershell" else "action"))
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
            start_new_session=True,
        )

        assert child.stdin is not None and child.stdout is not None and child.stderr is not None

        def cleanup(*_: object) -> None:
            # Kill the group even when its original leader has already exited.
            with suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGKILL)
            child.wait()

        def terminate(*_: object) -> None:
            cleanup()
            raise SystemExit(0)

        signal.signal(signal.SIGTERM, terminate)
        signal.signal(signal.SIGINT, terminate)
        selector = selectors.DefaultSelector()
        for name, stream in (("stdout", child.stdout), ("stderr", child.stderr)):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)
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
                    command = f". '{source}'\nprintf '%s' \"$?\" > '{control}'\n"
                else:
                    # Dot-sourcing and try do not introduce a PowerShell scope.
                    command = (
                        "$global:LASTEXITCODE = 0; $tau_status = 0; "
                        f"try {{ . '{source}'; if (-not $?) {{ $tau_status = 1 }}; "
                        "if ($LASTEXITCODE) { $tau_status = $LASTEXITCODE } } "
                        "catch { [Console]::Error.WriteLine($_.ToString()); $tau_status = 1 }; "
                        f"[IO.File]::WriteAllText('{control}', [string]$tau_status)\n"
                    )
                child.stdin.write(command.encode("utf-8"))
                child.stdin.flush()
                outputs = {"stdout": bytearray(), "stderr": bytearray()}
                truncated = False
                deadline = time.monotonic() + request["timeout"]

                def drain(timeout: float, outputs: dict[str, bytearray] = outputs) -> None:
                    nonlocal truncated
                    for key, _ in selector.select(timeout):
                        data = os.read(key.fd, 8192)
                        if not data:
                            selector.unregister(key.fileobj)
                            continue
                        target = outputs[key.data]
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
                        while selector.select(0):
                            drain(0)
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
            selector.close()


if __name__ == "__main__":
    main()
