"""Google Sheets mirror of the outreach table.

The database is the source of truth; the sheet is a read-only view that is
overwritten on every sync (edits made in the sheet are not read back). Rows are
keyed by outreach id in column A and upserted: existing rows are updated in
place, new ones appended. Rows for outreaches deleted from the database are left
as they are.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import quote

from sqlalchemy import func, select

from app.config import settings
from app.db.models import (
    ChannelType,
    Company,
    Contact,
    Message,
    MessageDirection,
    Outreach,
    Post,
    Setting,
)
from app.workers.send_queue import IST, as_utc, utc_now

logger = logging.getLogger(__name__)

HEADER = [
    "ID",
    "Company",
    "Person",
    "Role",
    "Source",
    "Channel",
    "Status",
    "Sent Date",
    "Last Reply",
    "Next Follow-up",
    "Post URL",
]
LAST_COLUMN = "K"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
LAST_SYNC_KEY = "sheets_last_synced"  # settings row: UTC ISO time of the last successful sync
CHANNEL_LABELS = {ChannelType.LINKEDIN: "LinkedIn", ChannelType.EMAIL: "Email"}


class SheetsError(Exception):
    pass


def fmt_dt(value: datetime | None) -> str:
    if value is None:
        return ""
    return as_utc(value).astimezone(IST).strftime("%Y-%m-%d %H:%M")


def to_row(
    outreach: Outreach,
    contact: Contact,
    company: Company | None,
    post_url: str | None,
    last_reply: datetime | None,
) -> list[Any]:
    role = contact.role.value if contact.role else ""
    if contact.title:
        role = f"{role} ({contact.title})" if role else contact.title
    return [
        outreach.id,
        company.name if company else "",
        contact.name,
        role,
        "LinkedIn post" if outreach.post_id else "Company search",
        CHANNEL_LABELS.get(outreach.channel, str(outreach.channel)),
        outreach.state.value if outreach.state else "",
        fmt_dt(outreach.sent_at),
        fmt_dt(last_reply),
        fmt_dt(outreach.follow_up_due),
        post_url or "",
    ]


def rows_statement() -> Any:
    last_reply = (
        select(Message.outreach_id, func.max(Message.sent_at).label("last_reply"))
        .where(Message.direction == MessageDirection.INBOUND)
        .group_by(Message.outreach_id)
        .subquery()
    )
    return (
        select(Outreach, Contact, Company, Post.url, last_reply.c.last_reply)
        .join(Contact, Contact.id == Outreach.contact_id)
        .join(Company, Company.id == Contact.company_id, isouter=True)
        .join(Post, Post.id == Outreach.post_id, isouter=True)
        .join(last_reply, last_reply.c.outreach_id == Outreach.id, isouter=True)
        .order_by(Outreach.id)
    )


def build_rows(db: Any) -> list[list[Any]]:
    """Current state of every outreach, one row each, in HEADER column order."""
    return [to_row(*record) for record in db.execute(rows_statement()).all()]


@dataclass
class SyncReport:
    updated: int = 0
    added: int = 0


def row_range(row_number: int) -> str:
    return f"A{row_number}:{LAST_COLUMN}{row_number}"


class SheetsClient:
    def __init__(
        self,
        worksheet: Any = None,
        sheet_id: str | None = None,
        credentials_file: str | None = None,
    ):
        self._worksheet = worksheet
        self.sheet_id = settings.google_sheet_id if sheet_id is None else sheet_id
        self.credentials_file = credentials_file or settings.google_service_account_file

    @property
    def worksheet(self) -> Any:
        if self._worksheet is None:
            if not self.sheet_id:
                raise SheetsError("GOOGLE_SHEET_ID is not set")
            import gspread

            account = gspread.service_account(filename=self.credentials_file, scopes=SCOPES)
            self._worksheet = account.open_by_key(self.sheet_id).sheet1
        return self._worksheet

    def upsert(self, rows: list[list[Any]]) -> SyncReport:
        ws = self.worksheet
        ids = ws.col_values(1)  # blocking; includes the header at index 0
        report = SyncReport()
        updates: list[dict[str, Any]] = []
        if not ids or ids[0] != HEADER[0]:
            updates.append({"range": row_range(1), "values": [HEADER]})
            ids = [HEADER[0]]
        existing = {value: index + 1 for index, value in enumerate(ids) if index > 0 and value}

        appended: list[list[Any]] = []
        for row in rows:
            row_number = existing.get(str(row[0]))
            if row_number is None:
                appended.append(row)
            else:
                updates.append({"range": row_range(row_number), "values": [row]})
                report.updated += 1
        if updates:
            ws.batch_update(updates, value_input_option="RAW")
        if appended:
            # Start below the last used row so new rows never land in a gap.
            ws.append_rows(appended, value_input_option="RAW", table_range="A1")
            report.added = len(appended)
        return report


_client: SheetsClient | None = None


def get_client() -> SheetsClient:
    global _client
    if _client is None:
        _client = SheetsClient()
    return _client


def sheets_enabled() -> bool:
    return bool(settings.google_sheet_id)


async def sync_sheet(
    db: Any, client: SheetsClient | None = None, rows: list[list[Any]] | None = None
) -> SyncReport | None:
    """Mirror the outreach table. Never raises: a broken sheet must not stop outreach."""
    client = client or get_client()
    if not sheets_enabled() and client._worksheet is None:
        return None
    try:
        data = rows if rows is not None else build_rows(db)
        return await asyncio.to_thread(client.upsert, data)
    except Exception as exc:
        logger.error(f"Google Sheets sync failed: {exc}")
        return None


def record_sync(db: Any, now: datetime | None = None) -> None:
    """Remember when the sheet was last synced (shown on the dashboard)."""
    stamp = (now or utc_now()).isoformat()
    row = db.get(Setting, LAST_SYNC_KEY)
    if row is None:
        db.add(Setting(key=LAST_SYNC_KEY, value=stamp))
    else:
        row.value = stamp
    db.commit()


def last_synced(db: Any) -> datetime | None:
    row = db.get(Setting, LAST_SYNC_KEY)
    try:
        return datetime.fromisoformat(row.value) if row is not None and row.value else None
    except (TypeError, ValueError):
        return None


async def sync_and_record(db: Any) -> SyncReport | None:
    """sync_sheet, plus the last-synced time when it worked."""
    report = await sync_sheet(db)
    if report is not None:
        record_sync(db)
    return report


def sheet_url() -> str | None:
    if not settings.google_sheet_id:
        return None
    return f"https://docs.google.com/spreadsheets/d/{quote(settings.google_sheet_id, safe='')}/edit"
