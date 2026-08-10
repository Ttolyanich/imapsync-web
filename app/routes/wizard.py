"""Мастер нового проекта.

Шаги: откуда -> куда -> список ящиков -> маппинг колонок -> превью -> запись.

Черновик сохраняется на каждом шаге: список на двести строк нельзя терять
из-за случайно закрытой вкладки.

Загруженный файл живёт на диске только между шагами импорта и удаляется сразу
после записи в БД — иначе файл с двумя сотнями паролей остался бы в томе навсегда.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from flask import (
    Blueprint,
    abort,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from app.config import UPLOAD_DIR
from app.crypto import encrypt
from app.db import session_scope
from app.importer import (
    FIELDS,
    ParsedTable,
    build_preview,
    generate_from_addresses,
    guess_mapping,
    parse_upload,
)
from app.journal import log_action, log_event
from app.models import PROJECT_DRAFT, PROJECT_READY, Endpoint, Mailbox, Project
from app.presets import get_preset, presets_for_side
from app.security import can_edit_project, current_user, login_required

log = logging.getLogger(__name__)

bp = Blueprint("wizard", __name__, url_prefix="/wizard")

ALLOWED_SUFFIXES = {".csv", ".txt", ".xlsx", ".xlsm"}
SIDES = {"source": "src_endpoint_id", "destination": "dst_endpoint_id"}


# --- шаг 0: создание черновика --------------------------------------------


@bp.route("/new", methods=["GET", "POST"])
@login_required
def new():
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        if not name:
            flash("Дай проекту название — по нему его будут искать через полгода.", "error")
            return render_template("wizard/new.html")

        user = current_user()
        with session_scope() as db:
            project = Project(name=name, author_id=user.id if user else None,
                              status=PROJECT_DRAFT, wizard_step="source")
            db.add(project)
            db.flush()
            log_action(db, user=user, action="project_created",
                       target_type="project", target_id=project.id, details=name)
            log_event(db, project_id=project.id, code="project_created",
                      message=f"Проект «{name}» создан.")
            project_id = project.id

        return redirect(url_for("wizard.choose_endpoint", project_id=project_id, side="source"))

    return render_template("wizard/new.html")


# --- шаги 1-2: откуда и куда ----------------------------------------------


@bp.route("/<int:project_id>/endpoint/<side>", methods=["GET", "POST"])
@login_required
def choose_endpoint(project_id: int, side: str):
    if side not in SIDES:
        abort(404)

    with session_scope() as db:
        project = _editable(db, project_id)

        if request.method == "POST":
            endpoint_id = request.form.get("endpoint_id")
            if not endpoint_id:
                flash("Выбери сервер или создай новый.", "error")
            else:
                setattr(project, SIDES[side], int(endpoint_id))
                project.wizard_step = "destination" if side == "source" else "import"
                project.last_activity_at = datetime.now(timezone.utc)

                # Дефолт параллельности берём с более осторожного из серверов:
                # ограничение почтовых сервисов обычно действует на IP-адрес.
                _sync_parallel_default(db, project)

                nxt = (
                    url_for("wizard.choose_endpoint", project_id=project_id, side="destination")
                    if side == "source"
                    else url_for("wizard.import_list", project_id=project_id)
                )
                return redirect(nxt)

        selected = request.args.get("selected", type=int) or getattr(project, SIDES[side])
        existing = db.query(Endpoint).order_by(Endpoint.name).all()
        options = [
            {"id": e.id, "name": e.name, "host": e.host, "port": e.port,
             "preset": get_preset(e.preset)}
            for e in existing
        ]
        context = {
            "project": {"id": project.id, "name": project.name},
            "side": side,
            "title": "Откуда переносим" if side == "source" else "Куда переносим",
            "presets": presets_for_side(side),
            "options": options,
            "selected": selected,
        }

    return render_template("wizard/endpoint.html", **context)


# --- шаг 3: список ящиков --------------------------------------------------


@bp.route("/<int:project_id>/import", methods=["GET", "POST"])
@login_required
def import_list(project_id: int):
    with session_scope() as db:
        project = _editable(db, project_id)
        context = {"project": {"id": project.id, "name": project.name}}

    if request.method == "POST":
        mode = request.form.get("mode") or "file"

        if mode == "generate":
            addresses = (request.form.get("addresses") or "").splitlines()
            template = (request.form.get("template") or "").strip()
            if not template:
                flash("Укажи шаблон адреса приёмника, например {local}@newdomain.ru", "error")
                return render_template("wizard/import.html", **context)
            table = generate_from_addresses(addresses, template)
            if not table.rows:
                flash("Список адресов пуст.", "error")
                return render_template("wizard/import.html", **context)
            _store_generated(project_id, table)
            return redirect(url_for("wizard.mapping", project_id=project_id))

        upload = request.files.get("file")
        if upload is None or not upload.filename:
            flash("Выбери файл со списком ящиков.", "error")
            return render_template("wizard/import.html", **context)

        suffix = Path(upload.filename).suffix.lower()
        if suffix not in ALLOWED_SUFFIXES:
            flash("Поддерживаются CSV и xlsx.", "error")
            return render_template("wizard/import.html", **context)

        _clear_upload(project_id)
        target = UPLOAD_DIR / f"project_{project_id}{suffix}"
        upload.save(target)
        return redirect(url_for("wizard.mapping", project_id=project_id))

    return render_template("wizard/import.html", **context)


# --- шаг 4: маппинг колонок и превью --------------------------------------


@bp.get("/<int:project_id>/mapping")
@login_required
def mapping(project_id: int):
    with session_scope() as db:
        project = _editable(db, project_id)
        project_view = {"id": project.id, "name": project.name}
        already = (
            db.query(Mailbox).filter(Mailbox.project_id == project_id).count()
        )

    table = _load_table(project_id)
    if table is None:
        flash("Файл не найден — загрузи список заново.", "error")
        return redirect(url_for("wizard.import_list", project_id=project_id))

    guessed = guess_mapping(table.headers)
    preview = build_preview(table, guessed)

    return render_template(
        "wizard/mapping.html",
        project=project_view,
        table=table,
        mapping=guessed,
        preview=preview,
        fields=FIELDS,
        already=already,
    )


@bp.post("/<int:project_id>/preview")
@login_required
def preview(project_id: int):
    """Пересчёт превью при изменении маппинга — подгружается htmx."""
    with session_scope() as db:
        _editable(db, project_id)

    table = _load_table(project_id)
    if table is None:
        abort(404)

    selected = _mapping_from_form()
    trim = bool(request.form.get("trim_passwords"))
    result = build_preview(table, selected, trim_passwords=trim)
    return render_template("partials/import_preview.html", preview=result, trim=trim)


# --- шаг 5: запись ---------------------------------------------------------


@bp.post("/<int:project_id>/commit")
@login_required
def commit(project_id: int):
    table = _load_table(project_id)
    if table is None:
        flash("Файл не найден — загрузи список заново.", "error")
        return redirect(url_for("wizard.import_list", project_id=project_id))

    selected = _mapping_from_form()
    trim = bool(request.form.get("trim_passwords"))
    result = build_preview(table, selected, trim_passwords=trim)

    importable = [row for row in result.rows if row.importable]
    if not importable:
        flash("Импортировать нечего: во всех строках ошибки.", "error")
        return redirect(url_for("wizard.mapping", project_id=project_id))

    # Дописать к существующему списку или заменить его целиком. Раньше выбора
    # не было и импорт всегда заменял — из-за этого на каждую новую партию
    # ящиков заводили отдельный проект, хотя клиент один.
    append = request.form.get("import_mode") == "append"

    with session_scope() as db:
        project = _editable(db, project_id)

        removed = 0
        if not append:
            removed = (
                db.query(Mailbox).filter(Mailbox.project_id == project_id)
                .delete(synchronize_session=False)
            )
            existing: set[str] = set()
        else:
            existing = {
                email.casefold()
                for (email,) in db.query(Mailbox.src_email)
                .filter(Mailbox.project_id == project_id)
                .all()
            }

        added = 0
        duplicates = 0
        for row in importable:
            if row.src_email.casefold() in existing:
                # Такой ящик в проекте уже есть — молча плодить дубли нельзя.
                duplicates += 1
                continue
            existing.add(row.src_email.casefold())
            added += 1
            db.add(
                Mailbox(
                    project_id=project_id,
                    src_email=row.src_email,
                    dst_email=row.dst_email,
                    src_password_enc=encrypt(row.src_password),
                    dst_password_enc=encrypt(row.dst_password),
                    note=row.note or None,
                )
            )

        project.status = PROJECT_READY
        project.wizard_step = "check"
        project.last_activity_at = datetime.now(timezone.utc)

        skipped = result.total - len(importable)
        log_action(db, user=current_user(), action="mailboxes_imported",
                   target_type="project", target_id=project_id,
                   details=(f"{'дописано' if append else 'загружено'} {added}, "
                            f"с ошибками {skipped}, уже были {duplicates}"))
        log_event(
            db, project_id=project_id, code="import_committed",
            message=(
                (f"К списку добавлено {added} ящиков" if append
                 else f"Импортировано {added} ящиков")
                + (f", пропущено с ошибками {skipped}" if skipped else "")
                + (f", уже были в проекте {duplicates}" if duplicates else "")
                + (f", прежний список ({removed}) заменён" if removed else "")
                + "."
            ),
        )

    # Файл с паролями на диске больше не нужен.
    _clear_upload(project_id)

    message = (f"Добавлено {added} ящиков." if append
               else f"Импортировано {added} ящиков.")
    if duplicates:
        message += f" Уже были в проекте: {duplicates}."
    flash(message + " Теперь можно проверить доступы.", "ok")
    return redirect(url_for("projects.view", project_id=project_id))


# --- вспомогательное -------------------------------------------------------


def _editable(db, project_id: int) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        abort(404)
    if not can_edit_project(project):
        abort(403)
    return project


def _upload_path(project_id: int) -> Path | None:
    for suffix in ALLOWED_SUFFIXES:
        candidate = UPLOAD_DIR / f"project_{project_id}{suffix}"
        if candidate.exists():
            return candidate
    return None


def _clear_upload(project_id: int) -> None:
    for suffix in ALLOWED_SUFFIXES:
        candidate = UPLOAD_DIR / f"project_{project_id}{suffix}"
        if candidate.exists():
            try:
                candidate.unlink()
            except OSError as exc:
                log.warning("Не удалось удалить %s: %s", candidate, exc)


def _load_table(project_id: int) -> ParsedTable | None:
    path = _upload_path(project_id)
    if path is None:
        return None
    try:
        return parse_upload(path)
    except Exception as exc:  # noqa: BLE001 — кривой файл не должен ронять страницу
        log.warning("Не удалось разобрать %s: %s", path, exc)
        return None


def _store_generated(project_id: int, table: ParsedTable) -> None:
    """Сгенерированный список кладём тем же файлом, чтобы дальше шёл общий путь."""
    import csv

    _clear_upload(project_id)
    target = UPLOAD_DIR / f"project_{project_id}.csv"
    with target.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(table.headers)
        writer.writerows(table.rows)


def _mapping_from_form() -> dict[str, int | None]:
    selected: dict[str, int | None] = {}
    for field in FIELDS:
        raw = request.form.get(f"col_{field}")
        selected[field] = int(raw) if raw not in (None, "", "-1") else None
    return selected


def _sync_parallel_default(db, project: Project) -> None:
    limits = []
    for attr in SIDES.values():
        endpoint_id = getattr(project, attr)
        if endpoint_id:
            endpoint = db.get(Endpoint, endpoint_id)
            if endpoint:
                limits.append(endpoint.max_parallel)
    if limits:
        project.max_parallel = min(limits)
