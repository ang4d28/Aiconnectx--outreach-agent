from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import models
from app.db import schemas


# ─── Company ───────────────────────────────────────────────


def get_company(db: Session, company_id: int) -> Optional[models.Company]:
    return db.get(models.Company, company_id)


def get_company_by_name(db: Session, name: str) -> Optional[models.Company]:
    return db.scalar(select(models.Company).where(models.Company.name == name))


def get_companies(
    db: Session, *, skip: int = 0, limit: int = 100
) -> list[models.Company]:
    return list(
        db.scalars(select(models.Company).offset(skip).limit(limit))
    )


def create_company(
    db: Session, data: schemas.CompanyCreate
) -> models.Company:
    db_company = models.Company(**data.model_dump(exclude_unset=True))
    db.add(db_company)
    db.commit()
    db.refresh(db_company)
    return db_company


def update_company(
    db: Session, db_company: models.Company, data: schemas.CompanyUpdate
) -> models.Company:
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(db_company, field, value)
    db.add(db_company)
    db.commit()
    db.refresh(db_company)
    return db_company


def delete_company(db: Session, company_id: int) -> None:
    db_company = db.get(models.Company, company_id)
    if db_company:
        db.delete(db_company)
        db.commit()


# ─── Contact ───────────────────────────────────────────────


def get_contact(db: Session, contact_id: int) -> Optional[models.Contact]:
    return db.get(models.Contact, contact_id)


def get_contact_by_email(db: Session, email: str) -> Optional[models.Contact]:
    return db.scalar(select(models.Contact).where(models.Contact.email == email))


def get_contacts(
    db: Session, *, skip: int = 0, limit: int = 100
) -> list[models.Contact]:
    return list(db.scalars(select(models.Contact).offset(skip).limit(limit)))


def get_contacts_by_company(
    db: Session, company_id: int, *, skip: int = 0, limit: int = 100
) -> list[models.Contact]:
    return list(
        db.scalars(
            select(models.Contact)
            .where(models.Contact.company_id == company_id)
            .offset(skip)
            .limit(limit)
        )
    )


def create_contact(
    db: Session, data: schemas.ContactCreate
) -> models.Contact:
    db_contact = models.Contact(**data.model_dump(exclude_unset=True))
    db.add(db_contact)
    db.commit()
    db.refresh(db_contact)
    return db_contact


def update_contact(
    db: Session, db_contact: models.Contact, data: schemas.ContactUpdate
) -> models.Contact:
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(db_contact, field, value)
    db.add(db_contact)
    db.commit()
    db.refresh(db_contact)
    return db_contact


def delete_contact(db: Session, contact_id: int) -> None:
    db_contact = db.get(models.Contact, contact_id)
    if db_contact:
        db.delete(db_contact)
        db.commit()


# ─── Post ──────────────────────────────────────────────────


def get_post(db: Session, post_id: int) -> Optional[models.Post]:
    return db.get(models.Post, post_id)


def get_post_by_url(db: Session, url: str) -> Optional[models.Post]:
    return db.scalar(select(models.Post).where(models.Post.url == url))


def get_posts(
    db: Session, *, skip: int = 0, limit: int = 100
) -> list[models.Post]:
    return list(db.scalars(select(models.Post).offset(skip).limit(limit)))


def create_post(
    db: Session, data: schemas.PostCreate
) -> models.Post:
    db_post = models.Post(**data.model_dump(exclude_unset=True))
    db.add(db_post)
    db.commit()
    db.refresh(db_post)
    return db_post


def update_post(
    db: Session, db_post: models.Post, data: schemas.PostUpdate
) -> models.Post:
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(db_post, field, value)
    db.add(db_post)
    db.commit()
    db.refresh(db_post)
    return db_post


def delete_post(db: Session, post_id: int) -> None:
    db_post = db.get(models.Post, post_id)
    if db_post:
        db.delete(db_post)
        db.commit()


# ─── Outreach ──────────────────────────────────────────────


def get_outreach(db: Session, outreach_id: int) -> Optional[models.Outreach]:
    return db.get(models.Outreach, outreach_id)


def get_outreaches(
    db: Session, *, skip: int = 0, limit: int = 100
) -> list[models.Outreach]:
    return list(db.scalars(select(models.Outreach).offset(skip).limit(limit)))


def get_outreaches_by_contact(
    db: Session, contact_id: int, *, skip: int = 0, limit: int = 100
) -> list[models.Outreach]:
    return list(
        db.scalars(
            select(models.Outreach)
            .where(models.Outreach.contact_id == contact_id)
            .offset(skip)
            .limit(limit)
        )
    )


def create_outreach(
    db: Session, data: schemas.OutreachCreate
) -> models.Outreach:
    db_outreach = models.Outreach(**data.model_dump(exclude_unset=True))
    db.add(db_outreach)
    db.commit()
    db.refresh(db_outreach)
    return db_outreach


def update_outreach(
    db: Session,
    db_outreach: models.Outreach,
    data: schemas.OutreachUpdate,
) -> models.Outreach:
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(db_outreach, field, value)
    db.add(db_outreach)
    db.commit()
    db.refresh(db_outreach)
    return db_outreach


def delete_outreach(db: Session, outreach_id: int) -> None:
    db_outreach = db.get(models.Outreach, outreach_id)
    if db_outreach:
        db.delete(db_outreach)
        db.commit()


# ─── Message ───────────────────────────────────────────────


def get_message(db: Session, message_id: int) -> Optional[models.Message]:
    return db.get(models.Message, message_id)


def get_messages(
    db: Session, *, skip: int = 0, limit: int = 100
) -> list[models.Message]:
    return list(db.scalars(select(models.Message).offset(skip).limit(limit)))


def get_messages_by_outreach(
    db: Session, outreach_id: int, *, skip: int = 0, limit: int = 100
) -> list[models.Message]:
    return list(
        db.scalars(
            select(models.Message)
            .where(models.Message.outreach_id == outreach_id)
            .offset(skip)
            .limit(limit)
        )
    )


def create_message(
    db: Session, data: schemas.MessageCreate
) -> models.Message:
    db_message = models.Message(**data.model_dump(exclude_unset=True))
    db.add(db_message)
    db.commit()
    db.refresh(db_message)
    return db_message


def update_message(
    db: Session,
    db_message: models.Message,
    data: schemas.MessageUpdate,
) -> models.Message:
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(db_message, field, value)
    db.add(db_message)
    db.commit()
    db.refresh(db_message)
    return db_message


def delete_message(db: Session, message_id: int) -> None:
    db_message = db.get(models.Message, message_id)
    if db_message:
        db.delete(db_message)
        db.commit()


# ─── Setting ───────────────────────────────────────────────


def get_setting(db: Session, key: str) -> Optional[models.Setting]:
    return db.get(models.Setting, key)


def get_settings(
    db: Session, *, skip: int = 0, limit: int = 100
) -> list[models.Setting]:
    return list(db.scalars(select(models.Setting).offset(skip).limit(limit)))


def create_setting(
    db: Session, data: schemas.SettingCreate
) -> models.Setting:
    db_setting = models.Setting(**data.model_dump(exclude_unset=True))
    db.add(db_setting)
    db.commit()
    db.refresh(db_setting)
    return db_setting


def update_setting(
    db: Session, db_setting: models.Setting, data: schemas.SettingUpdate
) -> models.Setting:
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(db_setting, field, value)
    db.add(db_setting)
    db.commit()
    db.refresh(db_setting)
    return db_setting


def delete_setting(db: Session, key: str) -> None:
    db_setting = db.get(models.Setting, key)
    if db_setting:
        db.delete(db_setting)
        db.commit()
