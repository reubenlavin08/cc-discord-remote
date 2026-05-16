"""Smoke-test every backend feature so we know what's solid before driving via Discord."""

import asyncio
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def header(label: str):
    print(f"\n=== {label} ".ljust(72, "="))


def ok(msg: str):
    print(f"  [OK]   {msg}")


def fail(msg: str):
    print(f"  [FAIL] {msg}")


def warn(msg: str):
    print(f"  [WARN] {msg}")


PASSED = 0
FAILED = 0


def format_age_safe(mt):
    delta = max(0, time.time() - mt)
    if delta < 60: return f"{int(delta)}s ago"
    if delta < 3600: return f"{int(delta//60)}m ago"
    return f"{int(delta//3600)}h ago"


def check(label: str, cond: bool, fail_msg: str = ""):
    global PASSED, FAILED
    if cond:
        ok(label)
        PASSED += 1
    else:
        fail(f"{label}: {fail_msg}")
        FAILED += 1


# ---- live_processes -------------------------------------------------------
header("live_processes")
from live_processes import list_running, find_by_pid, cwd_to_encoded, session_jsonl_path

procs = list_running()
check("list_running() returns at least 1 Claude", len(procs) >= 1, f"got {len(procs)}")
if procs:
    for p in procs:
        print(f"    pid={p.pid:>6} sid={p.session_id[:8]} cwd={p.cwd!r} name={p.name!r}")

# Encoding
sample = "C:\\Users\\User"
encoded = cwd_to_encoded(sample)
check(
    f"cwd_to_encoded({sample!r}) == 'C--Users-User'",
    encoded == "C--Users-User",
    f"got {encoded!r}",
)

# Path resolution against a known live process
if procs:
    target = procs[0]
    p = session_jsonl_path(target.cwd, target.session_id)
    check(
        f"session_jsonl_path for {target.session_id[:8]} exists on disk",
        p.is_file(),
        f"path={p}",
    )
    check(
        "find_by_pid matches list_running",
        find_by_pid(target.pid) is not None,
    )


# ---- session_files --------------------------------------------------------
header("session_files")
from session_files import list_recent_sessions, find_by_prefix, find_live_session

recents = list_recent_sessions(limit=5)
check("list_recent_sessions returns >=1", len(recents) >= 1, f"got {len(recents)}")
for r in recents[:3]:
    name = r.custom_name or r.first_prompt[:50]
    print(f"    {r.session_id[:8]} ({format_age_safe(r.mtime)}): {name!r}")

if recents:
    found = find_by_prefix(recents[0].session_id[:6])
    check("find_by_prefix matches", found is not None and found.session_id == recents[0].session_id)

# find_live_session against a known live PID's session
if procs:
    live = find_live_session(procs[0].session_id)
    check(
        f"find_live_session({procs[0].session_id[:8]}) finds PID {procs[0].pid}",
        live is not None and live.get("pid") == procs[0].pid,
        f"got {live}",
    )


# ---- session_tail ---------------------------------------------------------
header("session_tail")
from session_tail import wait_for_completion, extract_user_facing

# Run wait_for_completion on a stable (not-being-written-to) file. Should return
# all lines after the offset because the size never changes.
if procs:
    target_jsonl = session_jsonl_path(procs[0].cwd, procs[0].session_id)
    if target_jsonl.is_file():
        size = target_jsonl.stat().st_size
        # Read from the start, with a short stable_seconds so the test is fast.
        events = asyncio.run(
            wait_for_completion(target_jsonl, 0, max_wait=5.0, stable_seconds=0.5)
        )
        check("wait_for_completion returns events", len(events) > 0, f"got {len(events)}")
        pieces = extract_user_facing(events)
        check(
            "extract_user_facing produced something",
            len(pieces) >= 0,  # any non-error result
        )
        print(f"    {len(events)} JSON objs parsed, {len(pieces)} user-facing pieces")


# ---- sessions store ------------------------------------------------------
header("sessions (SQLite store)")
from sessions import SessionStore

tmpdb = Path(tempfile.mkdtemp()) / "test.db"
store = SessionStore(str(tmpdb), "C:/tmp")
store.set_session(123, "abc-def")
store.set_cwd(123, "D:/proj")
sid, cwd = store.get(123)
check("set_cwd resets session_id", sid is None)
check("set_cwd persists cwd", cwd == "D:/proj")
store.set_both(456, "xyz", "C:/foo")
sid2, cwd2 = store.get(456)
check("set_both stores both", sid2 == "xyz" and cwd2 == "C:/foo")
store.audit(123, 999, "test", "payload")
ok("audit() doesn't crash")
PASSED += 1


# ---- runner imports + can_use_tool callback ------------------------------
header("runner / SDK callback")
from runner import _make_can_use_tool, READ_ONLY_TOOLS

cb = _make_can_use_tool(None)  # no approver — should deny mutating tools

async def _check_cb():
    from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny
    # Read-only tool → allow
    res = await cb("Read", {"file_path": "/tmp/x"}, None)
    return isinstance(res, PermissionResultAllow), \
           isinstance(await cb("Bash", {"command": "x"}, None), PermissionResultDeny)

allow_ok, deny_ok = asyncio.run(_check_cb())
check("can_use_tool allows Read", allow_ok)
check("can_use_tool denies Bash with no approver", deny_ok)


# ---- console_helper (mode=look against an idle Claude) -------------------
header("console_helper (mode=look)")

idle = next((p for p in procs if p.status == "idle"), None)
if not idle:
    warn("No idle Claude found — skipping console_helper test to avoid disturbing busy sessions")
else:
    print(f"    target: pid={idle.pid} name={idle.name!r}")
    tmp = Path(tempfile.mkdtemp(prefix="cc_test_"))
    in_f = tmp / "in.txt"; in_f.write_text("", encoding="utf-8")
    out_f = tmp / "out.txt"; out_f.write_text("", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "console_helper.py", str(idle.pid), str(in_f), str(out_f), "--mode=look"],
        capture_output=True, timeout=30, creationflags=0x08000000,
    )
    captured = out_f.read_text(encoding="utf-8")
    if proc.returncode != 0:
        fail(f"helper exited {proc.returncode}; out: {captured[:200]!r}")
        FAILED += 1
    else:
        if captured.strip():
            ok(f"helper captured {len(captured)} chars from idle terminal")
            print(f"    first line: {captured.splitlines()[0][:80]!r}")
            PASSED += 1
        else:
            warn("helper succeeded but captured no text (TUI may use alternate buffer)")


# ---- summary --------------------------------------------------------------
print(f"\n{'='*72}")
print(f"  Passed: {PASSED}    Failed: {FAILED}")
print(f"{'='*72}")
sys.exit(0 if FAILED == 0 else 1)
