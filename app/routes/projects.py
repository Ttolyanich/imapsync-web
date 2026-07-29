"""Проекты: список, карточка, запуск проверки, очистка кредов."""

from __future__ import annotations

from datetime import datetime, timezone

from flask import (
    Blueprint,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from sqlalchemy import case, func

from app.checker import get_check, start_check, stop_check
from app.config import RAW_LOG_DIR
from app.db import session_scope
from app.errors import HINTS, human
from app.folder_mapping import build_proposal, is_confirmed, save_mapping
from app.imapsync_runner import describe_exit, read_log_tail
from app.migrator import get_migration, start_migration, stop_migration
from app.reconcile import build_report, get_reconcile, start_reconcile
from app.journal import log_action, log_event
from app.models import (
    MB_CHECK_FAILED,
    MB_CHECK_OK,
    PROJECT_DONE,
    PROJECT_READY,
    Endpoint,
    Event,
    Mailbox,
    Project,
)
from app.presets import get_preset
from app.security import can_edit_project, current_user, login_required

bp = Blueprint("projects", __name__)

EVENTS_PAGE = 200


@bp.get("/")
@login_required
def index():
    with session_scope() as db:
        projects = db.query(Project).order_by(Project.updated_at.desc()).all()
        cards = []
        for project in projects:
            counts = _counts(db, project.id)
            src = db.get(Endpoint, project.src_endpoint_id) if project.src_endpoint_id else None
            dst = db.get(Endpoint, project.dst_endpoint_id) if project.dst_endpoint_id else None
            cards.append(
                {
                    "id": project.id,
                    "name": project.name,
                    "status": project.status,
                    "author": project.author.username if project.author else "—",
                    "src": src.name if src else "не выбран",
                    "dst": dst.name if dst else "не выбран",
                    "src_preset": get_preset(src.preset) if src else None,
                    "dst_preset": get_preset(dst.preset) if dst else None,
                    "created_at": project.created_at,
                    "last_activity_at": project.last_activity_at,
                    "credentials_purged_at": project.credentials_purged_at,
                    "counts": counts,
                    "can_edit": can_edit_project(project),
                }
            )
    return render_template("projects.html", cards=cards)


@bp.get("/projects/<int:project_id>")
@login_required
def view(project_id: int):
    tab = request.args.get("tab") or "mailboxes"
    selected_mailbox = request.args.get("mailbox", type=int)
    with session_scope() as db:
        project = _get(db, project_id)
        context = _project_context(db, project)
    return render_template(
        "project.html", tab=tab, selected_mailbox=selected_mailbox, **context
    )


# --- проверка доступов -----------------------------------------------------


@bp.post("/projects/<int:project_id>/check/start")
@login_required
def check_start(project_id: int):
    force = bool(request.form.get("force"))
    with session_scope() as db:
        project = _get(db, project_id)
        if not can_edit_project(project):
            abort(403)
        log_action(
            db, user=current_user(), action="check_started",
            target_type="project", target_id=project_id,
            details="повторно, включая ранее упавшие" if force else None,
        )
        project.last_activity_at = datetime.now(timezone.utc)

    start_check(project_id, force=force)
    return redirect(url_for("projects.view", project_id=project_id))


@bp.post("/projects/<int:project_id>/check/stop")
@login_required
def check_stop(project_id: int):
    with session_scope() as db:
        project = _get(db, project_id)
        if not can_edit_project(project):
            abort(403)
        log_action(db, user=current_user(), action="check_stopped",
                   target_type="project", target_id=project_id)
    stop_check(project_id)
    return redirect(url_for("projects.view", project_id=project_id))


@bp.get("/projects/<int:project_id>/check/progress")
@login_required
def check_progress(project_id: int):
    """Кусок HTML для htmx: опрашивается раз в 2 секунды.

    Опрос, а не SSE: образ публичный, его ставят за неизвестным прокси, и
    буферизация ломает потоковые ответы чаще, чем хотелось бы.
    """
    runner = get_check(project_id)
    with session_scope() as db:
        project = _get(db, project_id)
        counts = _counts(db, project_id)
    return render_template(
        "partials/check_progress.html",
        progress=runner.progress if runner else None,
        counts=counts,
        project_id=project_id,
        can_edit=can_edit_project(project),
    )


# --- папки -----------------------------------------------------------------


@bp.get("/projects/<int:project_id>/folders")
@login_required
def folders(project_id: int):
    with session_scope() as db:
        project = _get(db, project_id)
        rows = build_proposal(db, project)
        context = _project_context(db, project)
        confirmed = is_confirmed(db, project_id)
    return render_template(
        "project.html", tab="folders", folder_rows=rows,
        folders_confirmed=confirmed, selected_mailbox=None, **context
    )


@bp.post("/projects/<int:project_id>/folders")
@login_required
def save_folders(project_id: int):
    with session_scope() as db:
        project = _get(db, project_id)
        if not can_edit_project(project):
            abort(403)

        submitted: dict[str, tuple[str, str]] = {}
        for key, value in request.form.items():
            if not key.startswith("action__"):
                continue
            src_name = key[len("action__"):]
            submitted[src_name] = (value, request.form.get(f"dst__{src_name}", ""))

        saved = save_mapping(db, project_id, submitted)
        skipped = sum(1 for action, _ in submitted.values() if action == "skip")
        log_action(db, user=current_user(), action="folders_confirmed",
                   target_type="project", target_id=project_id,
                   details=f"{saved} папок, из них пропускаем {skipped}")
        log_event(
            db, project_id=project_id, code="folders_confirmed",
            message=(
                f"Таблица соответствий папок подтверждена: {saved} записей, "
                f"не переносим {skipped}."
            ),
        )
        project.last_activity_at = datetime.now(timezone.utc)

    flash("Соответствия папок сохранены и будут применяться ко всем ящикам.", "ok")
    return redirect(url_for("projects.folders", project_id=project_id))


# --- перенос ---------------------------------------------------------------


@bp.post("/projects/<int:project_id>/migrate/start")
@login_required
def migrate_start(project_id: int):
    """Первый прогон: только те ящики, которые ещё не переносили."""
    return _launch(project_id, include_done=False, action="migrate_started")


@bp.post("/projects/<int:project_id>/migrate/resync")
@login_required
def migrate_resync(project_id: int):
    """«Досинхронить» — тот же прогон повторно, включая уже перенесённые.

    Дублей не будет: imapsync сопоставляет письма по заголовкам и везёт только
    то, чего на приёмнике нет.
    """
    return _launch(project_id, include_done=True, action="migrate_resync")


@bp.post("/projects/<int:project_id>/migrate/stop")
@login_required
def migrate_stop(project_id: int):
    with session_scope() as db:
        project = _get(db, project_id)
        if not can_edit_project(project):
            abort(403)
        log_action(db, user=current_user(), action="migrate_stopped",
                   target_type="project", target_id=project_id)
    stop_migration(project_id)
    flash("Останавливаю. Уже начатые письма дойдут, новые не начнутся.", "ok")
    return redirect(url_for("projects.view", project_id=project_id))


@bp.post("/projects/<int:project_id>/mailboxes/<int:mailbox_id>/resync")
@login_required
def mailbox_resync(project_id: int, mailbox_id: int):
    with session_scope() as db:
        project = _get(db, project_id)
        if not can_edit_project(project):
            abort(403)
        mailbox = db.get(Mailbox, mailbox_id)
        if mailbox is None or mailbox.project_id != project_id:
            abort(404)
        log_action(db, user=current_user(), action="mailbox_resync",
                   target_type="mailbox", target_id=mailbox_id, details=mailbox.src_email)

    start_migration(project_id, include_done=True, only_mailbox_ids=[mailbox_id])
    return redirect(url_for("projects.view", project_id=project_id))


@bp.get("/projects/<int:project_id>/migrate/progress")
@login_required
def migrate_progress(project_id: int):
    runner = get_migration(project_id)
    with session_scope() as db:
        project = _get(db, project_id)
        can_edit = can_edit_project(project)
    return render_template(
        "partials/migrate_progress.html",
        progress=runner.progress if runner else None,
        project_id=project_id,
        can_edit=can_edit,
    )


@bp.get("/projects/<int:project_id>/mailboxes/<int:mailbox_id>/log")
@login_required
def mailbox_log(project_id: int, mailbox_id: int):
    """Хвост сырого лога с указанной позиции.

    Опрос с offset, а не поток: панель ставят за неизвестным прокси, а
    потоковые ответы регулярно ломаются о буферизацию. Разрыв здесь безвреден —
    следующий запрос продолжит с той же позиции.
    """
    offset = request.args.get("from", type=int) or 0
    path = _log_path(project_id, mailbox_id)
    if path is None:
        return jsonify({"text": "", "offset": 0, "missing": True})

    text, new_offset = read_log_tail(path, offset)
    return jsonify({"text": text, "offset": new_offset, "missing": False})


@bp.get("/projects/<int:project_id>/mailboxes/<int:mailbox_id>/log/download")
@login_required
def mailbox_log_download(project_id: int, mailbox_id: int):
    path = _log_path(project_id, mailbox_id)
    if path is None:
        abort(404)
    return send_file(path, as_attachment=True, download_name=path.name)


@bp.get("/projects/<int:project_id>/mailboxes")
@login_required
def mailboxes_partial(project_id: int):
    only_failed = request.args.get("failed") == "1"
    with session_scope() as db:
        project = _get(db, project_id)
        rows = _mailbox_rows(db, project_id, only_failed=only_failed)
    return render_template(
        "partials/mailbox_table.html",
        rows=rows, project_id=project_id, only_failed=only_failed,
        can_edit=can_edit_project(project),
    )


@bp.get("/projects/<int:project_id>/events")
@login_required
def events_partial(project_id: int):
    with session_scope() as db:
        _get(db, project_id)
        events = (
            db.query(Event)
            .filter(Event.project_id == project_id)
            .order_by(Event.id.desc())
            .limit(EVENTS_PAGE)
            .all()
        )
        rows = [
            {"ts": e.ts, "level": e.level, "code": e.code, "message": e.message}
            for e in events
        ]
    return render_template("partials/event_feed.html", events=rows)


# --- сверка ----------------------------------------------------------------


@bp.post("/projects/<int:project_id>/reconcile")
@login_required
def reconcile_start(project_id: int):
    """Опросить приёмник и сравнить с тем, что было на источнике.

    Именно опросить: собственные счётчики переноса говорят, сколько писем мы
    отправили, а не сколько там лежит. Разница между этими числами и есть
    смысл сверки.
    """
    with session_scope() as db:
        project = _get(db, project_id)
        if not can_edit_project(project):
            abort(403)
        log_action(db, user=current_user(), action="reconcile_started",
                   target_type="project", target_id=project_id)
        project.last_activity_at = datetime.now(timezone.utc)

    start_reconcile(project_id)
    flash("Сверка запущена: опрашиваю приёмник.", "ok")
    return redirect(url_for("projects.view", project_id=project_id, tab="report"))


@bp.get("/projects/<int:project_id>/reconcile/progress")
@login_required
def reconcile_progress(project_id: int):
    runner = get_reconcile(project_id)
    return render_template(
        "partials/reconcile_progress.html",
        progress=runner.progress if runner else None,
    )


@bp.get("/projects/<int:project_id>/report.xlsx")
@login_required
def report_xlsx(project_id: int):
    with session_scope() as db:
        _get(db, project_id)
    buffer, filename = build_report(project_id)
    return send_file(
        buffer,
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# --- настройки и обслуживание ---------------------------------------------


@bp.post("/projects/<int:project_id>/settings")
@login_required
def save_settings(project_id: int):
    with session_scope() as db:
        project = _get(db, project_id)
        if not can_edit_project(project):
            abort(403)
        try:
            project.max_parallel = max(1, int(request.form.get("max_parallel") or 3))
        except ValueError:
            flash("Параллельность должна быть числом.", "error")
            return redirect(url_for("projects.view", project_id=project_id, tab="settings"))

        project.migrate_trash = bool(request.form.get("migrate_trash"))
        project.migrate_spam = bool(request.form.get("migrate_spam"))
        project.unknown_folder_policy = request.form.get("unknown_folder_policy") or "create"
        project.unknown_folder_container = (
            request.form.get("unknown_folder_container") or ""
        ).strip() or None
        try:
            project.max_message_size_mb = max(0, int(request.form.get("max_message_size_mb") or 0))
        except ValueError:
            project.max_message_size_mb = 0
        project.last_activity_at = datetime.now(timezone.utc)
        log_action(db, user=current_user(), action="project_settings_changed",
                   target_type="project", target_id=project_id)

    flash("Настройки сохранены.", "ok")
    return redirect(url_for("projects.view", project_id=project_id, tab="settings"))


@bp.post("/projects/<int:project_id>/purge-credentials")
@login_required
def purge_credentials(project_id: int):
    """Стереть пароли, оставив проект как документ.

    Миграция — процесс конечный: пароли нужны, пока он идёт, и превращаются
    в чистый риск, когда он закончен.
    """
    with session_scope() as db:
        project = _get(db, project_id)
        if not can_edit_project(project):
            abort(403)

        cleared = (
            db.query(Mailbox)
            .filter(Mailbox.project_id == project_id)
            .update(
                {Mailbox.src_password_enc: None, Mailbox.dst_password_enc: None},
                synchronize_session=False,
            )
        )
        project.credentials_purged_at = datetime.now(timezone.utc)
        log_action(db, user=current_user(), action="credentials_purged",
                   target_type="project", target_id=project_id, details=f"{cleared} ящиков")
        log_event(db, project_id=project_id, code="credentials_purged", level="warning",
                  message=f"Пароли стёрты у {cleared} ящиков. История и отчёты сохранены.")

    flash("Пароли стёрты. История, счётчики и логи остались на месте.", "ok")
    return redirect(url_for("projects.view", project_id=project_id, tab="settings"))


@bp.post("/projects/<int:project_id>/complete")
@login_required
def complete(project_id: int):
    with session_scope() as db:
        project = _get(db, project_id)
        if not can_edit_project(project):
            abort(403)
        project.status = PROJECT_DONE
        log_action(db, user=current_user(), action="project_completed",
                   target_type="project", target_id=project_id)
        log_event(db, project_id=project_id, code="project_completed",
                  message="Проект переведён в статус «завершён».")
    flash("Проект завершён.", "ok")
    return redirect(url_for("projects.view", project_id=project_id))


@bp.post("/projects/<int:project_id>/delete")
@login_required
def delete(project_id: int):
    with session_scope() as db:
        project = _get(db, project_id)
        if not can_edit_project(project):
            abort(403)
        name = project.name
        log_action(db, user=current_user(), action="project_deleted",
                   target_type="project", target_id=project_id, details=name)
        db.delete(project)
    flash(f"Проект «{name}» удалён.", "ok")
    return redirect(url_for("projects.index"))


@bp.post("/projects/<int:project_id>/mailboxes/<int:mailbox_id>/unlock")
@login_required
def unlock_mailbox(project_id: int, mailbox_id: int):
    """Снять липкий флаг вручную.

    Флаг ставится, когда логин упал по паролю, и защищает от повторных
    попыток: в домене их обычно всего пять до блокировки на час.
    """
    with session_scope() as db:
        project = _get(db, project_id)
        if not can_edit_project(project):
            abort(403)
        mailbox = db.get(Mailbox, mailbox_id)
        if mailbox is None or mailbox.project_id != project_id:
            abort(404)
        mailbox.auth_locked = False
        log_action(db, user=current_user(), action="mailbox_unlocked",
                   target_type="mailbox", target_id=mailbox_id, details=mailbox.src_email)
    return redirect(url_for("projects.view", project_id=project_id))


# --- вспомогательное -------------------------------------------------------


def _launch(project_id: int, *, include_done: bool, action: str):
    with session_scope() as db:
        project = _get(db, project_id)
        if not can_edit_project(project):
            abort(403)
        ready = (
            db.query(func.count(Mailbox.id))
            .filter(Mailbox.project_id == project_id, Mailbox.status == MB_CHECK_OK)
            .scalar()
        )
        log_action(db, user=current_user(), action=action,
                   target_type="project", target_id=project_id)
        project.last_activity_at = datetime.now(timezone.utc)

    if not include_done and not ready:
        flash(
            "Нечего переносить: нет ящиков с подтверждённым доступом. "
            "Сначала «Проверить доступы».",
            "error",
        )
        return redirect(url_for("projects.view", project_id=project_id))

    start_migration(project_id, include_done=include_done)
    return redirect(url_for("projects.view", project_id=project_id))


def _log_path(project_id: int, mailbox_id: int):
    """Файл лога последнего прогона ящика — сжатый или ещё пишущийся."""
    with session_scope() as db:
        mailbox = db.get(Mailbox, mailbox_id)
        if mailbox is None or mailbox.project_id != project_id:
            return None
        name = mailbox.log_filename

    folder = RAW_LOG_DIR / f"project_{project_id}"
    if name:
        candidate = folder / name
        if candidate.exists():
            return candidate

    # Прогон ещё идёт: имя в БД появится только по завершении.
    live = sorted(folder.glob(f"mailbox_{mailbox_id}_*.log"), reverse=True)
    if live:
        return live[0]
    archived = sorted(folder.glob(f"mailbox_{mailbox_id}_*.log.gz"), reverse=True)
    return archived[0] if archived else None


def _get(db, project_id: int) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        abort(404)
    return project


def _counts(db, project_id: int) -> dict:
    total, ok, failed, messages, size = (
        db.query(
            func.count(Mailbox.id),
            func.sum(case((Mailbox.status == MB_CHECK_OK, 1), else_=0)),
            func.sum(case((Mailbox.status == MB_CHECK_FAILED, 1), else_=0)),
            func.sum(Mailbox.total_messages),
            func.sum(Mailbox.total_bytes),
        )
        .filter(Mailbox.project_id == project_id)
        .one()
    )
    return {
        "total": total or 0,
        "ok": ok or 0,
        "failed": failed or 0,
        "messages": messages or 0,
        "size": size or 0,
    }


def _mailbox_rows(db, project_id: int, *, only_failed: bool) -> list[dict]:
    query = db.query(Mailbox).filter(Mailbox.project_id == project_id)
    if only_failed:
        query = query.filter(Mailbox.status == MB_CHECK_FAILED)

    rows = []
    for mailbox in query.order_by(Mailbox.src_email).all():
        rows.append(
            {
                "id": mailbox.id,
                "src_email": mailbox.src_email,
                "dst_email": mailbox.dst_email,
                "note": mailbox.note,
                "status": mailbox.status,
                "src_result": mailbox.src_check_result,
                "dst_result": mailbox.dst_check_result,
                "src_message": human(mailbox.src_check_result)
                if mailbox.src_check_result not in (None, "ok") else "",
                "dst_message": human(mailbox.dst_check_result)
                if mailbox.dst_check_result not in (None, "ok") else "",
                "hint": HINTS.get(mailbox.src_check_result) or HINTS.get(mailbox.dst_check_result),
                "auth_locked": mailbox.auth_locked,
                "attempts": max(mailbox.src_auth_attempts, mailbox.dst_auth_attempts),
                "total_messages": mailbox.total_messages,
                "total_bytes": mailbox.total_bytes,
                "quota_warning": mailbox.quota_will_overflow,
                "has_src_password": bool(mailbox.src_password_enc),
                "has_dst_password": bool(mailbox.dst_password_enc),
                # Перенос
                "done_messages": mailbox.done_messages,
                "done_bytes": mailbox.done_bytes,
                "percent": _percent(mailbox.done_messages, mailbox.total_messages),
                "run_attempts": mailbox.run_attempts,
                "current_folder": mailbox.current_folder,
                "last_error": mailbox.last_error,
                "exit_message": describe_exit(mailbox.last_exit_code)
                if mailbox.last_exit_code not in (None, 0) else "",
                "has_log": bool(mailbox.log_filename) or mailbox.run_attempts > 0,
                # Сверка
                "dst_total_messages": mailbox.dst_total_messages,
                "dst_total_bytes": mailbox.dst_total_bytes,
                "reconciled_at": mailbox.reconciled_at,
                "gap": (
                    (mailbox.dst_total_messages or 0) - (mailbox.total_messages or 0)
                    if mailbox.reconciled_at and mailbox.total_messages is not None
                    else None
                ),
            }
        )
    return rows


def _percent(done: int | None, total: int | None) -> int:
    if not total:
        return 0
    return min(100, int(round(100 * (done or 0) / total)))


def _project_context(db, project: Project) -> dict:
    src = db.get(Endpoint, project.src_endpoint_id) if project.src_endpoint_id else None
    dst = db.get(Endpoint, project.dst_endpoint_id) if project.dst_endpoint_id else None
    counts = _counts(db, project.id)
    runner = get_check(project.id)
    migration = get_migration(project.id)
    rows = _mailbox_rows(db, project.id, only_failed=False)

    return {
        "project": {
            "id": project.id,
            "name": project.name,
            "status": project.status,
            "author": project.author.username if project.author else "—",
            "max_parallel": project.max_parallel,
            "migrate_trash": project.migrate_trash,
            "migrate_spam": project.migrate_spam,
            "unknown_folder_policy": project.unknown_folder_policy,
            "unknown_folder_container": project.unknown_folder_container or "",
            "max_message_size_mb": project.max_message_size_mb,
            "credentials_purged_at": project.credentials_purged_at,
            "is_ready": project.status in (PROJECT_READY, PROJECT_DONE),
        },
        "src": {"name": src.name, "host": src.host, "preset": get_preset(src.preset),
                "max_parallel": src.max_parallel} if src else None,
        "dst": {"name": dst.name, "host": dst.host, "preset": get_preset(dst.preset),
                "max_parallel": dst.max_parallel} if dst else None,
        "counts": counts,
        "rows": rows,
        "progress": runner.progress if runner else None,
        "migration": migration.progress if migration else None,
        "can_edit": can_edit_project(project),
        # Предупреждение о нехватке места должно всплыть ДО старта переноса,
        # а не на четвёртом часу.
        "quota_warnings": sum(1 for r in rows if r["quota_warning"]),
    }
