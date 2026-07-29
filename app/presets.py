"""Каталог пресетов серверов.

Пресет — это данные, а не код: YAML-файл в presets/. Добавить почтовый сервис
можно пул-реквестом с одним файлом. Это и есть заявленная универсальность —
предусмотреть все серверы мира самостоятельно всё равно невозможно.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

from app.config import PRESETS_DIR

log = logging.getLogger(__name__)

FOLDER_ROLES = ("inbox", "sent", "drafts", "trash", "junk", "archive")

# Флаги SPECIAL-USE (RFC 6154) -> наши роли. Это первый и самый надёжный
# источник: сервер сам говорит, какая папка чем является.
SPECIAL_USE_TO_ROLE = {
    "\\inbox": "inbox",
    "\\sent": "sent",
    "\\drafts": "drafts",
    "\\trash": "trash",
    "\\junk": "junk",
    "\\archive": "archive",
    "\\all": "archive",
}

GENERIC_PRESET_ID = "generic-imap"


@dataclass(frozen=True)
class Preset:
    id: str
    name: str
    icon: str
    verified: bool
    roles: tuple[str, ...]
    host: str
    port: int
    security: str
    verify_cert: bool
    auth_mode: str
    max_parallel: int
    folders: dict[str, str]
    hints: tuple[str, ...] = ()
    imapsync_args: tuple[str, ...] = ()

    def folder_for_role(self, role: str) -> str | None:
        """Как называется папка этой роли на данном сервере."""
        return self.folders.get(role)

    def can_be(self, side: str) -> bool:
        """side: 'source' или 'destination'."""
        return side in self.roles


@dataclass(frozen=True)
class FolderRoleDictionary:
    """Запасной способ опознать спецпапку — по имени, когда сервер молчит."""

    by_name: dict[str, str] = field(default_factory=dict)
    never_migrate: frozenset[str] = frozenset()

    def role_for_name(self, name: str) -> str | None:
        return self.by_name.get(_normalize(name))

    def is_never_migrate(self, name: str) -> bool:
        return _normalize(name) in self.never_migrate


def _normalize(name: str) -> str:
    return name.strip().casefold()


def _read_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


@lru_cache(maxsize=1)
def load_folder_dictionary() -> FolderRoleDictionary:
    path = PRESETS_DIR / "_folder_roles.yaml"
    if not path.exists():
        log.warning("Нет %s — опознание спецпапок будет только по SPECIAL-USE", path)
        return FolderRoleDictionary()

    raw = _read_yaml(path)
    by_name: dict[str, str] = {}
    for role, names in (raw.get("roles") or {}).items():
        for name in names or []:
            by_name[_normalize(str(name))] = role

    never = frozenset(_normalize(str(n)) for n in (raw.get("never_migrate") or []))
    return FolderRoleDictionary(by_name=by_name, never_migrate=never)


@lru_cache(maxsize=1)
def load_presets() -> dict[str, Preset]:
    presets: dict[str, Preset] = {}

    if not PRESETS_DIR.exists():
        log.error("Каталог пресетов %s не найден", PRESETS_DIR)
        return presets

    for path in sorted(PRESETS_DIR.glob("*.yaml")):
        if path.name.startswith("_"):  # служебные файлы вроде словаря папок
            continue
        try:
            raw = _read_yaml(path)
            preset = Preset(
                id=str(raw["id"]),
                name=str(raw.get("name") or raw["id"]),
                icon=str(raw.get("icon") or "generic.svg"),
                verified=bool(raw.get("verified", False)),
                roles=tuple(raw.get("roles") or ("source", "destination")),
                host=str(raw.get("host") or ""),
                port=int(raw.get("port") or 993),
                security=str(raw.get("security") or "ssl"),
                verify_cert=bool(raw.get("verify_cert", True)),
                auth_mode=str(raw.get("auth_mode") or "password"),
                max_parallel=int(raw.get("max_parallel") or 3),
                folders={str(k): str(v) for k, v in (raw.get("folders") or {}).items()},
                hints=tuple(str(h) for h in (raw.get("hints") or [])),
                imapsync_args=tuple(str(a) for a in (raw.get("imapsync_args") or [])),
            )
        except (KeyError, ValueError, TypeError, OSError, yaml.YAMLError) as exc:
            # Пресеты приходят пул-реквестами от посторонних людей. Синтаксическая
            # ошибка в одном файле не должна ронять всю панель — пропускаем его
            # и говорим об этом в логе.
            log.error("Пресет %s пропущен из-за ошибки в файле: %s", path.name, exc)
            continue

        presets[preset.id] = preset

    return presets


def get_preset(preset_id: str | None) -> Preset | None:
    return load_presets().get(preset_id or "")


def presets_for_side(side: str) -> list[Preset]:
    """Плитки для шага мастера. «Другой IMAP» всегда последний."""
    items = [p for p in load_presets().values() if p.can_be(side)]
    items.sort(key=lambda p: (p.id == GENERIC_PRESET_ID, p.name.casefold()))
    return items


def detect_role(name: str, special_use: str | None = None) -> str | None:
    """Опознать роль папки: сначала спрашиваем сервер, потом словарь.

    Порядок принципиален. Флаг SPECIAL-USE — это утверждение самого сервера,
    и оно всегда точнее догадки по названию.
    """
    if special_use:
        role = SPECIAL_USE_TO_ROLE.get(special_use.strip().casefold())
        if role:
            return role

    if _normalize(name) == "inbox":
        return "inbox"

    return load_folder_dictionary().role_for_name(name)


def reload_catalog() -> None:
    """Сбросить кэш — чтобы правка YAML подхватывалась без перезапуска."""
    load_presets.cache_clear()
    load_folder_dictionary.cache_clear()
