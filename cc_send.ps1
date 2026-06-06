# cc_send.ps1 <path> — invoked by the /cc-send slash command (or directly by the agent).
# Copies <path> into outbox/<claude_pid>__<filename>; the cc-discord-remote bot's
# _outbox_watcher uploads it to THIS session's Discord channel within ~3s, then deletes it.
# This is the push side (terminal → Discord); `!cc get` is the pull side (Discord asks).
param([Parameter(Mandatory = $true)][string]$Path)
$ErrorActionPreference = 'Stop'
$outbox = 'C:\Users\User\Desktop\Claude Projects\cc-discord-remote\outbox'
New-Item -ItemType Directory -Force -Path $outbox | Out-Null

if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
    Write-Output "cc-send: file not found: $Path"
    exit 1
}
$full = (Resolve-Path -LiteralPath $Path).Path
$base = Split-Path -Leaf $full

# Walk up from this helper to find the claude.exe that owns this session.
$cur = $PID
$claudePid = $null
for ($i = 0; $i -lt 15; $i++) {
    $p = Get-CimInstance Win32_Process -Filter "ProcessId=$cur" -ErrorAction SilentlyContinue
    if (-not $p) { break }
    if ($p.Name -eq 'claude.exe') { $claudePid = $cur; break }
    $cur = $p.ParentProcessId
}
if (-not $claudePid) {
    Write-Output 'cc-send: could not locate this claude.exe session.'
    exit 1
}

$dest = Join-Path $outbox ("{0}__{1}" -f $claudePid, $base)
Copy-Item -LiteralPath $full -Destination $dest -Force
Write-Output "cc-send: queued '$base' for Discord (claude $claudePid). It will appear in ~3s."
