# cc_close.ps1 — invoked by the /cc-close Claude Code slash command.
# Finds THIS Claude session's claude.exe (by walking up the process tree from this
# helper) and drops close-markers/<claude_pid> (content = parent PowerShell pid). The
# cc-discord-remote bot's _close_marker_watcher picks it up within ~2s and closes the
# session + its terminal tab cleanly, with no auto-resume. This script does NOT kill
# anything itself, so the agent stays responsive until the bot tears it down.
$ErrorActionPreference = 'Stop'
$markerDir = 'C:\Users\User\Desktop\Claude Projects\cc-discord-remote\close-markers'
New-Item -ItemType Directory -Force -Path $markerDir | Out-Null

# Walk up the ancestry from this helper to find the claude.exe that owns this session.
$cur = $PID
$claudePid = $null
for ($i = 0; $i -lt 15; $i++) {
    $p = Get-CimInstance Win32_Process -Filter "ProcessId=$cur" -ErrorAction SilentlyContinue
    if (-not $p) { break }
    if ($p.Name -eq 'claude.exe') { $claudePid = $cur; break }
    $cur = $p.ParentProcessId
}
if (-not $claudePid) {
    Write-Output 'cc-close: could not locate this claude.exe session (no claude.exe ancestor).'
    exit 1
}
$shellPid = (Get-CimInstance Win32_Process -Filter "ProcessId=$claudePid" -ErrorAction SilentlyContinue).ParentProcessId
Set-Content -Path (Join-Path $markerDir "$claudePid") -Value "$shellPid" -Encoding ascii -NoNewline
Write-Output "cc-close: marked session for close (claude $claudePid). This tab will close in ~2s."
