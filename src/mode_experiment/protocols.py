"""Completed-message modal grammar. No parsing or execution of streaming deltas."""

import re
from collections.abc import Mapping
from dataclasses import dataclass

from tau_agent.types import JSONValue

OPERATIONS = ("read", "write", "edit", "bash", "powershell", "python")
FIELDS = {
    "read": ("path",),
    "write": ("path", "content"),
    "edit": ("path", "old_text", "new_text"),
    "bash": ("code",),
    "powershell": ("code",),
    "python": ("code",),
}


class ProtocolError(ValueError):
    pass


@dataclass(frozen=True)
class Action:
    operation: str
    arguments: Mapping[str, JSONValue]


@dataclass(frozen=True)
class Decision:
    mode: str | None
    action: Action | None = None
    final: str | None = None
    transition: bool = False


def first_line(text: str) -> tuple[str, str | None]:
    """Remove precisely the first LF/CRLF separator, not arbitrary whitespace."""
    head, separator, tail = text.partition("\n")
    return (head.removesuffix("\r"), tail) if separator else (text, None)


def whole(text: str, command: str) -> bool:
    return text in (command, command + "\n", command + "\r\n")


def file_action(mode: str, text: str) -> Action:
    if mode in ("bash", "powershell", "python"):
        return Action(mode, {"code": text})
    if mode == "read":
        # Only one optional terminal line ending is structural for read paths.
        path = re.sub(r"(?:\r\n|\n)$", "", text, count=1)
        if "\n" in path or "\r" in path:
            raise ProtocolError("read expects one path with at most one terminal LF/CRLF")
        return Action(mode, {"path": path})
    path, remainder = first_line(text)
    if remainder is None:
        raise ProtocolError(f"{mode} requires a path followed by LF/CRLF and content")
    if mode == "write":
        return Action(mode, {"path": path, "content": remainder})
    # Sections end at an unescaped delimiter *line*. Its preceding newline belongs
    # to the section. Escape a line beginning '\\' or '@@tau ' with one '\\'.
    header, remainder = first_line(remainder)
    if header != "@@tau old" or remainder is None:
        raise ProtocolError("edit requires @@tau old, @@tau new, @@tau end section lines")
    sections: list[list[str]] = [[], []]
    index = 0
    ended = False
    chomped = False
    lines = re.findall(r".+?(?:\n|$)|\n", remainder, flags=re.S)
    for line in lines:
        body = line[:-2] if line.endswith("\r\n") else line[:-1] if line.endswith("\n") else line
        if ended:
            raise ProtocolError("nothing may follow @@tau end")
        if body == "@@tau new" and index == 0:
            index = 1
            chomped = False
        elif body == "@@tau end" and index == 1:
            ended = True
        elif chomped:
            raise ProtocolError("edit \\n line must immediately precede its section delimiter")
        elif body == "\\n":
            value = "".join(sections[index])
            if not value.endswith("\n"):
                raise ProtocolError("edit \\n requires a preceding LF/CRLF")
            sections[index] = [value[:-2] if value.endswith("\r\n") else value[:-1]]
            chomped = True
        elif body.startswith("\\"):
            if not (body[1:].startswith("\\") or body[1:].startswith("@@tau ")):
                raise ProtocolError("edit escape must prefix a backslash or @@tau line")
            sections[index].append(line[1:])
        elif body.startswith("@@tau "):
            raise ProtocolError("unexpected edit delimiter; escape literal @@tau lines with \\")
        else:
            sections[index].append(line)
    if not ended:
        raise ProtocolError("edit requires @@tau new and @@tau end")
    return Action(
        mode, {"path": path, "old_text": "".join(sections[0]), "new_text": "".join(sections[1])}
    )


def classify(text: str, mode: str | None) -> Decision:
    head, remainder = first_line(text)
    if mode is not None and head == "@@tau literal" and remainder is not None:
        return Decision(mode, action=file_action(mode, remainder))
    match = re.fullmatch(r"@@tau mode ([a-z]+)", head)
    if match:
        selected = match[1]
        if selected not in OPERATIONS:
            raise ProtocolError(f"unknown mode {selected!r}; choose {', '.join(OPERATIONS)}")
        if remainder is None or remainder == "":
            return Decision(selected, transition=True)
        return Decision(selected, action=file_action(selected, remainder), transition=True)
    if whole(text, "@@tau exit"):
        if mode is None:
            raise ProtocolError("@@tau exit is only allowed in an active mode")
        return Decision(None, transition=True)
    if head == "@@tau final" and remainder is not None:
        if mode is not None:
            raise ProtocolError("exit the active mode before @@tau final")
        if not remainder:
            raise ProtocolError("@@tau final requires answer text")
        return Decision(None, final=remainder)
    if head.startswith("@@tau"):
        raise ProtocolError(
            "malformed/reserved command; use exact syntax or active-mode @@tau literal"
        )
    if mode is None:
        raise ProtocolError(
            "control requires @@tau mode NAME plus payload, or @@tau final plus answer"
        )
    return Decision(mode, action=file_action(mode, text))


MODE_PROMPT = """Use only ordinary assistant text, never native tools or Markdown fences.
The harness starts in control; its feedback is authoritative about the current mode.
Select read/write/edit/bash/powershell/python with first line @@tau mode NAME and
submit the action in the same response after LF or CRLF. Header-only entry is
optional. Direct switching or selecting the same mode is valid and preserves state.
In an active mode ordinary entire text is one action and stays in that mode.
Executable payloads are raw multiline source, not JSON. Each language has its own
persistent process: variables, functions, imports and cwd survive all later actions.
Whole @@tau exit (optionally one final LF/CRLF) returns to control. Only in control,
@@tau final followed by LF/CRLF and answer text finishes this USER TURN, not the session.
First-line @@tau commands are reserved and case sensitive; do not embed multiple actions.
In an active mode @@tau literal plus LF/CRLF removes only that prefix and passes
all remaining text literally. Prefix twice for a payload beginning with that header.
A mode-selection remainder is already literal and is never reclassified.
read: one relative path (one optional final LF/CRLF is structural).
write: path, LF/CRLF, then exact content, including empty content.
edit: path, LF/CRLF, @@tau old line, old text, @@tau new line, new text,
@@tau end line (optionally one final LF/CRLF). Section text retains its line endings.
Escape section lines beginning backslash or @@tau with one extra backslash.
Use the special line \\n (backslash followed by lowercase n) immediately before a
section delimiter to remove exactly one preceding LF/CRLF from that section;
it allows matching/replacing text without a terminal newline. Empty new section deletes.
No whitespace trimming, fence removal, or code repair is performed. File paths
must be workspace-relative; direct file operations reject symlinks and traversal.
Recover from execution/protocol errors using the feedback within the turn limits.
"""
TOOL_PROMPT = """Operate on the experiment workspace using the six native tools.
Use serial actions; each executable language has an independent persistent process.
Variables, functions, imports and cwd survive subsequent calls and user prompts.
Paths for direct file tools must be workspace-relative. Recover from useful error
feedback. A normal final answer finishes the current user turn, not the session.
"""


def reminder(mode: str | None) -> str:
    return f"Current mode: {mode or 'control'}. " + (
        "Next: literal action, @@tau mode NAME plus payload, or whole @@tau exit."
        if mode
        else "Next: @@tau mode NAME plus payload, or @@tau final plus answer."
    )
