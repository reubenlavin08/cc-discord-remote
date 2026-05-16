"""Tail a Claude Code session JSONL for new turns after a prompt was injected.

Claude Code writes every event (user, assistant, tool_use, tool_result) to a JSONL
as it streams. Instead of scraping the terminal screen we read this file — cleaner,
gives us structured tool-use data, no UI chrome.
"""

import asyncio
import json
import time
from pathlib import Path
from typing import AsyncIterator, Tuple


def _collect_tool_ids(obj: dict, pending: set, resolved: set) -> None:
    """Update pending/resolved sets from one JSON line."""
    t = obj.get("type")
    msg = obj.get("message") or {}
    content = msg.get("content")
    if not isinstance(content, list):
        return
    if t == "assistant":
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                tid = block.get("id")
                if tid:
                    pending.add(tid)
    elif t == "user":
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                tid = block.get("tool_use_id")
                if tid:
                    resolved.add(tid)


async def wait_for_completion(
    jsonl_path: Path,
    start_offset: int,
    max_wait: float = 300.0,
    quiet_seconds: float = 3.0,
    stuck_seconds: float = 45.0,
    poll_interval: float = 0.5,
) -> list[dict]:
    """Wait until Claude's turn is complete.

    Completion = file has been quiet for `quiet_seconds` AND every tool_use in the new
    content has a matching tool_result. If quiet for `stuck_seconds` without that
    resolution, we give up — Claude may be waiting on something unrelated.
    """
    objects: list[dict] = []
    pending: set = set()
    resolved: set = set()
    parsed_to = start_offset
    last_size = start_offset
    last_change = time.time()
    deadline = time.time() + max_wait

    while time.time() < deadline:
        try:
            cur_size = jsonl_path.stat().st_size
        except OSError:
            cur_size = last_size

        if cur_size > parsed_to:
            try:
                with jsonl_path.open("rb") as f:
                    f.seek(parsed_to)
                    chunk = f.read(cur_size - parsed_to).decode("utf-8", errors="replace")
            except OSError:
                chunk = ""
            for line in chunk.split("\n"):
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                objects.append(obj)
                _collect_tool_ids(obj, pending, resolved)
            parsed_to = cur_size

        if cur_size != last_size:
            last_size = cur_size
            last_change = time.time()
        elif cur_size > start_offset:
            quiet = time.time() - last_change
            all_resolved = pending.issubset(resolved)
            if quiet >= quiet_seconds and all_resolved:
                break
            if quiet >= stuck_seconds:
                # Long quiet but still pending — bail rather than hang forever.
                break

        await asyncio.sleep(poll_interval)

    return objects


def _extract_tool_result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and "text" in block:
                parts.append(block["text"])
        return "\n".join(parts)
    return ""


def extract_user_facing(events: list[dict]) -> list[Tuple[str, dict]]:
    """Distil the JSONL into a stream of:
        ('text', {text})                        — assistant prose
        ('tool', {id, name, input})             — tool call
        ('tool_result', {id, text, is_error})   — what the tool returned
    """
    out: list[Tuple[str, dict]] = []
    for obj in events:
        t = obj.get("type")
        msg = obj.get("message") or {}
        content = msg.get("content")

        if t == "assistant":
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text" and block.get("text"):
                    out.append(("text", {"text": block["text"]}))
                elif btype == "tool_use":
                    out.append(("tool", {
                        "id": block.get("id"),
                        "name": block.get("name", "?"),
                        "input": block.get("input", {}),
                    }))

        elif t == "user":
            # Tool results come back as a user-typed message with tool_result content blocks.
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                text = _extract_tool_result_text(block.get("content"))
                if text.strip():
                    out.append(("tool_result", {
                        "id": block.get("tool_use_id"),
                        "text": text,
                        "is_error": bool(block.get("is_error")),
                    }))
    return out
