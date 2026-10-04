# Deploy on Ubuntu 22.04 (Oracle Cloud Always Free, no containers)

What you end up with: PostgreSQL, the API (`aiconnectx-backend`) and the scheduler (`aiconnectx-scheduler`) as systemd services running as the non-root user `aiconnectx`, and Caddy serving the dashboard and landing page on `https://<your domain>` with a free certificate, proxying `/api` to the API on `127.0.0.1:8000`. A nightly timer backs up the database. Only ports 22, 80 and 443 are open.

## 1. Create the VM

1. Create an Oracle Cloud account and pick a home region (it cannot be changed later).
2. Compute > Instances > Create instance:
   - Image: **Ubuntu 22.04** (aarch64 for Ampere).
   - Shape: **VM.Standard.A1.Flex**, 2 OCPU and 12 GB RAM (do not use the 1 GB AMD micro shape: Chromium and Postgres will not fit).
   - Boot volume 50-100 GB, add your SSH public key.
   - If A1 capacity is full, retry later or try another availability domain.
3. Note the VM's public IP.

## 2. Open ports 80 and 443 in Oracle's network

The VM firewall (ufw) is set up by the install script, but Oracle also filters traffic in the cloud network:

Networking > Virtual cloud networks > your VCN > Security Lists > Default Security List > **Add Ingress Rules**:

| Source CIDR | Protocol | Destination port |
|---|---|---|
| 0.0.0.0/0 | TCP | 80 |
| 0.0.0.0/0 | TCP | 443 |
| 0.0.0.0/0 | UDP | 443 (optional, HTTP/3) |

Keep the existing rule for port 22. Add nothing else: 8000 (API) and 5432 (Postgres) must stay closed. They listen on 127.0.0.1 only.

## 3. Point a domain at the VM

At your DNS provider create an **A record** `agent.example.com` (use your own name) pointing to the VM's public IP. Wait until `nslookup agent.example.com` returns that IP. Caddy cannot get a certificate before this works. If the domain is on Cloudflare, set the record to **DNS only** (grey cloud) for the first run.

## 4. Get the code and fill in `.env`

```bash
ssh ubuntu@<vm-ip>
sudo apt-get update && sudo apt-get install -y git
git clone https://github.com/ang4d28/Aiconnectx--outreach-agent.git aiconnectx
cd aiconnectx
cp .env.example .env
nano .env
```

Set at least:

- `POSTGRES_USER=aiconnectx` (not `postgres`), `POSTGRES_DB=aiconnectx` and a strong `POSTGRES_PASSWORD`. The install script creates this user and database with that password.
- `JWT_SECRET`: 32+ random characters (`openssl rand -hex 32`).
- One LLM key (`GROQ_API_KEY`), `WHATSAPP_*`, `MS_CLIENT_ID` / `MS_TENANT` / `OUTLOOK_SENDER_ADDRESS`, `MS_AUTH_CLIENT_ID` / `MS_AUTH_CLIENT_SECRET`, `TPO_NAME` / `TPO_PHONE` / `TPO_EMAIL`, and optionally `GOOGLE_SHEET_ID`.
- **Keep `DRY_RUN=true`.** The script also sets `APP_ENV=production`, `BACKEND_URL` and `FRONTEND_URL` to `https://<your domain>`, `POSTGRES_HOST=localhost` and `BACKEND_RELOAD=false`.

Optional landing-page figures: create `frontend/.env.local` with `VITE_STAT_COMPANIES`, `VITE_STAT_STUDENTS`, `VITE_STAT_MOUS` before installing (unset ones are hidden).

If you use Google Sheets, copy the service-account JSON to `secrets/gspread-service-account.json` and share the sheet with that account as an editor.

## 5. Run the installer

```bash
sudo DOMAIN=agent.example.com ./deploy/install.sh
```

It takes several minutes. It installs PostgreSQL, Python 3.11 (deadsnakes PPA), Node 20, Caddy and ufw; creates the `aiconnectx` system user, the database and user; creates `backend/.venv`; installs the backend and Chromium; runs `alembic upgrade head`; builds the frontend (`npm run build`) into `/var/www/aiconnectx`; installs and starts the services; and enables the firewall for 22, 80 and 443 only. It is safe to run again.

## 6. Create the first admin

```bash
cd ~/aiconnectx/backend
sudo -u aiconnectx .venv/bin/python -m app.cli create-admin --email you@example.com
```

It prompts for a password. Then open `https://agent.example.com/login` and use the **Admin** tab. Others register or sign in with Microsoft and wait as `pending` until you approve them under Users.

## 7. Microsoft sign-in: add the live redirect URI

Azure portal > Microsoft Entra ID > App registrations > the app whose client ID is `MS_AUTH_CLIENT_ID` > Authentication > Web > **Add URI**:

```
https://agent.example.com/api/auth/microsoft/callback
```

Save. This must match `BACKEND_URL` + `/api/auth/microsoft/callback` exactly (https, no trailing slash). Keep the localhost URI if you still develop locally. If sign-in later redirects with `?auth_error=...`, the URI or `MS_AUTH_TENANT` is the usual cause.

## 8. WhatsApp: set the Meta webhook

Meta for Developers > your app > WhatsApp > Configuration > Webhook > Edit:

- Callback URL: `https://agent.example.com/api/whatsapp/webhook`
- Verify token: the value of `WHATSAPP_VERIFY_TOKEN` in `.env`
- Verify and save, then subscribe to the **messages** field.

Check from your laptop (should print `123`):

```bash
curl "https://agent.example.com/api/whatsapp/webhook?hub.mode=subscribe&hub.verify_token=<WHATSAPP_VERIFY_TOKEN>&hub.challenge=123"
```

Message the bot `STATUS` from `WHATSAPP_OWNER_NUMBER`; the reply confirms it works. Outside WhatsApp's 24-hour window only approved templates are delivered, so create a template named `owner_notification` with one text variable `{{1}}`. The app secret (`WHATSAPP_APP_SECRET`) is used to verify webhook signatures.

## 9. One-time logins (as the service user)

Outlook (device code; open the URL it prints and sign in as the sender mailbox):

```bash
cd ~/aiconnectx/backend
sudo -u aiconnectx .venv/bin/python -c "import logging; logging.basicConfig(level=logging.WARNING); from app.channels.outlook import get_client; get_client().tokens.get_token(); print('Outlook token saved')"
```

Test mail without leaving DRY_RUN: set `SEND_ALLOWLIST` to your own address and run `sudo -u aiconnectx .venv/bin/python -m app.cli test-channel outlook --to you@example.com`.

LinkedIn needs a visible browser for the first login and a profile made on Linux. From a Linux desktop (or WSL2 with GUI) run `python -m app.channels.linkedin login` with `LINKEDIN_PROFILE_DIR=./li-profile`, then copy it to the server:

```bash
scp -r li-profile ubuntu@<vm-ip>:/tmp/li-profile
ssh ubuntu@<vm-ip> 'sudo rm -rf ~ubuntu/aiconnectx/secrets/linkedin-profile && sudo mv /tmp/li-profile ~ubuntu/aiconnectx/secrets/linkedin-profile && sudo chown -R aiconnectx:aiconnectx ~ubuntu/aiconnectx/secrets'
sudo systemctl restart aiconnectx-scheduler
```

A new IP can trigger a LinkedIn security check; the channel then disables itself and alerts you (see the main README, Operations).

## 10. Verify

```bash
systemctl status aiconnectx-backend aiconnectx-scheduler caddy --no-pager
journalctl -u aiconnectx-scheduler -n 50 --no-pager     # expect "Scheduler started in DRY_RUN"
curl -s https://agent.example.com/api/health
sudo ss -tlnp | grep -E ':(22|80|443|8000|5432)\b'      # 8000 and 5432 on 127.0.0.1 only
sudo ufw status verbose                                  # only 22, 80, 443 allowed
systemctl list-timers aiconnectx-backup.timer
```

From your laptop, `nmap -Pn <vm-ip> -p 22,80,443,5432,8000` should show 22, 80 and 443 open and the rest closed or filtered.

## 11. Go live

Let it run in DRY_RUN for a few days and check drafts, WhatsApp previews and the 20:00 summary. Then turn DRY_RUN off in dashboard Settings (it asks for confirmation) or set `DRY_RUN=false` in `.env`, and `sudo systemctl restart aiconnectx-backend aiconnectx-scheduler`. Send `STOP` on WhatsApp at any time to pause sending.

## Operations

| Task | Command |
|---|---|
| Logs | `journalctl -u aiconnectx-backend -f`, `journalctl -u aiconnectx-scheduler -f`, `journalctl -u caddy -f` |
| Restart after editing `.env` | `sudo systemctl restart aiconnectx-backend aiconnectx-scheduler` |
| Update | `cd ~/aiconnectx && git pull && sudo DOMAIN=agent.example.com ./deploy/install.sh` |
| Backup now | `sudo -u aiconnectx ~/aiconnectx/deploy/backup.sh` |
| Backups | nightly 02:30 (server time) to `~/aiconnectx/backups/aiconnectx-<timestamp>.sql.gz`, newest 7 kept |
| Restore | `gunzip -c backups/<file>.sql.gz \| psql -h localhost -U aiconnectx aiconnectx` (stop the two services first) |

Copy the backups folder off the VM now and then: it is on the same disk as the database.
