"""Append-only record of every discretionary action.

Anything a human chose to do — override a guest quota, force a second meal,
change someone's plan, revoke a card — lands here with a name attached.
"""
from __future__ import annotations

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.models import AuditLog


def record(
    db: Session,
    actor: str,
    action: str,
    entity_type: str | None = None,
    entity_id: int | None = None,
    detail: dict | None = None,
) -> AuditLog:
    entry = AuditLog(
        actor=actor,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        detail=detail,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def recent(db: Session, limit: int = 200) -> list[AuditLog]:
    stmt = select(AuditLog).order_by(desc(AuditLog.at)).limit(limit)
    return list(db.scalars(stmt))
