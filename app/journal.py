"""Две ленты записей, которые нельзя путать.

Событийная лента (Event) — что происходило с ящиками и проектами. Компактная:
десятки строк на ящик, а не строка на каждое письмо. Живёт в БД всегда, в том
числе после удаления сырых логов и стирания паролей.

Журнал действий (AuditLog) — кто из людей что сделал. Для инструмента, который
лазает в чужую почту, это не бюрократия, а ответ на вопрос «кто качал ящик
директора».
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.models import AuditLog, Event, User

log = logging.getLogger(__name__)


def log_event(
    session: Session,
    *,
    code: str,
    message: str,
    project_id: int | None = None,
    mailbox_id: int | None = None,
    level: str = "info",
) -> Event:
    event = Event(
        project_id=project_id,
        mailbox_id=mailbox_id,
        level=level,
        code=code,
        message=message,
    )
    session.add(event)
    return event


def log_action(
    session: Session,
    *,
    user: User | None,
    action: str,
    target_type: str | None = None,
    target_id: int | None = None,
    details: str | None = None,
) -> AuditLog:
    entry = AuditLog(
        user_id=user.id if user else None,
        # Имя копируем: пользователя могут удалить, а запись должна остаться читаемой.
        username=user.username if user else None,
        action=action,
        target_type=target_type,
        target_id=target_id,
        details=details,
    )
    session.add(entry)
    return entry
