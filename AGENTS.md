# AGENTS.md — AI ConnectX Outreach Agent

## PROJECT

AI ConnectX Outreach Agent. Automates university placement outreach: finds LinkedIn hiring posts and companies, picks the right contact (poster if recruiter/founder; otherwise HR for mid-tier, founder for startup, MD for corporate), messages them on LinkedIn and email, tracks everything, handles replies.

## RULES

- **100% free tools only.** No paid APIs. No Claude API, no OpenAI API.
- **LLM layer:** Groq free tier (primary), Gemini free (secondary), Ollama Cloud (fallback: OLLAMA_URL + OLLAMA_API_KEY Bearer auth, POST /api/chat) — all behind one unified interface.
- **DRY_RUN=true by default** — drafts only, nothing actually sent, until explicitly disabled.
- **Tech stack:** Python 3.11, FastAPI, SQLAlchemy 2 + Alembic, PostgreSQL, APScheduler, httpx, Playwright, gspread, Gmail API, python-telegram-bot.
- **Frontend:** Vite + React + TypeScript + Tailwind CSS + recharts + lucide-react.
- **Deployment:** Docker Compose. Secrets only in `.env` — never hardcoded.

## STRUCTURE

```
.
├── AGENTS.md
├── docker-compose.yml
├── .env.example
├── backend/
│   ├── app/
│   │   ├── __init__.py
│   │   ├── main.py                          # FastAPI app entry
│   │   ├── workers/                         # APScheduler + outreach logic
│   │   │   ├── __init__.py
│   │   │   ├── scheduler.py
│   │   │   ├── outreach_worker.py
│   │   │   └── followup_worker.py
│   │   ├── channels/                        # LinkedIn, Email, Telegram integrations
│   │   │   ├── __init__.py
│   │   │   ├── linkedin.py
│   │   │   ├── email.py                     # Gmail API
│   │   │   └── telegram.py                  # python-telegram-bot
│   │   ├── llm/                             # Unified LLM interface
│   │   │   ├── __init__.py
│   │   │   ├── client.py                    # Provider-agnostic facade
│   │   │   ├── groq_provider.py
│   │   │   ├── gemini_provider.py
│   │   │   └── ollama_provider.py
│   │   ├── db/                              # SQLAlchemy models, schemas, repo
│   │   │   ├── __init__.py
│   │   │   ├── models.py
│   │   │   ├── schemas.py
│   │   │   ├── repository.py
│   │   │   └── alembic/
│   │   │       └── versions/                # migration scripts
│   │   └── api/                             # FastAPI routes
│   │       ├── __init__.py
│   │       ├── main.py
│   │       └── routes/
│   │           ├── __init__.py
│   │           ├── outreach.py
│   │           ├── companies.py
│   │           ├── persons.py
│   │           ├── tracking.py
│   │           └── control.py               # pause/resume, caps, config
│   ├── tests/
│   │   ├── __init__.py
│   │   ├── conftest.py
│   │   ├── test_outreach.py
│   │   ├── test_state_machine.py
│   │   ├── test_guardrails.py
│   │   └── test_llm.py
│   └── Dockerfile
├── frontend/
│   ├── index.html
│   ├── package.json
│   ├── tsconfig.json
│   ├── vite.config.ts
│   ├── tailwind.config.ts
│   ├── postcss.config.ts
│   └── src/
│       ├── main.tsx
│       ├── App.tsx
│       ├── App.css
│       ├── vite-env.d.ts
│       ├── components/
│       ├── pages/
│       ├── hooks/
│       ├── lib/
│       ├── types/
│       └── styles/
└── .env.example
```

## STATE MACHINE

```
NEW > QUALIFIED > TARGETED > ENRICHED > DRAFTED > CONTACTED > REPLIED
                                                          > REPLIED > {INTERESTED, NO_OPPORTUNITY, WRONG_PERSON, NOT_NOW, UNSUBSCRIBED}

If no reply after CONTACTED:
  - Follow-up day 4
  - Follow-up day 10
  - Then CLOSED
```

## GUARDRAILS

| Rule | Cap / Window |
|------|-------------|
| LinkedIn messages | 20 / day |
| Email messages | 40 / day |
| Random delays | Yes — jitter between operations |
| Send window | 10:00–17:00 IST only |
| One touch per person | Every 14 days minimum |
| Unsubscribe | Permanent block on that person/company |
| Global pause | Single flag to halt all sending |

## CODE STYLE

- Python: **type hints** on all functions. **Small functions** — do one thing, do it well.
- Tests: **pytest** for all pure logic (state machine transitions, guardrail checks, LLM provider selection, contact-role heuristics).
- No comments explaining obvious code. No docstrings on trivial functions.
- Frontend: TypeScript strict, components in `src/components/`, pages in `src/pages/`, API client in `src/lib/`.

## DEVELOPMENT WORKFLOW

1. `cp .env.example .env` and fill in secrets.
2. `docker-compose up` — PostgreSQL, Redis (APScheduler backing), backend, frontend.
3. `cd backend && uv run pytest` — run logic tests.
4. `cd frontend && npm run dev` — frontend hot reload.
5. Default `DRY_RUN=true` in `.env` — verify everything in drafts before going live.
