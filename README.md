# AI ConnectX Outreach Agent

Automated placement outreach for AI ConnectX. It finds LinkedIn hiring posts, picks the right contact, drafts a message, holds every message for your review on WhatsApp, sends within strict guardrails (LinkedIn DMs, Outlook email), handles replies and mirrors everything to Google Sheets.

**Safe by default:** `DRY_RUN=true` means nothing is ever sent to a lead. Drafts, review previews and reports still work, so you can watch it run before going live.

## What runs and when

One scheduler process (`python -m app.scheduler`) runs every job (APScheduler, job store in Postgres, times in IST):

| Job | Schedule | What it does |
|---|---|---|
| post_monitor | every 2.5 h | searches for hiring posts and saves relevant ones |
| company_finder | daily 08:00 | picks a contact per new post, creates the outreach, drafts messages |
| send_queue | every 10 min, 10:00-17:00 | starts the hold window and WhatsApp preview for approved drafts, sends released ones |
| release_held | every minute, 10:00-17:00 | sends messages whose hold has passed (no new holds) |
| connection_poll | every 2 h, 10:00-17:00 | checks accepted LinkedIn connection requests, queues the first DM |
| replies | every 30 min, 10:00-17:00 | reads LinkedIn and Outlook replies and applies the reply flow |
| followups | hourly | queues day-4 / day-10 follow-ups and 30/60-day rechecks |
| sheets_sync | every 30 min | mirrors outreach to Google Sheets |
| daily_summary | daily 20:00 | WhatsApp summary of the day |

Drafts appear after the 08:00 run. You approve them on the dashboard; each approved message then gets a WhatsApp preview (`SEND <id>`, `CANCEL <id>`, `EDIT <id> <text>`) and goes out when the hold ends.

If a job fails, you get a dashboard "Needs attention" item and a WhatsApp alert. After 3 failures in a row the failing channel is disabled (see [Operations](#operations)).

## Processes

| Process | Command | Purpose |
|---|---|---|
| PostgreSQL | local install, `localhost:5432` | database and scheduler job store |
| API | `uvicorn app.main:app --port 8000` | REST API and WhatsApp webhook |
| Scheduler | `python -m app.scheduler` | all scheduled jobs, including the LinkedIn browser |
| Frontend | `npm run dev` (Vite, port 5173) | landing page and operator dashboard |

## Prerequisites

- Python 3.11+
- Node 20+ and npm
- PostgreSQL 15+ running on `localhost:5432`
- Git

## Set up on Windows

1. **PostgreSQL.** Install it from https://www.postgresql.org/download/windows/, remember the `postgres` password, then create the database:

   ```powershell
   psql -U postgres -c "CREATE DATABASE aiconnectx;"
   ```

2. **Configure.**

   ```powershell
   Copy-Item .env.example .env
   ```

   Edit `.env`. At least: `POSTGRES_PASSWORD`, `JWT_SECRET` (32+ random characters), one LLM key (`GROQ_API_KEY`), `WHATSAPP_*`, `MS_CLIENT_ID` / `MS_TENANT` / `OUTLOOK_SENDER_ADDRESS`, `TPO_NAME` / `TPO_PHONE` / `TPO_EMAIL`, and optionally `GOOGLE_SHEET_ID`. Keep `DRY_RUN=true`. File paths in `.env` (`secrets/...`) are relative to the project root and their folders are created automatically. Put the Google service-account JSON at `secrets/gspread-service-account.json` and share the sheet with that account as an editor.

3. **Python environment and migrations.**

   ```powershell
   cd backend
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -e ".[dev]"
   playwright install chromium
   alembic -c app/db/alembic.ini upgrade head
   python -m app.cli create-admin --email you@example.com    # prompts for a password
   cd ..
   ```

4. **Frontend.**

   ```powershell
   cd frontend
   npm install
   cd ..
   ```

5. **Run everything.** This opens three windows: the API on port 8000, the scheduler, and the Vite dev server.

   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\dev.ps1
   ```

   - Dashboard: http://localhost:5173/dashboard (sign in with the admin account)
   - API docs: http://localhost:8000/docs
   - The scheduler window should show `Scheduler started in DRY_RUN (nothing is sent)` followed by the job list. To print the schedule without a database: `cd backend; python -m app.scheduler --check`.

Tests: `cd backend; pytest -q`. Frontend build check: `cd frontend; npm run build`.

On Linux use `scripts/dev.sh` instead of `dev.ps1` (create the venv with `python3 -m venv .venv`).

## One-time logins

**Outlook (device-code login).** Run once from `backend` with the venv active; open the printed URL, enter the code and sign in as the sender mailbox:

```powershell
python -c "import logging; logging.basicConfig(level=logging.WARNING); from app.channels.outlook import get_client; get_client().tokens.get_token(); print('Outlook token saved')"
```

The token cache is stored in `secrets/ms_token.json`.

**LinkedIn.** The first login is manual in a visible browser and no password is stored:

```powershell
cd backend
python -m app.channels.linkedin login
```

The profile is saved in `secrets/linkedin-profile`. A Chromium profile is tied to the OS that created it: a profile made on Windows will not work on the Ubuntu server, so log in again there (a desktop session or a forwarded display is needed). Logging in from a new IP can trigger a LinkedIn security check; if it does, the channel disables itself and alerts you (see Operations). Automating LinkedIn is against its terms of service: keep the caps low.

## WhatsApp webhook

Meta needs a public HTTPS URL for your webhook. A Cloudflare Tunnel provides one without opening any inbound port. You need a domain on Cloudflare (free plan is fine).

1. Cloudflare Zero Trust dashboard > Networks > **Tunnels** > Create a tunnel > **Cloudflared**. Name it, then copy the **token**.
2. Install `cloudflared` natively (Windows: `winget install Cloudflare.cloudflared`; Ubuntu: Cloudflare's package repo) and run it: `cloudflared tunnel run --token <token>`. Keep the token out of the repo. On the server, install it as a service.
3. In the tunnel's **Public Hostnames** tab, add:
   - Hostname: `hooks.yourdomain.com`
   - Path: `api/whatsapp/webhook`
   - Service: `http://localhost:8000`

   **Publish only this path.** The rest of the API requires a dashboard login, but it should not face the internet.
4. Check it. The first must echo `123`; the second must **not** return `{"status":"ok"}` (expect 404):

   ```bash
   curl "https://hooks.yourdomain.com/api/whatsapp/webhook?hub.mode=subscribe&hub.verify_token=<WHATSAPP_VERIFY_TOKEN>&hub.challenge=123"
   curl -i https://hooks.yourdomain.com/api/health
   ```
5. Meta for Developers > your app > WhatsApp > Configuration > Webhook:
   - Callback URL: `https://hooks.yourdomain.com/api/whatsapp/webhook`
   - Verify token: the value of `WHATSAPP_VERIFY_TOKEN`
   - Subscribe to the **messages** field.
6. Put the Phone number ID, a permanent access token, the **App secret** (`WHATSAPP_APP_SECRET`, used to verify signatures) and your number in international format without `+` (`WHATSAPP_OWNER_NUMBER`) in `.env`, then restart the API and scheduler.
7. Message the bot `STATUS` from your phone. The reply confirms the webhook works. Outside the 24-hour window WhatsApp only delivers approved templates: create a template named `owner_notification` with one text variable `{{1}}` in Meta's WhatsApp Manager.

## Dashboard

Set `JWT_SECRET` in `.env` first; without it nobody can sign in. Create the first admin with `python -m app.cli create-admin --email you@example.com`. Other people register or use Microsoft sign-in (set `MS_AUTH_CLIENT_ID/SECRET`, `BACKEND_URL`, `FRONTEND_URL`) and wait as `pending` until an admin approves them.

From the dashboard you can supervise the agent (overview, leads, drafts, replies, needs attention), pause all sending from the header, re-enable a channel after an error, and change caps, the send window, hold minutes, DRY_RUN, the placement officer details and the search queries. Changes apply to the API immediately and to the scheduler on its next job; a *wider* send window needs a scheduler restart.

## Deploy on Ubuntu

The same steps work natively on an Ubuntu server (22.04 or newer; 2 CPU and 8 GB RAM is comfortable for Chromium plus Postgres):

```bash
sudo apt install -y postgresql python3-venv python3-pip nodejs npm git nginx
sudo -u postgres psql -c "CREATE DATABASE aiconnectx;"
git clone https://github.com/ang4d28/Aiconnectx--outreach-agent.git aiconnectx && cd aiconnectx
cp .env.example .env && nano .env
cd backend && python3 -m venv .venv && source .venv/bin/activate
pip install -e . && playwright install --with-deps chromium
alembic -c app/db/alembic.ini upgrade head
python -m app.cli create-admin --email you@example.com
```

Run the API (`uvicorn app.main:app --host 127.0.0.1 --port 8000`) and the scheduler (`python -m app.scheduler`) as two systemd services with `WorkingDirectory=<repo>/backend`, the venv's python and `Restart=always`, so they come back after a reboot.

Build the frontend with `cd frontend && npm ci && npm run build`, copy `dist/` to `/var/www/aiconnectx` and serve it with `frontend/nginx.conf`. Public port 80 proxies only `POST /api/inquiries`; the private port 8081 serves the dashboard and proxies the whole API, so reach it over an SSH tunnel (`ssh -L 8081:localhost:8081 user@server`). Never publish port 8081 or the full API to the internet.

## Go live

1. Let it run in DRY_RUN for a few days. Check that drafts look right, previews reach WhatsApp, and `GET /api/reports/today` and the 20:00 summary make sense.
2. Set `DRY_RUN=false` in `.env` (or turn it off in dashboard Settings, which asks for confirmation).
3. Restart the API and scheduler.

Send `STOP` on WhatsApp at any time to pause all sending, `START` to resume.

## Operations

Backup and update (PowerShell, from the project root; `backups/` is git-ignored):

```powershell
New-Item -ItemType Directory -Force backups | Out-Null
pg_dump -U postgres aiconnectx -f backups\aiconnectx.sql

git pull
cd backend; pip install -e .; alembic -c app/db/alembic.ini upgrade head
```

On Linux: `pg_dump -U postgres aiconnectx > backups/aiconnectx.sql`. Restart the API and scheduler after updating.

**A channel got disabled** (alert on WhatsApp, item in "Needs attention"): fix the cause, then re-enable:

| Channel | Re-enable |
|---|---|
| LinkedIn (captcha / checkpoint) | solve it in a browser, then `python -m app.channels.linkedin enable` and send `START` on WhatsApp |
| Outlook | `PATCH /api/settings/outlook_disabled` with `{"value": "false"}` |
| Sheets | `PATCH /api/settings/sheets_disabled` with `{"value": "false"}` |

If the setting does not exist yet, `POST /api/settings` with `{"key": "outlook_disabled", "value": "false"}`.

**Search returns nothing:** the post monitor uses the `duckduckgo-search` library with the queries in `search_queries.yaml` (or those edited on the dashboard). On a rate limit it retries with backoff (3 attempts) and then skips that query; the next run tries again. Look for `DuckDuckGo search failed` in the scheduler log.

**Landing page figures:** the hero's "Companies connected / Students / MoUs signed" chips show only the real numbers you set at build time (`VITE_STAT_COMPANIES`, `VITE_STAT_STUDENTS`, `VITE_STAT_MOUS`, e.g. in `frontend/.env.local`); unset ones are hidden in the production build. Run `npm run build` again after changing them.

**Messages stay "held":** they only go out between 10:00 and 17:00 IST, when not paused, under the daily caps, and with `DRY_RUN=false`.
