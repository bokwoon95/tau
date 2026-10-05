"""Tau coding guidance plus the experiment's explicitly taught interaction protocol."""

from pathlib import Path

from mode_experiment.protocols import MODE_PROMPT, TOOL_PROMPT
from tau_agent.tools import AgentTool
from tau_coding.system_prompt import (
    BuildSystemPromptOptions,
    PromptSection,
    build_system_prompt,
    format_available_tools,
    format_guidelines,
)


def experiment_prompt(protocol: str, tools: list[AgentTool], workspace: Path) -> str:
    base = (
        "You are an expert coding assistant operating inside Tau's manual protocol experiment. "
        "Help the user by reading files, executing commands, editing code, and writing files.\n\n"
        "Available operations (interfaces, not uploaded implementations):\n"
        f"{format_available_tools(tools)}\n\nGuidelines:\n{format_guidelines(tools)}\n\n"
        "LOCAL UNSANDBOXED EXECUTION: source runs on the host and can access host files "
        "and the network. Operate only on the selected workspace; do not access host secrets. "
        "Relative file paths use the workspace root, independent of interpreter cwd. "
        "Bash, PowerShell, and Python have separate persistent state and working directories. "
        "Do not launch a fresh interpreter to emulate one of these operations. "
        "Execution feedback contains only the latest action's output, not a cumulative REPL dump. "
        "There are no skills, plugins, slash commands, or other hidden operations."
    )
    return build_system_prompt(
        BuildSystemPromptOptions(
            cwd=workspace,
            tools=tools if protocol == "tools" else (),
            custom_prompt=base,
            append_sections=(
                PromptSection(
                    title="Interaction protocol (must follow exactly)",
                    body=TOOL_PROMPT if protocol == "tools" else MODE_PROMPT,
                ),
            ),
        )
    )
