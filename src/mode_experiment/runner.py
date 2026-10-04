"""Small manual turn loop; Tau message objects remain authoritative for replay."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from mode_experiment.execution import Backend, Result
from mode_experiment.prompts import experiment_prompt
from mode_experiment.protocols import (
    Action,
    ProtocolError,
    classify,
    reminder,
)
from mode_experiment.tracing import Output, Trace
from tau_agent.messages import (
    AgentMessage,
    AssistantMessage,
    TextContent,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)
from tau_agent.provider import ModelProvider
from tau_agent.provider_events import AssistantDoneEvent, AssistantErrorEvent


@dataclass(frozen=True)
class Limits:
    requests: int = 24
    actions: int = 24
    recoveries: int = 4
    wall_time: float = 300


@dataclass
class Counters:
    requests: int = 0
    actions: int = 0
    failures: int = 0
    transitions: int = 0
    recoveries: int = 0


@dataclass
class Turn:
    status: str
    counters: Counters
    answer: str | None = None


class TurnError(Exception):
    def __init__(self, status: str, message: str):
        self.status = status
        super().__init__(message)


class Runner:
    def __init__(
        self,
        provider: ModelProvider,
        backend: Backend,
        output: Output,
        *,
        protocol: str,
        model: str,
        limits: Limits | None = None,
        trace: Trace | None = None,
        completed: Callable[[], bool] | None = None,
    ) -> None:
        self.provider, self.backend, self.output = provider, backend, output
        self.protocol, self.model, self.limits = protocol, model, limits or Limits()
        self.trace, self.completed = trace, completed
        self.system = experiment_prompt(protocol, backend.tools(), backend.workspace.path)
        self.messages: list[AgentMessage] = []
        self.mode: str | None = None
        self.session_id = uuid.uuid4().hex
        self.turn_number = 0
        self.total_requests = 0
        self.counters = Counters()
        self.pending: list[ToolCall] = []
        self.active_action: Action | None = None

    def feedback(self, text: str) -> None:
        if self.protocol == "modes":
            text += "\n" + reminder(self.mode)
        self.output.emit("[harness: feedback] " + text)
        self.messages.append(UserMessage(content="[harness feedback]\n" + text))

    async def request(self) -> AssistantMessage:
        self.counters.requests += 1
        self.total_requests += 1
        if self.trace is not None:
            self.trace.association = (
                f"user turn {self.turn_number}, model turn {self.total_requests}"
            )
            self.trace.last_inference = None
        tools = self.backend.tools() if self.protocol == "tools" else []
        iterator = self.provider.stream_response(
            model=self.model,
            system=self.system,
            messages=list(self.messages),
            tools=tools,
            session_id=self.session_id,
        )
        message = None
        try:
            async for event in iterator:
                if isinstance(event, AssistantErrorEvent):
                    raise TurnError("provider failure", event.error.error_message or event.reason)
                if isinstance(event, AssistantDoneEvent):
                    if event.reason == "length" or event.message.stop_reason not in (
                        "stop",
                        "toolUse",
                    ):
                        raise TurnError(
                            "truncated response",
                            "No action/state change from an incomplete response.",
                        )
                    message = event.message
        finally:
            close = getattr(iterator, "aclose", None)
            if close is not None:
                await close()
        if message is None or (self.completed is not None and not self.completed()):
            raise TurnError(
                "provider failure",
                "Response lacked successful completed transport evidence; nothing executed.",
            )
        # Preserve blocks, IDs, thinking/replay signatures and all original text.
        self.messages.append(message)
        usage = message.usage
        if self.trace is not None:
            record = self.trace.last_inference
            raw_usage = record.usage if record is not None else None
            self.output.emit(
                f"[harness: usage] {raw_usage if raw_usage is not None else 'not reported'}; "
                "cost unknown (not free)"
            )
        elif usage.total_tokens or usage.input or usage.output:
            self.output.emit(
                f"[harness: usage] input={usage.input}, output={usage.output}, "
                f"total={usage.total_tokens}; cost unknown"
            )
        return message

    async def action(self, action: Action) -> Result:
        if self.counters.actions >= self.limits.actions:
            return Result(action.operation, error="action limit exhausted; not executed")
        self.counters.actions += 1
        self.active_action = action
        self.output.emit(
            f"[harness: action #{self.counters.actions}] {action.operation} {action.arguments}"
        )
        result = await self.backend.execute(action)
        self.active_action = None
        self.counters.failures += not result.success
        self.output.emit("[harness: result] " + result.text())
        return result

    def native_result(self, call: ToolCall, result: Result) -> None:
        self.messages.append(
            ToolResultMessage(
                tool_call_id=call.id,
                tool_name=call.name,
                content=[TextContent(text=result.text())],
                is_error=not result.success,
            )
        )

    async def native(self, message: AssistantMessage) -> str | None:
        if message.text:
            self.output.emit("[assistant: completed text] " + message.text)
        if not message.tool_calls:
            return message.text
        self.pending = list(message.tool_calls)
        if len(self.pending) > 1:
            self.output.emit(
                "[harness] Provider returned multiple calls; executing serially in response order "
                "(Codex parallel_tool_calls default remains true)."
            )
        infrastructure = False
        exhausted = False
        while self.pending:
            call = self.pending[0]
            if infrastructure or self.counters.actions >= self.limits.actions:
                exhausted |= not infrastructure
                result = Result(
                    call.name, error="not executed: turn interrupted by infrastructure/action limit"
                )
            else:
                result = await self.action(Action(call.name, dict(call.arguments)))
            self.native_result(call, result)
            self.pending.pop(0)
            infrastructure |= result.infrastructure
        if infrastructure:
            raise TurnError(
                "execution-infrastructure failure",
                "Execution infrastructure failed; pending calls were rejected.",
            )
        if exhausted:
            raise TurnError("action exhaustion", "Execution-action limit reached.")
        return None

    async def modal(self, message: AssistantMessage) -> str | None:
        try:
            if message.tool_calls:
                for call in message.tool_calls:
                    self.native_result(
                        call,
                        Result(
                            call.name,
                            error="protocol violation: no native calls permitted in modes",
                        ),
                    )
                raise ProtocolError(
                    "native calls are forbidden in modes; use ordinary assistant text"
                )
            decision = classify(message.text, self.mode)
        except ProtocolError as exc:
            self.counters.failures += 1
            self.counters.recoveries += 1
            self.feedback("PROTOCOL ERROR: " + str(exc))
            if self.counters.recoveries >= self.limits.recoveries:
                raise TurnError("protocol failure", "Protocol-recovery limit exhausted.") from None
            return None
        if decision.final is not None:
            self.output.emit("[assistant: final answer] " + decision.final)
            return decision.final
        self.mode = decision.mode
        if decision.transition:
            self.counters.transitions += 1
        transition = (
            f"Transition selected {self.mode or 'control'}. " if decision.transition else ""
        )
        if decision.action is not None:
            if self.counters.actions >= self.limits.actions:
                self.feedback(transition + "Action limit reached; payload not executed.")
                raise TurnError("action exhaustion", "Execution-action limit reached.")
            result = await self.action(decision.action)
            self.feedback(
                transition + result.text()
            )  # ONE feedback, never an acknowledgement request
            if result.infrastructure:
                raise TurnError("execution-infrastructure failure", result.error)
        else:
            self.feedback(transition + "Standalone transition (one model request, no action).")
        return None

    async def turn(self, prompt: str) -> Turn:
        self.turn_number += 1
        self.counters = Counters()
        self.messages.append(UserMessage(content=prompt))
        if self.protocol == "modes":
            self.feedback("New user turn.")
        answer = None
        status = "completed answer"
        try:
            async with asyncio.timeout(self.limits.wall_time):
                while self.counters.requests < self.limits.requests:
                    message = await self.request()
                    answer = await (
                        self.native(message) if self.protocol == "tools" else self.modal(message)
                    )
                    if answer is not None:
                        break
                else:
                    raise TurnError("request exhaustion", "Model-request limit reached.")
        except asyncio.CancelledError:
            status = "cancelled"
        except TimeoutError:
            status = "wall-time exhaustion"
        except TurnError as exc:
            status = exc.status
            self.output.emit(f"[harness: {status}] {exc}")
        except Exception as exc:
            status = "provider failure"
            self.output.emit(f"[harness: provider failure] {exc}")
        if status != "completed answer":
            self.counters.failures += 1
            active = self.active_action
            for call in self.pending:
                self.native_result(
                    call,
                    Result(
                        call.name,
                        error=f"{status}; not completed/executed",
                        state_lost=active is not None and active.operation == call.name,
                    ),
                )
            self.pending = []
            if active is not None:
                self.output.emit(
                    f"[harness] {active.operation} execution interrupted; "
                    "if executable its process was destroyed and state lost."
                )
            self.active_action = None
            states = "; ".join(
                f"{name}: {process.reason or 'destroyed'}"
                for name, process in self.backend.processes.items()
                if process.dead
            )
            self.feedback(
                f"Turn interrupted: {status}. Conversation, workspace, and active mode retained; "
                "Other live interpreter state retained. "
                f"Lost/unavailable processes: {states or 'none'}. "
                "Failed/partial model output was not executed or retained as an assistant message."
            )
        c = self.counters
        self.output.emit(
            f"[harness: turn {self.turn_number}] {status}; "
            f"requests={c.requests}, actions={c.actions}, "
            f"failures={c.failures}, transitions={c.transitions}, recoveries={c.recoveries}"
        )
        return Turn(status, self.counters, answer)
