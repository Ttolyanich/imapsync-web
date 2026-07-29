"""Пользователи и журнал действий.

Пароли ящиков панель не показывает никому, включая администратора, — поэтому
управление пользователями здесь про доступ к панели, а не про доступ к почте.

Журнал действий для инструмента, который лазает в чужую почту, — не бюрократия,
а ответ на вопрос «кто вообще качал ящик директора».
"""

from __future__ import annotations

import secrets

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from app.db import session_scope
from app.journal import log_action
from app.models import ROLE_ADMIN, ROLE_OPERATOR, AuditLog, User
from app.security import (
    ROLES,
    admin_required,
    current_user,
    hash_password,
    login_required,
    logout_user,
    verify_password,
)

bp = Blueprint("settings", __name__, url_prefix="/settings")

AUDIT_PAGE = 300

ACTION_TITLES = {
    "login": "вход в панель",
    "project_created": "создан проект",
    "project_deleted": "удалён проект",
    "project_completed": "проект завершён",
    "project_settings_changed": "изменены настройки проекта",
    "mailboxes_imported": "импортирован список ящиков",
    "check_started": "запущена проверка доступов",
    "check_stopped": "остановлена проверка доступов",
    "migrate_started": "запущен перенос",
    "migrate_resync": "запущен досинхрон",
    "migrate_stopped": "остановлен перенос",
    "mailbox_resync": "досинхрон ящика",
    "mailbox_unlocked": "снята блокировка повторных попыток",
    "folders_confirmed": "подтверждены соответствия папок",
    "reconcile_started": "запущена сверка",
    "credentials_purged": "стёрты пароли проекта",
    "endpoint_created": "добавлен сервер",
    "endpoint_updated": "изменён сервер",
    "endpoint_deleted": "удалён сервер",
    "user_created": "создан пользователь",
    "user_role_changed": "изменена роль пользователя",
    "user_password_reset": "сброшен пароль пользователя",
    "user_deactivated": "пользователь отключён",
    "user_activated": "пользователь включён",
    "password_changed": "смена собственного пароля",
    "admin_password_reset": "аварийный сброс пароля администратора",
}


@bp.get("/")
@login_required
def index():
    user = current_user()
    with session_scope() as db:
        users = []
        if user.is_admin:
            users = [
                {
                    "id": u.id, "username": u.username, "role": u.role,
                    "role_title": ROLES.get(u.role, u.role), "is_active": u.is_active,
                    "created_at": u.created_at, "last_login_at": u.last_login_at,
                    "is_me": u.id == user.id,
                }
                for u in db.query(User).order_by(User.username).all()
            ]

        entries = []
        if user.is_admin:
            entries = [
                {
                    "ts": e.ts, "username": e.username or "—",
                    "action": ACTION_TITLES.get(e.action, e.action),
                    "target": f"{e.target_type} #{e.target_id}" if e.target_type else "",
                    "details": e.details or "",
                }
                for e in db.query(AuditLog).order_by(AuditLog.id.desc()).limit(AUDIT_PAGE).all()
            ]

    return render_template("settings.html", users=users, entries=entries, roles=ROLES)


@bp.post("/users")
@admin_required
def create_user():
    username = (request.form.get("username") or "").strip()
    role = request.form.get("role") or ROLE_OPERATOR
    password = request.form.get("password") or ""

    if not username:
        flash("Логин обязателен.", "error")
        return redirect(url_for("settings.index"))
    if role not in ROLES:
        role = ROLE_OPERATOR

    generated = None
    if not password:
        # Никаких предсказуемых паролей по умолчанию: показываем один раз
        # и больше нигде не храним в открытом виде.
        generated = secrets.token_urlsafe(12)
        password = generated

    with session_scope() as db:
        if db.query(User).filter(User.username == username).one_or_none() is not None:
            flash(f"Пользователь «{username}» уже есть.", "error")
            return redirect(url_for("settings.index"))

        user = User(username=username, password_hash=hash_password(password), role=role)
        db.add(user)
        db.flush()
        log_action(db, user=current_user(), action="user_created",
                   target_type="user", target_id=user.id, details=f"{username}, {role}")

    if generated:
        flash(
            f"Пользователь «{username}» создан. Временный пароль: {generated} — "
            "передай его лично, здесь он больше не появится.",
            "ok",
        )
    else:
        flash(f"Пользователь «{username}» создан.", "ok")
    return redirect(url_for("settings.index"))


@bp.post("/users/<int:user_id>/role")
@admin_required
def change_role(user_id: int):
    role = request.form.get("role") or ROLE_OPERATOR
    if role not in ROLES:
        abort(400)

    with session_scope() as db:
        user = db.get(User, user_id)
        if user is None:
            abort(404)
        if user.id == current_user().id and role != ROLE_ADMIN:
            # Иначе единственный администратор может разжаловать сам себя
            # и запереть панель.
            flash("Нельзя снять с себя роль администратора.", "error")
            return redirect(url_for("settings.index"))

        user.role = role
        log_action(db, user=current_user(), action="user_role_changed",
                   target_type="user", target_id=user_id,
                   details=f"{user.username} -> {role}")

    flash("Роль изменена.", "ok")
    return redirect(url_for("settings.index"))


@bp.post("/users/<int:user_id>/reset")
@admin_required
def reset_password(user_id: int):
    # Свой пароль меняют только в блоке «Мой пароль» — там он задаётся
    # осознанно и с подтверждением текущего, а не выдаётся случайной строкой
    # самому себе через управление чужими учётными записями.
    if user_id == current_user().id:
        flash("Свой пароль меняется в блоке «Мой пароль».", "error")
        return redirect(url_for("settings.index"))

    password = secrets.token_urlsafe(12)
    with session_scope() as db:
        user = db.get(User, user_id)
        if user is None:
            abort(404)
        user.password_hash = hash_password(password)
        username = user.username
        log_action(db, user=current_user(), action="user_password_reset",
                   target_type="user", target_id=user_id, details=username)

    flash(
        f"Пароль пользователя «{username}» сброшен. Временный пароль: {password} — "
        "передай его лично, здесь он больше не появится.",
        "ok",
    )
    return redirect(url_for("settings.index"))


@bp.post("/users/<int:user_id>/toggle")
@admin_required
def toggle_user(user_id: int):
    with session_scope() as db:
        user = db.get(User, user_id)
        if user is None:
            abort(404)
        if user.id == current_user().id:
            flash("Нельзя отключить самого себя.", "error")
            return redirect(url_for("settings.index"))

        if user.is_active:
            active_admins = (
                db.query(User)
                .filter(User.role == ROLE_ADMIN, User.is_active.is_(True))
                .count()
            )
            if user.role == ROLE_ADMIN and active_admins <= 1:
                flash("Это последний активный администратор — отключать нельзя.", "error")
                return redirect(url_for("settings.index"))

        user.is_active = not user.is_active
        log_action(
            db, user=current_user(),
            action="user_activated" if user.is_active else "user_deactivated",
            target_type="user", target_id=user_id, details=user.username,
        )
        state = "включён" if user.is_active else "отключён"

    flash(f"Пользователь {state}.", "ok")
    return redirect(url_for("settings.index"))


@bp.post("/password")
@login_required
def change_password():
    """Смена собственного пароля.

    Пароль администратора из .env действует только при первом запуске — дальше
    он меняется здесь, иначе в файле на диске остался бы вечный бэкдор.
    """
    current = request.form.get("current_password") or ""
    new = request.form.get("new_password") or ""
    repeat = request.form.get("repeat_password") or ""

    if len(new) < 8:
        flash("Новый пароль короче восьми символов.", "error")
        return redirect(url_for("settings.index"))
    if new != repeat:
        flash("Новые пароли не совпадают.", "error")
        return redirect(url_for("settings.index"))

    user_id = current_user().id
    with session_scope() as db:
        user = db.get(User, user_id)
        if user is None or not verify_password(user.password_hash, current):
            flash("Текущий пароль неверен.", "error")
            return redirect(url_for("settings.index"))
        user.password_hash = hash_password(new)
        log_action(db, user=user, action="password_changed",
                   target_type="user", target_id=user.id)

    # Пароль сменился — старая сессия больше не должна работать.
    logout_user()
    flash("Пароль изменён, войди заново.", "ok")
    return redirect(url_for("auth.login"))
