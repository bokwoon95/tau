import asyncio
import os
import shutil

import pytest

from mode_experiment.execution import Backend
from mode_experiment.protocols import Action


@pytest.mark.anyio
async def test_files_confinement_newlines_atomicity(tmp_path):
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
        (tmp_path / "link").symlink_to(tmp_path.parent)
        (tmp_path / "filelink").symlink_to(tmp_path / "dir/file")
        os.link(tmp_path / "dir/file", tmp_path / "hardlink")
        for path in ("../escape", "/etc/passwd", "link/escape", "filelink", "hardlink", "./bad"):
            for name, args in (
                ("read", {}),
                ("write", {"content": "evil"}),
                ("edit", {"old_text": "a", "new_text": "b"}),
            ):
                assert not (await backend.execute(Action(name, {"path": path, **args}))).success
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
        if language != "python":
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
    if language == "powershell" and not shutil.which("pwsh"):
        pytest.skip("pwsh unavailable")
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
    backend = Backend(tmp_path, timeout=0.15, output_limit=32)
    try:
        result = await backend.execute(Action("python", {"code": "print('x'*1000)"}))
        assert result.success and result.truncated and len(result.stdout) == 32
        result = await backend.execute(Action("python", {"code": "import time; time.sleep(10)"}))
        assert not result.success and result.state_lost and "timeout" in result.error
        code = "sleep 30 &\necho $! > descendant.pid\nwait"
        task = asyncio.create_task(backend.execute(Action("bash", {"code": code})))
        for _ in range(100):
            if (tmp_path / "descendant.pid").exists():
                break
            await asyncio.sleep(0.01)
        assert (tmp_path / "descendant.pid").exists()
        pid = int((tmp_path / "descendant.pid").read_text())
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert backend.processes["bash"].dead
        for _ in range(100):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("descendant survived cancellation")
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
