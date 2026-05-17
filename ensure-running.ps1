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

$existing = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
    Where-Object { $_.CommandLine -like '*bot.py*' -and $_.CommandLine -like '*cc-discord-remote*' }
if ($existing) { exit 0 }

foreach ($f in @($log, $errlog)) {
    if (Test-Path $f) { Move-Item $f "$f.old" -Force }
}

Start-Process -FilePath $py `
    -ArgumentList "`"$bot`"" `
    -WorkingDirectory $root `
    -WindowStyle Hidden `
    -RedirectStandardOutput $log `
    -RedirectStandardError $errlog | Out-Null
