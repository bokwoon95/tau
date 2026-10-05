import asyncio
import os
import signal
import sys
from io import StringIO
from types import SimpleNamespace

import pytest

from mode_experiment import __main__ as cli
from mode_experiment.tracing import Output


@pytest.mark.anyio
async def test_redirected_windows_input_and_eof(monkeypatch):
    monkeypatch.setattr(sys, "stdin", StringIO("one\r\ntwo\n"))
    terminal = cli.Terminal(Output(StringIO()))
    assert await terminal.windows_readline(secret=True) == "one"
    assert await terminal.windows_readline(secret=False) == "two"
    with pytest.raises(EOFError):
        await terminal.windows_readline(secret=False)


@pytest.mark.anyio
@pytest.mark.parametrize("secret", [True, False])
async def test_windows_console_editing_and_secret(monkeypatch, secret):
    chars = iter("ab\b\x00Kc\r")
    monkeypatch.setitem(
        sys.modules,
        "msvcrt",
        SimpleNamespace(
            kbhit=lambda: True,
            getwch=lambda: next(chars),
        ),
    )
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
    console = StringIO()
    monkeypatch.setattr(sys, "stdout", console)
    terminal = cli.Terminal(Output(console))
    assert await terminal.windows_readline(secret=secret) == "ac"
    assert console.getvalue() == ("\n" if secret else "ab\b \bc\n")


@pytest.mark.anyio
async def test_windows_ctrl_j_inserts_newline_enter_submits(monkeypatch):
    chars = iter("first\nsecond\r")
    monkeypatch.setitem(
        sys.modules,
        "msvcrt",
        SimpleNamespace(
            kbhit=lambda: True,
            getwch=lambda: next(chars),
        ),
    )
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
    console = StringIO()
    monkeypatch.setattr(sys, "stdout", console)
    terminal = cli.Terminal(Output(console))
    assert await terminal.windows_readline(secret=False) == "first\nsecond"
    assert console.getvalue() == "first\nsecond\n"


def test_backspace_across_newline_preserves_buffer(monkeypatch):
    monkeypatch.setattr(sys, "stdout", StringIO())
    terminal = cli.Terminal(Output(StringIO()))
    chars = []
    for char in "a\n\bb\r":
        result = terminal.input_key(chars, char, secret=False)
    assert result == "ab"


@pytest.mark.anyio
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX pseudo-terminal")
async def test_posix_ctrl_j_and_terminal_restoration(monkeypatch):
    from importlib import import_module

    termios = import_module("termios")
    master, slave = os.openpty()
    original = termios.tcgetattr(slave)
    console = StringIO()
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(fileno=lambda: slave))
    monkeypatch.setattr(sys, "stdout", console)
    terminal = cli.Terminal(Output(console))
    try:
        task = asyncio.create_task(terminal.readline("Prompt:"))
        await asyncio.sleep(0.01)
        assert not termios.tcgetattr(slave)[3] & termios.ICANON
        os.write(master, "first\nsecond é\r".encode())
        assert await asyncio.wait_for(task, 1) == "first\nsecond é"
        assert termios.tcgetattr(slave) == original
        task = asyncio.create_task(terminal.readline("Code:", secret=True))
        await asyncio.sleep(0.01)
        os.write(master, b"secret\r")
        assert await asyncio.wait_for(task, 1) == "secret"
        assert "secret" not in console.getvalue()
        task = asyncio.create_task(terminal.readline("Prompt:"))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert termios.tcgetattr(slave) == original
    finally:
        os.close(master)
        os.close(slave)


@pytest.mark.anyio
async def test_windows_console_cancellation_and_eof(monkeypatch):
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(kbhit=lambda: False))
    terminal = cli.Terminal(Output(StringIO()))
    task = asyncio.create_task(terminal.windows_readline(secret=True))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    monkeypatch.setitem(
        sys.modules,
        "msvcrt",
        SimpleNamespace(
            kbhit=lambda: True,
            getwch=lambda: "\x1a",
        ),
    )
    with pytest.raises(EOFError):
        await terminal.windows_readline(secret=False)


@pytest.mark.anyio
async def test_login_entrypoint_and_signal_restoration(tmp_path, monkeypatch):
    async def fake_login(store, client, output, prompt, *, open_browser):
        assert not open_browser
        assert await prompt("Code:") == "test-code"

    async def fake_readline(self, prompt, *, secret=False):
        assert secret
        return "test-code"

    monkeypatch.setattr(cli, "login", fake_login)
    monkeypatch.setattr(cli.Terminal, "readline", fake_readline)
    monkeypatch.setattr(
        cli,
        "FileCredentialStore",
        lambda: SimpleNamespace(
            path=tmp_path / "credentials.json",
        ),
    )
    previous = signal.getsignal(signal.SIGINT)
    args = cli.parser().parse_args(
        [
            "login",
            "--no-browser",
            "--no-trace",
            "--trace-file",
            str(tmp_path / "trace.log"),
        ]
    )
    assert await cli.main_async(args) == 0
    assert signal.getsignal(signal.SIGINT) == previous
    assert (tmp_path / "trace.log").exists()
