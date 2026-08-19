import os
from pathlib import Path
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


# Headless SDK queries are the high-frequency `claude` startups that were racing on
# the global ~/.claude.json and corrupting it (forcing full relogins). Isolate ONLY
# these to a dedicated config dir + long-lived OAuth token (single-use refresh tokens
# can't be shared across concurrent queries). Interactive terminal tabs the bot spawns
# deliberately do NOT get this — they must use the main config so `claude --resume`
# can find session transcripts that live under ~/.claude.
_BOT_CONFIG_DIR = r"C:\Users\User\.claude-bot"
_BOT_TOKEN_FILE = Path(_BOT_CONFIG_DIR) / "oauth-token.txt"


def _isolation_env() -> dict:
    """Env overrides for the SDK subprocess. Empty (= inherit main config) until the
    long-lived token is in place, so the bot never runs unauthenticated."""
    if _BOT_TOKEN_FILE.is_file():
        return {
            "CLAUDE_CONFIG_DIR": _BOT_CONFIG_DIR,
            "CLAUDE_CODE_OAUTH_TOKEN": _BOT_TOKEN_FILE.read_text(encoding="utf-8").strip(),
        }
    return {}


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
        env=_isolation_env(),
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
