"""Гарантия единственного управляющего процесса.

Если gunicorn запустить с несколькими воркерами, фоновые потоки поднимутся
в каждом — и один и тот же ящик поедет проверяться (а на этапе 2 и мигрировать)
в нескольких экземплярах одновременно. С почтой это не «немного медленнее»,
это гонка и потенциальные дубли.

В образе воркер жёстко один, но пользователь может поправить конфиг «чтобы
быстрее работало». Файловый лок делает такую правку безвредной: фоновые
задачи возьмёт только тот процесс, который первым захватил лок.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from app.config import DATA_DIR

log = logging.getLogger(__name__)

_LOCK_PATH = DATA_DIR / "supervisor.lock"
_handle = None  # держим файл открытым: закроется — снимется лок


def acquire_supervisor_lock() -> bool:
    """True, если этот процесс стал управляющим."""
    global _handle

    if _handle is not None:
        return True

    try:
        handle = open(_LOCK_PATH, "a+b")
    except OSError as exc:
        log.error("Не удалось открыть файл блокировки %s: %s", _LOCK_PATH, exc)
        return False

    try:
        _lock_exclusive(handle)
    except OSError:
        handle.close()
        log.warning(
            "Фоновые задачи уже ведёт другой процесс — этот работает только как веб-интерфейс. "
            "Если ты увеличил число воркеров gunicorn, верни --workers 1: "
            "несколько управляющих процессов будут мешать друг другу."
        )
        return False

    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()).encode("ascii"))
    handle.flush()
    _handle = handle
    log.info("Процесс %s стал управляющим (фоновые задачи здесь).", os.getpid())
    return True


def _lock_exclusive(handle) -> None:
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
