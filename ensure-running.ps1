# Idempotent supervisor for bot.py.
# Called by the CCDiscordRemote scheduled task on logon AND every 5 min.
# If bot.py is already running, exits silently. Otherwise relaunches via
# pythonw.exe (no console window) with stdout/stderr redirected to log files.
$ErrorActionPreference = 'SilentlyContinue'

$root   = $PSScriptRoot
$py     = Join-Path $root '.venv\Scripts\pythonw.exe'
$bot    = Join-Path $root 'bot.py'
$log    = Join-Path $root 'bot.log'
$errlog = Join-Path $root 'bot.err.log'

# Serialize the check-then-launch. This task has BOTH a logon and a 5-minute
# trigger; when they fire in the same second, two runs each saw "not running"
# and each started a bot -> two reconcilers racing = duplicate channels. The
# mutex makes only one run do the check at a time. (bot.py also holds its own
# named mutex as the authoritative guard.)
$mutex = New-Object System.Threading.Mutex($false, 'Local\cc-discord-remote-supervisor')
try {
    if (-not $mutex.WaitOne(0)) { exit 0 }   # another supervisor run owns it
} catch [System.Threading.AbandonedMutexException] {
    # Previous holder died mid-run; we own it now and may proceed.
}
try {
    $existing = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
        Where-Object { $_.CommandLine -like '*bot.py*' -and $_.CommandLine -like '*cc-discord-remote*' }
    if ($existing) { exit 0 }

# NOTE: Claude config isolation is scoped INSIDE the bot (runner.py sets CLAUDE_CONFIG_DIR
# + CLAUDE_CODE_OAUTH_TOKEN only on the headless SDK-query subprocess, which is the
# high-frequency `claude` startup that was corrupting the global ~/.claude.json). The bot
# process and the interactive terminal tabs it spawns intentionally stay on the MAIN
# config, so `claude --resume <id>` can find session transcripts under ~/.claude.
# Do NOT set CLAUDE_CONFIG_DIR globally here — it would break terminal-tab resume.

    foreach ($f in @($log, $errlog)) {
        if (Test-Path $f) { Move-Item $f "$f.old" -Force }
    }

    Start-Process -FilePath $py `
        -ArgumentList "`"$bot`"" `
        -WorkingDirectory $root `
        -WindowStyle Hidden `
        -RedirectStandardOutput $log `
        -RedirectStandardError $errlog | Out-Null
}
finally {
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
