# cc-discord-remote

Drive [Claude Code](https://claude.com/claude-code) running in a terminal on your laptop from a Discord channel on your phone. Both real live-attach to an existing terminal session **and** a headless fallback using the Agent SDK.

```
┌─────────┐   Discord    ┌──────────┐    Windows Console API   ┌──────────────┐
│ phone   │ ────────────►│ bot      │ ───── WriteConsoleInput ►│ claude.exe   │
│ (you)   │ ◄────────────│ (Python) │ ◄──── JSONL tail ────────│ (in terminal)│
└─────────┘              └──────────┘                          └──────────────┘
```

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

- `!cc live` / `/cc live` — list running Claude Code processes by custom name
- `!cc attach <name>` — drive that terminal from this channel
- `!cc <prompt>` — type into the attached terminal; receive Claude's reply via JSONL tail
- `!cc look` — snapshot the terminal screen
- `!cc detach` — disconnect
- `!cc sessions` — list past sessions (including renamed ones with their `/rename` title)
- `!cc resume <id>` — attach to a stored session; warns if it's currently live in a terminal
- `!cc cancel` — interrupt a running headless turn
- `!cc <prompt>` (when not attached) — spawn a headless Claude via Agent SDK
- @mention pings on turns longer than 15s and on pending approvals

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

## License

MIT.
