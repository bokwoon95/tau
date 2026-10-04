"""Reconstructed application-level HTTP/SSE diagnostics, not a packet capture.

httpx response hooks tee the raw stream without consuming or replacing bytes.
Independent content decoders let framing observe compressed responses too.
Oversized frames are withheld entirely, rather than leaking partially redacted JSON.
"""

from __future__ import annotations

import codecs
import json
import re
import threading
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

import httpx
from httpx._decoders import SUPPORTED_DECODERS, MultiDecoder

from tau_ai.http import create_async_client

# Values, not merely field names, are remembered to redact later echoes/errors.
SENSITIVE = re.compile(
    r"^(?:authorization|proxy.authorization|.*(?:token|secret|cookie)|api.?key|access|refresh|"
    r"id_token|authorization_code|code_verifier|code_challenge|state|.*account.*id|account_id|"
    r"session.?id|.*(?:organization|user).?id|organization|login_hint|email|sub|password)$",
    re.I,
)


class Redactor:
    def __init__(self) -> None:
        self.secrets: set[str] = set()

    def remember(self, value: object) -> None:
        if isinstance(value, str) and value and value != "<redacted>":
            self.secrets.add(value)
            if value.lower().startswith("bearer "):
                self.secrets.add(value[7:])
        elif isinstance(value, (dict, list)):
            for item in value.values() if isinstance(value, dict) else value:
                self.remember(item)

    def structure(self, value: Any, auth: bool = False) -> Any:
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                if SENSITIVE.fullmatch(key) or (auth and key == "code"):
                    self.remember(item)
                    result[key] = "<redacted>"
                else:
                    result[key] = self.structure(item, auth)
            return result
        if isinstance(value, list):
            return [self.structure(item, auth) for item in value]
        return self.text(value) if isinstance(value, str) else value

    def url(self, value: str) -> str:
        try:
            parsed = urlsplit(value)
            fields = []
            for key, item in parse_qsl(parsed.query, keep_blank_values=True):
                if SENSITIVE.fullmatch(key) or key == "code":
                    self.remember(item)
                    item = "<redacted>"
                fields.append((key, item))
            netloc = parsed.netloc
            if "@" in netloc:
                self.remember(netloc.split("@", 1)[0])
                netloc = "<redacted>@" + netloc.split("@", 1)[1]
            fragment = parsed.fragment
            if fragment:
                self.remember(fragment)
                fragment = "<redacted>"
            return urlunsplit(
                (parsed.scheme, netloc, parsed.path, urlencode(fields, safe="<>"), fragment)
            )
        except ValueError:
            return "<withheld malformed URL>"

    def text(self, text: str) -> str:
        # URLs must be handled before the generic key matcher: otherwise an
        # 'https:' match could swallow the first sensitive query parameter.
        text = re.sub(r"https?://[^\s<>\"']+", lambda match: self.url(match[0]), text)
        text = re.sub(r"(?i)\bBearer\s+[\w.+=/~-]+", "Bearer <redacted>", text)
        text = re.sub(r"(?i)(authorization\s*:\s*Basic\s+)[^\s,;]+", r"\1<redacted>", text)
        # Keyed JSON/form/header values, URLs nested in messages, and bare JWTs.
        keyed = re.compile(
            r'(?P<key>["\']?[\w.-]+["\']?\s*[:=]\s*)(?P<value>"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[^\s&,;\[\]{}]+)'
        )

        def replace(match: re.Match[str]) -> str:
            key = re.split(r"\s*[:=]", match["key"], maxsplit=1)[0].strip("\"' ")
            if SENSITIVE.fullmatch(key):
                value = match["value"].strip("\"'")
                self.remember(value)
                self.remember(unquote(value))
                return match["key"] + '"<redacted>"'
            return match[0]

        text = keyed.sub(replace, text)
        text = re.sub(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*", "<redacted>", text)
        for secret in sorted(self.secrets, key=len, reverse=True):
            text = text.replace(secret, "<redacted>")
            # Secrets can also appear JSON escaped or percent encoded.
            text = text.replace(json.dumps(secret)[1:-1], "<redacted>")
            text = text.replace(quote(secret, safe=""), "<redacted>")
        return text

    def body(self, data: str, content_type: str, auth: bool = False) -> str:
        if "json" in content_type:
            try:
                value = self.structure(json.loads(data), auth)
                return self.text(json.dumps(value, ensure_ascii=False))
            except (ValueError, TypeError):
                # Malformed structured auth data is not safely inspectable.
                return "<withheld malformed JSON body>"
        if "x-www-form-urlencoded" in content_type:
            fields = dict(parse_qsl(data, keep_blank_values=True))
            return self.text(json.dumps(self.structure(fields, auth=True), ensure_ascii=False))
        return self.text(data)

    def headers(self, headers: httpx.Headers) -> str:
        lines = []
        for key, value in headers.multi_items():
            if SENSITIVE.fullmatch(key):
                self.remember(value)
                value = "<redacted>"
            lines.append(f"{key}: {self.text(value)}")
        return "\n".join(lines)

    def frame(self, frame: str) -> str:
        # Join multiline data fields for structured redaction, then restore their
        # count/position. Other fields (including comments) remain visible.
        lines = frame.splitlines()
        indices = [i for i, line in enumerate(lines) if line.startswith("data:")]
        if indices:
            data = "\n".join(lines[i][5:].removeprefix(" ") for i in indices)
            try:
                self.structure(json.loads(data))  # Learn secrets before any line is output.
            except (ValueError, TypeError):
                if re.search(
                    r"(?i)(token|verifier|state|authorization|account.?id|cookie|secret)", data
                ):
                    for i in indices:
                        lines[i] = "data: <withheld non-JSON sensitive data>"
        return self.text("\n".join(lines))


class Output:
    """One ordered, flushed output path for prompts, trace and harness records."""

    def __init__(self, console: TextIO, trace_file: Path | None = None, limit: int = 65536) -> None:
        self.console = console
        self.file: TextIO | None = None
        if trace_file is not None:
            # Diagnostic transcript is private by default, including existing files.
            import os

            fd = os.open(trace_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            os.fchmod(fd, 0o600)
            self.file = os.fdopen(fd, "w", encoding="utf-8")
        self.redactor = Redactor()
        self.limit = limit
        self.lock = threading.RLock()

    def emit(self, text: str) -> None:
        with self.lock:
            text = self.redactor.text(text)
            # Don't allow arbitrary workspace/source bytes to control the terminal.
            text = text.replace("\r\n", "\n")
            text = "".join(
                c if c in "\n\t" or (ord(c) >= 32 and ord(c) != 127) else f"\\x{ord(c):02x}"
                for c in text
            )
            if len(text) > self.limit:
                text = text[: self.limit] + "\n[harness: diagnostic display truncated]"
            for stream in (self.console, self.file):
                if stream is not None:
                    stream.write(text + "\n")
                    stream.flush()

    def auth_link(self, url: str) -> None:
        # Required user-facing authorization link is the ONLY bypass. It is never
        # copied into the transcript, and query secrets are learned for later logs.
        self.redactor.text(url)
        with self.lock:
            self.console.write(
                "[harness: login] Open in your browser (sensitive link; not logged):\n" + url + "\n"
            )
            self.console.flush()

    def close(self) -> None:
        if self.file is not None:
            self.file.close()


class Frames:
    """Incremental UTF-8/line decoder with a bounded complete-frame buffer."""

    def __init__(self, emit: Callable[[str | None], None], limit: int = 65536) -> None:
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.emit, self.limit = emit, limit
        self.frame: list[str] = []
        self.line = ""
        self.size = 0
        self.overflow = False
        self.pending_cr = False

    def feed(self, data: bytes) -> None:
        self.characters(self.decoder.decode(data))

    def characters(self, text: str) -> None:
        for char in text:
            if self.pending_cr:
                self.pending_cr = False
                if char == "\n":
                    continue
            if char in ("\r", "\n"):
                self.end_line()
                self.pending_cr = char == "\r"
            else:
                self.size += 1
                if self.size > self.limit:
                    self.overflow = True
                    self.frame.clear()
                # Even while discarding an oversized frame, detect blank lines
                # using a bounded line sentinel, not an ever-growing line buffer.
                if self.overflow:
                    self.line = "x"
                else:
                    self.line += char

    def end_line(self) -> None:
        if not self.line:
            self.emit(None if self.overflow else "\n".join(self.frame))
            self.frame, self.size, self.overflow = [], 0, False
        elif not self.overflow:
            self.frame.append(self.line)
            self.size += 1
        self.line = ""

    def finish(self) -> bool:
        self.characters(self.decoder.decode(b"", final=True))
        # No synthetic complete frame for a disconnected/incomplete response.
        return bool(self.line or self.frame or self.overflow)


@dataclass
class TraceRecord:
    number: int
    start: float
    purpose: str
    auth: bool
    events: int = 0
    completed: bool = False
    usage: object = None


class Trace:
    def __init__(self, output: Output, enabled: bool = True, frame_limit: int = 65536) -> None:
        self.output, self.enabled, self.frame_limit = output, enabled, frame_limit
        self.number = 0
        self.association = "startup"
        self.last_inference: TraceRecord | None = None

    def emit(self, text: str) -> None:
        if self.enabled:
            self.output.emit(text)

    async def request(self, request: httpx.Request) -> None:
        self.number += 1
        record = TraceRecord(
            self.number, time.monotonic(), self.association, auth="/oauth/" in request.url.path
        )
        request.extensions["tau_experiment_trace"] = record
        if request.url.path.endswith("/responses"):
            self.last_inference = record
        headers = self.output.redactor.headers(request.headers)
        try:
            body = request.content.decode("utf-8", errors="replace")
        except httpx.RequestNotRead:
            body = "<unbuffered request body not inspected>"
        body = (
            self.output.redactor.body(body, request.headers.get("content-type", ""), record.auth)
            if body
            else ""
        )
        timestamp = datetime.now(UTC).isoformat()
        self.emit(
            f">>> request #{self.number} [{self.association}, {timestamp}, elapsed 0.000s]\n"
            f"{request.method} {self.output.redactor.text(str(request.url))} "
            "HTTP/1-style diagnostic\n"
            f"{headers}\n\n{body}"
        )

    async def response(self, response: httpx.Response) -> None:
        record: TraceRecord = response.request.extensions["tau_experiment_trace"]
        raw_version = response.extensions.get("http_version")
        version = (
            raw_version.decode(errors="replace")
            if isinstance(raw_version, bytes)
            else "not reported"
        )
        headers = self.output.redactor.headers(response.headers)
        self.emit(
            f"<<< response #{record.number} [{datetime.now(UTC).isoformat()}, "
            f"actual version: {version}, elapsed {time.monotonic() - record.start:.3f}s]\n"
            f"{response.status_code} {response.reason_phrase}\n{headers}\n"
        )
        if response.is_stream_consumed:
            self.emit_body(record, response.content, response.headers.get("content-type", ""))
        else:
            assert isinstance(response.stream, httpx.AsyncByteStream)
            response.stream = Tee(response.stream, response, record, self)

    def emit_body(
        self, record: TraceRecord, body: bytes, content_type: str, truncated: bool = False
    ) -> None:
        if truncated:
            text = "<body withheld: diagnostic buffer limit exceeded>"
        else:
            text = self.output.redactor.body(
                body.decode("utf-8", errors="replace"), content_type, record.auth
            )
        self.emit(f"<<< body #{record.number}\n{text}")

    def emit_frame(self, record: TraceRecord, frame: str | None) -> None:
        record.events += 1
        if frame is None:
            text = "<SSE frame withheld: diagnostic buffer limit exceeded>"
        else:
            data = "\n".join(
                line[5:].removeprefix(" ")
                for line in frame.splitlines()
                if line.startswith("data:")
            )
            try:
                event = json.loads(data)
                if isinstance(event, dict) and event.get("type") in (
                    "response.completed",
                    "response.done",
                ):
                    response = event.get("response", {})
                    if response.get("status") == "completed":
                        record.completed = True
                        record.usage = response.get("usage")
            except (ValueError, TypeError, AttributeError):
                pass
            text = self.output.redactor.frame(frame)
        self.emit(
            f"<<< SSE #{record.number}.{record.events} "
            f"[elapsed {time.monotonic() - record.start:.3f}s]\n{text}"
        )

    def client(self, **kwargs: Any) -> httpx.AsyncClient:
        return create_async_client(
            event_hooks={"request": [self.request], "response": [self.response]}, **kwargs
        )

    def completed(self) -> bool:
        return bool(self.last_inference and self.last_inference.completed)


class Tee(httpx.AsyncByteStream):
    def __init__(
        self,
        original: httpx.AsyncByteStream,
        response: httpx.Response,
        record: TraceRecord,
        trace: Trace,
    ) -> None:
        self.original, self.record, self.trace = original, record, trace
        self.content_type = response.headers.get("content-type", "")
        self.sse = "text/event-stream" in self.content_type.lower()
        self.frames = Frames(lambda frame: trace.emit_frame(record, frame), trace.frame_limit)
        self.body = bytearray()
        self.truncated = False
        decoders = []
        for encoding in response.headers.get("content-encoding", "").split(","):
            decoder = SUPPORTED_DECODERS.get(encoding.strip().lower())
            if decoder is not None:
                decoders.append(decoder())
        self.decoder = MultiDecoder(decoders)

    def observe(self, data: bytes) -> None:
        if self.sse:
            self.frames.feed(data)
        else:
            remaining = max(0, self.trace.frame_limit - len(self.body))
            self.body.extend(data[:remaining])
            self.truncated |= len(data) > remaining

    async def __aiter__(self) -> AsyncIterator[bytes]:
        try:
            async for chunk in self.original:
                self.observe(self.decoder.decode(chunk))
                yield chunk  # exact original bytes, no second read/request
            self.observe(self.decoder.flush())
            if self.sse:
                if self.frames.finish():
                    self.trace.emit(
                        f"[harness: trace #{self.record.number}] incomplete SSE frame withheld"
                    )
            else:
                self.trace.emit_body(
                    self.record, bytes(self.body), self.content_type, self.truncated
                )
        except GeneratorExit:
            # The adapter deliberately stops at its terminal event or retries.
            raise
        except BaseException:
            self.trace.emit(
                f"[harness: trace #{self.record.number}] stream interrupted/disconnected"
            )
            raise

    async def aclose(self) -> None:
        await self.original.aclose()
