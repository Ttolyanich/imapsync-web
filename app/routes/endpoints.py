"""Почтовые серверы: список, создание, правка, проверка соединения."""

from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, request, url_for

from app.crypto import encrypt, is_set
from app.db import session_scope
from app.imap_probe import EndpointConfig, probe
from app.journal import log_action
from app.models import AUTH_MASTER, AUTH_PASSWORD, AUTH_XOAUTH2, Endpoint
from app.presets import get_preset, load_presets, presets_for_side
from app.security import admin_required, current_user, login_required

bp = Blueprint("endpoints", __name__, url_prefix="/endpoints")

AUTH_MODES = {
    AUTH_PASSWORD: "Пароль на каждый ящик",
    AUTH_MASTER: "Общий админский пароль (мастер-доступ)",
    AUTH_XOAUTH2: "Токен XOAUTH2",
}

SECURITY_MODES = {
    "ssl": "SSL/TLS (обычно порт 993)",
    "starttls": "STARTTLS (обычно порт 143)",
    "none": "Без шифрования",
}


@bp.get("/")
@login_required
def index():
    with session_scope() as db:
        items = db.query(Endpoint).order_by(Endpoint.name).all()
        rows = [
            {
                "id": e.id,
                "name": e.name,
                "preset": get_preset(e.preset),
                "host": e.host,
                "port": e.port,
                "security": e.security,
                "auth_mode": AUTH_MODES.get(e.auth_mode, e.auth_mode),
                "max_parallel": e.max_parallel,
                "has_secret": is_set(e.master_secret_enc),
            }
            for e in items
        ]
    return render_template("endpoints.html", endpoints=rows, presets=load_presets())


@bp.route("/new", methods=["GET", "POST"])
@admin_required
def create():
    preset_id = request.args.get("preset") or request.form.get("preset") or "generic-imap"
    preset = get_preset(preset_id)

    if request.method == "POST":
        endpoint_id = _save(None)
        if endpoint_id is None:
            return _render_form(None, preset)
        return _after_save(endpoint_id)

    return _render_form(None, preset)


@bp.route("/<int:endpoint_id>/edit", methods=["GET", "POST"])
@admin_required
def edit(endpoint_id: int):
    with session_scope() as db:
        endpoint = db.get(Endpoint, endpoint_id)
        if endpoint is None:
            flash("Сервер не найден.", "error")
            return redirect(url_for("endpoints.index"))
        data = _to_dict(endpoint)
        preset = get_preset(endpoint.preset)

    if request.method == "POST":
        saved = _save(endpoint_id)
        if saved is None:
            return _render_form(data, preset)
        return _after_save(saved)

    return _render_form(data, preset)


@bp.post("/<int:endpoint_id>/delete")
@admin_required
def delete(endpoint_id: int):
    with session_scope() as db:
        endpoint = db.get(Endpoint, endpoint_id)
        if endpoint is not None:
            log_action(
                db, user=current_user(), action="endpoint_deleted",
                target_type="endpoint", target_id=endpoint_id, details=endpoint.name,
            )
            db.delete(endpoint)
    flash("Сервер удалён.", "ok")
    return redirect(url_for("endpoints.index"))


@bp.post("/<int:endpoint_id>/test")
@login_required
def test(endpoint_id: int):
    """Проверка самого соединения — без логина в ящик.

    Отдельно от проверки ящиков специально: там каждая попытка расходует
    лимит до блокировки учётной записи, а тут мы только смотрим, что сервер
    вообще отвечает и каким он себя объявляет.
    """
    with session_scope() as db:
        endpoint = db.get(Endpoint, endpoint_id)
        if endpoint is None:
            return render_template("partials/endpoint_test.html", ok=False,
                                   message="Сервер не найден.")
        cfg = EndpointConfig(
            host=endpoint.host, port=endpoint.port, security=endpoint.security,
            verify_cert=endpoint.verify_cert,
        )

    # Логинимся заведомо несуществующим пользователем: нам нужен только факт
    # установленного соединения и список возможностей сервера.
    result = probe(cfg, "", "", inventory=False)
    if result.capabilities:
        return render_template(
            "partials/endpoint_test.html", ok=True,
            message="Сервер отвечает.", capabilities=result.capabilities,
        )
    if result.error and result.error.is_auth:
        return render_template(
            "partials/endpoint_test.html", ok=True,
            message="Сервер отвечает и требует аутентификацию — соединение в порядке.",
        )
    message = result.error.message if result.error else "Не удалось подключиться."
    hint = result.error.hint if result.error else None
    return render_template("partials/endpoint_test.html", ok=False, message=message, hint=hint)


# --- вспомогательное -------------------------------------------------------


def _render_form(data: dict | None, preset):
    return render_template(
        "endpoint_form.html",
        data=data or _defaults_from_preset(preset),
        preset=preset,
        presets=presets_for_side("source"),
        auth_modes=AUTH_MODES,
        security_modes=SECURITY_MODES,
    )


def _defaults_from_preset(preset) -> dict:
    if preset is None:
        return {
            "id": None, "name": "", "preset": "generic-imap", "host": "", "port": 993,
            "security": "ssl", "verify_cert": True, "auth_mode": AUTH_PASSWORD,
            "master_username": "", "master_separator": "*", "max_parallel": 3,
            "notes": "", "has_secret": False,
        }
    return {
        "id": None,
        "name": preset.name,
        "preset": preset.id,
        "host": preset.host,
        "port": preset.port,
        "security": preset.security,
        "verify_cert": preset.verify_cert,
        "auth_mode": preset.auth_mode,
        "master_username": "",
        "master_separator": "*",
        "max_parallel": preset.max_parallel,
        "notes": "",
        "has_secret": False,
    }


def _to_dict(endpoint: Endpoint) -> dict:
    return {
        "id": endpoint.id,
        "name": endpoint.name,
        "preset": endpoint.preset,
        "host": endpoint.host,
        "port": endpoint.port,
        "security": endpoint.security,
        "verify_cert": endpoint.verify_cert,
        "auth_mode": endpoint.auth_mode,
        "master_username": endpoint.master_username or "",
        "master_separator": endpoint.master_separator,
        "max_parallel": endpoint.max_parallel,
        "notes": endpoint.notes or "",
        "has_secret": is_set(endpoint.master_secret_enc),
    }


def _save(endpoint_id: int | None) -> int | None:
    form = request.form
    name = (form.get("name") or "").strip()
    host = (form.get("host") or "").strip()

    if not name or not host:
        flash("Название и адрес сервера обязательны.", "error")
        return None

    try:
        port = int(form.get("port") or 993)
        max_parallel = max(1, int(form.get("max_parallel") or 3))
    except ValueError:
        flash("Порт и параллельность должны быть числами.", "error")
        return None

    with session_scope() as db:
        endpoint = db.get(Endpoint, endpoint_id) if endpoint_id else Endpoint()
        if endpoint is None:
            flash("Сервер не найден.", "error")
            return None

        endpoint.name = name
        endpoint.preset = form.get("preset") or "generic-imap"
        endpoint.host = host
        endpoint.port = port
        endpoint.security = form.get("security") or "ssl"
        endpoint.verify_cert = bool(form.get("verify_cert"))
        endpoint.auth_mode = form.get("auth_mode") or AUTH_PASSWORD
        endpoint.master_username = (form.get("master_username") or "").strip() or None
        endpoint.master_separator = (form.get("master_separator") or "*").strip() or "*"
        endpoint.max_parallel = max_parallel
        endpoint.notes = (form.get("notes") or "").strip() or None

        # Пустое поле секрета означает «не менять»: показать сохранённый мы
        # всё равно не можем и не будем.
        secret = form.get("master_secret") or ""
        if secret:
            endpoint.master_secret_enc = encrypt(secret)
        elif form.get("clear_secret"):
            endpoint.master_secret_enc = None

        if endpoint_id is None:
            user = current_user()
            endpoint.created_by_id = user.id if user else None
            db.add(endpoint)
            db.flush()

        log_action(
            db, user=current_user(),
            action="endpoint_updated" if endpoint_id else "endpoint_created",
            target_type="endpoint", target_id=endpoint.id, details=endpoint.name,
        )
        return endpoint.id


def _after_save(endpoint_id: int):
    flash("Сервер сохранён.", "ok")
    # Форма могла открыться из мастера проекта — возвращаемся туда же.
    project_id = request.form.get("wizard_project")
    side = request.form.get("wizard_side")
    if project_id and side:
        return redirect(
            url_for("wizard.choose_endpoint", project_id=int(project_id), side=side,
                    selected=endpoint_id)
        )
    return redirect(url_for("endpoints.index"))
