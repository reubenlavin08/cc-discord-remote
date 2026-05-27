# cc-discord-remote — request log & status

Tracks every request Reuben has made for the Discord remote-control bot, with status. Newest concerns at the bottom.

## ✅ Done & verified
- **Portfolio Claude relaunch** — launched a fresh Claude in `OneDrive\Documents\Portfolio`, auto-attached a channel. (Was: portfolio chat returning nothing.)
- **Auto-spawn churn from claude-monitor scraper** — `_auto_spawn_watcher` now skips claude.exe younger than 30s (`AUTO_SPAWN_MIN_AGE_SECONDS`). Commit `8c69c89`.
- **Drop @mention on turn completions** — only approvals ping now; removed `PING_AFTER_SECONDS`. Commit `ed81240`.
- **Email digest retimed** — 16:00 (`afternoon`) + 21:00 (`evening`); renamed from morning/evening. Avoids dead overnight window.
- **Email digest embeds bullets in Discord** — action items + calendar items inline, not a file-path reference. Empty-section placeholder bullets filtered.
- **Email digest headless MCP** — `--mcp-config` added so `claude --print` actually loads workspace-mcp + ms365 (was silently missing → Gmail unscanned).
- **Terminal channels nested under `terminal` category** — all four create_text_channel sites pass `category=`. Commit `917a260`.
- **Notification settings** — answered: client-side Discord "Only @mentions" setting (no code change).

## ✅ Done — session_id rework (commit pending verification)
- **ROOT CAUSE fixed: bind by session_id, not PID** — `cmd_attach` now persists `session_id`+`cwd` (`set_identity`). `_pid_watcher`, on a dead PID, checks if the same session is alive under a NEW pid and **rebinds + restarts the mirror** instead of closing. Only a truly-gone session closes the channel. → fixes AskUserQuestion auto-close + channels dying on in-place restart.
- **Restore Claude tabs on reboot (ALL recent tabs)** — `_restore_terminals_on_boot` resumes every channel that has a session_id but no live process: `claude --resume <session_id>` in its cwd, reads back the forked session_id, re-attaches to the SAME channel. Auto-Enters trust + resume picker. Staggered 2s.
- **Reopen channels** — channels survive reboot in Discord (bot died before deleting them); restore reuses them, no churn. Orphan-delete now SKIPS channels with a session_id.
- **Duplicate messages** — auto-spawn watcher now skips a new PID whose `session_id` already has a channel (was creating a 2nd channel → 2 mirror loops → dup messages). Plus rebind cancels the old mirror.

## ⚠️ Shipped earlier, re-verify after this deploy
- AskUserQuestion eager-render (`701a402`) + line-buffered bot.log (`427cfc8`) — render confirmed working in bot.log; auto-close was the real bug (now fixed).
- Esc-before-type + picker-aware skip (`a263f94`); resume auto-Enter + auto-look (`f157b7f`); offline replay (`09453e6`).

## ✅ Done — channel protection + live-session adoption
- **Never delete human channels** — `_bot_deletable()` guard: bot only auto-deletes channels under the `terminal` category or hex-id orphans. `notifications`, `control-room`, `physics` etc. are untracked-but-never-deleted. Fixes the reboot deleting the notifications channel.
- **Adopt orphan live sessions on boot** — `_adopt_orphan_live_sessions()` gives a fresh channel to live NAMED sessions that lost theirs (survived a reboot but channel was deleted). Fixes "Discord hasn't repopulated" for still-running tabs.

## ❌ Still open
- **Catchup doesn't press Enter** — couldn't reproduce from code (`_write_text` DOES append Enter). NEEDS a concrete repro from Reuben (which message, what he saw).
- **Channels lost in the PAST reboot** — ones already deleted with no session_id are gone as their original channels; the still-LIVE ones get fresh channels via adoption on the next restart.
- **First-restart priming** — pre-existing channels attached with OLD code lack session_id; the deploying restart re-attaches + persists it, so only reboots AFTER that are fully restorable.
