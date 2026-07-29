"""Вход в панель, роли, CSRF и защита от перебора.

Пароль первого администратора берётся из .env только при первом запуске:
дальше он меняется в интерфейсе. Иначе получился бы вечный бэкдор — пароль
открытым текстом в файле, который нельзя сменить. На случай «забыл пароль»
есть ADMIN_PASSWORD_RESET=1: разовый сброс при старте с записью в журнал.
"""

from __future__ import annotations

import functools
import hmac
import logging
import secrets
import threading
import time
from datetime import datetime, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from flask import abort, g, redirect, request, session, url_for

from app.config import ADMIN_PASSWORD, ADMIN_PASSWORD_RESET, ADMIN_USERNAME
from app.db import session_scope
from app.journal import log_action
from app.models import ROLE_ADMIN, ROLE_OPERATOR, User

log = logging.getLogger(__name__)

_hasher = PasswordHasher()

# Перебор пароля панели: считаем неудачи по паре «IP + логин».
LOGIN_MAX_ATTEMPTS = 8
LOGIN_WINDOW_SECONDS = 300
_attempts: dict[str, list[float]] = {}
_attempts_lock = threading.Lock()

CSRF_SESSION_KEY = "_csrf"
CSRF_FIELD = "csrf_token"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


# --- пароли ----------------------------------------------------------------


def hash_password(raw: str) -> str:
    return _hasher.hash(raw)


def verify_password(stored_hash: str, raw: str) -> bool:
    try:
        return _hasher.verify(stored_hash, raw)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


# --- первый администратор --------------------------------------------------


def bootstrap_admin() -> None:
    """Создать администратора при первом запуске или сбросить пароль по флагу."""
    with session_scope() as db:
        existing = db.query(User).filter(User.username == ADMIN_USERNAME).one_or_none()

        if existing is None:
            password = ADMIN_PASSWORD or secrets.token_urlsafe(12)
            db.add(
                User(
                    username=ADMIN_USERNAME,
                    password_hash=hash_password(password),
                    role=ROLE_ADMIN,
                )
            )
            if ADMIN_PASSWORD:
                log.info("Создан администратор «%s» с паролем из .env", ADMIN_USERNAME)
            else:
                # Никаких admin/admin123 по умолчанию: одноразовый случайный пароль.
                log.warning(
                    "ADMIN_PASSWORD не задан. Создан администратор «%s» с временным "
                    "паролем: %s\n    Смени его после входа.",
                    ADMIN_USERNAME,
                    password,
                )
            return

        if ADMIN_PASSWORD_RESET and ADMIN_PASSWORD:
            existing.password_hash = hash_password(ADMIN_PASSWORD)
            existing.is_active = True
            log_action(
                db,
                user=None,
                action="admin_password_reset",
                target_type="user",
                target_id=existing.id,
                details="Сброс пароля администратора флагом ADMIN_PASSWORD_RESET",
            )
            log.warning(
                "Пароль администратора «%s» сброшен к значению из .env "
                "(ADMIN_PASSWORD_RESET=1). Убери этот флаг и перезапусти контейнер.",
                ADMIN_USERNAME,
            )


# --- сессия ----------------------------------------------------------------


def login_user(user: User) -> None:
    session.clear()
    session["user_id"] = user.id
    session.permanent = True


def logout_user() -> None:
    session.clear()


def load_current_user() -> None:
    """Кладём пользователя в g на каждый запрос."""
    g.user = None
    user_id = session.get("user_id")
    if not user_id:
        return
    with session_scope() as db:
        user = db.get(User, user_id)
        if user and user.is_active:
            g.user = user


def current_user() -> User | None:
    return getattr(g, "user", None)


def login_required(view):
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        if current_user() is None:
            return redirect(url_for("auth.login", next=request.full_path))
        return view(*args, **kwargs)

    return wrapper


def admin_required(view):
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        user = current_user()
        if user is None:
            return redirect(url_for("auth.login", next=request.full_path))
        if not user.is_admin:
            abort(403)
        return view(*args, **kwargs)

    return wrapper


def can_edit_project(project) -> bool:
    """Оператор видит чужие проекты, но не запускает и не правит.

    Полная изоляция мешала бы больше, чем защищала: миграции передают друг
    другу, и вопрос «этот ящик уже мигрировали?» возникает постоянно.
    """
    user = current_user()
    if user is None:
        return False
    if user.is_admin:
        return True
    return project.author_id == user.id


def mark_login(user: User) -> None:
    with session_scope() as db:
        stored = db.get(User, user.id)
        if stored:
            stored.last_login_at = datetime.now(timezone.utc)


# --- защита от перебора ----------------------------------------------------


def _attempt_key(username: str) -> str:
    return f"{request.remote_addr or '?'}|{username.casefold()}"


def login_attempts_exceeded(username: str) -> bool:
    key = _attempt_key(username)
    now = time.monotonic()
    with _attempts_lock:
        history = [t for t in _attempts.get(key, []) if now - t < LOGIN_WINDOW_SECONDS]
        _attempts[key] = history
        return len(history) >= LOGIN_MAX_ATTEMPTS


def register_failed_login(username: str) -> None:
    key = _attempt_key(username)
    with _attempts_lock:
        _attempts.setdefault(key, []).append(time.monotonic())


def reset_login_attempts(username: str) -> None:
    with _attempts_lock:
        _attempts.pop(_attempt_key(username), None)


# --- CSRF ------------------------------------------------------------------


def csrf_token() -> str:
    token = session.get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[CSRF_SESSION_KEY] = token
    return token


def verify_csrf() -> None:
    if request.method in SAFE_METHODS:
        return
    expected = session.get(CSRF_SESSION_KEY)
    provided = request.form.get(CSRF_FIELD) or request.headers.get("X-CSRF-Token", "")
    if not expected or not provided or not hmac.compare_digest(expected, provided):
        abort(400, "Форма устарела. Обнови страницу и попробуй ещё раз.")


ROLES = {ROLE_ADMIN: "Администратор", ROLE_OPERATOR: "Оператор"}
