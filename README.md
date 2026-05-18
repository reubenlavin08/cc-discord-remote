# cc-discord-remote

![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)
![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)
[![Build](https://github.com/reubenlavin08/cc-discord-remote/actions/workflows/ci.yml/badge.svg)](https://github.com/reubenlavin08/cc-discord-remote/actions/workflows/ci.yml)

Drive [Claude Code](https://claude.com/claude-code) running in a terminal on your laptop from a Discord channel on your phone. Real live-attach to an already-running terminal session — every keystroke goes into the *actual* Claude process, every response streams back. Headless fallback via the Agent SDK when no terminal exists.

```
┌─────────┐   Discord    ┌──────────┐    Windows Console API   ┌──────────────┐
│ phone   │ ────────────►│ bot      │ ───── WriteConsoleInput ►│ claude.exe   │
│ (you)   │ ◄────────────│ (Python) │ ◄──── JSONL tail ────────│ (in terminal)│
└─────────┘              └──────────┘                          └──────────────┘
```

## Highlights

- **True live-attach**, not screen-scraping — `AttachConsole` + `WriteConsoleInput` against any running `claude.exe`
- **Bidirectional mirror** — terminal activity streams to Discord, Discord messages stream to the terminal
- **TUI menus → Discord components** — slash commands (`/powerup`, `/model`, `/agents`, `/resume`) auto-snapshot the screen with a clickable keypad attached. Tap a button → key fires into the terminal → same Discord message edits in place with the new screen
- **Per-tool Discord button approvals** — both in SDK mode (`can_use_tool`) and in attached-terminal mode (screen-detected popup → Allow / Deny / Deny + tell Claude buttons, with a Modal for free-text reasoning)
- **One Discord channel per terminal** — `!cc spawn <name>` auto-creates `#<name>`, attached and mirroring; `!cc close` kills the PowerShell window too, not just the channel
- **Resilient** — channel↔terminal mappings persist in SQLite; bot restart restores every mirror
- **Honest fallback** — when no live terminal exists for a session, headless Agent SDK takes over with the same JSONL format

## Why this exists

Claude Code ships a built-in `/remote-control` feature, but it requires the phone's Claude account to match the laptop's. My phone uses a different account, so that feature is unusable. This bot solves the same problem through Discord: the phone authenticates to Discord, the bot runs locally and talks to Claude Code using the laptop's existing credentials. The account-mismatch problem is sidestepped entirely.

## What's interesting under the hood

- **Live attach to an already-running `claude.exe`** via Win32 console APIs (`AttachConsole`, `WriteConsoleInput`, `ReadConsoleOutputCharacter`) called through `ctypes`. The bot injects keystrokes into the terminal as if you'd typed them.
- **Response capture without screen-scraping**: Claude Code persists every turn to a JSONL at `~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`. Tailing that file (waiting for it to stop growing, then parsing new lines) gives clean structured text and tool-use events — no terminal chrome, no ANSI codes.
- **Console-handle isolation**: the bot would corrupt its own stdio if it called `AttachConsole` directly. Solved by spawning a single-purpose subprocess (`console_helper.py`) per write so the parent process's console is never touched.
- **Dual mode**: when the requested session is live in a terminal, the bot attaches to it. Otherwise it spawns a headless Claude via the Agent SDK and continues the session from disk. Same on-disk JSONL format = sessions are interchangeable.
- **Per-tool Discord-button approvals**: the SDK path exposes a `can_use_tool` callback. When Claude wants to run `Edit`/`Write`/`Bash`, the bot posts an embed with Approve/Deny buttons and blocks until the authorised user clicks. Read-only tools auto-run.

## Architecture

| Module | Role |
|---|---|
| `bot.py` | Discord event loop, command dispatch, prefix + slash commands |
| `runner.py` | Wraps `claude-agent-sdk` for the headless path |
| `console_helper.py` | Standalone Win32 `ctypes` subprocess that types into and reads from a foreign console |
| `live_processes.py` | Reads `~/.claude/sessions/*.json` to find running Claude processes |
| `session_tail.py` | Polls the JSONL Claude writes during a live turn, parses new entries |
| `session_files.py` | Lists prior sessions, resolves the `/rename` custom-title metadata |
| `approvals.py` | Discord button-based tool-approval embed with 5-min timeout |
| `sessions.py` | SQLite per-channel state + audit log |
| `test_all.py` | Backend test sweep (15 passing) |

## Features

**Multi-channel — one Discord channel per terminal:**
- `!cc launch <name> [cwd]` — start a brand-new terminal, name it, attach a fresh channel
- `!cc spawn <name>` — attach a new channel to an existing running terminal
- `!cc close [name]` — detach, **kill the PowerShell window**, delete the channel
- `!cc cleanup` — sweep orphan PowerShell windows from past `/exit`s

**Per-channel attach:**
- `!cc live` — list running Claude Code processes by custom name
- `!cc attach <name>` — drive that terminal from this channel
- `!cc detach` — disconnect
- `!cc look` — snapshot the terminal screen
- Anything typed in an attached channel (no prefix) is typed straight into the terminal

**Driving Claude Code's TUI from Discord:**
- `!cc pad` — pop a clickable keypad (arrows in inverted-T, Esc / Tab / Bksp / Enter / Space / 1-5 / Look). Each click sends one key and edits the message in place with the new screen
- `!cc keys <seq>` — raw key passthrough (e.g. `!cc keys down,down,enter`)
- Type any `/`-prefixed message in an attached channel → bot types it into the terminal, waits for the screen to stabilize, posts the snapshot with a keypad already attached for navigation
- Tool-approval popups in attached terminals auto-surface as Discord buttons (`✅ Allow / ❌ Deny / 💬 Deny + tell Claude`); the third opens a Modal for free-text reasoning

**Sessions:**
- `!cc sessions` — list past sessions (renamed ones show their `/rename` title)
- `!cc resume` (no arg) — dropdown picker of the 25 most-recent sessions; pick one and the bot spawns `claude --resume <id>` in a new terminal + new Discord channel
- `!cc resume <id>` — same, by direct id

**Headless SDK mode:**
- `!cc <prompt>` (in a non-attached channel) — spawn a headless Claude via Agent SDK and stream the response
- `!cc new` / `!cc cancel` / `!cc cd <path>` — manage SDK-mode session state
- `!cc status` — show current cwd + session id
- `!cc usage` — fetch usage stats from claude-monitor (model, context %, cost, 5h + weekly limits)

**Quality of life:**
- @mention pings on turns longer than 15 s and on pending approvals
- SQLite audit log of every command (`sessions.db`)
- Auto-spawn watcher: a fresh `claude` started in any terminal gets its own Discord channel within ~15 s

## Setup

Windows only. Requires Python 3.10+ and a Discord bot token.

```powershell
cd cc-discord-remote
.\setup.ps1                   # creates .venv, installs deps, scaffolds .env
# Edit .env with your token, user ID, channel ID
python bot.py
```

Required `.env`:
```
DISCORD_TOKEN=<bot token>
ALLOWED_USER_IDS=<your discord user id>
ALLOWED_CHANNEL_IDS=<channel id where bot listens>
DEFAULT_CWD=C:/Users/<you>
```

## Limitations (honest)

- **Windows only.** Console attach uses Win32. Mac/Linux would need a different approach (`pty` / `tmux`).
- **Fragile to Claude Code updates.** The JSONL format and session-registry layout are undocumented internals. An update could break the live-attach path; the headless path is safer.
- **One client per session at a time.** If you type in the terminal and the bot also writes, the JSONL gets garbled. The bot warns when you try to resume a live session.
- **Long Claude responses scroll past the visible screen.** `!cc look` only captures what's currently rendered, not scrollback. The live-attach path doesn't have this problem because it tails the JSONL, not the screen.

## Tech stack

Python 3.12 · `discord.py` 2.7 · `claude-agent-sdk` 0.2.82 · raw Win32 via `ctypes` · SQLite · async/await throughout.

## Design decisions worth flagging

- **Why a subprocess for `AttachConsole`?** A Win32 process can only own one console at a time. If the bot called `AttachConsole` directly it would corrupt its own stdio. Spawning `console_helper.py` as a one-shot subprocess per write isolates the attach so the parent process is never affected.
- **Why tail the JSONL instead of reading the terminal screen?** `ReadConsoleOutputCharacter` only sees the visible window; long responses scroll off. The JSONL has the full structured history (text, tool_use, tool_result), which gives clean responses without ANSI codes or UI chrome.
- **Why pair tool_use with tool_result by id?** Claude's session JSONL writes them as separate events. Pairing them in the bot means one Discord message per tool — `🛠️ Bash — ls *.py ↳ bot.py runner.py ...` — instead of two scattered ones.
- **Why a "quiet for 3s + all tools resolved" turn-complete check?** A simpler "file stopped growing for 2s" check fires prematurely during slow tool calls (e.g., a Bash that takes 10s). Tracking unresolved tool IDs and requiring both signals avoids posting partial turns.

## License

MIT.
