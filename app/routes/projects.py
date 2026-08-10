"""Проекты: список, карточка, запуск проверки, очистка кредов."""

from __future__ import annotations

from datetime import datetime, timezone

import math
import zipfile
from io import BytesIO

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
from sqlalchemy import case, func, or_

from app.checker import get_check, start_check, stop_check
from app.config import RAW_LOG_DIR
from app.db import session_scope
from app.errors import HINTS, human
from app.folder_mapping import build_proposal, is_confirmed, save_mapping
from app.imapsync_runner import cache_usage, describe_exit, purge_cache, read_log_tail
from app.migrator import (
    get_migration,
    start_migration,
    stop_migration,
    storage_headroom,
    storage_problem,
)
from app.reconcile import build_report, get_reconcile, start_reconcile
from app.journal import log_action, log_event
from app.models import (
    MB_CHECK_FAILED,
    MB_CHECK_OK,
    MB_DONE,
    MB_FAILED,
    MB_QUEUED,
    MB_RUNNING,
    PROJECT_DONE,
    PROJECT_READY,
    Endpoint,
    Event,
    Mailbox,
    MailboxFolder,
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


@bp.get("/projects/<int:project_id>/live")
@login_required
def live_state(project_id: int):
    """Только цифры, без разметки.

    Раньше страница раз в несколько секунд перерисовывала таблицу целиком:
    сбивался скролл, слетало выделение текста и фокус в поле поиска, а
    развёрнутые строки моргали. Теперь меняются значения на месте, а полная
    перерисовка нужна лишь когда меняется сам состав строк — за этим следит
    поле «structure».

    Строки для показа собирает сервер: иначе форматирование чисел пришлось бы
    повторить на javascript и оно бы разъехалось с серверным.
    """
    from app import human_count, human_size

    with session_scope() as db:
        _get(db, project_id)
        rows, _, _, _ = _mailbox_rows(db, project_id, per_page=1000)

    def counts_text(row: dict) -> str:
        if row["status"] not in ("running", "done", "failed", "queued"):
            return ""
        parts = [
            f"{human_count(row['done_messages'])} / {human_count(row['total_messages'])} писем",
            f"{human_size(row['done_bytes'])} / {human_size(row['total_bytes'])}",
        ]
        if row.get("speed"):
            parts.append(f"{row['speed'] / 1048576:.1f} МБ/с")
        return " · ".join(parts)

    def status_text(row: dict) -> str:
        if row["status"] == "running":
            folder = row.get("current_folder")
            return f"идёт · {folder}" if folder else "идёт"
        if row["status"] == "queued":
            return "в очереди"
        if row["status"] == "done":
            return "перенесён"
        if row["status"] == "failed":
            return row.get("exit_message") or "ошибка"
        return "не начинался"

    mailboxes = [
        {
            "id": row["id"],
            "percent": row["percent"],
            "counts": counts_text(row),
            "status": status_text(row),
            "kind": row["status"],
        }
        for row in rows
    ]

    migration = get_migration(project_id)
    check = get_check(project_id)

    payload = {
        "mailboxes": mailboxes,
        "migration": _live_migration(migration),
        "check": _live_check(check),
    }
    # Состав строк и их состояние: пока не меняется — перерисовывать нечего.
    payload["structure"] = "|".join(
        f"{m['id']}:{m['kind']}" for m in mailboxes
    ) + f"|m={bool(migration and migration.progress.running)}" \
        f"|c={bool(check and check.progress.running)}"

    return jsonify(payload)


def _live_migration(runner) -> dict | None:
    if runner is None:
        return None
    p = runner.progress
    detail = (
        f"{p.finished_mailboxes} / {p.total_mailboxes} ящиков"
        f" · перенесено {p.done_mailboxes} · с ошибками {p.failed_mailboxes}"
    )
    if p.total_speed:
        detail += f" · {p.total_speed / 1048576:.1f} МБ/с всего"
    return {"running": p.running, "percent": p.percent, "detail": detail}


def _live_check(runner) -> dict | None:
    if runner is None:
        return None
    p = runner.progress
    detail = (
        f"проверено {p.checked} из {p.total}"
        f" · доступны {p.ok} · с ошибкой {p.failed}"
    )
    if p.skipped:
        detail += f" · пропущено с прошлой ошибкой {p.skipped}"
    return {"running": p.running, "percent": p.percent, "detail": detail}


@bp.get("/projects/<int:project_id>/mailboxes/<int:mailbox_id>/folders")
@login_required
def mailbox_folders(project_id: int, mailbox_id: int):
    """Развёрнутая строка ящика: что происходит по папкам.

    Пока прогон идёт, цифры берём из памяти супервизора. Когда он закончен —
    из инвентаризации источника и, если делали сверку, из опроса приёмника.
    """
    with session_scope() as db:
        _get(db, project_id)
        mailbox = db.get(Mailbox, mailbox_id)
        if mailbox is None or mailbox.project_id != project_id:
            abort(404)

        src_folders = (
            db.query(MailboxFolder)
            .filter(MailboxFolder.mailbox_id == mailbox_id, MailboxFolder.side == "src")
            .order_by(MailboxFolder.name_display)
            .all()
        )
        dst_counts = {
            f.name_display: f.messages
            for f in db.query(MailboxFolder)
            .filter(MailboxFolder.mailbox_id == mailbox_id, MailboxFolder.side == "dst")
            .all()
        }
        project = db.get(Project, project_id)
        migrate_spam = project.migrate_spam
        migrate_trash = project.migrate_trash

        all_names = {f.name_display for f in src_folders}
        inventory = []
        for f in src_folders:
            reason = None
            is_spam = f.special_use == "\\Junk" or f.name_display.lower() in ("спам", "junk", "spam")
            is_trash = f.special_use == "\\Trash" or f.name_display.lower() in ("корзина", "trash", "deleted items")
            if is_spam and not migrate_spam:
                reason = "пропускается (спам отключён в настройках проекта)"
            elif is_trash and not migrate_trash:
                reason = "пропускается (корзина отключена в настройках проекта)"

            parts = f.name_display.replace(".", "/").split("/")
            depth = len(parts) - 1
            leaf_name = parts[-1] if parts else f.name_display
            parent_name = "/".join(parts[:-1]) if depth > 0 else None
            has_children = any(n != f.name_display and (n.startswith(f.name_display + "/") or n.startswith(f.name_display + ".")) for n in all_names)

            inventory.append({
                "name": f.name_display,
                "leaf_name": leaf_name,
                "depth": depth,
                "parent_name": parent_name,
                "has_children": has_children,
                "raw": f.name_raw,
                "messages": f.messages,
                "size_bytes": f.size_bytes,
                "special_use": f.special_use,
                "selectable": f.selectable,
                "excluded_reason": reason,
                "on_destination": dst_counts.get(f.name_display),
            })
        email = mailbox.src_email
        reconciled = mailbox.reconciled_at

    runner = get_migration(project_id)
    active = runner.progress.active.get(mailbox_id) if runner else None
    live = {f.raw: f for f in (active.folders if active else [])}
    live_by_name = {f.name: f for f in (active.folders if active else [])}

    for row in inventory:
        progress = live.get(row["raw"]) or live_by_name.get(row["name"])
        row["live"] = progress

    return render_template(
        "partials/mailbox_folders.html",
        rows=inventory,
        email=email,
        reconciled=reconciled,
        running=active is not None,
        project_id=project_id,
        mailbox_id=mailbox_id,
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


@bp.get("/projects/<int:project_id>/logs/zip")
@login_required
def project_logs_zip(project_id: int):
    with session_scope() as db:
        _get(db, project_id)

    folder = RAW_LOG_DIR / f"project_{project_id}"
    if not folder.exists():
        abort(404)

    log_files = list(folder.glob("mailbox_*"))
    if not log_files:
        abort(404)

    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in log_files:
            zf.write(f, arcname=f.name)
    buf.seek(0)

    filename = f"project_{project_id}_logs.zip"
    return send_file(buf, mimetype="application/zip", as_attachment=True, download_name=filename)


@bp.get("/projects/<int:project_id>/mailboxes")
@login_required
def mailboxes_partial(project_id: int):
    only_failed = request.args.get("failed") == "1"
    q = request.args.get("q", "").strip()
    page = request.args.get("page", type=int) or 1
    with session_scope() as db:
        project = _get(db, project_id)
        rows, total_count, page, total_pages = _mailbox_rows(
            db, project_id, only_failed=only_failed, q=q, page=page, per_page=100
        )
    return render_template(
        "partials/mailbox_table.html",
        rows=rows, project_id=project_id, only_failed=only_failed,
        q=q, page=page, total_pages=total_pages, total_count=total_count,
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
            val = max(1, int(request.form.get("max_parallel") or 3))
            project.max_parallel = val
            if project.src_endpoint_id:
                src = db.get(Endpoint, project.src_endpoint_id)
                if src and src.max_parallel < val:
                    src.max_parallel = val
            if project.dst_endpoint_id:
                dst = db.get(Endpoint, project.dst_endpoint_id)
                if dst and dst.max_parallel < val:
                    dst.max_parallel = val
        except ValueError:
            flash("Параллельность должна быть числом.", "error")
            return redirect(url_for("projects.view", project_id=project_id, tab="settings"))

        project.migrate_trash = bool(request.form.get("migrate_trash"))
        project.migrate_spam = bool(request.form.get("migrate_spam"))
        # Галка кэша живёт в отдельной форме. Без этой проверки сохранение
        # остальных настроек молча выключало бы кэш, потому что в той форме
        # поля нет вовсе.
        if request.form.get("use_cache_form"):
            project.use_cache = bool(request.form.get("use_cache"))
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


@bp.post("/projects/<int:project_id>/cache/purge")
@login_required
def purge_project_cache(project_id: int):
    """Удалить кэш imapsync этого проекта.

    Кэш ускоряет повторные прогоны, но это по файлу на каждое письмо: на
    больших проектах он исчерпывает inode-ы файловой системы, и тогда падает
    вообще всё, что пишет на диск.
    """
    with session_scope() as db:
        project = _get(db, project_id)
        if not can_edit_project(project):
            abort(403)

    removed = purge_cache(project_id)

    with session_scope() as db:
        log_action(db, user=current_user(), action="cache_purged",
                   target_type="project", target_id=project_id,
                   details=f"{removed} файлов")
        log_event(db, project_id=project_id, code="cache_purged",
                  message=f"Кэш imapsync очищен: удалено {removed} файлов. "
                          "Следующий прогон будет дольше обычного.")

    flash(f"Кэш очищен, удалено {removed} файлов.", "ok")
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

    # Кэш завершённому проекту не нужен, а места и inode-ов занимает много.
    removed = purge_cache(project_id)
    if removed:
        with session_scope() as db:
            log_event(db, project_id=project_id, code="cache_purged",
                      message=f"Кэш imapsync очищен вместе с завершением проекта: "
                              f"удалено {removed} файлов.")

    flash("Проект завершён." + (f" Кэш очищен ({removed} файлов)." if removed else ""), "ok")
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
        is_ready_cond = (
            (Mailbox.src_check_result == "ok") & (Mailbox.dst_check_result == "ok")
        ) | Mailbox.status.in_([MB_CHECK_OK, MB_QUEUED, MB_RUNNING, MB_DONE])
        ready = (
            db.query(func.count(Mailbox.id))
            .filter(Mailbox.project_id == project_id, is_ready_cond)
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
    is_ok = (
        (Mailbox.src_check_result == "ok") & (Mailbox.dst_check_result == "ok")
    ) | Mailbox.status.in_([MB_CHECK_OK, MB_QUEUED, MB_RUNNING, MB_DONE])
    is_failed = (Mailbox.status.in_([MB_CHECK_FAILED, MB_FAILED])) | (Mailbox.auth_locked == True)

    total, ok, failed, messages, size = (
        db.query(
            func.count(Mailbox.id),
            func.sum(case((is_ok, 1), else_=0)),
            func.sum(case((is_failed, 1), else_=0)),
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


def _mailbox_rows(
    db, project_id: int, *, only_failed: bool = False, q: str | None = None, page: int = 1, per_page: int = 100
) -> tuple[list[dict], int, int, int]:
    query = db.query(Mailbox).filter(Mailbox.project_id == project_id)
    if only_failed:
        query = query.filter(Mailbox.status == MB_CHECK_FAILED)
    if q and q.strip():
        term = f"%{q.strip()}%"
        query = query.filter(
            or_(
                Mailbox.src_email.ilike(term),
                Mailbox.dst_email.ilike(term),
                Mailbox.note.ilike(term),
            )
        )

    total_count = query.count()
    total_pages = max(1, math.ceil(total_count / per_page))
    page = max(1, min(page, total_pages))

    rows = []
    mailboxes = (
        query.order_by(Mailbox.src_email)
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )
    # Пока прогон идёт, свежие цифры живут в памяти супервизора, а не в базе:
    # писать в неё на каждое письмо было бы расточительно.
    runner = get_migration(project_id)
    live = runner.progress.active if runner else {}

    for mailbox in mailboxes:
        active = live.get(mailbox.id)
        done_messages = active.done_messages if active else mailbox.done_messages
        done_bytes = active.done_bytes if active else mailbox.done_bytes
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
                "done_messages": done_messages,
                "done_bytes": done_bytes,
                "percent": 100 if mailbox.status == MB_DONE else _percent(done_messages, mailbox.total_messages),
                "run_attempts": mailbox.run_attempts,
                "current_folder": active.folder if active else mailbox.current_folder,
                "speed": active.speed if active else 0,
                "is_live": active is not None,
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
    return rows, total_count, page, total_pages


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
    rows, _, _, _ = _mailbox_rows(db, project.id, only_failed=False)
    cache_files, cache_bytes = cache_usage(project.id)
    free_bytes, free_inodes = storage_headroom()

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
        "cache": {
            "files": cache_files,
            "bytes": cache_bytes,
            "free_bytes": free_bytes,
            "free_inodes": free_inodes,
            "problem": storage_problem(),
        },
        "can_edit": can_edit_project(project),
        # Предупреждение о нехватке места должно всплыть ДО старта переноса,
        # а не на четвёртом часу.
        "quota_warnings": sum(1 for r in rows if r["quota_warning"]),
    }
