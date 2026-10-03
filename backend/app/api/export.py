from __future__ import annotations

from datetime import datetime
from io import BytesIO
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from openpyxl import Workbook
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dashboard import leads_statement
from app.channels.sheets import (
    HEADER,
    build_rows,
    last_synced,
    rows_statement,
    sheet_url,
    sheets_enabled,
    sync_and_record,
    to_row,
)
from app.db import get_session
from app.db.models import ChannelType, Outreach, OutreachState
from app.workers.send_queue import utc_now

router = APIRouter(tags=["export"])

XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
COLUMN_WIDTHS = {"A": 6, "B": 24, "C": 22, "D": 22, "E": 15, "F": 10, "G": 16, "H": 17, "I": 17, "J": 17, "K": 50}


def build_workbook(rows: list[list]) -> Workbook:
    book = Workbook()
    sheet = book.active
    sheet.title = "Outreach"
    sheet.append(HEADER)
    for row in rows:
        sheet.append(row)
        for cell in sheet[sheet.max_row]:
            if isinstance(cell.value, str):
                cell.data_type = "s"  # text stays text, even when it starts with "="
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    for letter, width in COLUMN_WIDTHS.items():
        sheet.column_dimensions[letter].width = width
    return book


def workbook_bytes(rows: list[list]) -> bytes:
    buffer = BytesIO()
    build_workbook(rows).save(buffer)
    return buffer.getvalue()


def filtered_rows(
    db: Session, state: str | None, channel: str | None, source: str | None, q: str | None
) -> list[list[Any]]:
    """The sheet's rows, narrowed with the same filters as the Leads page."""
    if not (state or channel or source or (q and q.strip())):
        return build_rows(db)
    # leads_statement is the Leads page's own filter; reuse it so both always agree.
    matching = leads_statement(state, channel, source, q).with_only_columns(
        Outreach.id, maintain_column_froms=True
    ).order_by(None)
    stmt = rows_statement().where(Outreach.id.in_(matching))
    return [to_row(*record) for record in db.execute(stmt).all()]


@router.get("/export.xlsx")
def export_xlsx(
    state: OutreachState | None = Query(None, alias="status"),
    channel: ChannelType | None = None,
    source: Literal["post", "search"] | None = None,
    q: str | None = Query(None, max_length=100),
    db: Session = Depends(get_session),
) -> StreamingResponse:
    """Download the outreach table, optionally filtered like the Leads page (same rows as the Google Sheet)."""
    rows = filtered_rows(db, state.value if state else None, channel.value if channel else None, source, q)
    stamp = utc_now().strftime("%Y%m%d-%H%M")
    return StreamingResponse(
        BytesIO(workbook_bytes(rows)),
        media_type=XLSX_TYPE,
        headers={"Content-Disposition": f'attachment; filename="outreach-{stamp}.xlsx"'},
    )


def iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


@router.get("/sheet")
def sheet_info(db: Session = Depends(get_session)) -> dict[str, Any]:
    """Link and last sync time for the Google Sheet; not configured when GOOGLE_SHEET_ID is empty."""
    if not sheets_enabled():
        return {"configured": False, "url": None, "last_synced_at": None}
    return {"configured": True, "url": sheet_url(), "last_synced_at": iso(last_synced(db))}


@router.post("/sheet/sync")
async def sync_now(db: Session = Depends(get_session)) -> dict[str, Any]:
    """Push the current table to the Google Sheet now (admin and member)."""
    if not sheets_enabled():
        raise HTTPException(status.HTTP_409_CONFLICT, "Google Sheet is not set up (GOOGLE_SHEET_ID is empty)")
    report = await sync_and_record(db)
    if report is None:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "The Google Sheet sync failed. Check the service account and sheet sharing.")
    return {"updated": report.updated, "added": report.added, "last_synced_at": iso(last_synced(db))}
