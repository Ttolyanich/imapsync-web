"""Конфигурация из окружения плюс управление ключами.

Ключи (шифрование кредов и подпись сессий) берутся из переменных окружения.
Если их нет, генерируем и сохраняем в файл с правами 0600 внутри DATA_DIR —
иначе первый же перезапуск контейнера превратил бы все сохранённые пароли
в мусор, а сессии слетали бы на каждом деплое. При этом громко просим
перенести ключ в .env: файл рядом с БД — это компромисс, а не рекомендация.
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "да"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("%s=%r не число, беру значение по умолчанию %s", name, raw, default)
        return default


DATA_DIR = Path(os.environ.get("DATA_DIR") or "./data").resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "imapsync-web.db"
DATABASE_URL = os.environ.get("DATABASE_URL") or f"sqlite:///{DB_PATH}"

# Кэш imapsync. Держим на постоянном томе: без него каждый повторный прогон
# заново вычитывает заголовки всего ящика. На корректность не влияет.
IMAPSYNC_CACHE_DIR = DATA_DIR / "imapsync-cache"
RAW_LOG_DIR = DATA_DIR / "logs"
UPLOAD_DIR = DATA_DIR / "uploads"

for _d in (IMAPSYNC_CACHE_DIR, RAW_LOG_DIR, UPLOAD_DIR):
    _d.mkdir(parents=True, exist_ok=True)

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin").strip() or "admin"
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
ADMIN_PASSWORD_RESET = _env_bool("ADMIN_PASSWORD_RESET", False)

RAW_LOG_RETENTION_DAYS = _env_int("RAW_LOG_RETENTION_DAYS", 14)
CREDENTIALS_PURGE_DAYS = _env_int("CREDENTIALS_PURGE_DAYS", 30)

# Предохранители фазы проверки доступов. Меняются осознанно, не «чтобы быстрее».
AUTH_FAILURE_ABORT_RATIO = float(os.environ.get("AUTH_FAILURE_ABORT_RATIO", "0.2"))
AUTH_FAILURE_ABORT_MIN_CHECKED = _env_int("AUTH_FAILURE_ABORT_MIN_CHECKED", 5)
DEFAULT_MAX_PARALLEL = _env_int("DEFAULT_MAX_PARALLEL", 3)
IMAP_TIMEOUT_SECONDS = _env_int("IMAP_TIMEOUT_SECONDS", 30)

PRESETS_DIR = Path(__file__).resolve().parent.parent / "presets"


def _load_or_create_key(env_name: str, filename: str, generator, consequence: str) -> str:
    """Берём ключ из окружения, иначе из файла, иначе создаём новый."""
    from_env = os.environ.get(env_name, "").strip()
    if from_env:
        return from_env

    key_file = DATA_DIR / filename
    if key_file.exists():
        value = key_file.read_text(encoding="ascii").strip()
        if value:
            return value

    value = generator()
    key_file.write_text(value, encoding="ascii")
    try:
        key_file.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        # Windows и часть сетевых ФС прав не поддерживают — не повод падать.
        pass

    log.warning(
        "%s не задан. Сгенерирован новый ключ и сохранён в %s.\n"
        "    Перенеси его в .env как %s=%s\n"
        "    %s",
        env_name,
        key_file,
        env_name,
        value,
        consequence,
    )
    return value


def _generate_fernet_key() -> str:
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode("ascii")


def _generate_secret_key() -> str:
    import secrets

    return secrets.token_urlsafe(48)


FERNET_KEY = _load_or_create_key(
    "FERNET_KEY",
    "fernet.key",
    _generate_fernet_key,
    "Потеря этого ключа сделает все сохранённые пароли ящиков нечитаемыми.",
)
SECRET_KEY = _load_or_create_key(
    "SECRET_KEY",
    "secret.key",
    _generate_secret_key,
    "При его потере просто разлогинятся все сессии — данные не пострадают.",
)
