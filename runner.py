from typing import AsyncIterator, Awaitable, Callable, Optional, Tuple

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    query,
)

# Read-only tools are pre-approved with no Discord round-trip.
READ_ONLY_TOOLS = {"Read", "Grep", "Glob", "LS"}

# Async approver: given (tool_name, tool_input), returns True iff the user approved.
Approver = Callable[[str, dict], Awaitable[bool]]


def _make_can_use_tool(on_approval: Optional[Approver]):
    async def can_use_tool(tool_name, input_data, context):
        if tool_name in READ_ONLY_TOOLS:
            return PermissionResultAllow(updated_input=input_data)
        if on_approval is None:
            return PermissionResultDeny(
                message=f"{tool_name} is a mutating tool; no approver configured."
            )
        approved = await on_approval(tool_name, input_data)
        if approved:
            return PermissionResultAllow(updated_input=input_data)
        return PermissionResultDeny(
            message=f"User denied {tool_name} via Discord."
        )

    return can_use_tool


async def _single_prompt_stream(text: str):
    """can_use_tool requires streaming mode, so the prompt must be an async iterable."""
    yield {"type": "user", "message": {"role": "user", "content": text}}


async def run_turn(
    prompt: str,
    cwd: str,
    resume_id: Optional[str] = None,
    on_approval: Optional[Approver] = None,
) -> AsyncIterator[Tuple]:
    """Drive one Claude turn.

    Yields:
        ("text", str)
        ("tool", str, dict)            — tool was attempted (allow/deny is handled in callback)
        ("done", session_id, cost_usd)
    """
    options = ClaudeAgentOptions(
        cwd=cwd,
        resume=resume_id,
        can_use_tool=_make_can_use_tool(on_approval),
    )

    session_id: Optional[str] = None
    cost: Optional[float] = None

    async for message in query(prompt=_single_prompt_stream(prompt), options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    yield ("text", block.text)
                elif isinstance(block, ToolUseBlock):
                    yield ("tool", block.name, block.input)
        elif isinstance(message, ResultMessage):
            session_id = getattr(message, "session_id", None)
            cost = getattr(message, "total_cost_usd", None)

    yield ("done", session_id, cost)
