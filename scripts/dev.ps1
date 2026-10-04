# Starts the API (port 8000), the scheduler and the Vite dev server, each in its own window.
# Run from anywhere:  powershell -ExecutionPolicy Bypass -File scripts\dev.ps1
$root = Split-Path -Parent $PSScriptRoot
$activate = Join-Path $root "backend\.venv\Scripts\Activate.ps1"
if (-not (Test-Path $activate)) {
    Write-Error "backend\.venv not found. Create it first (see README): cd backend; python -m venv .venv; .\.venv\Scripts\Activate.ps1; pip install -e '.[dev]'"
    exit 1
}
if (-not (Test-Path (Join-Path $root ".env"))) {
    Write-Error ".env not found. Copy .env.example to .env and fill it in."
    exit 1
}

$backend = Join-Path $root "backend"
$frontend = Join-Path $root "frontend"

Start-Process powershell -ArgumentList "-NoExit", "-Command", "`$host.UI.RawUI.WindowTitle = 'api'; Set-Location '$backend'; . '$activate'; uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload"
Start-Process powershell -ArgumentList "-NoExit", "-Command", "`$host.UI.RawUI.WindowTitle = 'scheduler'; Set-Location '$backend'; . '$activate'; python -m app.scheduler"
Start-Process powershell -ArgumentList "-NoExit", "-Command", "`$host.UI.RawUI.WindowTitle = 'frontend'; Set-Location '$frontend'; npm run dev"

Write-Host "API http://localhost:8000 (docs at /docs), dashboard http://localhost:5173/dashboard. Close the three windows to stop."
