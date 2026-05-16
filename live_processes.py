"""List currently-running Claude Code processes from the session registry."""

import ctypes
import json
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

REGISTRY = Path.home() / ".claude" / "sessions"
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


@dataclass
class LiveClaude:
    pid: int
    session_id: str
    cwd: str
    status: str
    name: Optional[str] = None
    started_at_ms: Optional[int] = None


def _pid_alive(pid: int) -> bool:
    h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if h:
        ctypes.windll.kernel32.CloseHandle(h)
        return True
    return False


def list_running() -> List[LiveClaude]:
    if not REGISTRY.is_dir():
        return []
    out: List[LiveClaude] = []
    for f in REGISTRY.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        pid = data.get("pid")
        if not pid or not _pid_alive(pid):
            continue
        out.append(
            LiveClaude(
                pid=pid,
                session_id=data.get("sessionId", "?"),
                cwd=data.get("cwd", "?"),
                status=data.get("status", "?"),
                name=data.get("name"),
                started_at_ms=data.get("startedAt"),
            )
        )
    out.sort(key=lambda c: c.started_at_ms or 0, reverse=True)
    return out


def find_by_pid(pid: int) -> Optional[LiveClaude]:
    for c in list_running():
        if c.pid == pid:
            return c
    return None


def cwd_to_encoded(cwd: str) -> str:
    """Claude Code's encoding for ~/.claude/projects/<encoded>/ — non-alnum becomes `-`."""
    return "".join(ch if ch.isalnum() else "-" for ch in cwd)


def session_jsonl_path(cwd: str, session_id: str) -> Path:
    """Resolve the on-disk JSONL Claude Code is writing for this session."""
    return Path.home() / ".claude" / "projects" / cwd_to_encoded(cwd) / f"{session_id}.jsonl"
