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

PREFIX = "!cc"
MAX_CHUNK = 1900

CLAUDE_MONITOR_WS = os.environ.get("CLAUDE_MONITOR_WS", "ws://192.168.1.242:8765/ws")

HELP_TEXT = (
    f"**Claude Code remote**\n\n"
    f"**Multi-channel** — one Discord channel per terminal:\n"
    f"`{PREFIX} launch <name> [cwd]` — start a *brand-new* terminal, name it, attach a channel\n"
    f"`{PREFIX} spawn <name>` — attach a new channel to an *existing* running terminal\n"
    f"`{PREFIX} close [name]` — detach, **kill the terminal window**, delete the channel\n"
    f"`{PREFIX} cleanup` — sweep orphan PowerShell windows from past `/exit`s\n\n"
    f"**Per-channel attach** (manual):\n"
    f"`{PREFIX} live` — list running Claude Code processes\n"
    f"`{PREFIX} attach <name>` — drive that terminal from this channel\n"
    f"`{PREFIX} detach` — stop driving the terminal\n"
    f"`{PREFIX} look` — snapshot the terminal screen\n"
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
        subprocess.Popen(
            ["powershell.exe", "-NoExit", "-ExecutionPolicy", "Bypass", "-Command",
             f"claude --resume {s.session_id}"],
            creationflags=subprocess.CREATE_NEW_CONSOLE,
            cwd=cwd,
            close_fds=True,
        )
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

    # Defensive Enter in case a trust prompt rendered.
    await asyncio.sleep(2)
    await _run_console_helper(new_pid, "", mode="enter")

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
        new_chan = await channel.guild.create_text_channel(name=sanitized)
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
        subprocess.Popen(
            ["powershell.exe", "-NoExit", "-ExecutionPolicy", "Bypass", "-Command", "claude"],
            creationflags=subprocess.CREATE_NEW_CONSOLE,
            cwd=cwd,
            close_fds=True,
        )
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

    # Give the trust prompt a moment to render, then accept it with Enter.
    await asyncio.sleep(2)
    await _run_console_helper(new_pid, "", mode="enter")

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
        new_chan = await channel.guild.create_text_channel(name=sanitized)
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
    mt = mirror_tasks.pop(target_id, None)
    if mt and not mt.done():
        mt.cancel()
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
                    await _render_piece(channel, kind, data, pending_tools)
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

    # Slash commands are TUI-only (no JSONL writes). Use mode="send" so the
    # helper types AND polls the screen until it's been stable for ~1.5 s —
    # much more reliable than a fixed delay, especially for commands that need
    # a network roundtrip (e.g. /login) or render an interactive picker.
    is_slash = text.lstrip().startswith("/")
    mode = "send" if is_slash else "type"

    async with channel.typing():
        result = await _run_console_helper(pid, text, mode=mode)

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

COMMANDS = {
    "help", "status", "where", "new", "cancel", "live", "detach", "look",
    "close", "cd", "sessions", "resume", "attach", "spawn", "launch", "usage", "esc",
    "keys", "pad", "cleanup",
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
        return

    # First word isn't a command — treat the whole thing as a prompt.
    if channel_id in attached_pids:
        await cmd_terminal_send(channel, channel_id, user_id, rest)
    else:
        await cmd_ask(channel, channel_id, user_id, rest)


# ---------- Discord wiring --------------------------------------------------

_pid_watcher_started = False
_auto_spawn_watcher_started = False
_auto_spawn_seen: set = set()  # PIDs we've already processed — populated on first poll


async def _pid_watcher(interval: float = 15.0):
    """Poll attached PIDs; when a claude.exe exits, auto-close its Discord channel.

    Control rooms (env-configured ALLOWED_CHANNEL_IDS) are NEVER deleted — they
    just get their dead attachment cleared so the channel survives a Claude
    restart. Other channels get a goodbye message then are deleted.
    """
    while True:
        try:
            await asyncio.sleep(interval)
            for ch_id, pid in list(attached_pids.items()):
                if pid_alive(pid):
                    continue
                attached_pids.pop(ch_id, None)
                mt = mirror_tasks.pop(ch_id, None)
                if mt and not mt.done():
                    mt.cancel()
                sessions.set_attached_pid(ch_id, None)

                if ch_id in CONTROL_CHANNELS:
                    continue

                ALLOWED_CHANNELS.discard(ch_id)
                chan = bot.get_channel(ch_id)
                if chan is None:
                    sessions.conn.execute("DELETE FROM sessions WHERE channel_id = ?", (ch_id,))
                    sessions.conn.commit()
                    continue
                try:
                    await chan.send(f"🪦 Terminal exited (PID {pid} gone) — closing this channel.")
                    await chan.delete(reason="cc-discord-remote: terminal exited")
                    sessions.conn.execute("DELETE FROM sessions WHERE channel_id = ?", (ch_id,))
                    sessions.conn.commit()
                    print(f"  auto-closed channel {ch_id} (PID {pid} died)")
                except discord.Forbidden:
                    print(f"  can't auto-close channel {ch_id} (missing Manage Channels)")
                except Exception as e:
                    print(f"  couldn't auto-close channel {ch_id}: {e}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"  _pid_watcher error (continuing): {e}")


async def _auto_spawn_watcher(interval: float = 15.0):
    """Poll for newly-appeared claude.exe sessions; auto-create a Discord channel.

    Seeds with all currently-running claude.exes on the FIRST iteration so the
    bot doesn't spam channels for pre-existing sessions at startup. After that,
    any new claude session (the user typed `claude` in a terminal) gets its own
    channel automatically, named after `/rename` if set, else session-id prefix.

    Channels are created in the guild of the first env-configured control room.
    Skips sessions that are already attached to a channel (in attached_pids).
    """
    primary_user = next(iter(ALLOWED_USERS), 0)
    first_pass = True

    while True:
        try:
            await asyncio.sleep(interval)
            running = list_running()
            current_pids = {c.pid for c in running}

            if first_pass:
                _auto_spawn_seen.update(current_pids)
                first_pass = False
                continue

            # Drop dead PIDs from seen so PID-reuse doesn't suppress legitimate new sessions.
            _auto_spawn_seen.intersection_update(current_pids)

            already_attached = set(attached_pids.values())
            new_sessions = [c for c in running if c.pid not in _auto_spawn_seen and c.pid not in already_attached]
            if not new_sessions:
                continue

            # Pick a guild from the first reachable control room.
            guild = None
            for cid in CONTROL_CHANNELS:
                ch = bot.get_channel(cid)
                if ch and getattr(ch, "guild", None):
                    guild = ch.guild
                    break
            if guild is None:
                # No control room found — mark new PIDs as seen so we don't retry forever.
                _auto_spawn_seen.update(c.pid for c in new_sessions)
                continue

            for info in new_sessions:
                _auto_spawn_seen.add(info.pid)
                channel_name = _sanitize_channel_name(info.name or info.session_id[:8])
                try:
                    new_chan = await guild.create_text_channel(name=channel_name)
                except discord.Forbidden:
                    print(f"  auto-spawn: missing Manage Channels in guild {guild.id}")
                    continue
                except discord.HTTPException as e:
                    print(f"  auto-spawn: couldn't create channel for PID {info.pid}: {e}")
                    continue
                ALLOWED_CHANNELS.add(new_chan.id)
                try:
                    await cmd_attach(new_chan, new_chan.id, primary_user, str(info.pid))
                    print(f"  auto-spawned channel #{channel_name} for PID {info.pid}")
                except Exception as e:
                    print(f"  auto-spawn: attach failed for PID {info.pid}: {e}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"  _auto_spawn_watcher error (continuing): {e}")


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
    global _pid_watcher_started, _auto_spawn_watcher_started
    if not _pid_watcher_started:
        _pid_watcher_started = True
        asyncio.create_task(_pid_watcher())
    if not _auto_spawn_watcher_started:
        _auto_spawn_watcher_started = True
        asyncio.create_task(_auto_spawn_watcher())

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
