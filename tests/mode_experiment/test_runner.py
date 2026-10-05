import asyncio
from io import StringIO

import pytest

from mode_experiment.execution import Backend
from mode_experiment.protocols import Action
from mode_experiment.runner import Limits, Runner
from mode_experiment.tracing import Output
from tau_agent.messages import (
    AssistantMessage,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolResultMessage,
    Usage,
)
from tau_agent.provider_events import AssistantDoneEvent, AssistantErrorEvent, TextDeltaEvent
from tau_ai.openai_codex import _messages_to_responses_input


class Scripted:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    async def stream_response(self, **kwargs):
        self.requests.append(kwargs)
        response = self.responses.pop(0)
        if callable(response):
            async for event in response():
                yield event
        else:
            yield AssistantDoneEvent(message=response, reason=response.stop_reason)


def text(value):
    return AssistantMessage(content=value)


@pytest.mark.anyio
async def test_combined_modes_no_ack_repeated_selection_and_cross_prompt_state(tmp_path):
    original = AssistantMessage(
        content=[
            ThinkingContent(
                thinking="not executable",
                thinking_signature='{"type":"reasoning","encrypted_content":"replay"}',
            ),
            TextContent(text="@@tau mode python\nx=40", text_signature="message-id"),
        ]
    )
    provider = Scripted(
        original,
        text("print(x+2)"),
        text("@@tau mode bash\ny=5"),
        text("@@tau mode python\nprint(x)"),
        text("@@tau mode python\nprint(x)"),
        text("@@tau exit"),
        text("@@tau final\nDone"),
        text("@@tau mode python\nprint(x+2)"),
        text("@@tau exit\r\n"),
        text("@@tau final\nStill 42"),
    )
    console = StringIO()
    backend = Backend(tmp_path)
    runner = Runner(provider, backend, Output(console), protocol="modes", model="fake")
    try:
        turn = await runner.turn("keep x")
        assert turn.answer == "Done" and turn.counters.requests == 7
        assert turn.counters.actions == 5 and turn.counters.transitions == 5
        assert runner.messages[2] is original  # initial prompt + authoritative feedback
        assert all(request["tools"] == [] for request in provider.requests)
        second_request = provider.requests[1]["messages"]
        assert second_request[-1].text.count("Transition selected python") == 1
        assert '"success": true' in second_request[-1].text
        assert "Current mode: python" in second_request[-1].text
        replay = _messages_to_responses_input(second_request)
        assert any(item.get("encrypted_content") == "replay" for item in replay)
        assert any(item.get("id") == "message-id" for item in replay)
        assert (await runner.turn("use x again")).answer == "Still 42"
        assert "42" in console.getvalue()
    finally:
        await backend.close()


@pytest.mark.anyio
async def test_recovery_current_mode_retained_on_interrupted_turn(tmp_path):
    provider = Scripted(
        text("@@tau mode python"),
        text("bad control response"),
        text("@@tau final\nwrong state"),
        text("print(3)"),
        text("@@tau exit"),
        text("@@tau final\nrecovered"),
    )
    backend = Backend(tmp_path)
    runner = Runner(
        provider,
        backend,
        Output(StringIO()),
        protocol="modes",
        model="fake",
        limits=Limits(requests=2),
    )
    try:
        turn = await runner.turn("start")
        assert turn.status == "request exhaustion" and runner.mode == "python"
        assert turn.counters.actions == 1 and turn.counters.failures >= 1
        runner.limits = Limits()
        assert (await runner.turn("recover")).answer == "recovered"
        assert runner.mode is None
        assert "PROTOCOL ERROR" in provider.requests[3]["messages"][-1].text
    finally:
        await backend.close()


@pytest.mark.anyio
async def test_native_associations_malformed_arguments_and_serial_order(tmp_path):
    calls = [
        ToolCall(id="a|fc_a", name="write", arguments={"path": "f", "content": "original"}),
        ToolCall(
            id="b|fc_b",
            name="edit",
            arguments={"path": "f", "old_text": "original", "new_text": "new"},
        ),
        ToolCall(id="c|fc_c", name="read", arguments={"path": 4}),
        ToolCall(id="d|fc_d", name="bash", arguments={"_raw_arguments": "broken"}),
    ]
    original = AssistantMessage(
        content=[TextContent(text="Doing this"), *calls], stop_reason="toolUse"
    )
    provider = Scripted(original, text("final"))
    backend = Backend(tmp_path)
    output = StringIO()
    runner = Runner(provider, backend, Output(output), protocol="tools", model="fake")
    try:
        turn = await runner.turn("do things")
        assert turn.status == "completed answer" and turn.answer == "final"
        assert turn.counters.actions == 4 and turn.counters.failures == 2
        assert (tmp_path / "f").read_text() == "new"
        messages = provider.requests[1]["messages"]
        assert messages[1] is original
        results = [m for m in messages if isinstance(m, ToolResultMessage)]
        assert [result.tool_call_id for result in results] == [call.id for call in calls]
        assert [result.is_error for result in results] == [False, False, True, True]
        assert [tool.name for tool in provider.requests[0]["tools"]] == [
            "read",
            "write",
            "edit",
            "bash",
            "powershell",
            "python",
        ]
        assert "executing serially" in output.getvalue() and "Doing this" in output.getvalue()
    finally:
        await backend.close()


@pytest.mark.anyio
async def test_native_action_limit_returns_results_for_every_call(tmp_path):
    calls = [
        ToolCall(id=str(i), name="write", arguments={"path": str(i), "content": "x"})
        for i in range(3)
    ]
    provider = Scripted(AssistantMessage(content=calls, stop_reason="toolUse"))
    backend = Backend(tmp_path)
    runner = Runner(
        provider,
        backend,
        Output(StringIO()),
        protocol="tools",
        model="fake",
        limits=Limits(actions=1),
    )
    try:
        assert (await runner.turn("do")).status == "action exhaustion"
        assert len([m for m in runner.messages if isinstance(m, ToolResultMessage)]) == 3
        assert (tmp_path / "0").exists() and not (tmp_path / "1").exists()
    finally:
        await backend.close()


@pytest.mark.anyio
async def test_unexpected_native_call_modes_is_not_executed(tmp_path):
    provider = Scripted(
        AssistantMessage(
            content=[ToolCall(id="x", name="write", arguments={"path": "f", "content": "x"})],
            stop_reason="toolUse",
        ),
        text("@@tau final\nNo tools"),
    )
    backend = Backend(tmp_path)
    runner = Runner(provider, backend, Output(StringIO()), protocol="modes", model="fake")
    try:
        assert (await runner.turn("do")).answer == "No tools"
        assert not (tmp_path / "f").exists()
        assert runner.counters.actions == 0 and runner.counters.recoveries == 1
    finally:
        await backend.close()


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["error", "length", "partial", "cancel", "transport"])
async def test_incomplete_streams_never_change_state_or_execute(tmp_path, kind):
    message = text("@@tau mode write\nf\nevil")
    began = asyncio.Event()

    async def events():
        yield TextDeltaEvent(content_index=0, delta=message.text, partial=message)
        began.set()
        if kind == "error":
            yield AssistantErrorEvent(
                reason="error", error=AssistantMessage(error_message="broken", stop_reason="error")
            )
        elif kind == "length":
            yield AssistantDoneEvent(
                reason="length", message=message.model_copy(update={"stop_reason": "length"})
            )
        elif kind == "cancel":
            await asyncio.sleep(30)
        elif kind == "transport":
            yield AssistantDoneEvent(reason="stop", message=message)

    provider = Scripted(events)
    backend = Backend(tmp_path)
    runner = Runner(
        provider,
        backend,
        Output(StringIO()),
        protocol="modes",
        model="fake",
        completed=(lambda: False) if kind == "transport" else None,
    )
    try:
        task = asyncio.create_task(runner.turn("do"))
        await began.wait()
        if kind == "cancel":
            task.cancel()
        turn = await task
        assert turn.status != "completed answer"
        assert runner.mode is None and turn.counters.actions == 0
        assert not (tmp_path / "f").exists()
        assert not any(isinstance(m, AssistantMessage) for m in runner.messages)
    finally:
        await backend.close()


@pytest.mark.anyio
async def test_bounded_protocol_recovery_and_wall_time(tmp_path):
    provider = Scripted(text("wrong"), text("still wrong"))
    backend = Backend(tmp_path)
    runner = Runner(
        provider,
        backend,
        Output(StringIO()),
        protocol="modes",
        model="fake",
        limits=Limits(recoveries=2),
    )
    try:
        assert (await runner.turn("do")).status == "protocol failure"
        assert len(provider.requests) == 2

        async def slow():
            await asyncio.sleep(1)
            yield AssistantDoneEvent(reason="stop", message=text("@@tau final\nx"))

        runner.provider = Scripted(slow)
        runner.limits = Limits(wall_time=0.01)
        assert (await runner.turn("do")).status == "wall-time exhaustion"
    finally:
        await backend.close()


@pytest.mark.anyio
async def test_statistics_accumulate_usage_and_track_current_context(tmp_path):
    class RecordingOutput(Output):
        def __init__(self):
            super().__init__(StringIO())
            self.snapshots = []

        def statistics(self, payload):
            self.snapshots.append(payload)

    # Construct messages during each request so timestamps follow user/tool feedback.
    async def call():
        yield AssistantDoneEvent(
            message=AssistantMessage(
                content=[
                    ToolCall(id="write", name="write", arguments={"path": "f", "content": "x"})
                ],
                stop_reason="toolUse",
                usage=Usage(input=20, output=20, cache_read=80, total_tokens=120),
            ),
            reason="toolUse",
        )

    async def answer():
        yield AssistantDoneEvent(
            message=AssistantMessage(
                content="Done", usage=Usage(input=20, output=10, cache_read=180, total_tokens=210)
            ),
            reason="stop",
        )

    provider = Scripted(call, answer)
    backend = Backend(tmp_path)
    output = RecordingOutput()
    runner = Runner(provider, backend, output, protocol="tools", model="fake")
    try:
        assert runner.statistics()["cache_hit_percent"] is None
        assert (await runner.turn("write f")).answer == "Done"
        stats = runner.statistics()
        assert stats["input"] == 40 and stats["output"] == 30
        assert stats["cache_read"] == 260 and stats["cache_write"] == 0
        assert stats["cache_hit_percent"] == pytest.approx(100 * 260 / 300)
        assert stats["context_tokens"] == 210  # latest response, NOT session-total usage
        assert stats["context_provider_anchored"] is True
        assert stats["reported_requests"] == stats["completed_requests"] == 2
        assert any(
            s["context_tokens"] > 120 and s["completed_requests"] == 1 for s in output.snapshots
        )  # tool feedback contributes to context between model responses
    finally:
        await backend.close()


@pytest.mark.anyio
@pytest.mark.parametrize("protocol", ["tools", "modes"])
async def test_statistics_cache_writes_and_missing_usage(tmp_path, protocol):
    answer = "Done" if protocol == "tools" else "@@tau final\nDone"
    provider = Scripted(
        AssistantMessage(
            content=answer,
            usage=Usage(input=25, output=5, cache_read=50, cache_write=25, total_tokens=105),
        ),
        text(answer),
    )
    backend = Backend(tmp_path)
    runner = Runner(provider, backend, Output(StringIO()), protocol=protocol, model="fake")
    try:
        await runner.turn("first")
        await runner.turn("second")
        stats = runner.statistics()
        assert stats["cache_hit_percent"] == 50
        assert stats["cache_write"] == 25
        assert stats["completed_requests"] == 2 and stats["reported_requests"] == 1
        assert stats["context_tokens"] > 105  # missing usage uses the previous anchor + estimates
    finally:
        await backend.close()


@pytest.mark.anyio
async def test_native_and_modal_same_persistent_backend(tmp_path):
    backend = Backend(tmp_path)
    try:
        tools = {tool.name: tool for tool in backend.tools()}
        assert '"success": true' in (await tools["python"].execute("x", {"code": "x=40"})).text
        assert (
            await backend.execute(Action("python", {"code": "print(x+2)"}))
        ).stdout.splitlines() == ["42"]
    finally:
        await backend.close()
