"""Shared workspace operations and lazy, persistent LOCAL language processes.

Direct file operations are confined. Executable source is NOT sandboxed.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import stat
import sys
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import cast

from mode_experiment.protocols import FIELDS, OPERATIONS, Action
from tau_agent.messages import TextContent
from tau_agent.tools import AgentTool, AgentToolResult, ToolCancellationToken, ToolUpdateCallback
from tau_agent.types import JSONValue


@dataclass
class Result:
    operation: str
    success: bool = False
    stdout: str = ""
    stderr: str = ""
    error: str = ""
    truncated: bool = False
    state_lost: bool = False
    infrastructure: bool = False

    def text(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


class Workspace:
    """POSIX descriptor-relative operations; never follow symlinks, including races."""

    def __init__(self, path: Path, limit: int) -> None:
        self.path = path.resolve(strict=True)
        self.limit = limit
        self.root = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY)

    def close(self) -> None:
        os.close(self.root)

    @contextmanager
    def parent(self, path: str, create: bool = False) -> Iterator[tuple[int, str]]:
        parts = path.split("/")
        if not path or any(part in ("", ".", "..") for part in parts) or "\x00" in path:
            raise ValueError(
                "path must be relative, nonempty, with no '.', '..', or empty components"
            )
        fd = os.dup(self.root)
        try:
            for part in parts[:-1]:
                if create:
                    with suppress(FileExistsError):
                        os.mkdir(part, dir_fd=fd)
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            yield fd, parts[-1]
        finally:
            os.close(fd)

    def existing(self, fd: int, name: str) -> int:
        handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        info = os.fstat(handle)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            os.close(handle)
            raise ValueError("only regular, non-hardlinked files are supported; symlinks forbidden")
        return handle

    def replace(self, fd: int, name: str, text: str) -> None:
        try:
            handle = self.existing(fd, name)
        except FileNotFoundError:
            pass
        else:
            os.close(handle)
        temporary = f".tau-{uuid.uuid4().hex}"
        try:
            handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=fd)
            with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
                stream.write(text)
            os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=fd)

    def execute(self, operation: str, args: Mapping[str, str]) -> Result:
        with self.parent(args["path"], create=operation == "write") as (fd, name):
            if operation == "write":
                self.replace(fd, name, args["content"])
                return Result(
                    operation, True, stdout=f"wrote {len(args['content'].encode('utf-8'))} bytes"
                )
            with os.fdopen(self.existing(fd, name), "r", encoding="utf-8", newline="") as stream:
                if operation == "read":
                    text = stream.read(self.limit + 1)
                    return Result(
                        operation, True, stdout=text[: self.limit], truncated=len(text) > self.limit
                    )
                # Edits are computed against the original file, never a truncated read.
                text = stream.read()
            old, new = args["old_text"], args["new_text"]
            if not old or text.find(old) < 0 or text.find(old) != text.rfind(old):
                raise ValueError(
                    "edit requires nonempty old_text matching exactly once; file unchanged"
                )
            self.replace(fd, name, text.replace(old, new, 1))
            return Result(operation, True, stdout="replaced exactly one match")


class LanguageProcess:
    def __init__(self, language: str, workspace: Path, timeout: float, output_limit: int) -> None:
        self.language, self.workspace = language, workspace
        self.timeout, self.output_limit = timeout, output_limit
        self.process: asyncio.subprocess.Process | None = None
        self.dead = False
        self.state_lost = False
        self.reason = ""

    async def start(self) -> None:
        source = Path(__file__).with_name("worker.py").read_text(encoding="utf-8")
        argv = [sys.executable, "-u", "-c", source, self.language, str(self.output_limit)]
        # Do not inherit provider tokens/proxies/credential configuration. This is
        # not a sandbox: generated source can still access the host filesystem.
        env = {
            "PATH": os.environ.get("PATH", os.defpath),
            "HOME": "/tmp",
            "LANG": "C.UTF-8",
            "TERM": "dumb",
        }
        self.process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=self.workspace,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            limit=max(65536, self.output_limit * 16 + 8192),
        )
        assert self.process.stdout is not None and self.process.stderr is not None
        ready = await asyncio.wait_for(self.process.stdout.readline(), 20)
        if not ready:
            stderr = await self.process.stderr.read(8192)
            raise RuntimeError(f"execution worker failed: {stderr.decode(errors='replace')}")
        record = json.loads(ready)
        if not record.get("ready"):
            raise FileNotFoundError(record.get("error", "worker startup failed"))

    async def execute(self, code: str) -> Result:
        if self.dead:
            return Result(
                self.language,
                error=self.reason + "; unavailable for this session (no silent restart)",
                state_lost=self.state_lost,
            )
        try:
            if self.process is None:
                await self.start()
            assert self.process and self.process.stdin and self.process.stdout
            self.process.stdin.write(
                (json.dumps({"code": code, "timeout": self.timeout}) + "\n").encode()
            )
            await self.process.stdin.drain()
            data = await asyncio.wait_for(self.process.stdout.readline(), self.timeout + 3)
            if not data:
                raise RuntimeError("execution worker died; state lost")
            record = json.loads(data)
            result = Result(
                self.language,
                success=record.get("success", False),
                stdout=record.get("stdout", ""),
                stderr=record.get("stderr", ""),
                error=record.get("error", ""),
                truncated=record.get("truncated", False),
                state_lost=record.get("state_lost", False),
            )
            if not result.success and not result.error:
                result.error = f"exit status {record.get('exit_code')}"
            if result.state_lost:
                self.state_lost = True
                self.reason = result.error
                await self.close()
            return result
        except asyncio.CancelledError:
            self.state_lost = self.process is not None
            self.reason = "execution cancelled; interpreter destroyed; state lost"
            await self.close()
            raise
        except FileNotFoundError as exc:
            self.reason = str(exc)
            await self.close()
            return Result(self.language, error=self.reason)
        except Exception as exc:
            self.state_lost = self.process is not None
            self.reason = f"execution infrastructure failure: {exc}; state lost"
            await self.close()
            return Result(
                self.language, error=self.reason, state_lost=self.state_lost, infrastructure=True
            )

    async def close(self) -> None:
        self.dead = True
        if self.process is not None:
            # TERM lets the supervisor kill its child's process group and reap it.
            with suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(self.process.wait(), 2)
            except TimeoutError:
                with suppress(ProcessLookupError):
                    os.killpg(self.process.pid, signal.SIGKILL)
                await self.process.wait()


class Backend:
    def __init__(self, workspace: Path, *, timeout: float = 30, output_limit: int = 16384) -> None:
        self.workspace = Workspace(workspace, output_limit)
        self.processes = {
            name: LanguageProcess(name, self.workspace.path, timeout, output_limit)
            for name in ("bash", "powershell", "python")
        }

    async def execute(self, action: Action) -> Result:
        expected = FIELDS.get(action.operation)
        if (
            expected is None
            or set(action.arguments) != set(expected)
            or any(not isinstance(value, str) for value in action.arguments.values())
        ):
            return Result(
                action.operation,
                error=f"invalid arguments: expected exactly string fields {expected}",
            )
        # Narrow only after validation; malformed native arguments are useful errors.
        args = cast(Mapping[str, str], action.arguments)
        try:
            if action.operation in self.processes:
                return await self.processes[action.operation].execute(args["code"])
            return self.workspace.execute(action.operation, args)
        except (OSError, ValueError) as exc:
            return Result(action.operation, error=str(exc))

    async def close(self) -> None:
        for process in self.processes.values():
            if process.process is not None:
                await process.close()
        self.workspace.close()

    def tools(self) -> list[AgentTool]:
        descriptions = {
            "read": "Read a workspace-relative UTF-8 file, with bounded output; no symlinks.",
            "write": "Create/overwrite a UTF-8 file and parent directories within the workspace.",
            "edit": (
                "Replace exactly one nonempty old_text match with new_text; "
                "ambiguous edits fail atomically."
            ),
            "bash": "Execute multiline Bash source in this session's persistent Bash process.",
            "powershell": (
                "Execute multiline PowerShell source in this session's persistent pwsh process."
            ),
            "python": (
                "Execute multiline Python source in this session's persistent Python process."
            ),
        }
        definitions = []
        for name in OPERATIONS:

            async def execute(
                tool_call_id: str,
                arguments: Mapping[str, JSONValue],
                signal: ToolCancellationToken | None = None,
                on_update: ToolUpdateCallback | None = None,
                *,
                operation: str = name,
            ) -> AgentToolResult:
                result = await self.execute(Action(operation, arguments))
                return AgentToolResult(content=[TextContent(text=result.text())])

            fields = FIELDS[name]
            parameters: dict[str, JSONValue] = {
                "type": "object",
                "properties": {field: {"type": "string"} for field in fields},
                "required": list(fields),
                "additionalProperties": False,
            }
            definitions.append(
                AgentTool(
                    name=name,
                    label=name,
                    description=descriptions[name],
                    prompt_snippet=descriptions[name],
                    parameters=parameters,
                    execute_fn=execute,
                    execution_mode="sequential",
                )
            )
        return definitions
