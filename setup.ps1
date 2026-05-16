# One-shot setup: venv + deps + .env scaffold.
# Run from this folder:  .\setup.ps1
$ErrorActionPreference = "Stop"

Write-Host "==> Checking Python..." -ForegroundColor Cyan
$py = Get-Command py -ErrorAction SilentlyContinue
if (-not $py) { $py = Get-Command python -ErrorAction SilentlyContinue }
if (-not $py) {
    Write-Host "Python not found. Install Python 3.10+ from https://www.python.org/downloads/ (tick 'Add to PATH'), then re-run." -ForegroundColor Red
    exit 1
}
& $py.Source --version

Write-Host "==> Creating .venv..." -ForegroundColor Cyan
if (-not (Test-Path ".venv")) {
    & $py.Source -m venv .venv
}

Write-Host "==> Activating .venv..." -ForegroundColor Cyan
. .\.venv\Scripts\Activate.ps1

Write-Host "==> Installing dependencies..." -ForegroundColor Cyan
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

if (-not (Test-Path ".env")) {
    Write-Host "==> Creating .env from template..." -ForegroundColor Cyan
    Copy-Item .env.example .env
    Write-Host "    Edit .env and fill in DISCORD_TOKEN, ALLOWED_USER_IDS, ALLOWED_CHANNEL_IDS." -ForegroundColor Yellow
} else {
    Write-Host "==> .env already exists, leaving it alone." -ForegroundColor DarkGray
}

Write-Host ""
Write-Host "Setup complete." -ForegroundColor Green
Write-Host "Next: edit .env, then run:  python bot.py" -ForegroundColor Green
