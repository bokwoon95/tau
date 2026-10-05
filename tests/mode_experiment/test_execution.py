import asyncio
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from mode_experiment.execution import Backend
from mode_experiment.protocols import Action


def process_alive(pid):
    if os.name == "nt":
        result = subprocess.run(
            ["tasklist.exe", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            check=True,
        )
        return f'","{pid}",' in result.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    status = Path(f"/proc/{pid}/stat")
    return not (status.exists() and status.read_text().split(")", 1)[1].split()[0] == "Z")


@pytest.mark.anyio
async def test_files_newlines_and_edit_validation(tmp_path):
    backend = Backend(tmp_path, output_limit=8)
    try:
        assert (
            await backend.execute(
                Action("write", {"path": "dir/file", "content": "a\r\na\r\nlong"})
            )
        ).success
        result = await backend.execute(Action("read", {"path": "dir/file"}))
        assert result.stdout == "a\r\na\r\nlo" and result.truncated
        original = (tmp_path / "dir/file").read_bytes()
        result = await backend.execute(
            Action("edit", {"path": "dir/file", "old_text": "a", "new_text": "b"})
        )
        assert not result.success and (tmp_path / "dir/file").read_bytes() == original
        assert (
            await backend.execute(
                Action("edit", {"path": "dir/file", "old_text": "long", "new_text": ""})
            )
        ).success
        assert (tmp_path / "dir/file").read_bytes() == b"a\r\na\r\n"
        # Absolute paths and parent traversal intentionally remain unrestricted.
        for path in (str(tmp_path.parent / "outside.txt"), "../outside.txt"):
            assert (
                await backend.execute(Action("write", {"path": path, "content": "host"}))
            ).success
            assert (await backend.execute(Action("read", {"path": path}))).stdout == "host"
            assert (
                await backend.execute(
                    Action("edit", {"path": path, "old_text": "host", "new_text": "changed"})
                )
            ).success
        os.link(tmp_path / "dir/file", tmp_path / "hardlink")
        assert (
            await backend.execute(Action("write", {"path": "hardlink", "content": "linked"}))
        ).success
        assert (tmp_path / "dir/file").read_text() == "linked"
        assert not (await backend.execute(Action("write", {"path": "x", "content": 1}))).success
        assert not (await backend.execute(Action("wat", {}))).success
    finally:
        await backend.close()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "language,first,second,error",
    [
        (
            "python",
            "import sys\nx=40\ndef plus(n):\n    return n+2\n"
            "print('first')\nprint('err', file=sys.stderr)",
            "print(plus(x))",
            "raise ValueError('bad')",
        ),
        (
            "bash",
            "x=40\nplus() { echo $(($1 + 2)); }\nprintf 'first\\n'\n"
            "printf 'err\\n' >&2\nmkdir sub\ncd sub",
            'plus "$x"\npwd',
            "false",
        ),
        (
            "powershell",
            "$x=40\nfunction plus($n) { $n+2 }\n[Console]::WriteLine('first')\n"
            "[Console]::Error.WriteLine('err')\nNew-Item -ItemType Directory sub | Out-Null\n"
            "Set-Location sub",
            "plus $x\n(Get-Location).Path",
            "throw 'bad'",
        ),
    ],
)
async def test_persistent_multiline_errors_and_independent_sessions(
    tmp_path, language, first, second, error
):
    if language != "python" and not shutil.which("pwsh" if language == "powershell" else "bash"):
        pytest.skip("runtime not installed")
    backend = Backend(tmp_path)
    try:
        result = await backend.execute(Action(language, {"code": first}))
        assert result.success, result.text()
        assert "first" in result.stdout and "err" in result.stderr
        process = backend.processes[language].process
        result = await backend.execute(Action(language, {"code": second}))
        assert result.success and "42" in result.stdout, result.text()
        if language == "bash" and os.name == "nt":
            assert result.stdout.strip().endswith("/sub")
        elif language != "python":
            assert str(tmp_path / "sub") in result.stdout
        assert backend.processes[language].process is process
        assert not (await backend.execute(Action(language, {"code": error}))).success
        assert "42" in (await backend.execute(Action(language, {"code": second}))).stdout
        separate = Backend(tmp_path)
        try:
            if language == "python":
                assert not (await separate.execute(Action(language, {"code": "print(x)"}))).success
        finally:
            await separate.close()
    finally:
        await backend.close()
    assert process.returncode is not None


@pytest.mark.anyio
@pytest.mark.parametrize(
    "language,code",
    [
        ("python", "import os; os._exit(7)"),
        ("bash", "exit 7"),
        ("powershell", "[Environment]::Exit(7)"),
    ],
)
async def test_death_never_silently_restarts(tmp_path, language, code):
    if language != "python" and not shutil.which("pwsh" if language == "powershell" else "bash"):
        pytest.skip("runtime unavailable")
    backend = Backend(tmp_path)
    try:
        result = await backend.execute(Action(language, {"code": code}))
        assert not result.success and result.state_lost, result.text()
        again = await backend.execute(Action(language, {"code": ""}))
        assert not again.success and "no silent restart" in again.error
    finally:
        await backend.close()


@pytest.mark.anyio
async def test_timeout_cancel_output_and_descendants(tmp_path):
    backend = Backend(tmp_path, timeout=1, output_limit=32)
    try:
        result = await backend.execute(Action("python", {"code": "print('x'*1000)"}))
        assert result.success and result.truncated and len(result.stdout) == 32
        result = await backend.execute(Action("python", {"code": "import time; time.sleep(10)"}))
        assert not result.success and result.state_lost and "timeout" in result.error
        code = (
            "import subprocess, sys, pathlib, time\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
            "pathlib.Path('descendant.pid').write_text(str(child.pid))\n"
            "time.sleep(30)"
        )
        # Use a separate backend: timed-out interpreters must never restart.
        cancelled = Backend(tmp_path)
        task = asyncio.create_task(cancelled.execute(Action("python", {"code": code})))
        for _ in range(100):
            if (tmp_path / "descendant.pid").exists():
                break
            await asyncio.sleep(0.01)
        assert (tmp_path / "descendant.pid").exists()
        pid = int((tmp_path / "descendant.pid").read_text())
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancelled.processes["python"].dead
        await cancelled.close()
        for _ in range(100):
            if not process_alive(pid):
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("descendant survived cancellation")
    finally:
        await backend.close()


@pytest.mark.anyio
async def test_large_output_is_bounded_and_not_replayed(tmp_path):
    backend = Backend(tmp_path, output_limit=32)
    try:
        result = await backend.execute(
            Action(
                "python",
                {"code": ("import sys\nprint('x'*1000000)\nprint('y'*1000000, file=sys.stderr)")},
            )
        )
        assert result.success and result.truncated
        assert result.stdout == "x" * 32 and result.stderr == "y" * 32
        result = await backend.execute(Action("python", {"code": "print('next')"}))
        assert result.success and result.stdout.splitlines() == ["next"] and not result.stderr
    finally:
        await backend.close()


@pytest.mark.anyio
async def test_missing_powershell_is_actionable(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    backend = Backend(tmp_path)
    try:
        result = await backend.execute(Action("powershell", {"code": "'hello'"}))
        assert not result.success and "install PowerShell (pwsh)" in result.error
        assert (await backend.execute(Action("python", {"code": "print(42)"}))).success
    finally:
        await backend.close()
