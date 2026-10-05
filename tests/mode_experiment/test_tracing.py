import gzip
import json
import os
from io import StringIO

import httpx
import pytest

from mode_experiment.tracing import Frames, Output, Redactor, Trace
from tau_ai.openai_codex import OpenAICodexConfig, OpenAICodexCredentials, OpenAICodexProvider


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks, before=None, error=None):
        self.chunks, self.before, self.error = chunks, before, error
        self.closed = False

    async def __aiter__(self):
        for index, chunk in enumerate(self.chunks):
            if self.before:
                self.before(index)
            yield chunk
        if self.error:
            raise self.error

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize("split", range(1, 35))
def test_arbitrary_splits_utf8_crlf_multiline_and_boundaries(split):
    data = (
        ": heartbeat\r\nid: 1\r\nretry: 20\r\nevent: other\r\n"
        'data: {"delta":\r\ndata: "hé😀"}\r\n\r\ndata: [DONE]\n\n'
    ).encode()
    observed = []
    frames = Frames(observed.append)
    for offset in range(0, len(data), split):
        frames.feed(data[offset : offset + split])
    assert not frames.finish()
    assert observed == [
        ': heartbeat\nid: 1\nretry: 20\nevent: other\ndata: {"delta":\ndata: "hé😀"}',
        "data: [DONE]",
    ]


def test_bounded_frames_withhold_overflow_and_incomplete():
    records = []
    frames = Frames(records.append, limit=10)
    frames.feed(b"data: " + b"a" * 1000000 + b"\n\ndata: ok\n\npartial")
    assert records == [None, "data: ok"]
    assert frames.finish() and len(frames.line) <= 10


@pytest.mark.anyio
async def test_live_http_sse_tee_precedes_finish_and_bytes_unchanged(tmp_path):
    console = StringIO()
    destination = tmp_path / "trace.log"
    output = Output(console, destination)
    trace = Trace(output)
    frames = [
        b": heartbeat\n\n",
        b'event: ignored\nid: 3\nretry: 1\ndata: {\ndata: "delta": "hi"}\n\n',
        b"data: [DONE]\n\n",
    ]

    def before(index):
        if index:
            assert f"SSE #1.{index}" in console.getvalue()

    stream = Chunks(frames, before)

    def handler(request):
        return httpx.Response(
            200,
            stream=stream,
            headers={"content-type": "text/event-stream"},
            extensions={"http_version": b"HTTP/2"},
        )

    try:
        async with (
            trace.client(transport=httpx.MockTransport(handler)) as client,
            client.stream(
                "POST",
                "https://example.test/responses",
                json={"stream": True, "prompt": "hi"},
                headers={"authorization": "Bearer VERYSECRET"},
            ) as response,
        ):
            assert [chunk async for chunk in response.aiter_raw()] == frames
        text = console.getvalue()
        assert "actual version: HTTP/2" in text and "HTTP/1-style diagnostic" in text
        assert "VERYSECRET" not in text and "Authorization" not in text  # httpx lowercases
        assert "event: ignored" in text and "id: 3" in text and "retry: 1" in text
        assert ": heartbeat" in text and "data: [DONE]" in text
        assert trace.number == 1 and stream.closed
        assert destination.read_text() == text
        if os.name == "posix":
            assert destination.stat().st_mode & 0o777 == 0o600
    finally:
        output.close()


@pytest.mark.anyio
async def test_compressed_sse_decoded_only_for_trace():
    raw = gzip.compress('data: {"delta":"hé😀"}\n\n'.encode())
    trace = Trace(Output(StringIO()))

    def handler(request):
        return httpx.Response(
            200,
            stream=Chunks([raw[:9], raw[9:]]),
            headers={"content-type": "text/event-stream", "content-encoding": "gzip"},
        )

    async with (
        trace.client(transport=httpx.MockTransport(handler)) as client,
        client.stream("GET", "https://example.test") as response,
    ):
        assert await response.aread() == 'data: {"delta":"hé😀"}\n\n'.encode()
    assert "hé😀" in trace.output.console.getvalue()


async def creds():
    return OpenAICodexCredentials("BEARER_SECRET", "ACCOUNT_SECRET")


def sse(event):
    return ("data: " + json.dumps(event) + "\n\n").encode()


@pytest.mark.anyio
@pytest.mark.parametrize("content_type", ["text/event-stream", None])
async def test_real_adapter_retries_argument_deltas_terminal_and_no_extra_requests(content_type):
    console = StringIO()
    trace = Trace(Output(console))
    events = [
        sse(
            {
                "type": "response.output_item.added",
                "item": {"type": "function_call", "id": "fc_x", "call_id": "x", "name": "python"},
            }
        ),
        sse(
            {
                "type": "response.function_call_arguments.delta",
                "item_id": "fc_x",
                "delta": '{"code":"print(42)"}',
            }
        ),
        sse(
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "id": "fc_x",
                    "call_id": "x",
                    "name": "python",
                    "arguments": '{"code":"print(42)"}',
                },
            }
        ),
        sse(
            {
                "type": "response.completed",
                "response": {
                    "status": "completed",
                    "usage": {"input_tokens": 3, "output_tokens": 4},
                },
            }
        ),
    ]
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(503, json={"error": "retry please"})
        return httpx.Response(
            200,
            stream=Chunks(events),
            headers={"content-type": content_type} if content_type else {},
        )

    async with trace.client(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAICodexProvider(
            OpenAICodexConfig(credential_resolver=creds, max_retries=1, max_retry_delay_seconds=0),
            client=client,
        )
        received = [
            event
            async for event in provider.stream_response(
                model="fake", system="test", messages=[], tools=[]
            )
        ]
    assert len(requests) == trace.number == 2
    assert received[-1].message.tool_calls[0].arguments == {"code": "print(42)"}
    assert trace.completed()
    assert '"tools"' not in requests[1].content.decode()
    assert "function_call_arguments.delta" in console.getvalue()
    assert "BEARER_SECRET" not in console.getvalue() and "ACCOUNT_SECRET" not in console.getvalue()
    assert "request #1" in console.getvalue() and "request #2" in console.getvalue()


@pytest.mark.anyio
async def test_redirects_non_sse_form_token_json_and_error_redaction():
    console = StringIO()
    trace = Trace(Output(console))

    def handler(request):
        if request.url.path == "/start":
            return httpx.Response(
                307,
                headers={
                    "location": "https://example.test/oauth/token?state=STATE_SECRET&code=QUERY_SECRET",
                    "set-cookie": "SESSION_COOKIE",
                },
            )
        return httpx.Response(
            400,
            json={
                "access_token": "ACCESS_SECRET",
                "refresh_token": "REFRESH_SECRET",
                "id_token": "ID_SECRET",
                "account_id": "ACCOUNT_SECRET",
                "error": "FORM_SECRET",
            },
        )

    async with trace.client(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as client:
        response = await client.post(
            "https://example.test/start",
            data={
                "code": "FORM_SECRET",
                "code_verifier": "VERIFIER_SECRET",
                "refresh_token": "OLD_SECRET",
            },
            headers={"cookie": "COOKIE_SECRET"},
        )
        assert response.json()["access_token"] == "ACCESS_SECRET"  # trace never mutates actual data
    assert trace.number == 2
    text = console.getvalue()
    for value in (
        "STATE_SECRET",
        "QUERY_SECRET",
        "SESSION_COOKIE",
        "ACCESS_SECRET",
        "REFRESH_SECRET",
        "ID_SECRET",
        "ACCOUNT_SECRET",
        "FORM_SECRET",
        "VERIFIER_SECRET",
        "OLD_SECRET",
        "COOKIE_SECRET",
    ):
        assert value not in text, (value, text)
    trace.output.emit(
        "error echo ACCESS_SECRET refresh_token=OTHER_SECRET https://example.test/?code=FINAL_SECRET"
    )
    assert "OTHER_SECRET" not in console.getvalue() and "FINAL_SECRET" not in console.getvalue()


@pytest.mark.anyio
@pytest.mark.parametrize("content_type", ["text/event-stream", None])
async def test_disconnect_no_terminal_completion_and_no_native_trace_substitution(content_type):
    console = StringIO()
    trace = Trace(Output(console))

    def handler(request):
        return httpx.Response(
            200,
            stream=Chunks(
                [b'data: {"type":"unknown"}\n\n', b"data: unfinished"],
                error=httpx.ReadError("disconnected"),
            ),
            headers={"content-type": content_type} if content_type else {},
        )

    async with trace.client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.ReadError):
            await client.get("https://example.test/responses")
    assert not trace.completed()
    assert "unknown" in console.getvalue() and "interrupted/disconnected" in console.getvalue()
    assert "unfinished" not in console.getvalue()


@pytest.mark.parametrize(
    "text,secrets",
    [
        ("Authorization: Bearer BARE_SECRET", ["BARE_SECRET"]),
        ("https://x/?state=FIRST_SECRET&code=SECOND_SECRET", ["FIRST_SECRET", "SECOND_SECRET"]),
        (
            '{"access_token":"JSON_SECRET","nested":{"account_id":"ACCOUNT_SECRET"}}',
            ["JSON_SECRET", "ACCOUNT_SECRET"],
        ),
        (
            "code_verifier=FORM_SECRET&refresh_token=REFRESH_SECRET",
            ["FORM_SECRET", "REFRESH_SECRET"],
        ),
        ("error https://x/?code=ONE_SECRET", ["ONE_SECRET"]),
        ("x-codex-turn-state: OPAQUE_SECRET", ["OPAQUE_SECRET"]),
    ],
)
def test_redaction_across_errors_urls_json_headers(text, secrets):
    safe = Redactor().text(text)
    assert all(secret not in safe for secret in secrets), safe


def test_sse_multiline_sensitive_and_malformed_and_auth_link_not_logged(tmp_path):
    output = Output(StringIO(), tmp_path / "trace")
    try:
        frame = (
            'event: token\ndata: {"access_token":\ndata: "MULTILINE_SECRET", "delta":"safe"}\nid: 4'
        )
        assert "MULTILINE_SECRET" not in output.redactor.frame(frame)
        assert "id: 4" in output.redactor.frame(frame)
        assert "malformed_secret" not in output.redactor.frame(
            'data: {"access_token":"malformed_secret"\n'
        )
        link = "https://auth.test/?state=AUTH_SECRET&code_challenge=CHALLENGE_SECRET"
        output.auth_link(link)
        assert link in output.console.getvalue()
        output.emit("error AUTH_SECRET CHALLENGE_SECRET")
        assert "AUTH_SECRET" not in (tmp_path / "trace").read_text()
        assert "CHALLENGE_SECRET" not in (tmp_path / "trace").read_text()
        assert (
            output.redactor.body('{"code":"AUTH_CODE"}', "application/json", auth=True).find(
                "AUTH_CODE"
            )
            == -1
        )
        assert "print(42)" in output.redactor.body('{"code":"print(42)"}', "application/json")
    finally:
        output.close()


@pytest.mark.anyio
async def test_disabled_trace_and_bounded_non_sse_dont_change_responses():
    console = StringIO()
    trace = Trace(Output(console), enabled=False, frame_limit=10)

    def handler(request):
        return httpx.Response(
            200, stream=Chunks([b"s" * 50]), headers={"content-type": "text/plain"}
        )

    async with trace.client(transport=httpx.MockTransport(handler)) as client:
        assert (await client.get("https://example.test")).content == b"s" * 50
        trace.enabled = True
        await client.get("https://example.test")
    assert "buffer limit exceeded" in console.getvalue()
    assert "request #1" not in console.getvalue()
