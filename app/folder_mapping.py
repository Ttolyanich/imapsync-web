"""Сопоставление папок источника и приёмника.

Таблица соответствий строится **одна на проект**, а не на каждый ящик: иначе
человеку пришлось бы подтверждать её двести раз, и никакого минимализма не
осталось бы. Предложение собирается из инвентаризации всех ящиков сразу —
папки, которые есть хотя бы у кого-то, попадают в общий список.

Роль папки определяется в том же порядке, что и везде: сначала флаги
SPECIAL-USE от самого сервера, потом словарь имён. Имя на приёмнике берётся
из его пресета. Именно это не даёт получить на приёмнике одновременно
«Sent Items» и «Отправленные» с размазанными по ним письмами.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func

from app.models import Endpoint, FolderMapping, Mailbox, MailboxFolder, Project
from app.presets import detect_role, get_preset, load_folder_dictionary

ACTION_MAP = "map"
ACTION_SKIP = "skip"

ORIGIN_SPECIAL_USE = "special-use"
ORIGIN_DICTIONARY = "dictionary"
ORIGIN_MANUAL = "manual"

ROLE_TITLES = {
    "inbox": "Входящие",
    "sent": "Отправленные",
    "drafts": "Черновики",
    "trash": "Корзина",
    "junk": "Спам",
    "archive": "Архив",
}


@dataclass
class FolderRow:
    src_name: str
    dst_name: str | None
    action: str
    origin: str
    role: str | None = None
    mailboxes: int = 0
    messages: int = 0
    size_bytes: int = 0
    confirmed: bool = False
    # Почему предложено именно так — показываем человеку, чтобы он мог
    # осознанно спорить, а не гадать.
    reason: str = ""

    @property
    def role_title(self) -> str | None:
        return ROLE_TITLES.get(self.role) if self.role else None


def build_proposal(session, project: Project) -> list[FolderRow]:
    """Собрать предложение по всему проекту, наложив уже сохранённые правки."""
    dst_endpoint = (
        session.get(Endpoint, project.dst_endpoint_id) if project.dst_endpoint_id else None
    )
    dst_preset = get_preset(dst_endpoint.preset) if dst_endpoint else None
    dictionary = load_folder_dictionary()

    saved = {
        row.src_name: row
        for row in session.query(FolderMapping)
        .filter(FolderMapping.project_id == project.id)
        .all()
    }

    aggregated = (
        session.query(
            MailboxFolder.name_display,
            func.count(func.distinct(MailboxFolder.mailbox_id)),
            func.sum(func.coalesce(MailboxFolder.messages, 0)),
            func.sum(func.coalesce(MailboxFolder.size_bytes, 0)),
            func.max(MailboxFolder.special_use),
        )
        .join(Mailbox, Mailbox.id == MailboxFolder.mailbox_id)
        .filter(Mailbox.project_id == project.id, MailboxFolder.side == "src")
        .group_by(MailboxFolder.name_display)
        .order_by(func.sum(func.coalesce(MailboxFolder.size_bytes, 0)).desc())
        .all()
    )

    rows: list[FolderRow] = []
    for name, mailbox_count, messages, size_bytes, special_use in aggregated:
        row = _propose(name, special_use, dst_preset, dictionary, project)
        row.mailboxes = mailbox_count or 0
        row.messages = int(messages or 0)
        row.size_bytes = int(size_bytes or 0)

        stored = saved.get(name)
        if stored is not None:
            # Правка человека всегда важнее нашей догадки.
            row.dst_name = stored.dst_name
            row.action = stored.action
            row.origin = stored.origin
            row.confirmed = stored.confirmed
            if stored.origin == ORIGIN_MANUAL:
                row.reason = "задано вручную"

        rows.append(row)

    return rows


def _propose(name, special_use, dst_preset, dictionary, project: Project) -> FolderRow:
    role = detect_role(name, special_use)

    if dictionary.is_never_migrate(name):
        return FolderRow(
            src_name=name, dst_name=None, action=ACTION_SKIP, origin=ORIGIN_DICTIONARY,
            role=role, reason="не почтовая папка",
        )

    if role == "junk" and not project.migrate_spam:
        return FolderRow(
            src_name=name, dst_name=None, action=ACTION_SKIP, origin=ORIGIN_DICTIONARY,
            role=role, reason="спам не переносим (можно включить в настройках проекта)",
        )

    if role == "trash" and not project.migrate_trash:
        return FolderRow(
            src_name=name, dst_name=None, action=ACTION_SKIP, origin=ORIGIN_DICTIONARY,
            role=role, reason="корзину не переносим (можно включить в настройках проекта)",
        )

    if role and dst_preset is not None:
        target = dst_preset.folder_for_role(role)
        if target:
            origin = ORIGIN_SPECIAL_USE if special_use else ORIGIN_DICTIONARY
            reason = (
                "сервер сам объявил назначение папки"
                if special_use
                else "опознано по названию"
            )
            return FolderRow(
                src_name=name, dst_name=target, action=ACTION_MAP, origin=origin,
                role=role, reason=reason,
            )

    # Пользовательская папка: переносим как есть либо по политике проекта.
    if project.unknown_folder_policy == "skip":
        return FolderRow(
            src_name=name, dst_name=None, action=ACTION_SKIP, origin=ORIGIN_DICTIONARY,
            role=role, reason="по настройке проекта незнакомые папки пропускаются",
        )

    if project.unknown_folder_policy == "container" and project.unknown_folder_container:
        return FolderRow(
            src_name=name,
            dst_name=f"{project.unknown_folder_container}/{name}",
            action=ACTION_MAP,
            origin=ORIGIN_DICTIONARY,
            role=role,
            reason="пользовательская папка, складываем в контейнер",
        )

    return FolderRow(
        src_name=name, dst_name=name, action=ACTION_MAP, origin=ORIGIN_DICTIONARY,
        role=role, reason="пользовательская папка, переносим как есть",
    )


def save_mapping(session, project_id: int, submitted: dict[str, tuple[str, str]]) -> int:
    """Сохранить подтверждённую таблицу.

    submitted: имя папки источника -> (действие, имя на приёмнике)
    """
    existing = {
        row.src_name: row
        for row in session.query(FolderMapping)
        .filter(FolderMapping.project_id == project_id)
        .all()
    }

    for src_name, (action, dst_name) in submitted.items():
        row = existing.get(src_name)
        if row is None:
            row = FolderMapping(project_id=project_id, src_name=src_name)
            session.add(row)
        row.action = action if action in (ACTION_MAP, ACTION_SKIP) else ACTION_MAP
        row.dst_name = (dst_name or "").strip() or None
        row.origin = ORIGIN_MANUAL
        row.confirmed = True

    return len(submitted)


def is_confirmed(session, project_id: int) -> bool:
    return bool(
        session.query(FolderMapping)
        .filter(FolderMapping.project_id == project_id, FolderMapping.confirmed.is_(True))
        .first()
    )


def effective_plan(session, mailbox_id: int, project: Project, dst_endpoint: Endpoint):
    """Что именно передавать imapsync по конкретному ящику.

    Подтверждённая человеком таблица главнее любых догадок. Папки, которых
    в ней нет (появились у одного ящика позже), обрабатываются той же
    автоматикой, что и при построении предложения, — молча пропускать их
    было бы хуже, чем перенести как есть.
    """
    dst_preset = get_preset(dst_endpoint.preset) if dst_endpoint else None
    dictionary = load_folder_dictionary()

    confirmed = {
        row.src_name: row
        for row in session.query(FolderMapping)
        .filter(FolderMapping.project_id == project.id, FolderMapping.confirmed.is_(True))
        .all()
    }

    folders = (
        session.query(MailboxFolder)
        .filter(MailboxFolder.mailbox_id == mailbox_id, MailboxFolder.side == "src")
        .all()
    )

    mapping: dict[str, str] = {}
    excludes: list[str] = []

    for folder in folders:
        name = folder.name_display

        if not folder.selectable:
            excludes.append(name)
            continue

        decision = confirmed.get(name)
        if decision is not None:
            if decision.action == ACTION_SKIP:
                excludes.append(name)
            elif decision.dst_name and decision.dst_name != name:
                mapping[name] = decision.dst_name
            continue

        row = _propose(name, folder.special_use, dst_preset, dictionary, project)
        if row.action == ACTION_SKIP:
            excludes.append(name)
        elif row.dst_name and row.dst_name != name:
            mapping[name] = row.dst_name

    return mapping, tuple(excludes)
