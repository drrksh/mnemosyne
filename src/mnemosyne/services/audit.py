"""
Audit trail helper.

Audit events are written through the *same* UnitOfWork as the change
they describe, so an audited change and its audit record commit or roll
back together — there is never a change without a record, or a record
of a change that did not happen. Events carry identifiers and metadata,
never memory content: the audit log must be safe to hand to a security
reviewer.
"""
from __future__ import annotations

from typing import Any, Optional
from uuid import uuid4

from mnemosyne.db.unit_of_work import UnitOfWork
from mnemosyne.domain.entities import AuditEvent
from mnemosyne.domain.principal import Principal
from mnemosyne.logging_config import get_request_id
from mnemosyne.utils.time import utcnow


def record(
    uow: UnitOfWork,
    principal: Principal,
    action: str,
    *,
    target_type: str,
    target_id: Optional[str] = None,
    **details: Any,
) -> None:
    request_id = get_request_id()
    uow.audit.add(
        AuditEvent(
            id=uuid4(),
            occurred_at=utcnow(),
            actor_id=principal.user_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            request_id=None if request_id == "-" else request_id,
            details={k: v for k, v in details.items() if v is not None},
        )
    )
