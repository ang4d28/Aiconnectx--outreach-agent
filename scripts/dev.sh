#!/usr/bin/env bash
# Starts the API (port 8000), the scheduler and the Vite dev server in the background.
# Ctrl+C stops all three. Needs backend/.venv (see README).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[ -f "$ROOT/backend/.venv/bin/activate" ] || { echo "backend/.venv not found. See README." >&2; exit 1; }
[ -f "$ROOT/.env" ] || { echo ".env not found. Copy .env.example to .env and fill it in." >&2; exit 1; }

source "$ROOT/backend/.venv/bin/activate"
pids=()
trap 'kill "${pids[@]}" 2>/dev/null || true' EXIT INT TERM

(cd "$ROOT/backend" && exec uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload) & pids+=($!)
(cd "$ROOT/backend" && exec python -m app.scheduler) & pids+=($!)
(cd "$ROOT/frontend" && exec npm run dev) & pids+=($!)

echo "API http://localhost:8000 (docs at /docs), dashboard http://localhost:5173/dashboard. Ctrl+C stops everything."
wait
