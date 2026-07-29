"""Шифрование паролей от почтовых ящиков.

Хешировать нельзя: IMAP требует пароль открытым текстом при логине.
Значит шифруем симметрично, ключ держим вне БД.

Честная граница: это защищает от кражи файла БД, но не от того, кто получил
доступ к работающему приложению — оно обязано уметь расшифровать. Отсюда
требование не выставлять панель в интернет и стирать креды после проекта.
"""

from __future__ import annotations

import logging

from cryptography.fernet import Fernet, InvalidToken

from app.config import FERNET_KEY

log = logging.getLogger(__name__)

_fernet = Fernet(FERNET_KEY.encode("ascii"))


def encrypt(value: str | None) -> str | None:
    """Пустое значение остаётся пустым — «пароль не задан» это не то же самое,
    что «зашифрованная пустая строка»."""
    if not value:
        return None
    return _fernet.encrypt(value.encode("utf-8")).decode("ascii")


def decrypt(token: str | None) -> str | None:
    if not token:
        return None
    try:
        return _fernet.decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken:
        # Обычно означает смену FERNET_KEY. Молча возвращать None нельзя —
        # человек должен понять, почему «сохранённые пароли перестали работать».
        log.error(
            "Не удалось расшифровать сохранённый пароль: ключ FERNET_KEY не тот, "
            "которым шифровали. Пароли придётся ввести заново."
        )
        return None


def is_set(token: str | None) -> bool:
    """Для интерфейса: сам пароль не показываем никому, только факт наличия."""
    return bool(token)
