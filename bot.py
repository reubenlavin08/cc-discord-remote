import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Dict, Optional

# Force stdout to UTF-8 so emoji / arrows in print() don't blow up on Windows cp1252.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Ping the user with @mention if a turn takes longer than this. Quick replies stay quiet.
PING_AFTER_SECONDS = 15

import discord
from discord import app_commands
from dotenv import load_dotenv

import asyncio as _asyncio
import sys
import tempfile

from approvals import request_approval
from live_processes import find_by_pid, list_running, session_jsonl_path
from runner import READ_ONLY_TOOLS, run_turn
from session_files import find_by_prefix, find_live_session, format_age, list_recent_sessions
from session_tail import extract_user_facing, wait_for_completion
from sessions import SessionStore

load_dotenv()

TOKEN = os.environ.get("DISCORD_TOKEN", "")
ALLOWED_USERS = {int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").split(",") if x.strip()}
ALLOWED_CHANNELS = {int(x) for x in os.environ.get("ALLOWED_CHANNEL_IDS", "").split(",") if x.strip()}
# Snapshot of the env-configured channels — never auto-deleted, even if unattached.
CONTROL_CHANNELS = set(ALLOWED_CHANNELS)
DEFAULT_CWD = os.environ.get("DEFAULT_CWD") or str(Path.cwd())

PREFIX = "!cc"
MAX_CHUNK = 1900

HELP_TEXT = (
    f"**Claude Code remote**\n\n"
    f"**Multi-channel** — one Discord channel per terminal:\n"
    f"`{PREFIX} spawn <name>` — make a new channel auto-attached to that terminal\n"
    f"`{PREFIX} close` — detach + delete this channel (only in spawned ones)\n\n"
    f"**Per-channel attach** (manual):\n"
    f"`{PREFIX} live` — list running Claude Code processes\n"
    f"`{PREFIX} attach <name>` — drive that terminal from this channel\n"
    f"`{PREFIX} detach` — stop driving the terminal\n"
    f"`{PREFIX} look` — snapshot the terminal screen\n\n"
    f"**In an attached channel**, you can just type messages without `{PREFIX}` — they go straight to the terminal.\n\n"
    f"**SDK mode** (separate Claude process):\n"
    f"`{PREFIX} <prompt>` — drive Claude in a non-attached channel\n"
    f"`{PREFIX} new` · `{PREFIX} cancel` · `{PREFIX} cd <path>` · `{PREFIX} sessions` · `{PREFIX} resume <id>` · `{PREFIX} status`"
)

intents = discord.Intents.default()
intents.message_content = True
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

sessions = SessionStore("sessions.db", DEFAULT_CWD)
active_turns: Dict[int, asyncio.Task] = {}  # channel_id → currently-running SDK turn
attached_pids: Dict[int, int] = {}  # channel_id → live claude.exe PID (in-memory only)
mirror_tasks: Dict[int, asyncio.Task] = {}  # channel_id → bg JSONL-tail task
CONSOLE_HELPER = str(Path(__file__).parent / "console_helper.py")


# ---------- helpers ---------------------------------------------------------

async def send_chunked(channel, text: str) -> None:
    text = text or ""
    while len(text) > MAX_CHUNK:
        cut = text.rfind("\n", 0, MAX_CHUNK)
        if cut < 500:
            cut = MAX_CHUNK
        await channel.send(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        await channel.send(text)


def _is_authorised(user_id: int, channel_id: int) -> bool:
    if user_id not in ALLOWED_USERS:
        return False
    if ALLOWED_CHANNELS and channel_id not in ALLOWED_CHANNELS:
        return False
    return True


def _format_tool_input(tool_name: str, tool_input: dict) -> str:
    """One-line preview of what a tool is doing."""
    if tool_name == "Bash":
        return f"`{(tool_input.get('command') or '')[:120]}`"
    if tool_name in ("Read", "Write", "Edit"):
        return f"`{tool_input.get('file_path', '?')}`"
    if tool_name == "Grep":
        pat = tool_input.get("pattern", "?")
        scope = tool_input.get("glob") or tool_input.get("path") or "."
        return f"`{pat}` in `{scope}`"
    if tool_name == "Glob":
        return f"`{tool_input.get('pattern', '?')}`"
    if tool_name == "LS":
        return f"`{tool_input.get('path', '.')}`"
    # Fallback — show first useful arg.
    for k in ("name", "url", "query", "command"):
        if k in tool_input:
            return f"`{k}: {str(tool_input[k])[:100]}`"
    return ""


# ---------- command handlers ------------------------------------------------

async def cmd_help(channel, _user_id):
    await channel.send(HELP_TEXT)


async def cmd_status(channel, channel_id):
    sid, cwd = sessions.get(channel_id)
    live = find_live_session(sid) if sid else None
    live_str = f" · ⚠️ in use by PID {live['pid']}" if live else ""
    await channel.send(f"cwd: `{cwd}`\nsession: `{sid or '(new)'}`{live_str}")


async def cmd_new(channel, channel_id, user_id):
    sessions.reset(channel_id)
    sessions.audit(channel_id, user_id, "session_reset")
    await channel.send("Session reset.")


async def cmd_cd(channel, channel_id, user_id, path: str):
    if not path:
        await channel.send("Usage: `cd <path>`")
        return
    if not Path(path).is_dir():
        await channel.send(f"Not a directory: `{path}`")
        return
    sessions.set_cwd(channel_id, path)
    sessions.audit(channel_id, user_id, "cwd_change", path)
    await channel.send(f"cwd → `{path}` (session reset)")


async def cmd_sessions(channel, count: int = 10):
    count = max(1, min(count, 25))
    summaries = list_recent_sessions(limit=count)
    if not summaries:
        await channel.send("No Claude Code sessions found on disk.")
        return
    lines = [f"**Recent Claude Code sessions** (use `{PREFIX} resume <id>`):"]
    for s in summaries:
        short = s.session_id[:8]
        headline = s.custom_name if s.custom_name else s.first_prompt
        headline = (headline or "(empty)").replace("\n", " ")[:90]
        tag = "📌 " if s.custom_name else ""
        live = find_live_session(s.session_id)
        live_tag = " 🔴" if live else ""
        lines.append(
            f"`{short}` · `{s.cwd}` · {format_age(s.mtime)}{live_tag}\n> {tag}{headline}"
        )
    await send_chunked(channel, "\n".join(lines))


async def cmd_resume(channel, channel_id, user_id, prefix: str):
    if not prefix:
        await channel.send(f"Usage: `resume <id>`. Run `{PREFIX} sessions` to list options.")
        return
    s = find_by_prefix(prefix)
    if not s:
        await channel.send(
            f"No session matches `{prefix}`. Run `{PREFIX} sessions` to see options."
        )
        return
    live = find_live_session(s.session_id)
    if live:
        await channel.send(
            f"⚠️ **Session is live in a terminal** (PID {live['pid']}, status `{live.get('status','?')}`).\n"
            f"Close that Claude Code window before continuing, or Discord and the terminal will both write to "
            f"the same file and corrupt it. Attaching anyway — proceed with care."
        )
    sessions.set_both(channel_id, s.session_id, s.cwd)
    sessions.audit(channel_id, user_id, "resume", s.session_id)
    headline = s.custom_name if s.custom_name else s.first_prompt
    tag = "📌 " if s.custom_name else ""
    await channel.send(
        f"Attached to `{s.session_id[:8]}` in `{s.cwd}`.\n> {tag}{(headline or '')[:160]}"
    )


async def cmd_cancel(channel, channel_id):
    task = active_turns.get(channel_id)
    if task is None or task.done():
        await channel.send("Nothing running to cancel.")
        return
    task.cancel()
    await channel.send("🛑 Cancelling current turn…")


# ---------- live-attach (Windows console) -----------------------------------

async def cmd_live(channel):
    procs = list_running()
    if not procs:
        await channel.send("No running Claude Code processes detected.")
        return
    lines = ["**Running Claude Code processes** (use `!cc attach <pid>`):"]
    for c in procs:
        label = c.name or c.session_id[:8]
        lines.append(
            f"`pid {c.pid}` · `{c.cwd}` · `{c.status}`\n> 📌 {label}"
        )
    await send_chunked(channel, "\n".join(lines))


async def cmd_attach(channel, channel_id, user_id, query: str):
    if not query:
        await channel.send("Usage: `!cc attach <name>` (e.g. `!cc attach compressedprompt`) or `!cc attach <pid>`")
        return
    procs = list_running()
    if not procs:
        await channel.send("No running Claude Code processes.")
        return

    match = None
    if query.isdigit():
        pid = int(query)
        match = next((c for c in procs if c.pid == pid), None)
    if match is None:
        q_lc = query.lower()
        name_hits = [c for c in procs if c.name and q_lc in c.name.lower()]
        id_hits = [c for c in procs if c.session_id.lower().startswith(q_lc)]
        hits = name_hits or id_hits
        if len(hits) > 1:
            labels = ", ".join(f"`{h.name or h.session_id[:8]}`" for h in hits)
            await channel.send(f"`{query}` is ambiguous: matches {labels}. Use the full name or PID.")
            return
        match = hits[0] if hits else None

    if match is None:
        await channel.send(f"No running Claude matches `{query}`. Try `!cc live`.")
        return

    # Refuse if another channel already drives this PID — two channels writing into the
    # same terminal would race, double-mirror, and confuse the user.
    other = next(
        (cid for cid, p in attached_pids.items() if p == match.pid and cid != channel_id),
        None,
    )
    if other is not None:
        await channel.send(
            f"⚠️ PID {match.pid} is already attached in <#{other}>. "
            f"Use `!cc detach` there first (or just talk to it from that channel)."
        )
        return

    # Cancel any existing mirror for this channel.
    old = mirror_tasks.pop(channel_id, None)
    if old and not old.done():
        old.cancel()

    attached_pids[channel_id] = match.pid
    sessions.set_attached_pid(channel_id, match.pid)
    label = match.name or match.session_id[:8]

    # Start the bidirectional mirror: anything Claude writes to this session's JSONL —
    # whether triggered from Discord OR typed locally in the terminal — gets posted here.
    jsonl = session_jsonl_path(match.cwd, match.session_id)
    if jsonl.is_file():
        start_offset = jsonl.stat().st_size
        mirror_tasks[channel_id] = asyncio.create_task(
            _mirror_loop(channel, channel_id, user_id, jsonl, start_offset, label)
        )
        mirror_note = f"\n📡 Live mirror started — anything Claude does will appear here."
    else:
        mirror_note = f"\n⚠️ Couldn't find session JSONL at `{jsonl}` — mirror disabled."

    await channel.send(
        f"🔌 Attached to `{label}` (PID {match.pid}). Prompts now go into that terminal."
        f"{mirror_note}\n"
        f"`!cc detach` to disconnect · `!cc look` to snapshot the screen."
    )


async def cmd_detach(channel, channel_id):
    pid = attached_pids.pop(channel_id, None)
    mt = mirror_tasks.pop(channel_id, None)
    if mt and not mt.done():
        mt.cancel()
    sessions.set_attached_pid(channel_id, None)
    if pid is None:
        await channel.send("Not attached to anything.")
        return
    await channel.send(f"🔌 Detached from PID {pid}. Mirror stopped.")


def _sanitize_channel_name(raw: str) -> str:
    out = "".join(c if c.isalnum() or c in "-_" else "-" for c in (raw or "").lower())
    out = out.strip("-")[:90]
    return out or "claude-attached"


async def cmd_spawn(channel, user_id: int, query: str):
    """Create a new Discord text channel auto-attached to the matched terminal."""
    if not query:
        await channel.send(f"Usage: `{PREFIX} spawn <name>` (matches a running Claude by name or PID)")
        return
    if not channel.guild:
        await channel.send("Can only spawn channels inside a server.")
        return

    procs = list_running()
    match = None
    if query.isdigit():
        match = next((c for c in procs if c.pid == int(query)), None)
    if match is None:
        q_lc = query.lower()
        name_hits = [c for c in procs if c.name and q_lc in c.name.lower()]
        id_hits = [c for c in procs if c.session_id.lower().startswith(q_lc)]
        hits = name_hits or id_hits
        if len(hits) > 1:
            labels = ", ".join(f"`{h.name or h.session_id[:8]}`" for h in hits)
            await channel.send(f"`{query}` is ambiguous: {labels}.")
            return
        match = hits[0] if hits else None
    if match is None:
        await channel.send(f"No running Claude matches `{query}`.")
        return

    channel_name = _sanitize_channel_name(match.name or match.session_id[:8])
    try:
        new_chan = await channel.guild.create_text_channel(name=channel_name)
    except discord.Forbidden:
        await channel.send(
            "⚠️ Bot needs **Manage Channels** permission. Re-authorize with:\n"
            "`https://discord.com/oauth2/authorize?client_id=1505073874885152768&permissions=83984&scope=bot`"
        )
        return
    except discord.HTTPException as e:
        await channel.send(f"⚠️ Couldn't create channel: {e}")
        return

    # Add to in-memory allowlist so messages there are accepted.
    ALLOWED_CHANNELS.add(new_chan.id)

    # Attach the new channel to the terminal.
    await cmd_attach(new_chan, new_chan.id, user_id, str(match.pid))
    await channel.send(f"📡 Spawned <#{new_chan.id}> attached to `{channel_name}` (PID {match.pid}).")


async def cmd_close(channel, channel_id, user_id):
    """Detach (if attached) and delete this channel. Works on orphaned channels too."""
    pid = attached_pids.pop(channel_id, None)
    mt = mirror_tasks.pop(channel_id, None)
    if mt and not mt.done():
        mt.cancel()
    sessions.set_attached_pid(channel_id, None)
    ALLOWED_CHANNELS.discard(channel_id)
    try:
        msg = f"🔌 Closing (was on PID {pid})…" if pid else "🔌 Closing channel…"
        await channel.send(msg)
        await channel.delete()
    except discord.Forbidden:
        await channel.send("⚠️ Bot needs **Manage Channels** to delete this channel.")
    except discord.HTTPException as e:
        await channel.send(f"⚠️ Couldn't delete channel: {e}")


async def _run_console_helper(pid: int, prompt: str, mode: str) -> str:
    """Run console_helper.py as a subprocess. Returns captured screen text."""
    tmp_dir = Path(tempfile.mkdtemp(prefix="cc_remote_"))
    prompt_file = tmp_dir / "in.txt"
    output_file = tmp_dir / "out.txt"
    prompt_file.write_text(prompt, encoding="utf-8")
    output_file.write_text("", encoding="utf-8")
    try:
        proc = await _asyncio.create_subprocess_exec(
            sys.executable,
            CONSOLE_HELPER,
            str(pid),
            str(prompt_file),
            str(output_file),
            f"--mode={mode}",
            stdout=_asyncio.subprocess.PIPE,
            stderr=_asyncio.subprocess.PIPE,
            creationflags=0x08000000,  # CREATE_NO_WINDOW — avoid flashing a console
        )
        _, stderr = await proc.wait(), b""
        text = output_file.read_text(encoding="utf-8", errors="replace")
        return text
    finally:
        try:
            prompt_file.unlink(missing_ok=True)
            output_file.unlink(missing_ok=True)
            tmp_dir.rmdir()
        except Exception:
            pass


async def cmd_look(channel, channel_id):
    pid = attached_pids.get(channel_id)
    if pid is None:
        await channel.send("Not attached. Use `!cc attach <pid>` first.")
        return
    async with channel.typing():
        screen = await _run_console_helper(pid, "", mode="look")
    if not screen.strip():
        await channel.send("_(screen empty)_")
        return
    await send_chunked(channel, f"```\n{screen[-3500:]}\n```")


async def _render_piece(channel, kind: str, data: dict, pending_tools: Dict[str, tuple]):
    """Render one parsed JSONL piece into Discord, pairing tools with their results."""
    if kind == "text":
        # Flush orphan tools first, then send the text.
        for _id, (name, inp) in list(pending_tools.items()):
            preview = _format_tool_input(name, inp)
            icon = "🔍" if name in READ_ONLY_TOOLS else "🛠️"
            await channel.send(f"{icon} `{name}` — {preview}")
        pending_tools.clear()
        await send_chunked(channel, data["text"])
    elif kind == "tool":
        tid = data.get("id")
        if tid:
            pending_tools[tid] = (data["name"], data["input"])
        else:
            preview = _format_tool_input(data["name"], data["input"])
            icon = "🔍" if data["name"] in READ_ONLY_TOOLS else "🛠️"
            await channel.send(f"{icon} `{data['name']}` — {preview}")
    elif kind == "tool_result":
        tid = data.get("id")
        text = data["text"]
        err = data.get("is_error")
        shown = text[:400]
        ellip = "…" if len(text) > 400 else ""
        err_tag = " ❌" if err else ""
        if tid and tid in pending_tools:
            name, inp = pending_tools.pop(tid)
            preview = _format_tool_input(name, inp)
            icon = "🔍" if name in READ_ONLY_TOOLS else "🛠️"
            line = f"{icon} `{name}` — {preview}{err_tag}\n```\n{shown}{ellip}\n```"
        else:
            line = f"↳{err_tag}\n```\n{shown}{ellip}\n```"
        await send_chunked(channel, line)


async def _mirror_loop(channel, channel_id: int, user_id: int, jsonl_path: Path,
                       start_offset: int, label: str):
    """Tail the JSONL forever, posting Claude's activity to Discord.

    Picks up everything Claude writes — whether the prompt came from Discord (via the
    typing path) or from the user typing directly in the actual terminal.
    """
    pending_tools: Dict[str, tuple] = {}
    pending_tool_ids: set = set()
    resolved_tool_ids: set = set()
    parsed_to = start_offset
    last_size = start_offset
    last_change = time.time()
    last_assistant_at = 0.0
    pinged_for_turn = True  # don't ping for already-resolved state at attach time

    try:
        while True:
            try:
                cur_size = jsonl_path.stat().st_size
            except OSError:
                await asyncio.sleep(0.5)
                continue

            if cur_size > parsed_to:
                try:
                    with jsonl_path.open("rb") as f:
                        f.seek(parsed_to)
                        chunk = f.read(cur_size - parsed_to).decode("utf-8", errors="replace")
                except OSError:
                    chunk = ""
                new_objs: list[dict] = []
                for line in chunk.split("\n"):
                    if not line.strip():
                        continue
                    try:
                        new_objs.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
                parsed_to = cur_size

                # Track tool-use / tool-result IDs so we can detect turn completion.
                for obj in new_objs:
                    t = obj.get("type")
                    msg = obj.get("message") or {}
                    content = msg.get("content")
                    if not isinstance(content, list):
                        continue
                    if t == "assistant":
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "tool_use":
                                tid = block.get("id")
                                if tid:
                                    pending_tool_ids.add(tid)
                    elif t == "user":
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "tool_result":
                                tid = block.get("tool_use_id")
                                if tid:
                                    resolved_tool_ids.add(tid)

                pieces = extract_user_facing(new_objs)
                for kind, data in pieces:
                    await _render_piece(channel, kind, data, pending_tools)
                    if kind == "text":
                        last_assistant_at = time.time()
                        pinged_for_turn = False

            if cur_size != last_size:
                last_size = cur_size
                last_change = time.time()
            else:
                quiet = time.time() - last_change
                all_resolved = pending_tool_ids.issubset(resolved_tool_ids)
                if (
                    quiet >= 3.0
                    and all_resolved
                    and last_assistant_at > 0
                    and not pinged_for_turn
                ):
                    # Turn appears complete — flush orphans, ping user.
                    for _id, (name, inp) in list(pending_tools.items()):
                        preview = _format_tool_input(name, inp)
                        icon = "🔍" if name in READ_ONLY_TOOLS else "🛠️"
                        await channel.send(f"{icon} `{name}` — {preview}")
                    pending_tools.clear()
                    elapsed = time.time() - last_assistant_at
                    await channel.send(f"<@{user_id}> ✅ _turn complete on `{label}`_")
                    pinged_for_turn = True

            await asyncio.sleep(0.5)
    except asyncio.CancelledError:
        return
    except Exception as e:
        try:
            await channel.send(f"⚠️ mirror loop error: `{type(e).__name__}: {e}`")
        except Exception:
            pass


async def cmd_terminal_send(channel, channel_id, user_id, text: str):
    """Type text into the attached terminal. The mirror loop posts Claude's response."""
    pid = attached_pids.get(channel_id)
    if pid is None:
        return False
    info = find_by_pid(pid)
    if not info:
        await channel.send(f"PID {pid} is no longer running. Use `!cc live` to re-attach.")
        attached_pids.pop(channel_id, None)
        mt = mirror_tasks.pop(channel_id, None)
        if mt and not mt.done():
            mt.cancel()
        return True

    async with channel.typing():
        ack = await _run_console_helper(pid, text, mode="type")
    if "AttachConsole" in ack and "failed" in ack:
        await channel.send(f"⚠️ {ack.strip()}")
    return True


async def cmd_ask(channel, channel_id, user_id, prompt: str):
    if not prompt:
        await channel.send(f"Usage: `{PREFIX} <prompt>` (try `{PREFIX} help`)")
        return

    if channel_id in active_turns and not active_turns[channel_id].done():
        await channel.send("A turn is already running in this channel — `cancel` it first.")
        return

    session_id, cwd = sessions.get(channel_id)
    sessions.audit(
        channel_id, user_id, "prompt",
        json.dumps({"len": len(prompt), "preview": prompt[:200]}),
    )

    async def approver(tool_name: str, tool_input: dict) -> bool:
        approved = await request_approval(channel, user_id, tool_name, tool_input)
        sessions.audit(
            channel_id, user_id,
            "approve" if approved else "deny",
            json.dumps({"tool": tool_name}),
        )
        return approved

    turn_started_at = time.time()

    async def runner_coro():
        async with channel.typing():
            async for event in run_turn(
                prompt=prompt, cwd=cwd, resume_id=session_id, on_approval=approver
            ):
                kind = event[0]
                if kind == "text":
                    await send_chunked(channel, event[1])
                elif kind == "tool":
                    _, name, tool_input = event
                    preview = _format_tool_input(name, tool_input)
                    icon = "🔍" if name in READ_ONLY_TOOLS else "🛠️"
                    line = f"{icon} `{name}`"
                    if preview:
                        line += f" — {preview}"
                    await channel.send(line)
                elif kind == "done":
                    _, new_session_id, cost = event
                    if new_session_id:
                        sessions.set_session(channel_id, new_session_id)
                    cost_str = f" · ${cost:.4f}" if cost else ""
                    elapsed = time.time() - turn_started_at
                    ping = f"<@{user_id}> " if elapsed > PING_AFTER_SECONDS else ""
                    await channel.send(f"{ping}_done{cost_str} · {elapsed:.0f}s_")
                    sessions.audit(
                        channel_id, user_id, "turn_done",
                        json.dumps({"session_id": new_session_id, "cost_usd": cost}),
                    )

    task = asyncio.create_task(runner_coro())
    active_turns[channel_id] = task
    try:
        await task
    except asyncio.CancelledError:
        await channel.send("🛑 Turn cancelled.")
        sessions.audit(channel_id, user_id, "cancelled")
    except Exception as e:
        await channel.send(f"⚠️ `{type(e).__name__}: {e}`")
        sessions.audit(channel_id, user_id, "error", str(e))
        raise
    finally:
        active_turns.pop(channel_id, None)


# ---------- dispatch --------------------------------------------------------

async def dispatch(channel, channel_id: int, user_id: int, text: str):
    """Route a raw '!cc <text>' style command (sans prefix) to the right handler."""
    rest = text.strip()
    if rest in ("", "help"):
        await cmd_help(channel, user_id)
        return
    if rest == "status":
        await cmd_status(channel, channel_id)
        return
    if rest == "new":
        await cmd_new(channel, channel_id, user_id)
        return
    if rest == "cancel":
        await cmd_cancel(channel, channel_id)
        return
    if rest == "live":
        await cmd_live(channel)
        return
    if rest.startswith("attach "):
        await cmd_attach(channel, channel_id, user_id, rest[7:].strip())
        return
    if rest == "detach":
        await cmd_detach(channel, channel_id)
        return
    if rest == "look":
        await cmd_look(channel, channel_id)
        return
    if rest.startswith("spawn "):
        await cmd_spawn(channel, user_id, rest[6:].strip())
        return
    if rest == "close":
        await cmd_close(channel, channel_id, user_id)
        return
    if rest.startswith("cd "):
        await cmd_cd(channel, channel_id, user_id, rest[3:].strip().strip('"').strip("'"))
        return
    if rest.startswith("sessions"):
        parts = rest.split()
        try:
            n = int(parts[1]) if len(parts) > 1 else 10
        except ValueError:
            n = 10
        await cmd_sessions(channel, n)
        return
    if rest.startswith("resume "):
        await cmd_resume(channel, channel_id, user_id, rest[7:].strip())
        return
    # Otherwise treat as a prompt.
    # If this channel is attached to a live terminal, send into it instead of the SDK.
    if channel_id in attached_pids:
        await cmd_terminal_send(channel, channel_id, user_id, rest)
        return
    await cmd_ask(channel, channel_id, user_id, rest)


# ---------- Discord wiring --------------------------------------------------

@bot.event
async def on_ready():
    print(f"Bot online as {bot.user}")
    print(f"  allowed users:    {sorted(ALLOWED_USERS) or '(none — bot will reject everyone)'}")
    print(f"  allowed channels: {sorted(ALLOWED_CHANNELS) or '(any)'}")
    print(f"  default cwd:      {DEFAULT_CWD}")

    # Every channel we've ever tracked stays in the allowlist — even orphaned ones —
    # so the user can always run `!cc close` to clean them up.
    for row in sessions.conn.execute("SELECT channel_id FROM sessions"):
        ALLOWED_CHANNELS.add(row[0])

    # Restore persisted per-channel attachments (channels spawned in earlier sessions).
    # Enforce one-channel-per-PID: keep the first occurrence, clear the rest.
    primary_user = next(iter(ALLOWED_USERS), 0)
    seen_pids: set = set()
    for ch_id, pid in sessions.all_attached():
        chan = bot.get_channel(ch_id)
        if not chan:
            sessions.set_attached_pid(ch_id, None)
            continue
        if pid in seen_pids:
            sessions.set_attached_pid(ch_id, None)
            try:
                await chan.send(
                    f"⚠️ PID {pid} was attached here AND elsewhere — clearing this one on restart "
                    f"(one channel per terminal). Run `!cc close` to delete this channel, "
                    f"or `!cc attach <name>` to grab a different terminal."
                )
            except Exception:
                pass
            continue
        info = find_by_pid(pid)
        if not info:
            sessions.set_attached_pid(ch_id, None)
            try:
                await chan.send(f"⚠️ PID {pid} no longer running — attachment cleared on restart.")
            except Exception:
                pass
            continue
        attached_pids[ch_id] = pid
        seen_pids.add(pid)
        ALLOWED_CHANNELS.add(ch_id)
        jsonl = session_jsonl_path(info.cwd, info.session_id)
        if jsonl.is_file():
            label = info.name or info.session_id[:8]
            mirror_tasks[ch_id] = asyncio.create_task(
                _mirror_loop(chan, ch_id, primary_user, jsonl, jsonl.stat().st_size, label)
            )
            print(f"  restored attachment: channel {ch_id} -> PID {pid} ({label})")

    # Auto-delete orphaned channels: previously-tracked channels with no active attachment
    # and no role as an env-configured control channel.
    orphan_ids = [
        r[0] for r in sessions.conn.execute(
            "SELECT channel_id FROM sessions WHERE attached_pid IS NULL"
        )
        if r[0] not in CONTROL_CHANNELS
    ]
    for ch_id in orphan_ids:
        chan = bot.get_channel(ch_id)
        if chan is None:
            # Discord channel already gone — just clean the DB row.
            sessions.conn.execute("DELETE FROM sessions WHERE channel_id = ?", (ch_id,))
            continue
        try:
            await chan.delete(reason="cc-discord-remote: orphaned, no attached terminal")
            sessions.conn.execute("DELETE FROM sessions WHERE channel_id = ?", (ch_id,))
            ALLOWED_CHANNELS.discard(ch_id)
            print(f"  auto-deleted orphan channel {ch_id}")
        except discord.Forbidden:
            print(f"  can't auto-delete channel {ch_id} (missing Manage Channels)")
        except Exception as e:
            print(f"  couldn't auto-delete channel {ch_id}: {e}")
    sessions.conn.commit()

    # Sync slash commands to each guild the allowed channels live in (instant per-guild).
    synced_guilds = set()
    for chan_id in ALLOWED_CHANNELS:
        chan = bot.get_channel(chan_id)
        if chan and getattr(chan, "guild", None) and chan.guild.id not in synced_guilds:
            try:
                tree.copy_global_to(guild=chan.guild)
                cmds = await tree.sync(guild=chan.guild)
                print(f"  synced {len(cmds)} slash commands to guild {chan.guild.name}")
                synced_guilds.add(chan.guild.id)
            except Exception as e:
                print(f"  slash sync failed for {chan.guild.id}: {e}")


async def _handle_attachments(message: discord.Message, channel_id: int, user_id: int) -> bool:
    """When a Discord message has attachments, save them to the attached terminal's cwd
    and forward a combined prompt (any text + long-paste content + a note about saved files).
    Returns True if attachments were present (handled or attempted)."""
    if not message.attachments:
        return False
    pid = attached_pids.get(channel_id)
    if not pid:
        await message.channel.send("📎 File handling only works in attached channels (`!cc attach <name>` first).")
        return True
    info = find_by_pid(pid)
    if not info:
        await message.channel.send(f"PID {pid} no longer running.")
        return True

    long_paste_text = None
    saved_files: list = []

    for att in message.attachments:
        # Discord auto-converts >2000-char pastes into a "message.txt" attachment.
        # Treat that as inline text, not a file save.
        if att.filename == "message.txt":
            try:
                long_paste_text = (await att.read()).decode("utf-8", errors="replace")
            except Exception as e:
                await message.channel.send(f"⚠️ Couldn't read long paste: {e}")
            continue
        target = Path(info.cwd) / att.filename
        try:
            await att.save(target)
            saved_files.append(att.filename)
        except Exception as e:
            await message.channel.send(f"⚠️ Failed to save {att.filename}: {e}")

    parts: list = []
    if message.content and message.content.strip() and not message.content.startswith(PREFIX):
        parts.append(message.content.strip())
    if long_paste_text:
        parts.append(long_paste_text)
    if saved_files:
        file_list = ", ".join(saved_files)
        await message.channel.send(f"📎 Saved to `{info.cwd}`: {file_list}")
        parts.append(f"(I just uploaded these files to your working directory: {file_list})")

    combined = "\n\n".join(parts)
    if combined:
        await cmd_terminal_send(message.channel, channel_id, user_id, combined)
    return True


async def _safe_react(message: discord.Message, emoji: str):
    try:
        await message.add_reaction(emoji)
    except Exception:
        pass


async def _safe_unreact(message: discord.Message, emoji: str):
    try:
        await message.remove_reaction(emoji, bot.user)
    except Exception:
        pass


@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return
    if message.author.id not in ALLOWED_USERS:
        return

    # Attachments in an attached channel → save files + forward any text/long-paste.
    if message.channel.id in attached_pids and message.attachments:
        await _safe_react(message, "⌛")
        try:
            await _handle_attachments(message, message.channel.id, message.author.id)
        except Exception:
            await _safe_unreact(message, "⌛")
            await _safe_react(message, "❌")
            raise
        await _safe_unreact(message, "⌛")
        await _safe_react(message, "✅")
        return

    # In an attached channel, bare text (no prefix) goes straight to the terminal.
    if message.channel.id in attached_pids and not message.content.startswith(PREFIX):
        if not message.content.strip():
            return
        await _safe_react(message, "⌛")
        try:
            await cmd_terminal_send(message.channel, message.channel.id, message.author.id, message.content)
        except Exception:
            await _safe_unreact(message, "⌛")
            await _safe_react(message, "❌")
            raise
        await _safe_unreact(message, "⌛")
        await _safe_react(message, "✅")
        return

    if not message.content.startswith(PREFIX):
        return
    if ALLOWED_CHANNELS and message.channel.id not in ALLOWED_CHANNELS:
        return

    rest = message.content[len(PREFIX):].strip()
    await _safe_react(message, "⌛")
    try:
        await dispatch(message.channel, message.channel.id, message.author.id, rest)
    except Exception:
        await _safe_unreact(message, "⌛")
        await _safe_react(message, "❌")
        raise
    await _safe_unreact(message, "⌛")
    await _safe_react(message, "✅")


@tree.command(name="cc", description="Drive Claude Code (try 'help' for commands)")
@app_commands.describe(
    input="Prompt, or subcommand: help, new, status, cancel, sessions, resume <id>, cd <path>",
)
async def slash_cc(interaction: discord.Interaction, input: str):
    if not _is_authorised(interaction.user.id, interaction.channel_id):
        await interaction.response.send_message("Unauthorized.", ephemeral=True)
        return
    # Echo the invocation into the channel so the conversation history reads naturally,
    # then dispatch normally. ephemeral=False so other allowed users see it.
    await interaction.response.send_message(f"`{PREFIX} {input}`")
    await dispatch(interaction.channel, interaction.channel_id, interaction.user.id, input)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN is empty. Fill it in .env before running.")
    if not ALLOWED_USERS:
        raise SystemExit("ALLOWED_USER_IDS is empty — refusing to start.")
    bot.run(TOKEN)
