"""Read Claude Code's on-disk session storage at ~/.claude/projects/.

Each session is a JSONL file where the first `type:"user"` line carries
the original cwd, session id, and prompt text. Using these we can list
existing sessions and let the bot resume them — same on-disk format as
the interactive `claude` CLI, so sessions are interchangeable.
"""

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"


@dataclass
class SessionSummary:
    session_id: str
    cwd: str
    first_prompt: str
    mtime: float  # seconds since epoch
    custom_name: Optional[str] = None  # set by /rename in Claude Code, if any

    @property
    def display(self) -> str:
        """What to show as the session's headline — its rename if any, else its first prompt."""
        if self.custom_name:
            return self.custom_name
        return self.first_prompt or "(no prompt)"


def _extract_text(content) -> str:
    """user message content can be a string or a list of content blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text" and "text" in block:
                    parts.append(block["text"])
                elif "text" in block:
                    parts.append(block["text"])
        return " ".join(parts)
    return ""


def _summarize(path: Path, mtime: float) -> Optional[SessionSummary]:
    """Walk the JSONL once, capturing the first real user prompt and the latest /rename."""
    first_prompt: Optional[str] = None
    cwd: Optional[str] = None
    custom_name: Optional[str] = None
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = obj.get("type")
                # /rename writes a custom-title line; the latest one wins.
                if t == "custom-title" and obj.get("customTitle"):
                    custom_name = obj["customTitle"]
                    continue
                if t == "user" and first_prompt is None:
                    msg = obj.get("message") or {}
                    text = _extract_text(msg.get("content"))
                    if not text.strip():
                        continue
                    # Skip injected meta messages and tool_result blobs.
                    if obj.get("isMeta") or "tool_use_id" in line:
                        continue
                    first_prompt = text.strip()[:200]
                    cwd = obj.get("cwd") or cwd
        if first_prompt is None and custom_name is None:
            return None
        return SessionSummary(
            session_id=path.stem,
            cwd=cwd or "?",
            first_prompt=first_prompt or "",
            mtime=mtime,
            custom_name=custom_name,
        )
    except OSError:
        return None


def list_recent_sessions(limit: int = 10) -> List[SessionSummary]:
    if not CLAUDE_PROJECTS.is_dir():
        return []
    candidates = []
    for jsonl in CLAUDE_PROJECTS.glob("*/*.jsonl"):
        try:
            candidates.append((jsonl.stat().st_mtime, jsonl))
        except OSError:
            continue
    candidates.sort(reverse=True)

    out: List[SessionSummary] = []
    # Over-fetch in case some files have no real user prompt.
    for mtime, path in candidates[: limit * 3]:
        s = _summarize(path, mtime)
        if s:
            out.append(s)
            if len(out) >= limit:
                break
    return out


def find_by_prefix(prefix: str) -> Optional[SessionSummary]:
    if not prefix:
        return None
    prefix_lc = prefix.lower()
    # Scan more broadly than the default limit so older sessions are still reachable.
    for s in list_recent_sessions(limit=200):
        if s.session_id.lower().startswith(prefix_lc):
            return s
    return None


def find_live_session(session_id: str) -> Optional[dict]:
    """Check ~/.claude/sessions/*.json for a running Claude Code process holding this session."""
    reg = Path.home() / ".claude" / "sessions"
    if not reg.is_dir():
        return None
    for f in reg.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("sessionId") == session_id:
            return data
    return None


def format_age(mtime: float) -> str:
    delta = max(0, time.time() - mtime)
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"
