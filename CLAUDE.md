# CLAUDE.md

See AGENTS.md for project rules, structure, state machine and guardrails. Where this file differs from AGENTS.md (Telegram removed, WhatsApp added, new states), this file wins.

## Channels (final design)

- **LinkedIn DMs**: main outreach and conversation channel.
- **Outlook email (Microsoft Graph)**: leads with no LinkedIn profile, plus a formal MoU email when a company is interested.
- **WhatsApp**: owner notifications and message review only. Never used to contact leads.
- **Dashboard "Needs attention"**: backup for anything that needs the owner. Backed by the attention_items table.
- All channels implement the Channel protocol in backend/app/channels/base.py: send_message(thread, text), read_new_replies(since), reply(thread, text).

## Rules

- Every outgoing message passes through a hold window before sending. The window is HOLD_WINDOW_MINUTES (default 10). The owner can approve, edit or cancel during the hold on WhatsApp or the dashboard. If nothing happens, the message is auto-released when hold_until passes.
- DRY_RUN=true by default. Nothing is sent until it is explicitly disabled.
- No Telegram anywhere. Do not reintroduce TELEGRAM_* keys or channels/telegram.py.

## States

LinkedIn leads: NEW > QUALIFIED > TARGETED > ENRICHED > DRAFTED > CONNECT_SENT > CONNECTED > DM_SENT > REPLIED.
Email leads skip the connect states: DRAFTED > CONTACTED > REPLIED.
Replies end in INTERESTED, NO_OPPORTUNITY, WRONG_PERSON, NOT_NOW or UNSUBSCRIBED. No reply after follow-ups on day 4 and day 10 ends in CLOSED.

## Data model additions

- messages: hold_until, review_status (pending_review, approved, cancelled, edited, auto_released), edited_text, reviewed_at, whatsapp_message_id.
- attention_items: id, type (interested, question, channel_error, needs_manual), payload, status (open, resolved, dismissed), created_at.
- notifications_log: id, type, payload, sent_at.
- Migration 003_final_design adds all of the above. Migrations 002 and 003 must be applied before running.

## Env keys

MS_CLIENT_ID, MS_TENANT, MS_TOKEN_CACHE_PATH, OUTLOOK_SENDER_ADDRESS, WHATSAPP_PHONE_NUMBER_ID, WHATSAPP_TOKEN, WHATSAPP_VERIFY_TOKEN, WHATSAPP_APP_SECRET, WHATSAPP_OWNER_NUMBER, HOLD_WINDOW_MINUTES.

## Status

DONE (parts 1-6B):
- LLM layer, DB models and repository, post monitor, targeting, enrichment.
- Drafter worker and drafts API (list, approve, reject, edit).
- Outlook channel and email send queue with all guardrails (caps 20 then 40, 60-240s delays, 10:00-17:00 IST, 14-day rule, global pause, do-not-contact list, DRY_RUN).
- Final-design refit: Telegram removed, WhatsApp config added, new states and review fields, Channel protocol, Outlook client implements it.
- Tests: 118 passing.

NEXT: Part 8.

Open items carried forward:
- Hold window and review flow are not wired into the send queue yet. The queue still sends on approval alone.
- Schedule the drafter and send queue in scheduler.py. Add a reply poller.
- First run needs an interactive device-code login to create the Microsoft token cache.
- Frontend drafts review page and "Needs attention" page.
