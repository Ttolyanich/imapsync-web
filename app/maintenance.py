"""Фоновая уборка: пароли и сырые логи.

Миграция — процесс конечный. Пароли нужны, пока он идёт, и превращаются в
чистый риск, когда он закончен. Логи imapsync — строка на письмо, то есть
гигабайты на проект; хранить их вечно бессмысленно.

Обе задачи ленивые и идемпотентные: раз в сутки, без расписаний и внешних
планировщиков. Запускается только в управляющем процессе.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from app.config import CREDENTIALS_PURGE_DAYS, RAW_LOG_DIR, RAW_LOG_RETENTION_DAYS
from app.db import session_scope
from app.journal import log_event
from app.models import Mailbox, Project

log = logging.getLogger(__name__)

INTERVAL_SECONDS = 6 * 60 * 60
FIRST_RUN_DELAY_SECONDS = 120


def start_maintenance() -> None:
    threading.Thread(target=_loop, name="maintenance", daemon=True).start()


def _loop() -> None:
    time.sleep(FIRST_RUN_DELAY_SECONDS)
    while True:
        try:
            purge_idle_credentials()
            purge_old_logs()
        except Exception:  # noqa: BLE001 — уборка не должна ронять приложение
            log.exception("Ошибка фоновой уборки")
        time.sleep(INTERVAL_SECONDS)


def purge_idle_credentials() -> int:
    """Стереть пароли у проектов, которые давно никто не трогал.

    Адреса, счётчики, лента событий и отчёты остаются — проект должен читаться
    как документ и через полгода.
    """
    if CREDENTIALS_PURGE_DAYS <= 0:
        return 0

    threshold = datetime.now(timezone.utc) - timedelta(days=CREDENTIALS_PURGE_DAYS)
    purged = 0

    with session_scope() as session:
        projects = (
            session.query(Project)
            .filter(
                Project.credentials_purged_at.is_(None),
                Project.last_activity_at < threshold.replace(tzinfo=None),
            )
            .all()
        )

        for project in projects:
            cleared = (
                session.query(Mailbox)
                .filter(Mailbox.project_id == project.id)
                .update(
                    {Mailbox.src_password_enc: None, Mailbox.dst_password_enc: None},
                    synchronize_session=False,
                )
            )
            project.credentials_purged_at = datetime.now(timezone.utc)
            purged += 1
            log_event(
                session, project_id=project.id, level="warning", code="credentials_auto_purged",
                message=(
                    f"Проект не трогали больше {CREDENTIALS_PURGE_DAYS} дней — "
                    f"пароли ({cleared} ящиков) стёрты автоматически. "
                    "История и отчёты сохранены; для нового прогона пароли нужно "
                    "будет импортировать заново."
                ),
            )

    if purged:
        log.info("Автоочистка паролей: обработано проектов — %s", purged)
    return purged


def purge_old_logs() -> int:
    """Удалить сырые логи старше срока хранения.

    Событийная лента в базе при этом остаётся: именно её читают в большинстве
    случаев, и она должна пережить удаление файлов.
    """
    if RAW_LOG_RETENTION_DAYS <= 0:
        return 0

    cutoff = time.time() - RAW_LOG_RETENTION_DAYS * 86400
    removed = 0

    for path in RAW_LOG_DIR.rglob("mailbox_*.log*"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError as exc:
            log.warning("Не удалось удалить лог %s: %s", path, exc)

    if removed:
        log.info("Удалено устаревших логов: %s", removed)
    return removed
