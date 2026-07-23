import asyncio
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, Optional

# Force stdout to UTF-8 so emoji / arrows in print() don't blow up on Windows cp1252.
# Also force line-buffered (write=line_buffering=True) so bot.log shows print() output
# as it happens — pythonw.exe's default block buffering means logs never flush to disk
# until shutdown, which makes mid-run debugging impossible.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
except Exception:
    pass

import discord
from discord import app_commands
from dotenv import load_dotenv

import asyncio as _asyncio
import sys
import tempfile

from approvals import request_approval
from live_processes import find_by_pid, list_running, pid_alive, session_jsonl_path
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
# Discord category to nest auto-created terminal channels under. Falls back to
# root (guild-level uncategorized) if no category with this name exists.
TERMINAL_CATEGORY_NAME = os.environ.get("TERMINAL_CATEGORY_NAME", "terminal")

PREFIX = "!cc"
MAX_CHUNK = 1900

CLAUDE_MONITOR_WS = os.environ.get("CLAUDE_MONITOR_WS", "ws://192.168.1.242:8765/ws")

HELP_TEXT = (
    f"**Claude Code remote**\n\n"
    f"**Multi-channel** — one Discord channel per terminal:\n"
    f"`{PREFIX} launch <name> [cwd]` — start a *brand-new* terminal, name it, attach a channel\n"
    f"`{PREFIX} spawn <name>` — attach a new channel to an *existing* running terminal\n"
    f"`{PREFIX} close [name]` — detach, **kill the terminal window**, delete the channel\n"
    f"`{PREFIX} cleanup` — sweep orphan PowerShell windows from past `/exit`s\n"
    f"`{PREFIX} sweep` — delete orphan hex-id Discord channels (no live claude.exe)\n\n"
    f"**Per-channel attach** (manual):\n"
    f"`{PREFIX} live` — list running Claude Code processes\n"
    f"`{PREFIX} attach <name>` — drive that terminal from this channel\n"
    f"`{PREFIX} detach` — stop driving the terminal\n"
    f"`{PREFIX} look` — snapshot the terminal screen\n"
    f"`{PREFIX} get <path>` — upload a file from the session's folder back to Discord (e.g. `{PREFIX} get notes.txt`)\n"
    f"`{PREFIX} pad` — pop a clickable keypad (arrows / Enter / Esc / Tab / 1-5 / Look) for the attached terminal\n"
    f"`{PREFIX} keys <seq>` — raw keys to the TUI (e.g. `down,down,enter`, `1`, `space,tab`)\n"
    f"**Tool-approval popups** auto-surface as Discord buttons (✅ Allow / ❌ Deny / 💬 Deny + tell Claude).\n\n"
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
_auto_resume_attempts: Dict[int, int] = {}  # channel_id → failed auto-resume tries (death path)
AUTO_RESUME_MAX_ATTEMPTS = 3  # give up + close after this many failed resumes (avoid window storm)
_last_channel_rename: Dict[int, float] = {}  # channel_id → time.time() of last name sync
# Discord rate-limits channel renames to 2 per 10 min per channel (and discord.py BLOCKS
# on the limit, which would stall the watcher). A 10-min cooldown keeps us to 1/10min.
CHANNEL_RENAME_COOLDOWN = 600.0
_session_shell_pid: Dict[int, int] = {}  # claude pid → its parent PowerShell pid (for /exit detection)
CONSOLE_HELPER = str(Path(__file__).parent / "console_helper.py")
# Drop-a-file close signal: the `/cc-close` slash command (via cc_close.ps1) writes
# close-markers/<claude_pid>; _close_marker_watcher acts on it within ~2s.
CLOSE_MARKER_DIR = Path(__file__).parent / "close-markers"
# Outbox: `/cc-send <file>` (via cc_send.ps1) copies a file to outbox/<claude_pid>__<name>;
# _outbox_watcher uploads it to that session's Discord channel, then deletes it.
OUTBOX_DIR = Path(__file__).parent / "outbox"


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


def _terminal_category(guild):
    """Return the CategoryChannel matching TERMINAL_CATEGORY_NAME (case-insensitive),
    or None if not found. Used to nest auto-created terminal channels."""
    if guild is None:
        return None
    target = TERMINAL_CATEGORY_NAME.lower()
    for cat in guild.categories:
        if cat.name.lower() == target:
            return cat
    return None


_HEX8_RE = re.compile(r"^[0-9a-f]{8}$")


def _bot_deletable(chan) -> bool:
    """True only for channels the bot is allowed to auto-delete: those under the
    `terminal` category, or legacy hex-id-named orphans it created. Protects
    human channels (notifications, control-room, physics, etc.) — the bot must
    never delete a channel it didn't create, even if it somehow got tracked."""
    if chan is None:
        return False
    if chan.id in CONTROL_CHANNELS:
        return False
    cat = getattr(chan, "category", None)
    if cat is not None and cat.name.lower() == TERMINAL_CATEGORY_NAME.lower():
        return True
    if _HEX8_RE.match(getattr(chan, "name", "") or ""):
        return True
    return False


def _format_tool_input(tool_name: str, tool_input: dict) -> str:
    """One-line preview of what a tool is doing. AskUserQuestion is multi-line."""
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
    if tool_name == "AskUserQuestion":
        # Render the question + numbered options so the user can answer from
        # Discord by typing the option number (the TUI picker accepts digits).
        questions = tool_input.get("questions") or []
        if not questions:
            return ""
        lines: list[str] = []
        for q in questions:
            if not isinstance(q, dict):
                continue
            qtext = (q.get("question") or "").strip()
            multi = q.get("multiSelect")
            suffix = " *(pick one or more)*" if multi else ""
            lines.append(f"❓ **{qtext}**{suffix}")
            opts = q.get("options") or []
            for i, opt in enumerate(opts, 1):
                if not isinstance(opt, dict):
                    continue
                label = (opt.get("label") or "").strip()
                desc = (opt.get("description") or "").strip()
                if desc:
                    lines.append(f"  `{i}.` **{label}** — {desc}")
                else:
                    lines.append(f"  `{i}.` **{label}**")
        return "\n" + "\n".join(lines) if lines else ""
    # Fallback — show first useful arg.
    for k in ("name", "url", "query", "command"):
        if k in tool_input:
            return f"`{k}: {str(tool_input[k])[:100]}`"
    return ""


def _pending_picker_tool(jsonl_path) -> str | None:
    """Return the name of the most recent unresolved tool_use in this JSONL,
    iff it's an interactive picker (AskUserQuestion). Used by cmd_terminal_send
    to decide whether sending Esc would cancel an open picker that the user is
    about to answer."""
    try:
        path = Path(jsonl_path)
        if not path.is_file():
            return None
        # Last ~64 KB is enough — picker tool_use is usually one of the last events.
        size = path.stat().st_size
        with path.open("rb") as f:
            f.seek(max(0, size - 65536))
            chunk = f.read().decode("utf-8", errors="replace")
    except OSError:
        return None

    last_tool_use: tuple[str, str] | None = None  # (id, name)
    resolved: set = set()
    for line in chunk.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = obj.get("type")
        msg = obj.get("message") or {}
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        if t == "assistant":
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tid = block.get("id")
                    name = block.get("name", "")
                    if tid:
                        last_tool_use = (tid, name)
        elif t == "user":
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    rid = block.get("tool_use_id")
                    if rid:
                        resolved.add(rid)

    if last_tool_use and last_tool_use[0] not in resolved:
        return last_tool_use[1]
    return None


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


def _attach_resumed_sdk(channel_id: int, user_id: int, s) -> str:
    """SDK-mode fallback: persist resume selection on the current channel. Returns reply text."""
    live = find_live_session(s.session_id)
    warn = ""
    if live:
        warn = (
            f"⚠️ Session is live in a terminal (PID {live['pid']}, status "
            f"`{live.get('status','?')}`). Close that window or Discord and the "
            f"terminal will both write to the same JSONL.\n"
        )
    sessions.set_both(channel_id, s.session_id, s.cwd)
    sessions.audit(channel_id, user_id, "resume", s.session_id)
    headline = s.custom_name if s.custom_name else s.first_prompt
    tag = "📌 " if s.custom_name else ""
    return (
        f"{warn}Attached to `{s.session_id[:8]}` in `{s.cwd}`.\n"
        f"> {tag}{(headline or '')[:160]}"
    )


def _list_claude_pids() -> set:
    """All running claude.exe PIDs via tasklist."""
    try:
        proc = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq claude.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return set()
    pids = set()
    for line in proc.stdout.splitlines():
        parts = [p.strip().strip('"') for p in line.split(",")]
        if len(parts) >= 2 and parts[1].isdigit():
            pids.add(int(parts[1]))
    return pids


def _spawn_session_window(inner_cmd: str, cwd: Optional[str], title: Optional[str] = None) -> None:
    """Launch a Claude session as a TAB in the existing Windows Terminal window.

    `wt -w 0 new-tab` adds a tab to the current WT window (creating one if none
    exists), so every session lands in ONE tabbed window instead of N scattered
    standalone windows. The bot drives WT-hosted tabs exactly like standalone
    consoles — AttachConsole / ReadConsoleOutputCharacterW / WriteConsoleInputW all
    operate on the console buffer regardless of WT hosting (verified 2026-05-31 by
    scraping + injecting into the WT-hosted remote-control session).

    Falls back to a standalone console if wt.exe is missing or errors, so launching
    never breaks if Windows Terminal isn't available.
    """
    inner = ["powershell.exe", "-NoExit", "-ExecutionPolicy", "Bypass", "-Command", inner_cmd]
    use_cwd = cwd if (cwd and cwd not in ("?", "") and Path(cwd).is_dir()) else None
    # wt.exe is a Windows App Execution Alias; bare "wt.exe" isn't on the bot's
    # (pythonw, scheduled-task) PATH, so resolve the full WindowsApps alias path —
    # CreateProcess launches it fine by absolute path even though PATH lookup fails.
    wt = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WindowsApps", "wt.exe")
    if os.path.isfile(wt):
        wt_args = [wt, "-w", "0", "new-tab"]
        if use_cwd:
            wt_args += ["-d", use_cwd]
        if title:
            wt_args += ["--title", title]
        wt_args += inner
        try:
            subprocess.Popen(wt_args, close_fds=True)
            return
        except Exception as e:
            print(f"  wt launch failed ({type(e).__name__}: {e}); standalone console fallback")
    else:
        print(f"  wt.exe not found at {wt}; standalone console fallback")
    subprocess.Popen(inner, creationflags=subprocess.CREATE_NEW_CONSOLE,
                     cwd=use_cwd, close_fds=True)


async def _drive_resume_startup(pid: int, max_wait: float = 90.0) -> str:
    """Walk a freshly-launched claude through its startup prompts so the session
    ends up actually resumed — and FULL, not summarized.

    Two gated prompts can appear, and a slow machine (DNS/pihole lag after a reboot)
    can push them well past any fixed sleep — which is exactly how a tab gets stuck:
      1. Trust prompt — accept (Enter). v2.1.158 reworded this to
         "Quick safety check: Is this a project you created or one you trust?" /
         "Yes, I trust this folder"; older builds said "Do you trust the files…".
      2. Resume picker — its DEFAULT highlight is "Resume from summary (recommended)",
         which is lossy. We want the full conversation, so move Down one and confirm
         (Down, Enter) to select "Resume full session as-is".

    Polls the screen (instead of sleeping blindly) until both are handled or the
    ready prompt/statusline appears. Brand-new launches have no resume picker — the
    trust branch fires, then it detects ready and returns. Returns a status string.

    Trust detection is deliberately strict: it matches a full prompt phrase AND the
    live "enter to confirm" menu affordance TOGETHER. A restored transcript can quote
    words like "trust" in the conversation body — the loose substring match this used
    to do ("do you trust") false-fired on that text and sent a stray Enter into the
    ready prompt. Requiring the interactive-menu footer scopes it to the real screen.
    """
    # Full prompt phrases (not loose substrings) so transcript text can't match.
    trust_phrases = ("do you trust the files", "quick safety check",
                     "yes, i trust this folder")
    deadline = time.time() + max_wait
    did_trust = False
    did_resume = False
    while time.time() < deadline:
        screen = (await _run_console_helper(pid, "", mode="look")).lower()
        if (not did_trust and "enter to confirm" in screen
                and any(p in screen for p in trust_phrases)
                and "resume full session" not in screen):
            await _run_console_helper(pid, "", mode="enter")  # accept trust (default: yes)
            did_trust = True
            await asyncio.sleep(1.5)
            continue
        if not did_resume and "resume full session" in screen and "enter to confirm" in screen:
            # Default highlight is option 1 (summary); Down -> option 2 (full), Enter confirms.
            # "down" goes out as a VT escape sequence (console_helper handles arrows that
            # way for v2.1.158+, which ignores synthesized VK_DOWN key events).
            await _run_console_helper(pid, "down,enter", mode="keys")
            did_resume = True
            await asyncio.sleep(1.5)
            continue
        # Ready: the normal prompt is up. Custom statusline shows "ctx N%"; Claude's
        # footer shows "shift+tab to cycle". Either means startup prompts are done.
        if "shift+tab to cycle" in screen or re.search(r"ctx\s+\d+%", screen):
            break
        await asyncio.sleep(1.0)
    return f"trust={did_trust} full_resume={did_resume}"


async def cmd_resume_spawn(channel, user_id: int, s):
    """Spawn `claude --resume <id>` in a new console, make a new Discord channel, attach it."""
    if not channel.guild:
        await channel.send(_attach_resumed_sdk(channel.id, user_id, s))
        return

    cwd = s.cwd
    if cwd in (None, "", "?") or not Path(cwd).is_dir():
        await channel.send(
            f"⚠️ Session's cwd `{cwd}` doesn't exist on this machine — can't resume here."
        )
        return

    sessions_dir = Path.home() / ".claude" / "sessions"
    before_pids = _list_claude_pids()

    short = s.session_id[:8]
    await channel.send(f"🚀 Resuming `{short}` in `{cwd}`…")

    try:
        _spawn_session_window(f"claude --resume {s.session_id}", cwd,
                              title=(s.custom_name or short))
    except Exception as e:
        await channel.send(f"⚠️ Couldn't launch: `{type(e).__name__}: {e}`")
        return

    new_pid: Optional[int] = None
    deadline = time.time() + 30
    while time.time() < deadline:
        await asyncio.sleep(1)
        diff = _list_claude_pids() - before_pids
        if diff:
            new_pid = max(diff)
            break

    if not new_pid:
        await channel.send("⚠️ PowerShell window opened but no claude.exe appeared.")
        return

    # Pre-claim the PID so the auto-spawn watcher (15 s poll) doesn't race us and
    # create a duplicate hex-id channel for the same terminal.
    _auto_spawn_seen.add(new_pid)

    # Walk trust + resume prompts, picking FULL resume (not the summary default).
    await asyncio.sleep(2)
    await _drive_resume_startup(new_pid)

    # Wait for the session JSON — this will hold the NEW forked session_id Claude assigned.
    session_deadline = time.time() + 30
    while time.time() < session_deadline:
        await asyncio.sleep(0.5)
        if (sessions_dir / f"{new_pid}.json").is_file():
            break
    else:
        await channel.send(
            f"⚠️ PID {new_pid} started but no session JSON appeared within 30s. "
            f"Trust prompt may still be open — try `!cc attach {new_pid}` then `!cc esc`."
        )
        return

    raw_name = s.custom_name or s.first_prompt or short
    sanitized = _sanitize_channel_name(raw_name)
    try:
        new_chan = await channel.guild.create_text_channel(
            name=sanitized,
            category=_terminal_category(channel.guild),
        )
    except discord.Forbidden:
        await channel.send(
            "⚠️ Terminal up but can't make a channel — bot needs **Manage Channels**."
        )
        return
    except discord.HTTPException as e:
        await channel.send(f"⚠️ Couldn't create channel: {e}")
        return

    ALLOWED_CHANNELS.add(new_chan.id)
    await cmd_attach(new_chan, new_chan.id, user_id, str(new_pid))
    await channel.send(f"📡 Resumed `{short}` (PID {new_pid}) in <#{new_chan.id}>.")

    # Auto-look so the user sees the screen state immediately on the new
    # channel — no need to manually `!cc look`. Useful after resume because
    # the TUI may still be showing a picker / boot screen that the JSONL
    # mirror won't capture (mirror only fires once Claude writes events).
    try:
        await asyncio.sleep(1.5)
        await cmd_look(new_chan, new_chan.id)
    except Exception as e:
        print(f"  cmd_resume_spawn: auto-look failed: {e}")


class ResumeSelect(discord.ui.Select):
    def __init__(self, summaries):
        options = []
        for s in summaries[:25]:
            short = s.session_id[:8]
            raw = (s.custom_name or s.first_prompt or "(empty)").replace("\n", " ").strip()
            label = (raw[:97] + "…") if len(raw) > 100 else (raw or "(empty)")
            desc = f"{short} · {s.cwd} · {format_age(s.mtime)}"
            if len(desc) > 100:
                desc = desc[:99] + "…"
            live = find_live_session(s.session_id)
            emoji = "🔴" if live else ("📌" if s.custom_name else None)
            options.append(discord.SelectOption(
                label=label,
                value=s.session_id,
                description=desc,
                emoji=emoji,
            ))
        super().__init__(
            placeholder="Pick a session to resume…",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        if not _is_authorised(interaction.user.id, interaction.channel_id):
            await interaction.response.send_message("Unauthorized.", ephemeral=True)
            return
        session_id = self.values[0]
        s = find_by_prefix(session_id)
        if not s:
            await interaction.response.send_message(
                f"Session `{session_id[:8]}` not found anymore.", ephemeral=True
            )
            return
        # Spawning a terminal can take 5-30 s, well past Discord's 3 s ack window.
        await interaction.response.defer()
        self.disabled = True
        self.placeholder = f"Resuming {s.session_id[:8]}…"
        if self.view is not None:
            self.view.stop()
            try:
                await interaction.message.edit(view=self.view)
            except discord.HTTPException:
                pass
        await cmd_resume_spawn(interaction.channel, interaction.user.id, s)


class ResumePickerView(discord.ui.View):
    def __init__(self, summaries):
        super().__init__(timeout=180)
        self.add_item(ResumeSelect(summaries))


# ---------- tool-approval bridge (attached-terminal mode) -------------------
#
# When Claude Code runs in a real terminal, tool-approval popups are TUI-only —
# no `can_use_tool` callback. Detection is two-stage so we don't post useless
# buttons for auto-approved-but-slow tools (e.g. WebSearch can take 5-10 s):
#   1. timing: tool_use unresolved for ≥APPROVAL_DELAY
#   2. screen-read: the terminal actually shows an approval popup right now
# Only when both fire do we surface the Discord embed.


# Heuristic signatures for Claude Code's approval popup. console_helper reads
# only the visible window (not scrollback), so old dialogs in history won't
# match — these patterns reflect the live screen state.
_APPROVAL_POPUP_SIGNATURES = (
    re.compile(r"Do you want to (?:allow|run|continue|proceed|edit|create|delete)", re.I),
    re.compile(r"^\s*[▶❯>]\s*\d+[.):]\s", re.M),  # cursored numbered option
    re.compile(r"^\s*1\.\s*(?:Yes|Allow)", re.I | re.M),
)


def _screen_shows_approval_popup(screen: str) -> bool:
    if not screen or not screen.strip():
        return False
    return any(sig.search(screen) for sig in _APPROVAL_POPUP_SIGNATURES)


# Indicators that the visible screen has something the user can navigate
# (picker cursor, checkboxes, or a numbered selection list). Used after a
# slash command to decide whether to auto-attach a keypad to the snapshot.
_NAVIGABLE_CURSOR_RE = re.compile(
    r"^\s*[▶❯▷]\s+\S"            # box-drawing cursors
    r"|^\s*>\s+[√✓✗(\[]",        # `>` followed by a checkbox/check glyph (menu cursor)
    re.M,
)
_NAVIGABLE_CHECKBOX_RE = re.compile(r"[(\[][√✓✗x• ][)\]]", re.I)
_NAVIGABLE_NUMBERED_RE = re.compile(r"^\s+\d+[.):]\s+\S", re.M)


def _screen_looks_navigable(screen: str) -> bool:
    """Heuristic: is there an interactive picker/menu on the screen right now?"""
    if not screen or not screen.strip():
        return False
    # Only consider the last ~30 lines so old scrollback indicators don't trip us.
    tail = "\n".join(screen.splitlines()[-30:])
    return bool(
        _NAVIGABLE_CURSOR_RE.search(tail)
        or _NAVIGABLE_CHECKBOX_RE.search(tail)
        or _NAVIGABLE_NUMBERED_RE.search(tail)
    )


def _screen_pending_question(screen: str) -> bool:
    """True if an AskUserQuestion picker is OPEN on screen, by its footer
    'Enter to select · ↑/↓ to navigate · Esc to cancel'. We detect questions by screen
    because Claude Code does NOT write the question to the JSONL until it's answered —
    confirmed 2026-06-08: the session log didn't grow one byte while a question sat on
    screen for 80s — so the JSONL-tailing mirror can't surface it in time."""
    if not screen:
        return False
    tail = "\n".join(screen.splitlines()[-22:]).lower()
    return ("to navigate" in tail) and ("to cancel" in tail) and ("to select" in tail)


def _extract_picker_text(screen: str) -> str:
    """Pull the visible question + options out of the on-screen picker (the lines ending
    at the 'to navigate' footer), dropping pure box-border lines."""
    lines = screen.splitlines()
    end = len(lines)
    for i in range(len(lines) - 1, -1, -1):
        if "to navigate" in lines[i].lower():
            end = i + 1
            break
    chunk = lines[max(0, end - 16):end]
    keep = [ln.rstrip() for ln in chunk
            if ln.strip() and (set(ln.strip()) - set("─│╭╮╰╯—-_= "))]
    return "\n".join(keep)[-1500:]


class DenyInstructModal(discord.ui.Modal, title="Tell Claude what to do differently"):
    instruction = discord.ui.TextInput(
        label="Instruction",
        style=discord.TextStyle.paragraph,
        placeholder="Why this is wrong, and what Claude should do instead.",
        required=True,
        max_length=2000,
    )

    def __init__(self, parent_view: "ToolApprovalView"):
        super().__init__()
        self.parent_view = parent_view

    async def on_submit(self, interaction: discord.Interaction):
        if not _is_authorised(interaction.user.id, interaction.channel_id):
            await interaction.response.send_message("Unauthorized.", ephemeral=True)
            return
        pid = attached_pids.get(self.parent_view.channel_id)
        if pid is None:
            await interaction.response.send_message(
                "Not attached anymore — can't reach the terminal.", ephemeral=True
            )
            return
        await interaction.response.defer()
        # Select "Deny + instruct" (option 3) in the popup.
        await _run_console_helper(pid, "3", mode="keys")
        # Give the TUI a beat to switch the popup into text-input mode.
        await asyncio.sleep(0.6)
        # Type the instruction; `type` mode appends Enter to submit.
        await _run_console_helper(pid, str(self.instruction), mode="type")
        sessions.audit(
            self.parent_view.channel_id,
            interaction.user.id,
            "tool_approval",
            f"{self.parent_view.tool_name}:Deny+Instruct",
        )
        for child in self.parent_view.children:
            child.disabled = True
        self.parent_view.stop()
        msg = self.parent_view.message
        if msg is not None:
            try:
                snippet = str(self.instruction)[:200]
                await msg.edit(
                    content=f"{msg.content}\n→ **Deny + instruct** by <@{interaction.user.id}>: {snippet}",
                    view=self.parent_view,
                )
            except discord.HTTPException:
                pass


class ToolApprovalView(discord.ui.View):
    def __init__(self, channel_id: int, tool_id: str, tool_name: str):
        super().__init__(timeout=600)
        self.channel_id = channel_id
        self.tool_id = tool_id
        self.tool_name = tool_name

    async def _send_keystroke(
        self,
        interaction: discord.Interaction,
        sequence: str,
        action_label: str,
    ):
        if not _is_authorised(interaction.user.id, interaction.channel_id):
            await interaction.response.send_message("Unauthorized.", ephemeral=True)
            return
        pid = attached_pids.get(self.channel_id)
        if pid is None:
            await interaction.response.send_message(
                "Not attached anymore — can't reach the terminal.", ephemeral=True
            )
            return
        await interaction.response.defer()
        await _run_console_helper(pid, sequence, mode="keys")
        sessions.audit(
            self.channel_id, interaction.user.id, "tool_approval",
            f"{self.tool_name}:{action_label}",
        )
        for child in self.children:
            child.disabled = True
        self.stop()
        try:
            await interaction.message.edit(
                content=f"{interaction.message.content}\n→ **{action_label}** by <@{interaction.user.id}>",
                view=self,
            )
        except discord.HTTPException:
            pass

    @discord.ui.button(label="Allow", style=discord.ButtonStyle.success, emoji="✅")
    async def btn_allow(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self._send_keystroke(interaction, "1", "Allow")

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger, emoji="❌")
    async def btn_deny(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self._send_keystroke(interaction, "2", "Deny")

    @discord.ui.button(label="Deny + tell Claude…", style=discord.ButtonStyle.secondary, emoji="💬")
    async def btn_deny_instruct(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if not _is_authorised(interaction.user.id, interaction.channel_id):
            await interaction.response.send_message("Unauthorized.", ephemeral=True)
            return
        if attached_pids.get(self.channel_id) is None:
            await interaction.response.send_message(
                "Not attached anymore — can't reach the terminal.", ephemeral=True
            )
            return
        await interaction.response.send_modal(DenyInstructModal(parent_view=self))


# ---------- remote keypad (interactive TUI driver) --------------------------
#
# Discord buttons → key events into the attached terminal. Lets you navigate
# Claude Code's pickers / popups without typing `!cc keys ...` each time.


class _AskOptionButton(discord.ui.Button):
    """One clickable option for an AskUserQuestion picker. Clicking it selects
    the corresponding numbered option in the attached terminal's TUI."""
    def __init__(self, channel_id: int, number: int, label: str, row: int):
        super().__init__(
            label=f"{number}. {label[:72]}",
            style=discord.ButtonStyle.primary,
            row=row,
        )
        self.channel_id = channel_id
        self.number = number

    async def callback(self, interaction: discord.Interaction):
        if not _is_authorised(interaction.user.id, interaction.channel_id):
            await interaction.response.send_message("Unauthorized.", ephemeral=True)
            return
        pid = attached_pids.get(self.channel_id)
        if pid is None:
            await interaction.response.send_message(
                "Not attached anymore — can't answer.", ephemeral=True
            )
            return
        await interaction.response.defer()
        # Navigate deterministically: clamp to the top with a few Ups (no-op once
        # at option 1), step Down (number-1) times, then Enter to confirm. This
        # works regardless of where the picker cursor currently sits and doesn't
        # rely on digit-select (AskUserQuestion's footer only advertises ↑/↓).
        seq = ["up"] * 5 + ["down"] * (self.number - 1) + ["enter"]
        await _run_console_helper(pid, ",".join(seq), mode="keys")
        sessions.audit(self.channel_id, interaction.user.id, "askq_answer", str(self.number))
        # Disable all buttons and mark the chosen one.
        view: discord.ui.View = self.view
        for child in view.children:
            child.disabled = True
            if isinstance(child, _AskOptionButton) and child.number == self.number:
                child.style = discord.ButtonStyle.success
        view.stop()
        try:
            await interaction.message.edit(view=view)
        except discord.HTTPException:
            pass


class AskUserQuestionView(discord.ui.View):
    """Interactive button menu for an AskUserQuestion prompt. One button per
    option of the FIRST question (Discord allows up to 25 buttons / 5 rows;
    AskUserQuestion has ≤4-5 options, so one row). Multi-question prompts still
    get the text rendering; the buttons answer the first question."""
    def __init__(self, channel_id: int, option_labels: list):
        super().__init__(timeout=3600)
        self.channel_id = channel_id
        for i, label in enumerate(option_labels[:5], 1):
            self.add_item(_AskOptionButton(channel_id, i, label, row=(i - 1) // 5))


class RemoteKeypadView(discord.ui.View):
    """3 × 5 grid: arrows, modifier keys, number keys for numbered popups, Look."""

    def __init__(self, channel_id: int):
        super().__init__(timeout=3600)
        self.channel_id = channel_id

    async def _update_message_with_screen(self, interaction: discord.Interaction, pid: int):
        """Read the current screen and edit this view's message to show it.

        Gives the user live feedback in the same Discord message instead of
        a follow-up per click. Errors are swallowed so a flaky read doesn't
        break the interaction.
        """
        try:
            screen = await _run_console_helper(pid, "", mode="look")
            body = screen[-1800:] if screen.strip() else "_(screen empty)_"
            await interaction.message.edit(content=f"```\n{body}\n```", view=self)
        except (discord.HTTPException, OSError):
            pass

    async def _send_key(self, interaction: discord.Interaction, key: str):
        if not _is_authorised(interaction.user.id, interaction.channel_id):
            await interaction.response.send_message("Unauthorized.", ephemeral=True)
            return
        pid = attached_pids.get(self.channel_id)
        if pid is None:
            await interaction.response.send_message(
                "Not attached anymore — `!cc attach` first.", ephemeral=True
            )
            return
        await interaction.response.defer()
        await _run_console_helper(pid, key, mode="keys")
        sessions.audit(self.channel_id, interaction.user.id, "keypad", key)
        # Small beat so the TUI has time to redraw before we capture it.
        await asyncio.sleep(0.4)
        await self._update_message_with_screen(interaction, pid)

    async def _snapshot(self, interaction: discord.Interaction):
        if not _is_authorised(interaction.user.id, interaction.channel_id):
            await interaction.response.send_message("Unauthorized.", ephemeral=True)
            return
        pid = attached_pids.get(self.channel_id)
        if pid is None:
            await interaction.response.send_message(
                "Not attached anymore.", ephemeral=True
            )
            return
        await interaction.response.defer()
        await self._update_message_with_screen(interaction, pid)

    # Button placement deliberately matches a physical keyboard arrow cluster:
    # `⬆` sits in row 0 col 1 directly above `⬇` in row 1 col 1, with `⬅` and
    # `➡` flanking — the classic inverted-T. The other utility keys fill the
    # remaining slots in each row.

    # Row 0 — Esc, ↑, Tab/Bksp/Look
    @discord.ui.button(label="Esc", style=discord.ButtonStyle.secondary, row=0)
    async def k_esc(self, i: discord.Interaction, _b): await self._send_key(i, "esc")

    @discord.ui.button(emoji="⬆️", style=discord.ButtonStyle.primary, row=0)
    async def k_up(self, i: discord.Interaction, _b): await self._send_key(i, "up")

    @discord.ui.button(label="Tab", style=discord.ButtonStyle.secondary, row=0)
    async def k_tab(self, i: discord.Interaction, _b): await self._send_key(i, "tab")

    @discord.ui.button(label="Bksp", style=discord.ButtonStyle.secondary, row=0)
    async def k_bksp(self, i: discord.Interaction, _b): await self._send_key(i, "backspace")

    @discord.ui.button(label="👁 Look", style=discord.ButtonStyle.success, row=0)
    async def k_look(self, i: discord.Interaction, _b): await self._snapshot(i)

    # Row 1 — ←, ↓, →, Enter, Space
    @discord.ui.button(emoji="⬅️", style=discord.ButtonStyle.primary, row=1)
    async def k_left(self, i: discord.Interaction, _b): await self._send_key(i, "left")

    @discord.ui.button(emoji="⬇️", style=discord.ButtonStyle.primary, row=1)
    async def k_down(self, i: discord.Interaction, _b): await self._send_key(i, "down")

    @discord.ui.button(emoji="➡️", style=discord.ButtonStyle.primary, row=1)
    async def k_right(self, i: discord.Interaction, _b): await self._send_key(i, "right")

    @discord.ui.button(label="Enter ↵", style=discord.ButtonStyle.success, row=1)
    async def k_enter(self, i: discord.Interaction, _b): await self._send_key(i, "enter")

    @discord.ui.button(label="Space", style=discord.ButtonStyle.secondary, row=1)
    async def k_space(self, i: discord.Interaction, _b): await self._send_key(i, "space")

    # Row 2 — number keys (Claude Code popups use 1/2/3/…)
    @discord.ui.button(label="1", style=discord.ButtonStyle.secondary, row=2)
    async def k_1(self, i: discord.Interaction, _b): await self._send_key(i, "1")

    @discord.ui.button(label="2", style=discord.ButtonStyle.secondary, row=2)
    async def k_2(self, i: discord.Interaction, _b): await self._send_key(i, "2")

    @discord.ui.button(label="3", style=discord.ButtonStyle.secondary, row=2)
    async def k_3(self, i: discord.Interaction, _b): await self._send_key(i, "3")

    @discord.ui.button(label="4", style=discord.ButtonStyle.secondary, row=2)
    async def k_4(self, i: discord.Interaction, _b): await self._send_key(i, "4")

    @discord.ui.button(label="5", style=discord.ButtonStyle.secondary, row=2)
    async def k_5(self, i: discord.Interaction, _b): await self._send_key(i, "5")


async def cmd_pad(channel, channel_id):
    pid = attached_pids.get(channel_id)
    if pid is None:
        await channel.send("Not attached. `!cc attach <name>` first.")
        return
    view = RemoteKeypadView(channel_id)
    msg = await channel.send(
        "⌨️ **Remote keypad** — tap to send keys to the attached terminal. "
        "Lives for 1 hour, then run `!cc pad` again.",
        view=view,
    )
    view.message = msg


async def cmd_resume(channel, channel_id, user_id, prefix: str):
    if not prefix:
        summaries = list_recent_sessions(limit=25)
        if not summaries:
            await channel.send("No Claude Code sessions found on disk.")
            return
        await channel.send(
            "**Pick a session to resume:**",
            view=ResumePickerView(summaries),
        )
        return
    s = find_by_prefix(prefix)
    if not s:
        await channel.send(
            f"No session matches `{prefix}`. Run `{PREFIX} resume` (no id) for a picker."
        )
        return
    await cmd_resume_spawn(channel, user_id, s)


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
    # Persist the session identity too (not just the ephemeral PID) so this
    # channel can be resumed after a reboot. See _restore_terminals_on_boot.
    sessions.set_identity(channel_id, match.session_id, match.cwd)
    label = match.name or match.session_id[:8]

    # Start the bidirectional mirror: anything Claude writes to this session's JSONL —
    # whether triggered from Discord OR typed locally in the terminal — gets posted here.
    # File may not exist yet when attaching to a fresh terminal (Claude creates the JSONL
    # on first activity, not at launch). _mirror_loop already tolerates missing files via
    # its stat() retry, so we always start it — it'll begin tailing once the file appears.
    jsonl = session_jsonl_path(match.cwd, match.session_id)
    start_offset = jsonl.stat().st_size if jsonl.is_file() else 0
    mirror_tasks[channel_id] = asyncio.create_task(
        _mirror_loop(channel, channel_id, user_id, jsonl, start_offset, label)
    )
    if jsonl.is_file():
        mirror_note = f"\n📡 Live mirror started — anything Claude does will appear here."
    else:
        mirror_note = f"\n📡 Live mirror armed — starts when Claude writes its first message."

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
        new_chan = await channel.guild.create_text_channel(
            name=channel_name,
            category=_terminal_category(channel.guild),
        )
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


async def cmd_usage(channel):
    """Fetch the latest snapshot from your claude-monitor dashboard's WebSocket."""
    import aiohttp  # already a transitive dep of discord.py

    try:
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(CLAUDE_MONITOR_WS, timeout=5) as ws:
                msg = await asyncio.wait_for(ws.receive(), timeout=6)
                if msg.type != aiohttp.WSMsgType.TEXT:
                    await channel.send(f"⚠️ Unexpected WS message type from claude-monitor: {msg.type}")
                    return
                data = json.loads(msg.data)
    except Exception as e:
        await channel.send(
            f"⚠️ Couldn't reach claude-monitor at `{CLAUDE_MONITOR_WS}`: "
            f"`{type(e).__name__}: {e}`"
        )
        return

    def _pct(s):
        return (s.get("context_tokens", 0) / max(s.get("max_context", 1), 1)) * 100

    title = data.get("title") or data.get("session_id", "?")[:8]
    cwd = data.get("cwd", "?")
    model = data.get("model_name", "?")
    pct = _pct(data)
    cost = data.get("cost_usd", 0)
    msgs = data.get("messages", 0)
    prompts = data.get("user_prompts", 0)

    ru = data.get("real_usage") or {}
    five_h = ru.get("five_hour_pct")
    five_h_reset = ru.get("five_hour_reset")
    week = ru.get("week_all_pct")
    week_reset = ru.get("week_all_reset")
    week_sonnet = ru.get("week_sonnet_pct")

    lines = [f"**📊 Claude usage**"]
    if five_h is not None:
        suffix = f" · resets {five_h_reset}" if five_h_reset else ""
        lines.append(f"5h: **{five_h:.0f}%**{suffix}")
    if week is not None:
        suffix = f" · resets {week_reset}" if week_reset else ""
        sonnet_part = f" (Sonnet: {week_sonnet:.0f}%)" if week_sonnet else ""
        lines.append(f"weekly: **{week:.0f}%**{sonnet_part}{suffix}")

    lines.append(
        f"\n**Active session** — `{title}` · `{cwd}` · {model}\n"
        f"context: **{pct:.1f}%** · {msgs} msgs ({prompts} prompts) · ${cost:.2f}"
    )

    others = data.get("other_sessions") or []
    if others:
        lines.append(f"\n**Other recent sessions** (top {min(len(others), 10)}):")
        for s in others[:10]:
            t = s.get("title") or s.get("session_id", "?")[:8]
            lines.append(
                f"`{t}` · {_pct(s):.0f}% ctx · ${s.get('cost_usd', 0):.2f}"
            )

    await send_chunked(channel, "\n".join(lines))


async def cmd_launch(channel, user_id: int, args: str):
    """Launch a brand-new claude.exe in a new console, rename it, attach to a new channel."""
    if not channel.guild:
        await channel.send("Can only launch from inside a server.")
        return
    parts = args.strip().split(maxsplit=1) if args else []
    if not parts:
        await channel.send(f"Usage: `{PREFIX} launch <name> [working-dir]`\nExample: `{PREFIX} launch helmet C:/esp-projects/vl53l8cx_esp32`")
        return
    name = parts[0]
    cwd = parts[1].strip().strip('"').strip("'") if len(parts) > 1 else DEFAULT_CWD
    if not Path(cwd).is_dir():
        await channel.send(f"Not a directory: `{cwd}`")
        return

    sessions_dir = Path.home() / ".claude" / "sessions"

    def _list_claude_pids() -> set:
        """Get all running claude.exe PIDs via tasklist — works even before session JSON exists."""
        try:
            proc = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq claude.exe", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=10,
            )
        except Exception:
            return set()
        pids = set()
        for line in proc.stdout.splitlines():
            parts = [p.strip().strip('"') for p in line.split(",")]
            if len(parts) >= 2 and parts[1].isdigit():
                pids.add(int(parts[1]))
        return pids

    before_pids = _list_claude_pids()

    await channel.send(f"🚀 Launching new Claude in `{cwd}`…")

    # Spawn in a new visible console. `claude` is a .ps1 script, so we need ExecutionPolicy
    # Bypass — subprocess-launched PowerShell can land on a restricted policy.
    try:
        _spawn_session_window("claude", cwd, title="claude")
    except Exception as e:
        await channel.send(f"⚠️ Couldn't launch: `{type(e).__name__}: {e}`")
        return

    # Wait for a new claude.exe to appear via tasklist (faster than waiting for the
    # session JSON, which doesn't exist until trust prompt is accepted).
    new_pid: Optional[int] = None
    deadline = time.time() + 30
    while time.time() < deadline:
        await asyncio.sleep(1)
        new_pids = _list_claude_pids() - before_pids
        if new_pids:
            new_pid = max(new_pids)
            break

    if not new_pid:
        await channel.send(
            "⚠️ Launched the PowerShell window but no new claude.exe appeared. "
            "Check if a window opened with an error."
        )
        return

    # Pre-claim so the auto-spawn watcher doesn't create a duplicate hex-id channel
    # during the ~5-10 s it takes us to finish trust prompt + /rename + attach.
    _auto_spawn_seen.add(new_pid)

    # Accept the trust prompt (screen-aware, so a slow startup doesn't leave it stuck).
    await asyncio.sleep(2)
    await _drive_resume_startup(new_pid)

    # NOW wait for the session JSON to register (trust accepted, claude initialised).
    session_deadline = time.time() + 30
    while time.time() < session_deadline:
        await asyncio.sleep(0.5)
        if (sessions_dir / f"{new_pid}.json").is_file():
            break
    else:
        await channel.send(
            f"⚠️ PID {new_pid} started but no session JSON appeared within 30s. "
            f"Trust prompt may still be open — try `!cc attach {new_pid}` then `!cc esc`."
        )
        return

    # Inject /rename to give the new session the chosen name.
    rename_helper_out = await _run_console_helper(new_pid, f"/rename {name}", mode="type")
    if "AttachConsole" in rename_helper_out and "failed" in rename_helper_out:
        await channel.send(
            f"⚠️ Started PID {new_pid} but couldn't rename it: {rename_helper_out.strip()}"
        )
    await asyncio.sleep(1.5)  # let the rename propagate

    # Create the Discord channel and attach.
    sanitized = _sanitize_channel_name(name)
    try:
        new_chan = await channel.guild.create_text_channel(
            name=sanitized,
            category=_terminal_category(channel.guild),
        )
    except discord.Forbidden:
        await channel.send(
            "⚠️ Started the terminal but can't make a channel — bot needs **Manage Channels**."
        )
        return
    except discord.HTTPException as e:
        await channel.send(f"⚠️ Couldn't create channel: {e}")
        return

    ALLOWED_CHANNELS.add(new_chan.id)
    await cmd_attach(new_chan, new_chan.id, user_id, str(new_pid))
    await channel.send(f"📡 New terminal `{name}` (PID {new_pid}) up in <#{new_chan.id}>.")


def _get_parent_pid_sync(pid: int) -> Optional[int]:
    """Best-effort parent-PID lookup via CIM. Returns None for dead pids or query failure."""
    if not pid_alive(pid):
        return None
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command",
             f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').ParentProcessId"],
            capture_output=True, text=True, timeout=10,
            creationflags=0x08000000,  # CREATE_NO_WINDOW
        )
        s = proc.stdout.strip()
        return int(s) if s.isdigit() else None
    except Exception:
        return None


def _kill_tree(pid: int) -> bool:
    """taskkill /T /F. Returns True if `pid` is dead afterwards."""
    try:
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(pid)],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return False
    time.sleep(0.3)  # give the OS a moment to actually tear down the tree
    return not pid_alive(pid)


async def _close_terminal_for_pid(pid: int) -> str:
    """Kill the PowerShell window owning claude.exe `pid`. Returns user-facing status."""
    if not pid_alive(pid):
        return "claude.exe already exited — window may persist, try `!cc cleanup`"
    ppid = _get_parent_pid_sync(pid)
    if ppid:
        if _kill_tree(ppid):
            return f"closed terminal window (PowerShell PID {ppid})"
        return f"sent kill to PowerShell {ppid} but it's still alive"
    _kill_tree(pid)  # at least stop claude.exe from writing more JSONL
    return f"killed claude.exe {pid}; PowerShell parent unknown — window may persist"


async def _close_session_cleanly(ch_id, claude_pid: int, shell_pid: int, reason: str) -> None:
    """Tear a session down cleanly and permanently (no auto-resume, no re-channel).

    `ch_id` may be None (an orphaned session with no channel) — we still kill it so a
    close always closes the terminal. Order: mark CLOSING (so the reconciler can't
    re-create a channel for it mid-teardown) + UNTRACK first; end claude and WAIT for it
    to actually die (otherwise the reconciler sees a 'live session with no channel' and
    re-adds one); send the `-NoExit` shell a graceful `exit` so WT closes the tab cleanly;
    finally delete the Discord channel.
    """
    _closing_pids.add(claude_pid)
    try:
        if ch_id is not None:
            mt = mirror_tasks.pop(ch_id, None)
            if mt and not mt.done():
                mt.cancel()
            attached_pids.pop(ch_id, None)
            _auto_resume_attempts.pop(ch_id, None)
            try:
                sessions.conn.execute("DELETE FROM sessions WHERE channel_id = ?", (ch_id,))
                sessions.conn.commit()
            except Exception:
                pass
            ALLOWED_CHANNELS.discard(ch_id)
        _session_shell_pid.pop(claude_pid, None)

        if claude_pid and pid_alive(claude_pid):
            _kill_tree(claude_pid)  # ends claude + helper/bash it spawned; NOT the parent shell
            for _ in range(20):     # wait up to ~4s for it to actually exit
                if not pid_alive(claude_pid):
                    break
                await asyncio.sleep(0.2)
        if shell_pid and pid_alive(shell_pid):
            await asyncio.sleep(0.4)  # let the shell return to its prompt
            try:
                # claude.exe was hard-killed above, so it never ran its terminal
                # restore — the shell is left with mouse-reporting + bracketed-paste
                # modes still ON. On the next mouse move that floods the prompt with
                # "[<..M" SGR codes, which ALSO mangles a plain `exit` (it lands in the
                # middle of the garbage and never runs → the tab hangs open forever,
                # exactly the /cc-close symptom). So first WRITE the disable sequences
                # to the console to stop the flood, then exit 0 so WT closes the tab
                # cleanly. [char]27 (not `e) because these tabs run Windows PowerShell 5.1.
                reset = ("[Console]::Write([char]27+'[?1000l'+[char]27+'[?1002l'+"
                         "[char]27+'[?1003l'+[char]27+'[?1006l'+[char]27+'[?2004l'); exit")
                await _run_console_helper(shell_pid, reset, mode="type")  # graceful → WT closes tab
            except Exception:
                pass
            # Guarantee the tab actually closes: if the graceful exit hasn't taken
            # within ~1.6s (e.g. a mouse-flood mangled the typed command), force-kill
            # the shell so /cc-close never leaves a stuck, flooded tab behind.
            for _ in range(8):
                if not pid_alive(shell_pid):
                    break
                await asyncio.sleep(0.2)
            if pid_alive(shell_pid):
                _kill_tree(shell_pid)

        if ch_id is not None:
            chan = bot.get_channel(ch_id)
            if chan is not None and _bot_deletable(chan):
                try:
                    await chan.delete(reason=f"cc-discord-remote: {reason}")
                except discord.Forbidden:
                    print(f"  can't close channel {ch_id} ({reason}): missing Manage Channels")
                except discord.HTTPException as e:
                    print(f"  couldn't delete channel {ch_id} ({reason}): {e}")
        print(f"  cleanly closed (ch={ch_id}, {reason}; claude {claude_pid}, shell {shell_pid})")
    finally:
        _closing_pids.discard(claude_pid)


async def _close_marker_watcher(interval: float = 2.0):
    """Watch close-markers/ for `/cc-close` signals and close those sessions fast.

    The slash command's helper (cc_close.ps1) writes close-markers/<claude_pid> (content
    = parent shell pid). We only act on a marker whose pid matches a TRACKED session —
    a stale/unknown pid is just removed (never kill an arbitrary reused pid). Stale
    markers from before this process started are cleared on boot so they can't mis-fire.
    """
    try:
        CLOSE_MARKER_DIR.mkdir(parents=True, exist_ok=True)
        for mf in CLOSE_MARKER_DIR.glob("*"):
            try:
                mf.unlink()
            except OSError:
                pass
    except Exception as e:
        print(f"  _close_marker_watcher boot-clear error: {e}")

    while True:
        try:
            await asyncio.sleep(interval)
            if not CLOSE_MARKER_DIR.is_dir():
                continue
            for mf in list(CLOSE_MARKER_DIR.glob("*")):
                try:
                    claude_pid = int(mf.name)
                except ValueError:
                    try:
                        mf.unlink()
                    except OSError:
                        pass
                    continue
                shell_pid = 0
                try:
                    shell_pid = int((mf.read_text(encoding="utf-8", errors="ignore").strip() or "0"))
                except Exception:
                    shell_pid = 0

                # Find the channel attached to this claude pid (in-memory, then DB).
                ch_id = next((c for c, p in list(attached_pids.items()) if p == claude_pid), None)
                if ch_id is None:
                    ch_id = next((c for c, p in sessions.all_attached() if p == claude_pid), None)

                # /cc-close must ALWAYS close the session — including the Discord channel
                # if one is bound, and the terminal regardless. The marker pid came from
                # cc_close.ps1 walking up to THIS session's own claude.exe, so it's never an
                # arbitrary reused pid. Close if the pid is live (kill tab) or has a channel.
                if ch_id is not None or pid_alive(claude_pid):
                    if not shell_pid:
                        shell_pid = _session_shell_pid.get(claude_pid) or (_get_parent_pid_sync(claude_pid) or 0)
                    await _close_session_cleanly(ch_id, claude_pid, shell_pid, "cc-close command")
                else:
                    print(f"  cc-close marker for dead/unknown pid {claude_pid} — ignoring")
                try:
                    mf.unlink()
                except OSError:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"  _close_marker_watcher error (continuing): {e}")


async def _outbox_watcher(interval: float = 3.0):
    """Watch outbox/ for files queued by `/cc-send` and upload them to the right channel.

    Filenames are `<claude_pid>__<original_name>`. Upload to the Discord channel attached
    to that claude pid, then delete the file. A file whose pid never gets a channel
    (truly-gone session) is dropped after a ~30s grace so the outbox can't pile up.
    """
    try:
        OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        print(f"  _outbox_watcher mkdir error: {e}")
    seen_no_channel: Dict[str, int] = {}  # filename → ticks waited for a channel to appear
    while True:
        try:
            await asyncio.sleep(interval)
            if not OUTBOX_DIR.is_dir():
                continue
            for f in list(OUTBOX_DIR.glob("*__*")):
                pidpart, _, orig = f.name.partition("__")
                try:
                    claude_pid = int(pidpart)
                except ValueError:
                    try:
                        f.unlink()
                    except OSError:
                        pass
                    continue
                ch_id = next((c for c, p in list(attached_pids.items()) if p == claude_pid), None)
                if ch_id is None:
                    ch_id = next((c for c, p in sessions.all_attached() if p == claude_pid), None)
                chan = bot.get_channel(ch_id) if ch_id else None
                if chan is None:
                    n = seen_no_channel.get(f.name, 0) + 1  # freshly-spawned session may lack a channel yet
                    seen_no_channel[f.name] = n
                    if n >= 10:  # ~30s
                        print(f"  outbox: no channel for pid {claude_pid} after 30s — dropping {orig}")
                        try:
                            f.unlink()
                        except OSError:
                            pass
                        seen_no_channel.pop(f.name, None)
                    continue
                seen_no_channel.pop(f.name, None)
                try:
                    size = f.stat().st_size
                    if size == 0:
                        await chan.send(f"⚠️ `{orig}` is empty — not sent.")
                    elif size > 25 * 1024 * 1024:
                        await chan.send(f"⚠️ `{orig}` is {size / 1024 / 1024:.1f} MB — over Discord's ~25 MB limit.")
                    else:
                        await chan.send(content=f"📤 `{orig}` — {size:,} bytes",
                                        file=discord.File(str(f), filename=orig))
                        print(f"  outbox: sent {orig} to channel {ch_id}")
                except discord.HTTPException as e:
                    print(f"  outbox upload failed for {orig}: {e}")
                try:
                    f.unlink()
                except OSError:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"  _outbox_watcher error (continuing): {e}")


async def _picker_watcher(interval: float = 1.5):
    """Surface a pending AskUserQuestion to Discord IN TIME by reading the live SCREEN.

    Claude Code buffers the question out of the JSONL until it's answered, so the
    JSONL-tailing mirror always shows it too late. This watcher scrapes every attached
    session's screen each tick (concurrently — _run_console_helper is temp-file isolated)
    and, when an AskUserQuestion picker is open, posts the question + the keypad so the
    user can answer from Discord before they answer in the terminal. Deduped per channel
    by the visible question text; reset when the picker leaves the screen.
    """
    async def _scrape(ch_id, pid):
        if not pid_alive(pid):
            return ch_id, None
        try:
            return ch_id, await _run_console_helper(pid, "", mode="look")
        except Exception:
            return ch_id, None

    while True:
        try:
            await asyncio.sleep(interval)
            items = list(attached_pids.items())
            if not items:
                continue
            for ch_id, screen in await asyncio.gather(*[_scrape(c, p) for c, p in items]):
                if not screen:
                    continue
                if not _screen_pending_question(screen):
                    _picker_rendered.pop(ch_id, None)  # picker gone → allow the next question
                    continue
                qtext = _extract_picker_text(screen)
                # Dedup on a NORMALIZED key (letters+digits only) so the blinking cursor /
                # moving option highlight doesn't change the text and make it re-post every
                # 1.5s tick. A genuinely new question (different options) still re-posts.
                key = re.sub(r"[^a-z0-9]+", "", qtext.lower())
                if not key or _picker_rendered.get(ch_id) == key:
                    continue
                _picker_rendered[ch_id] = key
                chan = bot.get_channel(ch_id)
                if chan is None:
                    continue
                primary = next(iter(ALLOWED_USERS), None)
                ping = f"<@{primary}> " if primary else ""
                try:
                    await send_chunked(
                        chan,
                        f"{ping}❓ **Claude is asking you something** — answer with the keypad "
                        f"below (↑/↓ to move · Enter to select):\n```\n{qtext}\n```",
                    )
                    await chan.send("⬇️ keypad:", view=RemoteKeypadView(ch_id))
                    print(f"  [picker] surfaced screen-detected question to channel {ch_id}")
                except discord.HTTPException as e:
                    print(f"  [picker] surface failed for {ch_id}: {e}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"  _picker_watcher error (continuing): {e}")


async def cmd_close(channel, channel_id, user_id, name: str = ""):
    """Detach, kill the terminal window, and delete the Discord channel.

    `!cc close`         — close THIS channel.
    `!cc close <name>`  — close the channel matching <name> (resolved against
                          guild channels by sanitized name). Refuses to fall
                          back to current channel if <name> doesn't match —
                          otherwise a typo would nuke the wrong room.
    """
    target = channel
    if name:
        sanitized = _sanitize_channel_name(name)
        match = discord.utils.get(channel.guild.text_channels, name=sanitized) if channel.guild else None
        if match is None:
            await channel.send(
                f"⚠️ No channel named `#{sanitized}` in this server. "
                f"Refusing to close — type `!cc close` (no name) to close this channel."
            )
            return
        target = match

    target_id = target.id
    if target_id in CONTROL_CHANNELS:
        await channel.send(
            f"⚠️ <#{target_id}> is an env-configured control channel — refusing to delete. "
            f"Remove it from `ALLOWED_CHANNEL_IDS` first if you really mean it."
        )
        return
    pid = attached_pids.pop(target_id, None)
    if pid:
        _closing_pids.add(pid)  # block the reconciler from re-channeling during teardown
    mt = mirror_tasks.pop(target_id, None)
    if mt and not mt.done():
        mt.cancel()
    # Delete the row (not just clear the pid) so a closed session stays closed — no
    # resume on the next reboot.
    try:
        sessions.conn.execute("DELETE FROM sessions WHERE channel_id = ?", (target_id,))
        sessions.conn.commit()
    except Exception:
        sessions.set_attached_pid(target_id, None)
    ALLOWED_CHANNELS.discard(target_id)

    kill_status = ""
    if pid:
        kill_status = await _close_terminal_for_pid(pid)

    try:
        scope = f"<#{target_id}>" if target_id != channel_id else "channel"
        info_bits = []
        if pid:
            info_bits.append(f"PID {pid}")
        if kill_status:
            info_bits.append(kill_status)
        suffix = f" — {' · '.join(info_bits)}" if info_bits else ""
        await channel.send(f"🔌 Closing {scope}{suffix}")
        await target.delete()
    except discord.Forbidden:
        await channel.send("⚠️ Bot needs **Manage Channels** to delete that channel.")
    except discord.HTTPException as e:
        await channel.send(f"⚠️ Couldn't delete channel: {e}")
    finally:
        if pid:
            _closing_pids.discard(pid)


async def cmd_sweep_channels(channel):
    """Delete bot-created hex-id channels whose claude.exe is gone."""
    if not channel.guild:
        await channel.send("Can only sweep inside a server.")
        return
    hex_re = re.compile(r"^[0-9a-f]{8}$")
    live_prefixes = {c.session_id[:8].lower() for c in list_running()}
    candidates = [
        ch for ch in channel.guild.text_channels
        if hex_re.match(ch.name) and ch.id not in CONTROL_CHANNELS
    ]
    if not candidates:
        await channel.send("🧹 No hex-id channels found.")
        return

    killed: list[str] = []
    skipped: list[str] = []
    for ch in candidates:
        if ch.name.lower() in live_prefixes:
            skipped.append(ch.name)
            continue
        try:
            await ch.delete(reason="cc-discord-remote: orphan hex-id channel sweep")
            sessions.conn.execute("DELETE FROM sessions WHERE channel_id = ?", (ch.id,))
            ALLOWED_CHANNELS.discard(ch.id)
            attached_pids.pop(ch.id, None)
            killed.append(ch.name)
        except discord.Forbidden:
            await channel.send(
                f"⚠️ Missing Manage Channels permission — can't delete `#{ch.name}`."
            )
            return
        except discord.HTTPException as e:
            print(f"  couldn't delete #{ch.name}: {e}")
    sessions.conn.commit()

    lines = []
    if killed:
        s = "s" if len(killed) > 1 else ""
        lines.append(
            f"🧹 Deleted **{len(killed)}** orphan hex channel{s}: "
            + ", ".join(f"`#{n}`" for n in killed)
        )
    if skipped:
        s = "s" if len(skipped) > 1 else ""
        lines.append(
            f"Kept **{len(skipped)}** hex channel{s} with a live claude.exe: "
            + ", ".join(f"`#{n}`" for n in skipped)
        )
    if not killed and not skipped:
        lines.append("Nothing to clean.")
    await channel.send("\n".join(lines))


async def cmd_cleanup(channel):
    """Sweep up orphan PowerShell windows whose `claude` invocation has already exited."""
    # Match cmd_launch / cmd_resume_spawn's launch pattern, and exclude `$PID`
    # (the running query process — it always self-matches because its argument
    # literal contains the pattern string).
    LAUNCH_MARKER = "-NoExit -ExecutionPolicy Bypass -Command claude"
    proc = await _asyncio.create_subprocess_exec(
        "powershell.exe", "-NoProfile", "-Command",
        "Get-CimInstance Win32_Process -Filter \"Name='powershell.exe'\" | "
        "Where-Object { $_.ProcessId -ne $PID -and "
        f"$_.CommandLine -like '*{LAUNCH_MARKER}*' }} | "
        "ForEach-Object { @{ Pid = $_.ProcessId } } | "
        "ConvertTo-Json -Compress",
        stdout=_asyncio.subprocess.PIPE,
        stderr=_asyncio.subprocess.PIPE,
        creationflags=0x08000000,
    )
    out, _ = await proc.communicate()
    raw = out.decode("utf-8", errors="replace").strip()
    if not raw:
        await channel.send("🧹 No claude-launched PowerShell windows found.")
        return
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        await channel.send(f"⚠️ Couldn't parse process list: `{raw[:200]}`")
        return
    if isinstance(data, dict):
        data = [data]

    # Build the set of PowerShell PIDs that currently own a live claude.exe —
    # those are healthy and must NOT be killed.
    live_parents = set()
    for c in list_running():
        ppid = _get_parent_pid_sync(c.pid)
        if ppid:
            live_parents.add(ppid)

    killed: list[int] = []
    skipped: list[int] = []
    for entry in data:
        ppid = entry.get("Pid")
        if not isinstance(ppid, int):
            continue
        if ppid in live_parents:
            skipped.append(ppid)
            continue
        if _kill_tree(ppid):
            killed.append(ppid)

    lines = []
    if killed:
        s = "s" if len(killed) > 1 else ""
        lines.append(
            f"🧹 Closed **{len(killed)}** orphan terminal window{s}: "
            + ", ".join(f"`{p}`" for p in killed)
        )
    if skipped:
        s = "s" if len(skipped) > 1 else ""
        lines.append(f"Left **{len(skipped)}** window{s} alone (live claude.exe inside).")
    if not killed and not skipped:
        lines.append("Nothing to clean up.")
    await channel.send("\n".join(lines))


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


async def cmd_esc(channel, channel_id):
    """Send a single Escape keystroke — dismisses /usage and other TUI dialogs."""
    pid = attached_pids.get(channel_id)
    if pid is None:
        await channel.send("Not attached. `!cc attach <name>` first.")
        return
    async with channel.typing():
        result = await _run_console_helper(pid, "", mode="esc")
    if "AttachConsole" in result and "failed" in result:
        await channel.send(f"⚠️ {result.strip()}")
    else:
        await channel.send("⎋ Escape sent.")


async def cmd_keys(channel, channel_id, user_id, sequence: str):
    """Raw passthrough: send a comma-separated key sequence to the attached terminal.

    Examples:
      !cc keys 1                       → number-key approval response
      !cc keys down,down,enter         → navigate a picker
      !cc keys space,tab,y,enter       → toggle a checkbox, jump to field, confirm

    Recognised tokens: enter, esc, up, down, left, right, tab, space, backspace,
    plus any single printable character.
    """
    if not sequence:
        await channel.send(
            "Usage: `!cc keys <comma-separated>` e.g. `!cc keys down,down,enter`"
        )
        return
    pid = attached_pids.get(channel_id)
    if pid is None:
        await channel.send("Not attached. `!cc attach <name>` first.")
        return
    async with channel.typing():
        result = await _run_console_helper(pid, sequence, mode="keys")
    sessions.audit(channel_id, user_id, "keys", sequence)
    if "failed" in result.lower() or "unknown key" in result.lower():
        await channel.send(f"⚠️ `{result.strip()}`")
    else:
        await channel.send(f"⌨️ Sent: `{sequence}`")


async def _render_piece(
    channel,
    kind: str,
    data: dict,
    pending_tools: Dict[str, tuple],
    pickers_rendered: set,
    resolved_tool_ids: set = frozenset(),
):
    """Render one parsed JSONL piece into Discord, pairing tools with their results.

    AskUserQuestion is special-cased to render eagerly on tool_use rather than
    waiting for the (paired) tool_result: the tool BLOCKS Claude until the user
    answers, so deferring the Discord message would hide the question from the
    user driving via phone, leaving the picker hanging indefinitely. Tracked
    in `pickers_rendered` so the eventual tool_result is shown as just the
    answer, not a re-render of the prompt.
    """
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
        name = data["name"]
        if name == "AskUserQuestion":
            # Claude Code buffers the question out of the JSONL until it's answered, so by
            # the time we read it here it's already RESOLVED — and the live picker was
            # surfaced from the screen by _picker_watcher. Don't re-post a stale picker.
            if tid and tid in resolved_tool_ids:
                pickers_rendered.add(tid)
                return
            # Eager render with @mention so the user notices the blocking prompt,
            # AND attach clickable option buttons so they can answer with a tap
            # instead of typing the number.
            print(f"  [mirror] eager-rendering AskUserQuestion tid={tid} to channel {channel.id}")
            preview = _format_tool_input(name, data["input"])
            primary = next(iter(ALLOWED_USERS), None)
            ping = f"<@{primary}> " if primary else ""
            # Pull the first question's option labels for the button menu.
            questions = (data["input"] or {}).get("questions") or []
            option_labels: list = []
            multi = False
            if questions and isinstance(questions[0], dict):
                multi = bool(questions[0].get("multiSelect"))
                for opt in (questions[0].get("options") or []):
                    if isinstance(opt, dict) and opt.get("label"):
                        option_labels.append(opt["label"])
            # Buttons for any single-select, single-question prompt. The pid is resolved
            # at CLICK time (the button handler already does attached_pids.get + errors if
            # gone), so DON'T gate the render on pid_here — that gate was suppressing the
            # keypad whenever the channel's attachment had momentarily drifted (the exact
            # "no keypad pops up" symptom). Multi-select / multi-question still fall back to
            # text + type-the-number.
            # ALWAYS attach a keypad. Single-select single-question → one tappable button
            # per option (the convenient case). Multi-select / multi-question → option
            # buttons don't map cleanly to the toggle/sequential TUI, so fall back to the
            # generic keypad (↑/↓ move, Enter confirm, Space toggles a multi-select) so the
            # user can still answer from Discord instead of getting no keypad at all.
            if option_labels and not multi and len(questions) == 1:
                view = AskUserQuestionView(channel.id, option_labels)
                hint = "tap an option below"
                choose_label = "⬇️ choose:"
            else:
                view = RemoteKeypadView(channel.id)
                hint = "use the keypad — ↑/↓ to move, Enter to confirm, Space to toggle (multi-select), then Enter"
                choose_label = "⬇️ keypad (answer each question in order):"
            try:
                # send_chunked can split long text; send the prompt body first,
                # then a final short line carrying the buttons so the view always
                # attaches to a delivered message.
                await send_chunked(channel, f"{ping}❓ **Claude needs your input** — {hint}:{preview}")
                await channel.send(choose_label, view=view)
                print(f"  [mirror] AskUserQuestion render succeeded for tid={tid} (view={type(view).__name__})")
            except Exception as e:
                print(f"  [mirror] AskUserQuestion render FAILED for tid={tid}: {type(e).__name__}: {e}")
                raise
            if tid:
                pickers_rendered.add(tid)
            return
        if tid:
            pending_tools[tid] = (name, data["input"])
        else:
            preview = _format_tool_input(name, data["input"])
            icon = "🔍" if name in READ_ONLY_TOOLS else "🛠️"
            await channel.send(f"{icon} `{name}` — {preview}")
    elif kind == "tool_result":
        tid = data.get("id")
        text = data["text"]
        err = data.get("is_error")
        shown = text[:400]
        ellip = "…" if len(text) > 400 else ""
        err_tag = " ❌" if err else ""
        if tid and tid in pickers_rendered:
            pickers_rendered.discard(tid)
            line = f"↳ **Answered:**{err_tag}\n```\n{shown}{ellip}\n```"
        elif tid and tid in pending_tools:
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
    # AskUserQuestion tool_use_ids we've already eagerly rendered to Discord;
    # used so the eventual tool_result is shown as "Answered: …" rather than
    # re-rendering the picker prompt. See _render_piece's special-case.
    pickers_rendered: set = set()
    # Tool-approval tracking: per-tool emit time, surfacing state, and the set of
    # tool_ids we already screen-checked (and decided no popup was visible) so we
    # don't re-poll the terminal every mirror tick.
    approval_emit_at: Dict[str, float] = {}            # tool_id → t_first_seen
    approval_meta: Dict[str, tuple] = {}               # tool_id → (name, input)
    surfaced_approvals: Dict[str, tuple] = {}          # tool_id → (msg, view)
    approval_no_popup: set = set()                     # tool_ids already screen-checked, no popup
    APPROVAL_DELAY = 4.0  # seconds before screen-check (was 3.0 — too aggressive)
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
                                tname = block.get("name", "?")
                                if tid:
                                    pending_tool_ids.add(tid)
                                    # Only non-readonly tools trigger Claude Code's
                                    # approval popup; readonly ones run silently.
                                    if tname not in READ_ONLY_TOOLS:
                                        approval_emit_at[tid] = time.time()
                                        approval_meta[tid] = (tname, block.get("input", {}))
                    elif t == "user":
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "tool_result":
                                tid = block.get("tool_use_id")
                                if tid:
                                    resolved_tool_ids.add(tid)
                                    # Tool resolved: close out any approval embed we surfaced.
                                    if tid in surfaced_approvals:
                                        appr_msg, appr_view = surfaced_approvals.pop(tid)
                                        for child in appr_view.children:
                                            child.disabled = True
                                        appr_view.stop()
                                        try:
                                            await appr_msg.edit(
                                                content=f"{appr_msg.content}\n→ _Resolved._",
                                                view=appr_view,
                                            )
                                        except discord.HTTPException:
                                            pass
                                    approval_emit_at.pop(tid, None)
                                    approval_meta.pop(tid, None)
                                    approval_no_popup.discard(tid)

                pieces = extract_user_facing(new_objs)
                for kind, data in pieces:
                    await _render_piece(channel, kind, data, pending_tools, pickers_rendered, resolved_tool_ids)
                    if kind == "text":
                        last_assistant_at = time.time()
                        pinged_for_turn = False

            # Two-stage approval surfacing: timing AND a screen-check. Many tools
            # (WebSearch, WebFetch on permissive configs) auto-approve but still
            # take 5-10 s to resolve. The timing heuristic alone would flood the
            # channel with useless approval embeds for those.
            now = time.time()
            for tid, emit_ts in list(approval_emit_at.items()):
                if tid in resolved_tool_ids or tid in surfaced_approvals:
                    continue
                if tid in approval_no_popup:
                    continue
                if now - emit_ts < APPROVAL_DELAY:
                    continue
                pid_for_screen = attached_pids.get(channel_id)
                if pid_for_screen is None:
                    # No PID to query — surface anyway; better to over-prompt than miss.
                    screen = ""
                    popup_visible = True
                else:
                    screen = await _run_console_helper(pid_for_screen, "", mode="look")
                    popup_visible = _screen_shows_approval_popup(screen)
                if not popup_visible:
                    approval_no_popup.add(tid)
                    continue
                tname, tinput = approval_meta.get(tid, ("?", {}))
                preview = _format_tool_input(tname, tinput)
                view = ToolApprovalView(channel_id, tid, tname)
                try:
                    sent = await channel.send(
                        f"🛑 <@{user_id}> **{tname}** wants approval — {preview}",
                        view=view,
                    )
                    view.message = sent
                    surfaced_approvals[tid] = (sent, view)
                except discord.HTTPException as e:
                    print(f"  approval embed send failed for {tid}: {e}")

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
                    # Turn appears complete — flush orphans, send a quiet "done" line.
                    # No @mention here: turn completions happen often enough that pinging
                    # on each one buries the user in notifications. Approvals still ping.
                    for _id, (name, inp) in list(pending_tools.items()):
                        preview = _format_tool_input(name, inp)
                        icon = "🔍" if name in READ_ONLY_TOOLS else "🛠️"
                        await channel.send(f"{icon} `{name}` — {preview}")
                    pending_tools.clear()
                    elapsed = time.time() - last_assistant_at
                    await channel.send(f"✅ _turn complete on `{label}`_")
                    pinged_for_turn = True

            await asyncio.sleep(0.5)
    except asyncio.CancelledError:
        return
    except Exception as e:
        try:
            await channel.send(f"⚠️ mirror loop error: `{type(e).__name__}: {e}`")
        except Exception:
            pass


# Claude Code shows "Compacting conversation…" (and a "/compact" affordance) while
# it compacts — auto-triggered when you resume a near-full session, or from a manual
# /compact. Typing into that busy input doesn't wait: the keystrokes land on the
# in-progress /compact line and the message comes out as `/compact "your message"`.
_COMPACTING_RE = re.compile(r"compacting\b", re.IGNORECASE)


async def _wait_while_compacting(channel, pid: int, max_wait: float = 240.0) -> None:
    """Hold until Claude Code is done compacting, so the message reaches a clean
    prompt instead of being appended to the running /compact command."""
    screen = await _run_console_helper(pid, "", mode="look")
    if not _COMPACTING_RE.search(screen):
        return
    try:
        await channel.send("⏳ Claude is compacting — holding your message until it finishes…")
    except Exception:
        pass
    deadline = time.time() + max_wait
    while time.time() < deadline:
        await asyncio.sleep(2.0)
        screen = await _run_console_helper(pid, "", mode="look")
        if not _COMPACTING_RE.search(screen):
            await asyncio.sleep(1.0)  # let the prompt redraw/settle after compaction
            return
    try:
        await channel.send("⚠️ Still compacting after a while — sending your message anyway.")
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

    # Slash commands are TUI-only (no JSONL writes). Use mode="send" so the
    # helper types AND polls the screen until it's been stable for ~1.5 s —
    # much more reliable than a fixed delay, especially for commands that need
    # a network roundtrip (e.g. /login) or render an interactive picker.
    is_slash = text.lstrip().startswith("/")
    mode = "send" if is_slash else "type"

    # If Claude Code is mid-compaction (common right after a resume), wait for it
    # to finish before typing — otherwise the message gets appended to the running
    # /compact and arrives as `/compact "the message"`. A real /compact command the
    # user sent on purpose still goes through (we only HOLD when already compacting).
    if not is_slash:
        await _wait_while_compacting(channel, pid)

    async with channel.typing():
        busy = False
        # We normally Esc before typing to dismiss any stray TUI menu/dialog that would
        # swallow the keystrokes. BUT Esc CANCELS an active turn — so if a message arrives
        # while Claude is mid-tool-use, the Esc interrupts it (the bug). Scrape once: if
        # Claude is working (its footer shows "esc to interrupt"), DON'T Esc — just type.
        # Claude Code queues a message typed mid-turn and runs it after, instead of
        # interrupting. Also never Esc an AskUserQuestion picker (Esc cancels it).
        if mode == "type":
            pre = await _run_console_helper(pid, "", mode="look")
            # Working = the spinner line, e.g. "✶ Choreographing… (2m 47s · ↓ 11.8k
            # tokens)". Match that elapsed-time+tokens pattern (the "esc to interrupt"
            # hint truncates at narrow widths, so don't rely on it alone).
            busy = bool(re.search(r"\(\d[\dms\s]*s\b[^)]*tokens", pre)) or "esc to interrupt" in pre.lower()
            picker = _pending_picker_tool(session_jsonl_path(info.cwd, info.session_id))
            if picker != "AskUserQuestion" and not busy:
                await _run_console_helper(pid, "", mode="esc")
        result = await _run_console_helper(pid, text, mode=mode)

        # Verify the submit. On the newer TUI the trailing Enter is sometimes dropped,
        # leaving the typed text sitting in the input box unsent ("my message didn't
        # send"). If the tail of what we typed is still in the bottom (input) area, the
        # Enter was lost — press it again. A spurious extra Enter at an empty prompt is a
        # harmless no-op. SKIP when Claude was busy: the message is queued (input box
        # clears), so the tail check doesn't apply and a re-press could disturb the queue.
        if mode == "type" and not busy:
            tail = " ".join(text.split())[-22:]
            for _ in range(2):
                if not tail:
                    break
                await asyncio.sleep(0.5)
                screen = await _run_console_helper(pid, "", mode="look")
                bottom = " ".join("\n".join(screen.splitlines()[-4:]).split())
                if tail in bottom:  # still in the input box → not submitted
                    print(f"  [send] Enter dropped for channel {channel_id}; re-pressing")
                    await _run_console_helper(pid, "", mode="enter")
                else:
                    break

    if "AttachConsole" in result and "failed" in result:
        await channel.send(f"⚠️ {result.strip()}")
        return True

    if is_slash:
        # Always attach a keypad to slash-command snapshots — the user almost
        # always wants a way to navigate back / Esc / scroll, even if the
        # snapshot is a static info page. The earlier heuristic was too clever
        # and would silently omit the keypad on detail views.
        body = result[-1800:] if result.strip() else "_(screen empty)_"
        formatted = f"```\n{body}\n```"
        view = RemoteKeypadView(channel_id)
        view.timeout = 600  # 10 min for auto-attached pads vs 1 h manual
        msg = await channel.send(formatted, view=view)
        view.message = msg
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
                    await channel.send(f"_done{cost_str} · {elapsed:.0f}s_")
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


async def cmd_get_file(channel, channel_id: int, user_id: int, arg: str):
    """Upload a file from the attached session's folder back to this Discord channel.

    `!cc get <path>` — `<path>` is relative to the session's working directory, or an
    absolute path. The inverse of dropping a file into the channel (which saves it INTO
    the cwd). Lets you pull a text file / report / log the terminal produced back to
    Discord (e.g. your phone).
    """
    arg = arg.strip().strip('"').strip("'")
    if not arg:
        await channel.send("Usage: `!cc get <path>` — sends a file from this session's folder to Discord (e.g. `!cc get notes.txt`).")
        return

    # Resolve the session's working directory for relative paths.
    cwd = None
    pid = attached_pids.get(channel_id)
    if pid:
        info = find_by_pid(pid)
        cwd = info.cwd if info else None
    if not cwd:
        _, db_cwd = sessions.get(channel_id)
        cwd = db_cwd

    p = Path(arg)
    if not p.is_absolute():
        if not cwd or cwd in ("?", "") or not Path(cwd).is_dir():
            await channel.send("⚠️ No working folder known for this channel — give an absolute path.")
            return
        p = Path(cwd) / arg

    try:
        if not p.exists():
            await channel.send(f"⚠️ Not found: `{p}`")
            return
        if p.is_dir():
            await channel.send(f"⚠️ `{p.name}` is a folder, not a file — give a file path (zip it first for a folder).")
            return
        size = p.stat().st_size
    except OSError as e:
        await channel.send(f"⚠️ Can't read `{arg}`: {e}")
        return

    UPLOAD_LIMIT = 25 * 1024 * 1024  # Discord's default per-file cap (non-boosted)
    if size == 0:
        await channel.send(f"⚠️ `{p.name}` is empty (0 bytes).")
        return
    if size > UPLOAD_LIMIT:
        await channel.send(
            f"⚠️ `{p.name}` is {size / 1024 / 1024:.1f} MB — over Discord's ~25 MB upload limit. "
            f"Zip/split it or share another way."
        )
        return

    sessions.audit(channel_id, user_id, "get_file", str(p))
    try:
        await channel.send(content=f"📄 `{p.name}` — {size:,} bytes",
                           file=discord.File(str(p), filename=p.name))
    except discord.HTTPException as e:
        await channel.send(f"⚠️ Upload failed ({e}). The server's file-size limit may be lower than this file.")


# ---------- dispatch --------------------------------------------------------

COMMANDS = {
    "help", "status", "where", "new", "cancel", "live", "detach", "look",
    "close", "cd", "sessions", "resume", "attach", "spawn", "launch", "usage", "esc",
    "keys", "pad", "cleanup", "sweep", "get", "file",
}


async def dispatch(channel, channel_id: int, user_id: int, text: str):
    """Route a raw '!cc <text>' to a command OR forward as a prompt.

    Strict first-word matching: if the first word is a known command, it is
    ALWAYS dispatched as that command (extras ignored) and never falls through
    to prompt mode. Prevents catastrophes like `!cc close X` being interpreted
    as a prompt that asks an SDK Claude to "close X" via Bash.
    """
    rest = text.strip()
    if not rest:
        await cmd_help(channel, user_id)
        return

    head, _, tail = rest.partition(" ")
    head = head.lower()
    tail = tail.strip()

    if head in COMMANDS:
        if head == "help":
            await cmd_help(channel, user_id)
        elif head in ("status", "where"):
            await cmd_status(channel, channel_id)
        elif head == "new":
            await cmd_new(channel, channel_id, user_id)
        elif head == "cancel":
            await cmd_cancel(channel, channel_id)
        elif head == "live":
            await cmd_live(channel)
        elif head == "detach":
            await cmd_detach(channel, channel_id)
        elif head == "look":
            await cmd_look(channel, channel_id)
        elif head == "esc":
            await cmd_esc(channel, channel_id)
        elif head == "close":
            await cmd_close(channel, channel_id, user_id, tail)
        elif head == "cd":
            await cmd_cd(channel, channel_id, user_id, tail.strip('"').strip("'"))
        elif head == "sessions":
            try:
                n = int(tail) if tail else 10
            except ValueError:
                n = 10
            await cmd_sessions(channel, n)
        elif head == "resume":
            await cmd_resume(channel, channel_id, user_id, tail)
        elif head == "attach":
            await cmd_attach(channel, channel_id, user_id, tail)
        elif head == "spawn":
            await cmd_spawn(channel, user_id, tail)
        elif head == "launch":
            await cmd_launch(channel, user_id, tail)
        elif head == "usage":
            await cmd_usage(channel)
        elif head == "keys":
            await cmd_keys(channel, channel_id, user_id, tail)
        elif head == "pad":
            await cmd_pad(channel, channel_id)
        elif head == "cleanup":
            await cmd_cleanup(channel)
        elif head == "sweep":
            await cmd_sweep_channels(channel)
        elif head in ("get", "file"):
            await cmd_get_file(channel, channel_id, user_id, tail)
        return

    # First word isn't a command — treat the whole thing as a prompt.
    if channel_id in attached_pids:
        await cmd_terminal_send(channel, channel_id, user_id, rest)
    else:
        await cmd_ask(channel, channel_id, user_id, rest)


# ---------- Discord wiring --------------------------------------------------

_pid_watcher_started = False
_auto_spawn_watcher_started = False
_orphan_sweeper_started = False
_close_marker_watcher_started = False
_outbox_watcher_started = False
_picker_watcher_started = False
_picker_rendered: Dict[int, str] = {}  # channel_id -> question text already surfaced (dedup)
_boot_done = False  # on_ready re-fires on every gateway reconnect; restore must run once
_auto_spawn_seen: set = set()  # PIDs we've already processed — populated on first poll
_closing_pids: set = set()  # claude pids being torn down (close) — reconciler skips these


async def _pid_watcher(interval: float = 15.0):
    """Poll attached PIDs; when a claude.exe exits, auto-close its Discord channel.

    Control rooms (env-configured ALLOWED_CHANNEL_IDS) are NEVER deleted — they
    just get their dead attachment cleared so the channel survives a Claude
    restart. Other channels get a goodbye message then are deleted.

    Sources from the UNION of in-memory attached_pids and sessions.db so we
    catch named channels created via auto-spawn whose cmd_attach didn't update
    the in-memory dict. Only clears state AFTER chan.delete() succeeds, so
    transient Discord errors (rate-limit, network blip) get retried next loop
    instead of orphaning the channel forever.
    """
    while True:
        try:
            await asyncio.sleep(interval)
            tracked: Dict[int, int] = dict(attached_pids)
            for ch_id, pid in sessions.all_attached():
                if pid is not None:
                    tracked.setdefault(ch_id, pid)

            live_by_pid = {c.pid: c for c in list_running()}
            for ch_id, pid in tracked.items():
                if pid_alive(pid):
                    # Record this claude's parent PowerShell pid once. When claude later
                    # dies we use it to tell an intentional /exit (shell stays alive thanks
                    # to `powershell -NoExit`) from an X-out / sleep / crash (shell dies too).
                    if ch_id not in CONTROL_CHANNELS and pid not in _session_shell_pid:
                        _session_shell_pid[pid] = _get_parent_pid_sync(pid) or 0
                    # Keep the persisted session_id pinned to the live process's
                    # CURRENT session. Claude Code advances/forks the session_id as a
                    # conversation grows (resume, auto-compaction); if the DB holds an
                    # older id, reboot-restore runs `claude --resume <stale-id>`, the
                    # on-disk JSONL check fails, and the session is silently dropped.
                    # Re-pinning every pass means a reboot always resumes the real,
                    # current conversation — "full session as is".
                    if ch_id not in CONTROL_CHANNELS:
                        live = live_by_pid.get(pid)
                        if live and live.session_id and live.session_id != "?":
                            stored_sid, stored_cwd = sessions.get(ch_id)
                            if live.session_id != stored_sid:
                                new_cwd = live.cwd or stored_cwd
                                sessions.set_identity(ch_id, live.session_id, new_cwd)
                                print(f"  sync: channel {ch_id} sid {str(stored_sid)[:8]} -> {live.session_id[:8]}")
                                # Claude forked the session_id (auto-compaction / in-place
                                # resume) and is now writing to a NEW JSONL. The mirror was
                                # tailing the OLD file and would go SILENT — that's the
                                # "Claude responds but nothing shows on Discord" bug. Re-point
                                # the mirror at the new file (start at its end so we don't
                                # replay compacted history) so responses keep flowing.
                                new_jsonl = session_jsonl_path(new_cwd, live.session_id)
                                chan = bot.get_channel(ch_id)
                                if chan is not None and new_jsonl.is_file():
                                    old_mt = mirror_tasks.pop(ch_id, None)
                                    if old_mt and not old_mt.done():
                                        old_mt.cancel()
                                    primary = next(iter(ALLOWED_USERS), 0)
                                    label = live.name or live.session_id[:8]
                                    mirror_tasks[ch_id] = asyncio.create_task(
                                        _mirror_loop(chan, ch_id, primary, new_jsonl,
                                                     new_jsonl.stat().st_size, label)
                                    )
                                    print(f"  mirror re-pointed to new jsonl for channel {ch_id} (sid {live.session_id[:8]})")
                        # Keep the Discord channel name in sync with the session's name
                        # (set via Claude Code's /rename) — "rename the chat → rename the
                        # channel". Only on an actual change, gated by a cooldown because
                        # Discord caps renames at 2/10min and discord.py blocks on the limit.
                        if live and live.name:
                            desired = _sanitize_channel_name(live.name)
                            chan = bot.get_channel(ch_id)
                            if (desired and chan is not None and chan.name != desired
                                    and _bot_deletable(chan)
                                    and time.time() - _last_channel_rename.get(ch_id, 0) >= CHANNEL_RENAME_COOLDOWN):
                                _last_channel_rename[ch_id] = time.time()
                                try:
                                    await chan.edit(name=desired, reason="cc-discord-remote: session renamed")
                                    print(f"  renamed channel {ch_id} -> #{desired}")
                                except discord.Forbidden:
                                    print(f"  can't rename channel {ch_id} (missing Manage Channels)")
                                except discord.HTTPException as e:
                                    print(f"  rename failed for channel {ch_id}: {e}")
                    continue

                # Before treating this as a terminal exit: the SAME session may
                # be alive under a NEW pid (an in-place Claude restart keeps the
                # session_id but changes the process). Rebind instead of closing —
                # this is what stops channels (and AskUserQuestion pickers) from
                # dying when the pid number changes out from under us.
                session_id, sess_cwd = sessions.get(ch_id)
                if session_id:
                    live = find_live_session(session_id)
                    new_pid = live.get("pid") if live else None
                    if new_pid and new_pid != pid and pid_alive(new_pid):
                        old_mt = mirror_tasks.pop(ch_id, None)
                        if old_mt and not old_mt.done():
                            old_mt.cancel()
                        attached_pids[ch_id] = new_pid
                        sessions.set_attached_pid(ch_id, new_pid)
                        chan = bot.get_channel(ch_id)
                        jsonl = session_jsonl_path(sess_cwd, session_id)
                        if chan is not None and jsonl.is_file():
                            label = live.get("name") or session_id[:8]
                            primary = next(iter(ALLOWED_USERS), 0)
                            mirror_tasks[ch_id] = asyncio.create_task(
                                _mirror_loop(chan, ch_id, primary, jsonl, jsonl.stat().st_size, label)
                            )
                        print(f"  rebound channel {ch_id}: PID {pid} -> {new_pid} (session {session_id[:8]})")
                        _auto_resume_attempts.pop(ch_id, None)
                        _session_shell_pid.pop(pid, None)
                        continue

                # NOTE: a "/exit = claude-dead + shell-alive" heuristic used to live here.
                # It FALSE-FIRED on claude.exe crashes / auto-updates (claude dies while the
                # `-NoExit` shell survives) and deleted LIVE sessions (lost personalclaw on
                # 2026-06-04). Removed. Intentional close now goes only through /cc-close
                # (close-marker, reliable) or `!cc close`. An unexpected claude death here
                # falls through to auto-resume below — so a crash/update recovers itself.
                # Capture the parent shell: if this was a /exit, the `-NoExit` shell is still
                # idle in its old tab, and we close that orphan tab after the resume succeeds.
                orphan_shell_pid = _session_shell_pid.pop(pid, 0)

                # The session isn't alive anywhere, but it's still resumable on disk.
                # That's the overnight-sleep case: closing the laptop killed every
                # console while the (pythonw) bot kept running, so the windows are
                # gone but the conversations survive as JSONL. Auto-RESUME into the
                # SAME channel — same flow as reboot-restore — instead of deleting the
                # channel. This is the auto-resume the user expects after a sleep cycle.
                # Capped at AUTO_RESUME_MAX_ATTEMPTS so a session that genuinely can't
                # resume doesn't respawn a PowerShell window every loop forever.
                if (session_id and sess_cwd and sess_cwd not in ("?", "")
                        and ch_id not in CONTROL_CHANNELS
                        and Path(sess_cwd).is_dir()
                        and session_jsonl_path(sess_cwd, session_id).is_file()):
                    chan = bot.get_channel(ch_id)
                    if chan is not None and _bot_deletable(chan):
                        if _auto_resume_attempts.get(ch_id, 0) < AUTO_RESUME_MAX_ATTEMPTS:
                            # Cancel the dead mirror; the resume helper starts a fresh one.
                            mt = mirror_tasks.pop(ch_id, None)
                            if mt and not mt.done():
                                mt.cancel()
                            attached_pids.pop(ch_id, None)
                            sessions.set_attached_pid(ch_id, None)
                            try:
                                await chan.send("🔄 Terminal window closed — auto-resuming this session…")
                            except Exception:
                                pass
                            primary = next(iter(ALLOWED_USERS), 0)
                            ok = False
                            try:
                                ok = await _resume_into_existing_channel(
                                    chan, ch_id, session_id, sess_cwd, primary
                                )
                            except Exception as e:
                                print(f"  auto-resume error for channel {ch_id}: {e}")
                            if ok:
                                _auto_resume_attempts.pop(ch_id, None)
                                print(f"  auto-resumed channel {ch_id} (PID {pid} died, session {session_id[:8]})")
                                if orphan_shell_pid and pid_alive(orphan_shell_pid):
                                    # /exit left the old -NoExit shell idle in its tab; send it
                                    # `exit` so WT closes it → the restart is tidy (one new tab,
                                    # no leftover). Best-effort.
                                    try:
                                        await _run_console_helper(orphan_shell_pid, "exit", mode="type")
                                    except Exception:
                                        pass
                            else:
                                n = _auto_resume_attempts.get(ch_id, 0) + 1
                                _auto_resume_attempts[ch_id] = n
                                print(f"  auto-resume attempt {n}/{AUTO_RESUME_MAX_ATTEMPTS} "
                                      f"failed for channel {ch_id} (session {session_id[:8]})")
                            # Whether it worked or not, don't fall through to close —
                            # success is attached; failure retries next loop until the cap.
                            continue
                        else:
                            print(f"  auto-resume gave up for channel {ch_id} after "
                                  f"{AUTO_RESUME_MAX_ATTEMPTS} tries — closing channel")
                            _auto_resume_attempts.pop(ch_id, None)
                            # Fall through to the normal close path below.

                # Cancel mirror task immediately — it can't drive a dead process.
                mt = mirror_tasks.pop(ch_id, None)
                if mt and not mt.done():
                    mt.cancel()

                if ch_id in CONTROL_CHANNELS:
                    attached_pids.pop(ch_id, None)
                    sessions.set_attached_pid(ch_id, None)
                    continue

                chan = bot.get_channel(ch_id)
                if chan is None:
                    # Discord channel already gone (deleted out-of-band). Just clean DB.
                    attached_pids.pop(ch_id, None)
                    ALLOWED_CHANNELS.discard(ch_id)
                    sessions.conn.execute("DELETE FROM sessions WHERE channel_id = ?", (ch_id,))
                    sessions.conn.commit()
                    continue

                if not _bot_deletable(chan):
                    # Human channel (notifications etc.) that got tracked — untrack,
                    # never delete.
                    attached_pids.pop(ch_id, None)
                    sessions.set_attached_pid(ch_id, None)
                    continue

                try:
                    await chan.send(f"🪦 Terminal exited (PID {pid} gone) — closing this channel.")
                except Exception:
                    pass  # Goodbye message is nice-to-have, don't block deletion on it.

                try:
                    await chan.delete(reason="cc-discord-remote: terminal exited")
                except discord.Forbidden:
                    print(f"  can't auto-close channel {ch_id} (missing Manage Channels)")
                    continue  # Don't clear state — retry next loop in case perms come back.
                except discord.HTTPException as e:
                    print(f"  couldn't auto-close channel {ch_id}: {e}")
                    continue  # Don't clear state — retry next loop.

                attached_pids.pop(ch_id, None)
                ALLOWED_CHANNELS.discard(ch_id)
                sessions.conn.execute("DELETE FROM sessions WHERE channel_id = ?", (ch_id,))
                sessions.conn.commit()
                print(f"  auto-closed channel {ch_id} (PID {pid} died)")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"  _pid_watcher error (continuing): {e}")


async def _orphan_channel_sweeper(interval: float = 60.0):
    """Background equivalent of `!cc sweep`: every minute, delete hex-id channels
    whose 8-char prefix doesn't match a live claude.exe.

    Catches the orphans that slip past `_pid_watcher` because they were never
    inserted into the `sessions` DB (auto-spawn watcher creates the Discord
    channel but `cmd_attach` refused with "already attached elsewhere"). Without
    this, hex-id orphans pile up forever in the sidebar.
    """
    hex_re = re.compile(r"^[0-9a-f]{8}$")
    while True:
        try:
            await asyncio.sleep(interval)
            live_prefixes = {c.session_id[:8].lower() for c in list_running()}

            guilds_to_check = set()
            for cid in CONTROL_CHANNELS:
                ch = bot.get_channel(cid)
                if ch and getattr(ch, "guild", None):
                    guilds_to_check.add(ch.guild)

            for guild in guilds_to_check:
                for ch in guild.text_channels:
                    if ch.id in CONTROL_CHANNELS:
                        continue
                    if not hex_re.match(ch.name):
                        continue
                    if ch.name.lower() in live_prefixes:
                        continue
                    try:
                        await ch.delete(
                            reason="cc-discord-remote: orphan hex-id channel auto-sweep"
                        )
                        sessions.conn.execute(
                            "DELETE FROM sessions WHERE channel_id = ?", (ch.id,)
                        )
                        ALLOWED_CHANNELS.discard(ch.id)
                        attached_pids.pop(ch.id, None)
                        sessions.conn.commit()
                        print(f"  auto-swept orphan channel #{ch.name}")
                    except discord.Forbidden:
                        pass
                    except discord.HTTPException as e:
                        print(f"  couldn't auto-sweep #{ch.name}: {e}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"  _orphan_channel_sweeper error (continuing): {e}")


# Skip auto-attach for claude.exe processes younger than this. Filters out the
# short-lived scrapers / `claude --version` / IDE probes that spawn a session
# JSON for a few seconds and exit — would otherwise produce a perpetual
# attached→exited→closing-channel notification loop. Real user sessions take
# longer than this to start typing anyway.
AUTO_SPAWN_MIN_AGE_SECONDS = 30


async def _auto_spawn_watcher(interval: float = 15.0):
    """RECONCILER — ensure EVERY live terminal session ALWAYS has exactly one Discord
    channel, and collapse race duplicates.

    On every pass, for each live claude.exe session: if it has no channel (its pid isn't
    attached anywhere, and no other live pid for the same session_id is attached) and it's
    past the min-age, create + attach a channel. This covers brand-new sessions AND
    sessions orphaned mid-run (e.g. a channel torn down by a manual-restart-vs-auto-resume
    race — the symptom where a session "doesn't come up in Discord"). It's idempotent:
    already-attached or being-closed sessions are skipped. Every ~4th pass it also sweeps
    duplicate channels left by races. This is the "sessions always come up" guarantee.
    """
    primary_user = next(iter(ALLOWED_USERS), 0)
    pass_count = 0

    while True:
        try:
            await asyncio.sleep(interval)
            pass_count += 1
            running = list_running()
            now_ms = time.time() * 1000
            min_age_ms = AUTO_SPAWN_MIN_AGE_SECONDS * 1000
            attached_pid_set = set(attached_pids.values())
            # session_ids already bound to a LIVE channel — don't make a 2nd channel for
            # the same session under another pid (the _pid_watcher rebind owns that case).
            attached_sids = {
                c.session_id for c in running
                if c.pid in attached_pid_set and c.session_id
            }

            guild = None
            for cid in CONTROL_CHANNELS:
                ch = bot.get_channel(cid)
                if ch and getattr(ch, "guild", None):
                    guild = ch.guild
                    break

            if guild is not None:
                for c in running:
                    if c.pid in _closing_pids:
                        continue  # being torn down by /cc-close / !cc close
                    if c.pid in attached_pid_set:
                        continue  # already has a channel
                    if c.session_id and c.session_id in attached_sids:
                        continue  # same session already channelled under another pid
                    if c.started_at_ms is not None and now_ms - c.started_at_ms < min_age_ms:
                        continue  # too young — let an in-flight resume/rebind settle first
                    if c.pid in attached_pids.values():
                        continue  # final recheck (a parallel attach may have claimed it)
                    channel_name = _sanitize_channel_name(c.name or c.session_id[:8])
                    try:
                        new_chan = await guild.create_text_channel(
                            name=channel_name, category=_terminal_category(guild),
                        )
                    except discord.Forbidden:
                        print(f"  reconcile: missing Manage Channels in guild {guild.id}")
                        break
                    except discord.HTTPException as e:
                        print(f"  reconcile: couldn't create channel for PID {c.pid}: {e}")
                        continue
                    ALLOWED_CHANNELS.add(new_chan.id)
                    try:
                        await cmd_attach(new_chan, new_chan.id, primary_user, str(c.pid))
                        print(f"  reconcile: channel #{channel_name} for live session PID {c.pid}")
                    except Exception as e:
                        print(f"  reconcile: attach failed for PID {c.pid}: {e}")

            # Periodically collapse duplicate channels a restart race may have left.
            if pass_count % 4 == 0:
                try:
                    await _sweep_duplicate_terminal_channels()
                except Exception as e:
                    print(f"  reconcile: dedup sweep error: {e}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"  _auto_spawn_watcher error (continuing): {e}")


async def _resume_into_existing_channel(chan, ch_id: int, session_id: str, cwd: str, user_id: int) -> bool:
    """Relaunch `claude --resume <session_id>` in `cwd` and attach the EXISTING
    Discord channel `ch_id` to the new process. `claude --resume` forks a new
    session_id, so we read it back from the session registry and update the DB
    row, then attach. Returns True on success."""
    before = _list_claude_pids()
    try:
        _spawn_session_window(f"claude --resume {session_id}", cwd, title=session_id[:8])
    except Exception as e:
        print(f"  restore: couldn't launch resume for {session_id[:8]}: {e}")
        return False

    new_pid = None
    deadline = time.time() + 30
    while time.time() < deadline:
        await asyncio.sleep(1)
        diff = _list_claude_pids() - before
        if diff:
            new_pid = max(diff)
            break
    if not new_pid:
        print(f"  restore: no claude.exe appeared for {session_id[:8]}")
        return False

    _auto_spawn_seen.add(new_pid)
    # Walk trust + resume prompts. Picks "Resume full session as-is" (NOT the summary
    # default) and polls the screen, so a slow post-reboot startup doesn't leave the
    # tab stuck on the picker. This is what makes reboot = "full session as is".
    await asyncio.sleep(2)
    status = await _drive_resume_startup(new_pid)
    print(f"  restore: startup for {session_id[:8]} -> {status}")

    # Read back the NEW forked session_id from the registry so future rebinds work.
    sessions_dir = Path.home() / ".claude" / "sessions"
    new_sid = session_id
    sdeadline = time.time() + 30
    while time.time() < sdeadline:
        await asyncio.sleep(0.5)
        sj = sessions_dir / f"{new_pid}.json"
        if sj.is_file():
            try:
                new_sid = json.loads(sj.read_text(encoding="utf-8")).get("sessionId", session_id)
            except Exception:
                pass
            break

    sessions.set_identity(ch_id, new_sid, cwd)
    try:
        await cmd_attach(chan, ch_id, user_id, str(new_pid))
    except Exception as e:
        print(f"  restore: attach failed for PID {new_pid}: {e}")
        return False
    print(f"  restored terminal: channel {ch_id} resumed {session_id[:8]} -> PID {new_pid} (new sid {new_sid[:8]})")
    return True


async def _adopt_orphan_live_sessions():
    """Create channels for live, NAMED claude sessions that have no Discord
    channel — e.g. sessions that were alive through a reboot but whose channels
    were deleted. Without this they'd stay invisible (the auto-spawn watcher
    seeds already-running PIDs as 'seen' on its first pass and never makes them
    a channel)."""
    primary_user = next(iter(ALLOWED_USERS), 0)
    guild = None
    for cid in CONTROL_CHANNELS:
        ch = bot.get_channel(cid)
        if ch and getattr(ch, "guild", None):
            guild = ch.guild
            break
    if guild is None:
        return
    attached_now = set(attached_pids.values())
    adopted_sids: set = set()
    for c in list_running():
        if c.pid in attached_now:
            adopted_sids.add(c.session_id)
            continue
        if not c.name:
            continue  # skip throwaway/unnamed sessions
        if c.session_id in adopted_sids:
            continue
        adopted_sids.add(c.session_id)
        _auto_spawn_seen.add(c.pid)  # keep the auto-spawn watcher from double-creating
        channel_name = _sanitize_channel_name(c.name or c.session_id[:8])
        try:
            new_chan = await guild.create_text_channel(
                name=channel_name, category=_terminal_category(guild)
            )
        except discord.Forbidden:
            print("  adopt: missing Manage Channels")
            return
        except discord.HTTPException as e:
            print(f"  adopt: couldn't create channel for PID {c.pid}: {e}")
            continue
        ALLOWED_CHANNELS.add(new_chan.id)
        try:
            await cmd_attach(new_chan, new_chan.id, primary_user, str(c.pid))
            print(f"  adopted live session #{channel_name} (PID {c.pid})")
        except Exception as e:
            print(f"  adopt: attach failed for PID {c.pid}: {e}")
        await asyncio.sleep(1)


async def _sweep_duplicate_terminal_channels():
    """Remove orphan DUPLICATE channels in the terminal category — copies left
    behind by past reboots (same name as a live/tracked channel but not attached
    and not in the DB). Conservative: only deletes a same-named duplicate when a
    tracked channel of that name exists to keep, so we never remove a unique
    channel."""
    guild = None
    for cid in CONTROL_CHANNELS:
        ch = bot.get_channel(cid)
        if ch and getattr(ch, "guild", None):
            guild = ch.guild
            break
    if guild is None:
        return
    cat = _terminal_category(guild)
    if cat is None:
        return
    db_tracked = {r[0] for r in sessions.conn.execute("SELECT channel_id FROM sessions")}
    tracked = set(attached_pids.keys()) | db_tracked

    by_name: Dict[str, list] = {}
    for chan in cat.channels:
        if getattr(chan, "type", None) is not None and not hasattr(chan, "send"):
            continue  # skip non-text (voice etc.)
        by_name.setdefault(chan.name, []).append(chan)

    for name, chans in by_name.items():
        if len(chans) < 2:
            continue
        if not any(c.id in tracked for c in chans):
            continue  # none tracked — don't guess which to keep
        for c in chans:
            if c.id in tracked or c.id in CONTROL_CHANNELS:
                continue
            try:
                await c.delete(reason="cc-discord-remote: duplicate orphan terminal channel")
                ALLOWED_CHANNELS.discard(c.id)
                print(f"  swept duplicate terminal channel #{name} ({c.id})")
            except Exception as e:
                print(f"  couldn't sweep duplicate #{name} ({c.id}): {e}")


async def _dedup_session_channels():
    """If two+ channels share one session_id and one of them is currently attached
    (live), the others are stale leftovers from a past reboot that resumed the
    session into a fresh channel instead of reusing the old one. Delete the idle
    duplicates (Discord channel + DB row) so each terminal has exactly ONE channel.

    Why this matters: two channels on one session = the same terminal mirrored
    twice = every message posted twice. Safe by construction — only deletes a
    bot-managed channel when a LIVE sibling for that session exists; never touches
    the attached channel, control channels, or human (non-deletable) channels."""
    by_sid: Dict[str, list] = {}
    for ch_id, sid in sessions.conn.execute(
        "SELECT channel_id, session_id FROM sessions WHERE session_id IS NOT NULL AND session_id != ''"
    ):
        by_sid.setdefault(sid, []).append(ch_id)
    changed = False
    for sid, ch_ids in by_sid.items():
        if len(ch_ids) < 2:
            continue
        keep = {c for c in ch_ids if c in attached_pids}
        if not keep:
            continue  # none live — don't guess which to keep
        for ch_id in ch_ids:
            if ch_id in keep or ch_id in CONTROL_CHANNELS:
                continue
            chan = bot.get_channel(ch_id)
            if chan is not None and not _bot_deletable(chan):
                sessions.conn.execute("DELETE FROM sessions WHERE channel_id = ?", (ch_id,))
                changed = True
                continue  # human channel that shares a session — untrack, never delete
            if chan is not None:
                try:
                    await chan.delete(reason="cc-discord-remote: duplicate channel for live session")
                    print(f"  dedup: deleted duplicate channel {ch_id} for live session {sid[:8]}")
                except Exception as e:
                    print(f"  dedup: couldn't delete {ch_id}: {e}")
            sessions.conn.execute("DELETE FROM sessions WHERE channel_id = ?", (ch_id,))
            ALLOWED_CHANNELS.discard(ch_id)
            changed = True
    if changed:
        sessions.conn.commit()


async def _restore_terminals_on_boot():
    """After a reboot every claude.exe died. For each channel with a resumable
    session_id but no live process, resume it and re-attach to the SAME channel.
    Channels survive a reboot in Discord (the bot died before it could delete
    them), so we reuse them — no channel churn, stable ids."""
    primary_user = next(iter(ALLOWED_USERS), 0)
    restored_sids: set = set()
    # Seed from channels the on_ready restore loop already attached, so we don't
    # re-attach a SECOND (old, leftover) channel to a session/PID that's already
    # claimed. After `claude --resume` the session_id forks, so an old channel's
    # stored session_id can differ from the live one while pointing at the SAME
    # live PID — hence we guard on the PID, not just the session_id. Two channels
    # mirroring one terminal = every message posted twice (the duplicate bug).
    claimed_pids: set = set(attached_pids.values())
    for a_ch_id in list(attached_pids.keys()):
        row = sessions.conn.execute(
            "SELECT session_id FROM sessions WHERE channel_id = ?", (a_ch_id,)
        ).fetchone()
        if row and row[0]:
            restored_sids.add(row[0])
    for ch_id, session_id, cwd in sessions.all_resumable():
        if ch_id in CONTROL_CHANNELS:
            continue  # command room (SDK channel), not a terminal — never resume
        if ch_id in attached_pids:
            restored_sids.add(session_id)
            continue  # already live / re-attached by the on_ready restore loop
        if session_id in restored_sids:
            continue  # another channel already resumed this exact session
        chan = bot.get_channel(ch_id)
        if chan is None:
            continue
        if not cwd or cwd in ("?", "") or not Path(cwd).is_dir():
            print(f"  restore: skipping channel {ch_id} — cwd {cwd!r} not present")
            continue
        # Verify the session actually exists on disk — `claude --resume <id>`
        # errors with "No conversation found" otherwise (stale SDK ids, pruned
        # transcripts). Skip + leave the channel alone rather than spawn a
        # doomed console.
        if not session_jsonl_path(cwd, session_id).is_file():
            print(f"  restore: session {session_id[:8]} JSONL missing in {cwd} — skipping")
            continue
        restored_sids.add(session_id)
        # If the session is somehow already live (e.g. user relaunched it), just attach.
        live = find_live_session(session_id)
        if live and pid_alive(live.get("pid")):
            live_pid = live["pid"]
            if live_pid in claimed_pids:
                # Another channel is already mirroring this exact terminal. Attaching
                # here too would double every message. Leave this leftover channel idle.
                print(f"  restore: PID {live_pid} already attached elsewhere — not re-attaching channel {ch_id}")
                continue
            try:
                await cmd_attach(chan, ch_id, primary_user, str(live_pid))
                claimed_pids.add(live_pid)
                print(f"  restore: re-attached channel {ch_id} to live PID {live_pid}")
            except Exception as e:
                print(f"  restore: re-attach failed for channel {ch_id}: {e}")
            continue
        try:
            await chan.send("🔄 Restoring this terminal after reboot…")
        except Exception:
            pass
        await _resume_into_existing_channel(chan, ch_id, session_id, cwd, primary_user)
        # Stagger launches so a reboot with many tabs doesn't spawn N consoles at once.
        await asyncio.sleep(2)


@bot.event
async def on_ready():
    global _boot_done, _pid_watcher_started, _auto_spawn_watcher_started, _orphan_sweeper_started, _close_marker_watcher_started, _outbox_watcher_started, _picker_watcher_started
    print(f"Bot online as {bot.user}")
    print(f"  allowed users:    {sorted(ALLOWED_USERS) or '(none — bot will reject everyone)'}")
    print(f"  allowed channels: {sorted(ALLOWED_CHANNELS) or '(any)'}")
    print(f"  default cwd:      {DEFAULT_CWD}")

    # Every channel we've ever tracked stays in the allowlist — even orphaned ones —
    # so the user can always run `!cc close` to clean them up. Idempotent; safe to
    # re-run on every reconnect.
    for row in sessions.conn.execute("SELECT channel_id FROM sessions"):
        ALLOWED_CHANNELS.add(row[0])

    # on_ready fires again after every gateway RESUME/reconnect — NOT just at launch.
    # The restore/mirror-spawn block below starts one _mirror_loop per channel; on a
    # reconnect those loops are still alive in memory, so re-running it would spawn a
    # SECOND (third, …) mirror per channel and every terminal message would be posted
    # twice (the exact "duplicate messages on Discord" bug). Run it once per process.
    if _boot_done:
        print("  (reconnect — boot restore already done; mirrors still running)")
        return
    _boot_done = True

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
            # PID is dead (almost always: the machine rebooted). Clear the stale
            # pid but DON'T announce or delete — if this channel has a resumable
            # session_id, _restore_terminals_on_boot will bring it back below.
            sessions.set_attached_pid(ch_id, None)
            continue
        attached_pids[ch_id] = pid
        seen_pids.add(pid)
        ALLOWED_CHANNELS.add(ch_id)
        # Persist session identity so this channel is restorable on the NEXT
        # reboot (older rows attached pre-session_id only stored attached_pid).
        sessions.set_identity(ch_id, info.session_id, info.cwd)
        jsonl = session_jsonl_path(info.cwd, info.session_id)
        if jsonl.is_file():
            label = info.name or info.session_id[:8]
            mirror_tasks[ch_id] = asyncio.create_task(
                _mirror_loop(chan, ch_id, primary_user, jsonl, jsonl.stat().st_size, label)
            )
            print(f"  restored attachment: channel {ch_id} -> PID {pid} ({label})")

    # Auto-delete orphaned channels: previously-tracked channels with no active attachment
    # and no role as an env-configured control channel. Channels with a resumable
    # session_id are SKIPPED here — _restore_terminals_on_boot will bring them back.
    orphan_rows = [
        (r[0], r[1]) for r in sessions.conn.execute(
            "SELECT channel_id, session_id FROM sessions WHERE attached_pid IS NULL"
        )
        if r[0] not in CONTROL_CHANNELS
    ]
    for ch_id, session_id in orphan_rows:
        if session_id:
            continue  # resumable — leave it for the boot-restore task
        chan = bot.get_channel(ch_id)
        if chan is None:
            # Discord channel already gone — just clean the DB row.
            sessions.conn.execute("DELETE FROM sessions WHERE channel_id = ?", (ch_id,))
            continue
        if not _bot_deletable(chan):
            # Not a bot-managed channel (e.g. notifications, a human channel that
            # somehow got tracked). Stop tracking it but NEVER delete it.
            sessions.conn.execute("DELETE FROM sessions WHERE channel_id = ?", (ch_id,))
            print(f"  untracking non-managed channel {ch_id} (#{getattr(chan,'name','?')}) — not deleting")
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

    # Reboot restore: resume every channel that has a session_id but no live
    # process, re-attaching to the SAME Discord channel. Runs first so that
    # offline-replay (below) sees the restored attachments.
    async def _boot_then_replay():
        try:
            await _restore_terminals_on_boot()
            await _adopt_orphan_live_sessions()
            await _dedup_session_channels()
            await _sweep_duplicate_terminal_channels()
        finally:
            await _replay_offline_messages()
    asyncio.create_task(_boot_then_replay())

    # Sync slash commands to each guild the allowed channels live in (instant per-guild).
    if not _pid_watcher_started:
        _pid_watcher_started = True
        asyncio.create_task(_pid_watcher())
    if not _auto_spawn_watcher_started:
        _auto_spawn_watcher_started = True
        asyncio.create_task(_auto_spawn_watcher())
    if not _orphan_sweeper_started:
        _orphan_sweeper_started = True
        asyncio.create_task(_orphan_channel_sweeper())
    if not _close_marker_watcher_started:
        _close_marker_watcher_started = True
        asyncio.create_task(_close_marker_watcher())
    if not _outbox_watcher_started:
        _outbox_watcher_started = True
        asyncio.create_task(_outbox_watcher())
    if not _picker_watcher_started:
        _picker_watcher_started = True
        asyncio.create_task(_picker_watcher())

    synced_guilds = set()
    # Snapshot: the concurrent _boot_then_replay task (channel adoption/restore) can
    # add to ALLOWED_CHANNELS while this loop awaits tree.sync — iterating the live set
    # raised "Set changed size during iteration" and aborted the rest of on_ready.
    for chan_id in list(ALLOWED_CHANNELS):
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


async def _process_message(message: discord.Message) -> None:
    """Shared message handler used both live (on_message) and on startup (catchup)."""

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


_on_message_lock = asyncio.Lock()


@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return
    if message.author.id not in ALLOWED_USERS:
        return

    # Dedup against gateway RESUMEs that re-deliver messages we've already
    # processed (discord.py is supposed to handle this, but in practice it
    # sometimes refires on_message after a WebSocket reconnect). Snowflake
    # ids are monotonic per channel; if our persisted cursor is already at
    # or past this id, we've handled it. Lock makes the check-and-set atomic
    # so two concurrent invocations for the same msg can't both pass the
    # check before the cursor advances. Processing happens outside the lock.
    async with _on_message_lock:
        last_id = sessions.get_last_msg_id(message.channel.id) or 0
        if message.id <= last_id:
            return
        sessions.set_last_msg_id(message.channel.id, message.id)

    await _process_message(message)


async def _replay_offline_messages() -> None:
    """For each tracked channel, replay messages from allowed users that arrived
    while the bot was offline.

    Iterates Discord history since the last persisted last_msg_id and runs each
    qualifying message through the same handler as live traffic. Marks each
    message as seen BEFORE processing so a crash mid-replay doesn't loop forever
    on a poison message."""
    channels_to_replay = list(ALLOWED_CHANNELS)
    total_replayed = 0
    for ch_id in channels_to_replay:
        chan = bot.get_channel(ch_id)
        if chan is None:
            continue
        last_id = sessions.get_last_msg_id(ch_id)
        if last_id is None:
            # First time we've ever seen this channel — establish a baseline at the
            # latest message so we don't blast through old history on first install.
            try:
                async for last_msg in chan.history(limit=1):
                    sessions.set_last_msg_id(ch_id, last_msg.id)
                    break
            except Exception:
                pass
            continue

        try:
            anchor = discord.Object(id=last_id)
            queued = [m async for m in chan.history(after=anchor, limit=None, oldest_first=True)]
        except discord.Forbidden:
            continue
        except Exception as e:
            print(f"  catchup: history fetch failed for channel {ch_id}: {e}")
            continue

        eligible = [m for m in queued if m.author.id in ALLOWED_USERS and m.author != bot.user]
        if not eligible:
            # Still advance the cursor past any bot/non-allowed messages so we don't
            # refetch them every restart.
            if queued:
                sessions.set_last_msg_id(ch_id, queued[-1].id)
            continue

        # Re-read the cursor right before processing each message to avoid
        # double-handling: this task runs concurrently with on_message, so a
        # live arrival between our get_last_msg_id and chan.history call could
        # land in `queued` while also being processed by on_message. Discord
        # snowflakes are monotonic, so msg.id <= cursor means already handled.
        announced = False
        for msg in eligible:
            current_cursor = sessions.get_last_msg_id(ch_id) or 0
            if msg.id <= current_cursor:
                continue
            if not announced:
                remaining = sum(
                    1 for m in eligible
                    if m.id > (sessions.get_last_msg_id(ch_id) or 0)
                )
                try:
                    await chan.send(f"📨 Catching up on {remaining} message(s) sent while I was offline…")
                except Exception:
                    pass
                announced = True
            sessions.set_last_msg_id(ch_id, msg.id)
            try:
                await _process_message(msg)
                total_replayed += 1
            except Exception as e:
                print(f"  catchup: failed to replay message {msg.id} in channel {ch_id}: {e}")

    if total_replayed:
        print(f"  catchup: replayed {total_replayed} offline message(s) across {len(channels_to_replay)} channel(s)")


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
