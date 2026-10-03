# AI ConnectX Outreach Agent

Automated placement outreach for AI ConnectX. It finds LinkedIn hiring posts, picks the right contact, drafts a message, holds every message for your review on WhatsApp, sends within strict guardrails (LinkedIn DMs, Outlook email), handles replies and mirrors everything to Google Sheets.

**Safe by default:** `DRY_RUN=true` means nothing is ever sent to a lead. Drafts, review previews and reports still work, so you can watch it run before going live.

## What runs and when

One `scheduler` container runs every job (APScheduler, job store in Postgres, times in IST):

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

## Services

| Service | Purpose |
|---|---|
| `postgres` | database and scheduler job store |
| `searxng` | search engine for post discovery |
| `backend` | API and WhatsApp webhook; applies migrations on start |
| `scheduler` | all scheduled jobs, including the LinkedIn browser |
| `frontend` | landing page and operator dashboard (nginx, profile `frontend`). Port 80 is the public landing page and proxies only `POST /api/inquiries`; port 8081 is the private dashboard |
| `cloudflared` | Cloudflare Tunnel for the webhook (profile `tunnel`) |

Everything is bound to `127.0.0.1`. Nothing is exposed to the internet except what you publish through the tunnel.

## Run locally

```bash
cp .env.example .env        # fill in POSTGRES_PASSWORD at least
mkdir -p secrets creds
docker compose build
docker compose up -d
docker compose logs -f scheduler
```

You should see `Scheduler started in DRY_RUN (nothing is sent)` followed by the job list. To print the schedule without Docker or a database: `cd backend && python -m app.scheduler --check`.

Tests: `docker compose run --rm backend pytest -q`

## Deploy on Oracle Cloud Always Free

### 1. Create the VM

1. Create an Oracle Cloud account and pick a home region (it cannot be changed later).
2. Compute > Instances > Create instance:
   - Image: **Ubuntu 22.04** (aarch64 for Ampere).
   - Shape: **VM.Standard.A1.Flex** with **2 OCPU and 12 GB** RAM (Always Free allows up to 4 OCPU / 24 GB). Do not use the 1 GB AMD micro shape: Chromium and Postgres will not fit.
   - Boot volume: 50-100 GB.
   - Add your SSH public key.
3. Networking: keep only inbound **SSH (22)** open in the subnet's security list. You do not need ports 80/443 or 8000; the Cloudflare Tunnel connects outbound.

Always Free capacity for A1 is sometimes full in a region; retry later or try another availability domain.

### 2. Install Docker

```bash
ssh ubuntu@<vm-ip>
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER && newgrp docker
docker compose version
```

### 3. Get the code and configure

```bash
git clone https://github.com/ang4d28/Aiconnectx--outreach-agent.git aiconnectx
cd aiconnectx
cp .env.example .env
mkdir -p secrets creds
nano .env
```

Fill in at least: `POSTGRES_PASSWORD`, one LLM key (`GROQ_API_KEY`), `WHATSAPP_*` (see step 6), `MS_CLIENT_ID` / `MS_TENANT` / `OUTLOOK_SENDER_ADDRESS`, `TPO_NAME` / `TPO_PHONE` / `TPO_EMAIL`, and (optional) `GOOGLE_SHEET_ID`. Keep `DRY_RUN=true` for now. Put the Google service-account JSON in `creds/gspread-service-account.json` and share the sheet with that account's email as an editor.

### 4. Start

```bash
docker compose build        # first build takes a few minutes (Chromium)
docker compose up -d
docker compose ps           # all services running, backend and postgres healthy
docker compose logs -f scheduler
```

Database migrations run automatically when the backend starts.

### 5. One-time logins

**Outlook (device-code login).** Run once; open the printed URL, enter the code and sign in as the sender mailbox:

```bash
docker compose exec backend python -c "import logging; logging.basicConfig(level=logging.WARNING); from app.channels.outlook import get_client; get_client().tokens.get_token(); print('Outlook token saved')"
```

The token cache is stored in `secrets/` and survives rebuilds.

**LinkedIn.** The first login is manual and needs a visible browser, so do it on a Linux machine with a desktop (a Linux laptop, or WSL2 with GUI support), not on the server:

```bash
cd backend && uv sync && uv run playwright install chromium
LINKEDIN_PROFILE_DIR=./li-profile uv run python -m app.channels.linkedin login
scp -r li-profile ubuntu@<vm-ip>:aiconnectx/secrets/linkedin-profile
```

A profile created on Windows will not work on the server. Logging in from a new IP can trigger a LinkedIn security check; if it does, the channel disables itself and alerts you (see Operations). Automating LinkedIn is against its terms of service: keep the caps low.

### 6. WhatsApp webhook through a Cloudflare Tunnel

Meta needs a public HTTPS URL for your webhook. A Cloudflare Tunnel provides one without opening any inbound port. You need a domain on Cloudflare (free plan is fine).

1. Cloudflare Zero Trust dashboard > Networks > **Tunnels** > Create a tunnel > **Cloudflared**. Name it, then copy the **token** from the install command.
2. Put it in `.env`: `CLOUDFLARE_TUNNEL_TOKEN=<token>`.
3. In the tunnel's **Public Hostnames** tab, add:
   - Hostname: `hooks.yourdomain.com`
   - Path: `api/whatsapp/webhook`
   - Service: `http://backend:8000`

   **Publish only this path for the backend.** The rest of the API (drafts, settings, attention items) has no login and must not be reachable from the internet.
4. To publish the landing page, add a second public hostname: `www.yourdomain.com` (no path) with service `http://frontend:80`, and start the page with `docker compose --profile frontend --profile tunnel up -d`. The page's nginx only forwards the contact form (`POST /api/inquiries`) to the backend and answers 404 for every other `/api` path. Form submissions appear in "Needs attention" and are sent to you on WhatsApp.
5. Start the tunnel: `docker compose --profile tunnel up -d`
6. Check it. The first must echo `123`; the second must **not** return `{"status":"ok"}` (expect 404):

   ```bash
   curl "https://hooks.yourdomain.com/api/whatsapp/webhook?hub.mode=subscribe&hub.verify_token=<WHATSAPP_VERIFY_TOKEN>&hub.challenge=123"
   curl -i https://hooks.yourdomain.com/api/health
   ```
7. Meta for Developers > your app > WhatsApp > Configuration > Webhook:
   - Callback URL: `https://hooks.yourdomain.com/api/whatsapp/webhook`
   - Verify token: the value of `WHATSAPP_VERIFY_TOKEN`
   - Subscribe to the **messages** field.
8. Put the Phone number ID, a permanent access token, the **App secret** (`WHATSAPP_APP_SECRET`, used to verify signatures) and your number in international format without `+` (`WHATSAPP_OWNER_NUMBER`) in `.env`, then `docker compose up -d`.
9. Message the bot `STATUS` from your phone. The reply confirms the webhook works. Outside the 24-hour window WhatsApp only delivers approved templates: create a template named `owner_notification` with one text variable `{{1}}` in Meta's WhatsApp Manager.

### 7. Open the dashboard

Set `JWT_SECRET` in `.env` first (16+ random characters, e.g. `openssl rand -hex 32`). Without it nobody can sign in and every API route except the public ones refuses requests. Then create the first admin on the VM (`docker compose exec backend python -m app.cli create-admin --email you@example.com`, it prompts for a password). Other people register or use Google/Microsoft sign-in (set `GOOGLE_CLIENT_ID/SECRET`, `MS_AUTH_CLIENT_ID/SECRET`, `BACKEND_URL`, `FRONTEND_URL`) and wait as `pending` until an admin approves them. Then start the frontend (`docker compose --profile frontend up -d`).

The dashboard is not public. Reach it over an SSH tunnel from your laptop:

```bash
ssh -L 8081:localhost:8081 -L 8000:localhost:8000 ubuntu@<vm-ip>
```

Open `http://localhost:8081/dashboard` and sign in. Never add port 8081 to the Cloudflare Tunnel: it proxies the whole API (still behind the login, but it should not face the internet). API docs are at `http://localhost:8000/docs`.

From the dashboard you can supervise the agent (overview, leads, drafts, replies, needs attention), pause all sending from the header, re-enable a channel after an error, and change caps, the send window, hold minutes, DRY_RUN, the placement officer details and the search queries. Changes apply to the API immediately and to the scheduler on its next job; a *wider* send window needs `docker compose restart scheduler`.

### 8. Go live

1. Let it run in DRY_RUN for a few days. Check that drafts look right (`GET /api/drafts`), previews reach WhatsApp, and `GET /api/reports/today` and the 20:00 summary make sense.
2. Set `DRY_RUN=false` in `.env`.
3. `docker compose up -d --force-recreate backend scheduler`

Send `STOP` on WhatsApp at any time to pause all sending, `START` to resume.

## Operations

```bash
docker compose logs -f scheduler        # what the jobs are doing
docker compose ps                       # health
git pull && docker compose build && docker compose up -d     # update
docker compose exec postgres pg_dump -U connectx connectx > backup.sql   # backup
```

All services restart automatically (`unless-stopped`), including after a VM reboot.

**A channel got disabled** (alert on WhatsApp, item in "Needs attention"): fix the cause, then re-enable:

| Channel | Re-enable |
|---|---|
| LinkedIn (captcha / checkpoint) | solve it in a browser, then `docker compose exec scheduler python -m app.channels.linkedin enable` and send `START` on WhatsApp |
| Outlook | `PATCH /api/settings/outlook_disabled` with `{"value": "false"}` |
| Sheets | `PATCH /api/settings/sheets_disabled` with `{"value": "false"}` |

If the setting does not exist yet, `POST /api/settings` with `{"key": "outlook_disabled", "value": "false"}`.

**Search returns nothing:** SearXNG only answers JSON queries if `json` is listed under `search.formats` in `/etc/searxng/settings.yml` (inside the `searxng_data` volume). Without it the post monitor falls back to DuckDuckGo.

**Landing page figures:** the hero's "Companies connected / Students / MoUs signed" chips show only the real numbers you set at build time (`VITE_STAT_COMPANIES`, `VITE_STAT_STUDENTS`, `VITE_STAT_MOUS`, e.g. in `frontend/.env.local`); unset ones are hidden in the production build. Rebuild with `docker compose --profile frontend build frontend`.

**Messages stay "held":** they only go out between 10:00 and 17:00 IST, when not paused, under the daily caps, and with `DRY_RUN=false`.
