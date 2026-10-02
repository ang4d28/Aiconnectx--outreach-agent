from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.db import repository
from app.db import get_session
from app.db.schemas import CompanyCreate, CompanyRead, CompanyUpdate

router = APIRouter(prefix="/companies", tags=["companies"])


@router.get("/", response_model=list[CompanyRead])
def list_companies(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=200),
    db: Session = Depends(get_session),
) -> list[CompanyRead]:
    return repository.get_companies(db, skip=skip, limit=limit)


@router.get("/{company_id}", response_model=CompanyRead)
def get_company(
    company_id: int,
    db: Session = Depends(get_session),
) -> CompanyRead:
    company = repository.get_company(db, company_id)
    if not company:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Company not found")
    return company


@router.post("/", response_model=CompanyRead, status_code=status.HTTP_201_CREATED)
def create_company(
    data: CompanyCreate,
    db: Session = Depends(get_session),
) -> CompanyRead:
    existing = repository.get_company_by_name(db, data.name)
    if existing:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Company with this name already exists"
        )
    return repository.create_company(db, data)


@router.patch("/{company_id}", response_model=CompanyRead)
def update_company(
    company_id: int,
    data: CompanyUpdate,
    db: Session = Depends(get_session),
) -> CompanyRead:
    db_company = repository.get_company(db, company_id)
    if not db_company:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Company not found")
    if data.name and repository.get_company_by_name(db, data.name):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Company with this name already exists"
        )
    return repository.update_company(db, db_company, data)


@router.delete("/{company_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_company(
    company_id: int,
    db: Session = Depends(get_session),
) -> None:
    db_company = repository.get_company(db, company_id)
    if not db_company:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Company not found")
    repository.delete_company(db, company_id)
