"""Сборка приложения."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from flask import Flask, render_template

__version__ = "0.1.0"


def create_app() -> Flask:
    # Импорты внутри фабрики: модули приложения тянут app.config, и на уровне
    # модуля это создало бы кольцо.
    from app.config import SECRET_KEY
    from app.security import csrf_token, current_user, load_current_user, verify_csrf

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=SECRET_KEY,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=timedelta(days=7),
        MAX_CONTENT_LENGTH=32 * 1024 * 1024,  # список ящиков не бывает больше
        JSON_AS_ASCII=False,
    )

    app.before_request(load_current_user)
    app.before_request(verify_csrf)

    app.jinja_env.globals.update(
        csrf_token=csrf_token,
        current_user=current_user,
        version=__version__,
    )
    app.jinja_env.filters.update(
        human_size=human_size,
        human_count=human_count,
        local_time=local_time,
    )

    from app.routes.auth import bp as auth_bp
    from app.routes.endpoints import bp as endpoints_bp
    from app.routes.projects import bp as projects_bp
    from app.routes.settings import bp as settings_bp
    from app.routes.wizard import bp as wizard_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(projects_bp)
    app.register_blueprint(endpoints_bp)
    app.register_blueprint(wizard_bp)
    app.register_blueprint(settings_bp)

    @app.errorhandler(403)
    def forbidden(_exc):
        return render_template("error.html", code=403,
                               message="Недостаточно прав для этого действия."), 403

    @app.errorhandler(404)
    def not_found(_exc):
        return render_template("error.html", code=404, message="Страница не найдена."), 404

    @app.errorhandler(413)
    def too_large(_exc):
        return render_template("error.html", code=413,
                               message="Файл слишком большой."), 413

    _startup(app)
    return app


def _startup(app: Flask) -> None:
    from app.security import bootstrap_admin
    from app.singleton import acquire_supervisor_lock

    with app.app_context():
        bootstrap_admin()

    # Фоновые задачи ведёт только один процесс. Остальные (если пользователь
    # поднял несколько воркеров) работают как обычный веб-интерфейс.
    is_supervisor = acquire_supervisor_lock()
    app.config["IS_SUPERVISOR"] = is_supervisor

    if is_supervisor:
        # Ночной прогон, вставший из-за ребута и молча ждущий утра, —
        # это не отказоустойчивость. Поднимаем прерванное сами.
        from app.maintenance import start_maintenance
        from app.migrator import resume_interrupted

        resume_interrupted()
        start_maintenance()


# --- фильтры шаблонов ------------------------------------------------------


def human_size(value: int | None) -> str:
    if value is None:
        return "—"
    size = float(value)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if size < 1024 or unit == "ТБ":
            if unit in ("Б", "КБ"):
                return f"{size:.0f} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} ТБ"


def human_count(value: int | None) -> str:
    if value is None:
        return "—"
    return f"{value:,}".replace(",", " ")


def local_time(value: datetime | None, fmt: str = "%d.%m.%Y %H:%M") -> str:
    if value is None:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone().strftime(fmt)
