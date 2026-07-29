"""Вход и выход."""

from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, request, url_for

from app.db import session_scope
from app.journal import log_action
from app.models import User
from app.security import (
    current_user,
    login_attempts_exceeded,
    login_required,
    login_user,
    logout_user,
    mark_login,
    register_failed_login,
    reset_login_attempts,
    verify_password,
)

bp = Blueprint("auth", __name__)


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user() is not None:
        return redirect(url_for("projects.index"))

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""

        if login_attempts_exceeded(username):
            flash("Слишком много неудачных попыток. Подожди несколько минут.", "error")
            return render_template("login.html", username=username), 429

        with session_scope() as db:
            user = db.query(User).filter(User.username == username).one_or_none()
            valid = user is not None and user.is_active and verify_password(
                user.password_hash, password
            )
            if valid:
                log_action(db, user=user, action="login")

        if not valid:
            register_failed_login(username)
            # Не уточняем, что именно неверно: это подсказка для перебора.
            flash("Неверный логин или пароль.", "error")
            return render_template("login.html", username=username), 401

        reset_login_attempts(username)
        login_user(user)
        mark_login(user)

        target = request.args.get("next") or ""
        # Открытый редирект: пускаем только на свои же относительные адреса.
        if not target.startswith("/") or target.startswith("//"):
            target = url_for("projects.index")
        return redirect(target)

    return render_template("login.html", username="")


@bp.post("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("auth.login"))
