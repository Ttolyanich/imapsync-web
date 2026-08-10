"""Схема данных.

Иерархия: Эндпоинт (сервер) -> Проект (пара эндпоинтов) -> Ящик (мэппинг).

Аутентификация — свойство эндпоинта, а не строки списка. Тогда при смене
схемы доступа (был общий админский пароль, стали пароли приложений) не надо
переделывать выгрузку на двести строк.

Статусы и роли — обычные строки, а не Enum: SQLite их всё равно хранит текстом,
а миграции Alembic с Enum в SQLite выходят неоправданно болезненными.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

# --- роли ------------------------------------------------------------------

ROLE_ADMIN = "admin"
ROLE_OPERATOR = "operator"

# --- статусы проекта -------------------------------------------------------

PROJECT_DRAFT = "draft"          # мастер не закончен
PROJECT_READY = "ready"          # список импортирован и проверен
PROJECT_RUNNING = "running"
PROJECT_PAUSED = "paused"
PROJECT_DONE = "done"

# --- статусы ящика ---------------------------------------------------------

MB_NEW = "new"
MB_CHECKING = "checking"
MB_CHECK_OK = "check_ok"
MB_CHECK_FAILED = "check_failed"
MB_QUEUED = "queued"
MB_RUNNING = "running"
MB_DONE = "done"
MB_FAILED = "failed"

# --- режимы аутентификации к почте ----------------------------------------

AUTH_PASSWORD = "password"      # пароль на каждый ящик (в т.ч. пароли приложений)
AUTH_MASTER = "master"          # один админский пароль, логин вида user*admin
AUTH_XOAUTH2 = "xoauth2"        # готовый access token

# --- стороны ---------------------------------------------------------------

SIDE_SRC = "src"
SIDE_DST = "dst"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(16), default=ROLE_OPERATOR, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime)

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN


class Endpoint(Base):
    """Почтовый сервер. Одна и та же настройка переиспользуется в проектах."""

    __tablename__ = "endpoints"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    preset: Mapped[str] = mapped_column(String(64), default="generic-imap", nullable=False)

    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, default=993, nullable=False)
    security: Mapped[str] = mapped_column(String(16), default="ssl", nullable=False)  # ssl|starttls|none
    verify_cert: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    auth_mode: Mapped[str] = mapped_column(String(16), default=AUTH_PASSWORD, nullable=False)
    # Заполняется только для master/xoauth2: логин администратора либо владелец токена.
    master_username: Mapped[str | None] = mapped_column(String(255))
    master_secret_enc: Mapped[str | None] = mapped_column(Text)
    # Разделитель проксирующего логина: у Dovecot/Zimbra это 'user*admin'.
    master_separator: Mapped[str] = mapped_column(String(4), default="*", nullable=False)

    # Потолок безопасности для этого сервера. Троттлинг банит IP, а за корпоративным
    # NAT это IP всего офиса — поэтому лимит живёт на сервере, а не только в проекте.
    max_parallel: Mapped[int] = mapped_column(Integer, default=3, nullable=False)

    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    created_by: Mapped[User | None] = relationship()

    def __repr__(self) -> str:  # pragma: no cover - для отладки
        return f"<Endpoint {self.name} {self.host}:{self.port}>"


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default=PROJECT_DRAFT, nullable=False)
    # На каком шаге остановился мастер: 200 строк нельзя терять из-за закрытой вкладки.
    wizard_step: Mapped[str] = mapped_column(String(32), default="source", nullable=False)

    author_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    src_endpoint_id: Mapped[int | None] = mapped_column(ForeignKey("endpoints.id", ondelete="SET NULL"))
    dst_endpoint_id: Mapped[int | None] = mapped_column(ForeignKey("endpoints.id", ondelete="SET NULL"))

    # Настройки переноса
    max_parallel: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    migrate_trash: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    migrate_spam: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # что делать с папками, которых нет в таблице соответствий: create|container|skip
    unknown_folder_policy: Mapped[str] = mapped_column(String(16), default="create", nullable=False)
    unknown_folder_container: Mapped[str | None] = mapped_column(String(255))
    # 0 = без ограничения. Письмо крупнее лимита пропускается и попадает в отчёт —
    # обрезать его нельзя, это была бы тихая потеря данных.
    max_message_size_mb: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Кэш imapsync ускоряет повторные прогоны, но создаёт файл на каждое письмо
    # и растёт с каждым прогоном. На боевом сервере он однажды выбрал миллион
    # inode-ов файловой системы. У самого imapsync он выключен по умолчанию —
    # держим так же и включаем только осознанно.
    use_cache: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )
    # По нему считается простой для автоочистки кредов.
    last_activity_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    credentials_purged_at: Mapped[datetime | None] = mapped_column(DateTime)

    author: Mapped[User | None] = relationship()
    src_endpoint: Mapped[Endpoint | None] = relationship(foreign_keys=[src_endpoint_id])
    dst_endpoint: Mapped[Endpoint | None] = relationship(foreign_keys=[dst_endpoint_id])
    mailboxes: Mapped[list["Mailbox"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class Mailbox(Base):
    """Одна строка списка: откуда -> куда, плюс всё, что мы про неё узнали."""

    __tablename__ = "mailboxes"
    __table_args__ = (
        UniqueConstraint("project_id", "src_email", name="uq_mailbox_project_src"),
        Index("ix_mailbox_project_status", "project_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )

    src_email: Mapped[str] = mapped_column(String(320), nullable=False)
    dst_email: Mapped[str] = mapped_column(String(320), nullable=False)
    src_password_enc: Mapped[str | None] = mapped_column(Text)
    dst_password_enc: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)

    status: Mapped[str] = mapped_column(String(16), default=MB_NEW, nullable=False)

    # Результат проверки доступов по каждой стороне отдельно: человеку важно
    # знать, где именно отказ — на источнике или на приёмнике.
    src_check_result: Mapped[str | None] = mapped_column(String(32))
    dst_check_result: Mapped[str | None] = mapped_column(String(32))
    src_check_detail: Mapped[str | None] = mapped_column(Text)
    dst_check_detail: Mapped[str | None] = mapped_column(Text)

    # Липкий флаг: строка, упавшая по неверному паролю, при повторной проверке
    # пропускается. В AD пять попыток — и учётка заблокирована на час.
    auth_locked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    src_auth_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    dst_auth_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Инвентаризация: знаменатель прогресса и baseline для отчёта о сверке.
    total_messages: Mapped[int | None] = mapped_column(Integer)
    total_bytes: Mapped[int | None] = mapped_column(Integer)
    dst_quota_limit_bytes: Mapped[int | None] = mapped_column(Integer)
    dst_quota_used_bytes: Mapped[int | None] = mapped_column(Integer)

    done_messages: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    done_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Сверка: что реально лежит на приёмнике. Считать «доехало» по собственным
    # счётчикам переноса — самообман; для отчёта опрашиваем сам приёмник.
    dst_total_messages: Mapped[int | None] = mapped_column(Integer)
    dst_total_bytes: Mapped[int | None] = mapped_column(Integer)
    reconciled_at: Mapped[datetime | None] = mapped_column(DateTime)

    # Состояние переноса
    run_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    current_folder: Mapped[str | None] = mapped_column(String(1024))
    last_exit_code: Mapped[int | None] = mapped_column(Integer)
    last_error: Mapped[str | None] = mapped_column(Text)
    log_filename: Mapped[str | None] = mapped_column(String(255))

    checked_at: Mapped[datetime | None] = mapped_column(DateTime)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    project: Mapped[Project] = relationship(back_populates="mailboxes")
    folders: Mapped[list["MailboxFolder"]] = relationship(
        back_populates="mailbox", cascade="all, delete-orphan"
    )

    @property
    def quota_will_overflow(self) -> bool:
        """Предупреждение до старта, а не сюрприз на четвёртом часу."""
        if not self.total_bytes or not self.dst_quota_limit_bytes:
            return False
        free = self.dst_quota_limit_bytes - (self.dst_quota_used_bytes or 0)
        return self.total_bytes > free


class MailboxFolder(Base):
    """Папка, найденная при инвентаризации. Храним обе стороны."""

    __tablename__ = "mailbox_folders"
    __table_args__ = (
        Index("ix_folder_mailbox_side", "mailbox_id", "side"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    mailbox_id: Mapped[int] = mapped_column(
        ForeignKey("mailboxes.id", ondelete="CASCADE"), nullable=False
    )
    side: Mapped[str] = mapped_column(String(4), nullable=False)  # src|dst

    # name_raw — как отдал сервер (в т.ч. modified UTF-7),
    # name_display — то, что показываем человеку.
    name_raw: Mapped[str] = mapped_column(String(1024), nullable=False)
    name_display: Mapped[str] = mapped_column(String(1024), nullable=False)
    delimiter: Mapped[str | None] = mapped_column(String(4))
    special_use: Mapped[str | None] = mapped_column(String(32))  # \Sent, \Trash, ...
    selectable: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    messages: Mapped[int | None] = mapped_column(Integer)
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    uidvalidity: Mapped[int | None] = mapped_column(Integer)

    mailbox: Mapped[Mailbox] = relationship(back_populates="folders")


class FolderMapping(Base):
    """Таблица соответствий папок — одна на проект, подтверждается один раз.

    Если делать её на каждый ящик, минимализма не будет никогда.
    """

    __tablename__ = "folder_mappings"
    __table_args__ = (
        UniqueConstraint("project_id", "src_name", name="uq_mapping_project_src"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    src_name: Mapped[str] = mapped_column(String(1024), nullable=False)
    dst_name: Mapped[str | None] = mapped_column(String(1024))
    action: Mapped[str] = mapped_column(String(16), default="map", nullable=False)  # map|skip
    # откуда взялось соответствие: special-use | dictionary | manual
    origin: Mapped[str] = mapped_column(String(16), default="manual", nullable=False)
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class Event(Base):
    """Событийная лента — десятки строк на ящик, а не десятки тысяч.

    Именно её читают в 95% случаев; сырой лог imapsync лежит файлом отдельно
    и живёт ограниченное время.
    """

    __tablename__ = "events"
    __table_args__ = (
        Index("ix_event_project_ts", "project_id", "ts"),
        Index("ix_event_mailbox_ts", "mailbox_id", "ts"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    mailbox_id: Mapped[int | None] = mapped_column(ForeignKey("mailboxes.id", ondelete="CASCADE"))
    level: Mapped[str] = mapped_column(String(8), default="info", nullable=False)
    code: Mapped[str] = mapped_column(String(48), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)


class AuditLog(Base):
    """Кто что сделал. Для инструмента, который лазает в чужую почту, это не
    бюрократия, а ответ на вопрос «кто качал ящик директора»."""

    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_ts", "ts"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    username: Mapped[str | None] = mapped_column(String(64))  # копия — пользователя могут удалить
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(32))
    target_id: Mapped[int | None] = mapped_column(Integer)
    details: Mapped[str | None] = mapped_column(Text)
