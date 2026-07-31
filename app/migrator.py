"""Очередь переноса: кто, когда и в скольких потоках едет.

Устройство простое сознательно — поток-супервизор и пул потоков, каждый из
которых держит один процесс imapsync. Ни Redis, ни Celery: инструмент должен
подниматься одной командой у постороннего человека.

Что здесь важно и почему:

* **«Досинхронить» — это тот же прогон повторно.** imapsync сопоставляет письма
  по заголовкам, поэтому повторный запуск не создаёт дублей, а везёт только то,
  чего на приёмнике нет. Отдельной логики дельты не существует и не нужно.
* **Повтор при обрыве — да, при ошибке пароля — никогда.** Решение принимается
  по коду возврата imapsync, а не по тексту вывода.
* **Один упавший ящик не останавливает проект.** Он уходит в «упал», очередь
  едет дальше.
* **После перезапуска контейнера прерванные ящики продолжаются сами.** Ночной
  прогон, вставший из-за ребута и молча ждущий утра, — это не отказоустойчивость.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.crypto import decrypt
from app.db import session_scope
from app.imapsync_runner import (
    ImapsyncRun,
    RunSpec,
    SideSpec,
    describe_exit,
    is_available,
)
from app.journal import log_event
from app.models import (
    MB_CHECK_OK,
    MB_DONE,
    MB_FAILED,
    MB_QUEUED,
    MB_RUNNING,
    PROJECT_DONE,
    PROJECT_READY,
    PROJECT_RUNNING,
    AUTH_MASTER,
    Endpoint,
    Mailbox,
    Project,
)
from app.folder_mapping import effective_plan

log = logging.getLogger(__name__)

RETRY_BACKOFF_SECONDS = (5, 20, 60)
MAX_NETWORK_RETRIES = len(RETRY_BACKOFF_SECONDS)
RESUME_DELAY_SECONDS = 60


@dataclass
class MailboxProgress:
    mailbox_id: int
    email: str
    folder: str | None = None
    done_messages: int = 0
    total_messages: int | None = None
    done_bytes: int = 0
    total_bytes: int | None = None
    speed: float = 0.0
    last_line: str = ""
    # Прогресс по папкам этого ящика — для развёрнутой строки в таблице.
    folders: list = field(default_factory=list)
    # Формат вывода imapsync построчно не зафиксирован. Если ни одна строка не
    # опознана, интерфейс не показывает нули как факт — см. шаблон.
    output_recognised: bool = False
    started_at: float = field(default_factory=time.monotonic)


@dataclass
class MigrationProgress:
    project_id: int
    total_mailboxes: int = 0
    done_mailboxes: int = 0
    failed_mailboxes: int = 0
    running: bool = True
    stopping: bool = False
    resumed: int = 0
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    active: dict[int, MailboxProgress] = field(default_factory=dict)

    @property
    def finished_mailboxes(self) -> int:
        return self.done_mailboxes + self.failed_mailboxes

    @property
    def percent(self) -> int:
        """Общий бар — по числу ящиков, как договаривались."""
        if not self.total_mailboxes:
            return 0
        return int(round(100 * self.finished_mailboxes / self.total_mailboxes))

    @property
    def current(self) -> MailboxProgress | None:
        """Ящиков в работе может быть несколько; в шапке показываем тот,
        который начали позже всех, а полная картина — в таблице ниже."""
        if not self.active:
            return None
        return max(self.active.values(), key=lambda p: p.started_at)

    @property
    def total_speed(self) -> float:
        return sum(p.speed for p in self.active.values())


class MigrationRunner:
    def __init__(self, project_id: int, *, include_done: bool = False,
                 resumed: int = 0, dry_run: bool = False,
                 only_mailbox_ids: list[int] | None = None) -> None:
        self.project_id = project_id
        self.include_done = include_done
        self.dry_run = dry_run
        # Досинхрон одного ящика: остальные не трогаем.
        self.only_mailbox_ids = set(only_mailbox_ids) if only_mailbox_ids else None
        self.progress = MigrationProgress(project_id=project_id, resumed=resumed)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._runs: dict[int, ImapsyncRun] = {}
        self._thread: threading.Thread | None = None
        # Сколько писем и байт по каждому ящику было перенесено прошлыми прогонами.
        self._baselines: dict[int, tuple[int, int]] = {}

    # -- управление ---------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name=f"migrate-project-{self.project_id}", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self.progress.stopping = True
        self._stop.set()
        with self._lock:
            runs = list(self._runs.values())
        for run in runs:
            run.stop()

    @property
    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # -- прогон -------------------------------------------------------------

    def _run(self) -> None:
        try:
            plan = self._prepare()
        except Exception:  # noqa: BLE001
            log.exception("Не удалось подготовить перенос проекта %s", self.project_id)
            self._finish()
            return

        if plan is None:
            self._finish()
            return

        mailbox_ids, parallel = plan

        with ThreadPoolExecutor(max_workers=parallel, thread_name_prefix="migrate") as pool:
            futures = []
            for mailbox_id in mailbox_ids:
                if self._stop.is_set():
                    break
                futures.append(pool.submit(self._migrate_one, mailbox_id))
            for future in futures:
                try:
                    future.result()
                except Exception:  # noqa: BLE001
                    log.exception("Перенос ящика завершился исключением")

        self._finish()

    def _prepare(self):
        if not is_available():
            with session_scope() as session:
                log_event(
                    session, project_id=self.project_id, level="error",
                    code="imapsync_missing",
                    message=(
                        "imapsync не найден в системе. В контейнере он ставится при "
                        "сборке; при локальном запуске поставь его или укажи путь "
                        "в переменной IMAPSYNC_BIN."
                    ),
                )
            return None

        with session_scope() as session:
            project = session.get(Project, self.project_id)
            if project is None:
                return None

            src = session.get(Endpoint, project.src_endpoint_id) if project.src_endpoint_id else None
            dst = session.get(Endpoint, project.dst_endpoint_id) if project.dst_endpoint_id else None
            if src is None or dst is None:
                log_event(session, project_id=self.project_id, level="error",
                          code="migrate_no_endpoints",
                          message="Перенос не запущен: не выбраны серверы.")
                return None

            statuses = [MB_CHECK_OK, MB_QUEUED, MB_FAILED, MB_RUNNING]
            if self.include_done:
                statuses.append(MB_DONE)

            query = session.query(Mailbox).filter(
                Mailbox.project_id == self.project_id, Mailbox.status.in_(statuses)
            )
            if self.only_mailbox_ids:
                query = query.filter(Mailbox.id.in_(self.only_mailbox_ids))
            mailboxes = query.order_by(Mailbox.id).all()

            selected: list[int] = []
            skipped_locked = 0
            skipped_nocreds = 0
            for mailbox in mailboxes:
                if mailbox.auth_locked:
                    # Ящик уже падал по паролю. Прогон — это ещё одна попытка
                    # входа, а их до блокировки учётной записи всего несколько.
                    skipped_locked += 1
                    continue
                if not _has_credentials(mailbox, src, dst):
                    skipped_nocreds += 1
                    continue
                mailbox.status = MB_QUEUED
                selected.append(mailbox.id)

            parallel = max(1, project.max_parallel)
            project.status = PROJECT_RUNNING
            project.last_activity_at = datetime.now(timezone.utc)

            with self._lock:
                self.progress.total_mailboxes = len(selected)

            notes = []
            if self.progress.resumed:
                notes.append(f"возобновлено после перезапуска: {self.progress.resumed}")
            if skipped_locked:
                notes.append(f"пропущено с ошибкой пароля: {skipped_locked}")
            if skipped_nocreds:
                notes.append(f"пропущено без пароля: {skipped_nocreds}")

            log_event(
                session, project_id=self.project_id, code="migrate_started",
                message=(
                    f"Перенос запущен: {len(selected)} ящиков, параллельно {parallel}"
                    + (". " + "; ".join(notes) if notes else ".")
                ),
            )

        return selected, parallel

    def _migrate_one(self, mailbox_id: int) -> None:
        if self._stop.is_set():
            return

        attempt = 0
        while True:
            spec, email, totals = self._build_spec(mailbox_id)
            if spec is None:
                return

            tracker = MailboxProgress(
                mailbox_id=mailbox_id, email=email,
                total_messages=totals[0], total_bytes=totals[1],
                done_messages=totals[2], done_bytes=totals[3],
            )
            with self._lock:
                self.progress.active[mailbox_id] = tracker

            self._mark(mailbox_id, MB_RUNNING, started=True)

            run = ImapsyncRun(spec, on_progress=lambda r, e, t=tracker: self._on_progress(r, t))
            with self._lock:
                self._runs[mailbox_id] = run

            result = run.run()

            with self._lock:
                self._runs.pop(mailbox_id, None)
                self.progress.active.pop(mailbox_id, None)

            if result.stopped or self._stop.is_set():
                self._store(mailbox_id, result, status=MB_QUEUED,
                            note="прогон остановлен вручную")
                return

            if result.ok and result.did_nothing:
                # Код возврата 0, но в выводе нет ни одной строки о работе.
                # Именно так выглядел запуск imapsync в режиме CGI: ящик не
                # тронут, а панель показывала «перенесён». Молчать нельзя —
                # человек переключит MX на пустой ящик.
                self._store(mailbox_id, result, status=MB_FAILED, note=_noop_note(result))
                self._account(done=False)
                return

            if result.ok:
                self._store(mailbox_id, result, status=MB_DONE)
                self._account(done=True)
                return

            # Ошибку аутентификации не повторяем никогда: следующая попытка
            # только приблизит блокировку учётной записи.
            if result.fatal or not result.retriable or attempt >= MAX_NETWORK_RETRIES:
                self._store(mailbox_id, result, status=MB_FAILED)
                self._account(done=False)
                return

            delay = RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)]
            self._log(
                mailbox_id,
                level="warning",
                code="migrate_retry",
                message=(
                    f"{email}: {describe_exit(result.exit_code)}. "
                    f"Повтор через {delay} с (попытка {attempt + 2} из {MAX_NETWORK_RETRIES + 1})."
                ),
            )
            if self._stop.wait(delay):
                self._store(mailbox_id, result, status=MB_QUEUED, note="остановлено")
                return
            attempt += 1

    def _on_progress(self, run: ImapsyncRun, tracker: MailboxProgress) -> None:
        # К счётчикам этого прогона прибавляем то, что было перенесено раньше.
        # Иначе на дельта-прогоне полоска падала бы со 100% до нуля: показываем
        # «сколько из ящика уже лежит на приёмнике», а не «сколько за этот запуск».
        base_messages, base_bytes = self._baselines.get(tracker.mailbox_id, (0, 0))
        folders = run.folders
        # Пока прогон идёт, к уже лежащему на приёмнике прибавляем сегодняшнее.
        # Ограничиваем известным объёмом ящика: иначе на дельте счётчик уходил
        # бы за собственный знаменатель.
        messages = base_messages + run.result.copied_messages
        payload = base_bytes + run.result.copied_bytes
        if tracker.total_messages:
            messages = min(messages, tracker.total_messages)
        if tracker.total_bytes:
            payload = min(payload, tracker.total_bytes)

        with self._lock:
            tracker.folder = run.current_folder
            tracker.last_line = run.last_line
            tracker.output_recognised = run.output_recognised
            tracker.speed = run.speed_bytes_per_second
            tracker.done_messages = messages
            tracker.done_bytes = payload
            tracker.folders = folders

    # -- работа с БД --------------------------------------------------------

    def _build_spec(self, mailbox_id: int):
        with session_scope() as session:
            mailbox = session.get(Mailbox, mailbox_id)
            if mailbox is None:
                return None, "", (None, None, 0, 0)

            project = session.get(Project, mailbox.project_id)
            src = session.get(Endpoint, project.src_endpoint_id)
            dst = session.get(Endpoint, project.dst_endpoint_id)

            # Подтверждённая человеком таблица соответствий главнее догадок;
            # если её нет, работает та же автоматика, что строит предложение.
            folder_map, excludes = effective_plan(session, mailbox_id, project, dst)

            spec = RunSpec(
                mailbox_id=mailbox_id,
                project_id=mailbox.project_id,
                src=_side(src, mailbox.src_email, decrypt(mailbox.src_password_enc)),
                dst=_side(dst, mailbox.dst_email, decrypt(mailbox.dst_password_enc)),
                folder_map=folder_map,
                exclude_folders=excludes,
                max_message_bytes=(project.max_message_size_mb or 0) * 1024 * 1024 or None,
                dry_run=self.dry_run,
            )
            self._baselines[mailbox_id] = (mailbox.done_messages, mailbox.done_bytes)
            totals = (
                mailbox.total_messages,
                mailbox.total_bytes,
                mailbox.done_messages,
                mailbox.done_bytes,
            )
            return spec, mailbox.src_email, totals

    def _mark(self, mailbox_id: int, status: str, *, started: bool = False) -> None:
        with session_scope() as session:
            mailbox = session.get(Mailbox, mailbox_id)
            if mailbox is None:
                return
            mailbox.status = status
            if started:
                mailbox.started_at = datetime.now(timezone.utc)
                mailbox.run_attempts += 1

    def _store(self, mailbox_id: int, result, *, status: str, note: str | None = None) -> None:
        with session_scope() as session:
            mailbox = session.get(Mailbox, mailbox_id)
            if mailbox is None:
                return

            mailbox.status = status
            mailbox.finished_at = datetime.now(timezone.utc)
            mailbox.last_exit_code = result.exit_code
            mailbox.last_error = None if result.ok else describe_exit(result.exit_code)

            if result.summary_seen:
                # Сводка описывает ящик целиком: перенесённые плюс те, что уже
                # были на приёмнике. Это и есть «сколько писем ящика доехало»,
                # поэтому значение задаём, а не накапливаем — иначе повторные
                # прогоны считали бы одни и те же письма снова и снова.
                mailbox.done_messages = result.on_destination_messages
                mailbox.done_bytes = result.on_destination_bytes
            else:
                mailbox.done_messages += result.copied_messages
                mailbox.done_bytes += result.copied_bytes
            mailbox.current_folder = None
            if result.log_path is not None:
                mailbox.log_filename = result.log_path.name

            if status == MB_DONE:
                # Цифры берём из итоговой сводки imapsync, а не из подсчёта строк.
                message = f"{mailbox.src_email}: {result.summary}"
                level = "info"
                code = "migrate_done"
            elif status == MB_QUEUED:
                message = f"{mailbox.src_email}: {note or 'прогон прерван'}"
                level = "warning"
                code = "migrate_interrupted"
            else:
                message = f"{mailbox.src_email}: {note or describe_exit(result.exit_code)}"
                level = "error"
                code = "migrate_failed"
                if result.exit_code == 113:
                    message += " — увеличь квоту и запусти «Досинхронить»"

            log_event(session, project_id=self.project_id, mailbox_id=mailbox_id,
                      level=level, code=code, message=message)

    def _log(self, mailbox_id: int, *, level: str, code: str, message: str) -> None:
        with session_scope() as session:
            log_event(session, project_id=self.project_id, mailbox_id=mailbox_id,
                      level=level, code=code, message=message)

    def _account(self, *, done: bool) -> None:
        with self._lock:
            if done:
                self.progress.done_mailboxes += 1
            else:
                self.progress.failed_mailboxes += 1

    def _finish(self) -> None:
        with self._lock:
            self.progress.running = False
            self.progress.finished_at = datetime.now(timezone.utc)
            done = self.progress.done_mailboxes
            failed = self.progress.failed_mailboxes

        with session_scope() as session:
            project = session.get(Project, self.project_id)
            if project is not None and project.status == PROJECT_RUNNING:
                project.status = PROJECT_READY
                project.last_activity_at = datetime.now(timezone.utc)
            log_event(
                session, project_id=self.project_id, code="migrate_finished",
                message=(
                    f"Прогон завершён: перенесено {done} ящиков, с ошибками {failed}. "
                    "Если это был финальный прогон перед переключением MX — не забудь "
                    "догоняющий «Досинхронить» через день-другой: часть отправителей "
                    "ещё будет слать на старый адрес из-за кэша DNS."
                ),
            )


# --- реестр ----------------------------------------------------------------

_runners: dict[int, MigrationRunner] = {}
_registry_lock = threading.Lock()


def start_migration(project_id: int, *, include_done: bool = False,
                    resumed: int = 0, dry_run: bool = False,
                    only_mailbox_ids: list[int] | None = None) -> MigrationRunner:
    with _registry_lock:
        existing = _runners.get(project_id)
        if existing and existing.is_alive:
            return existing
        runner = MigrationRunner(project_id, include_done=include_done,
                                 resumed=resumed, dry_run=dry_run,
                                 only_mailbox_ids=only_mailbox_ids)
        _runners[project_id] = runner
    runner.start()
    return runner


def get_migration(project_id: int) -> MigrationRunner | None:
    with _registry_lock:
        return _runners.get(project_id)


def stop_migration(project_id: int) -> None:
    with _registry_lock:
        runner = _runners.get(project_id)
    if runner:
        runner.stop()


def resume_interrupted() -> None:
    """Поднять то, что оборвалось перезапуском контейнера.

    Вызывается один раз при старте и только в управляющем процессе. Задержка
    даёт человеку возможность нажать «Стоп», если возобновление некстати.
    """
    with session_scope() as session:
        stuck = (
            session.query(Mailbox)
            .filter(Mailbox.status.in_([MB_RUNNING, MB_QUEUED]))
            .all()
        )
        by_project: dict[int, int] = {}
        for mailbox in stuck:
            mailbox.status = MB_QUEUED
            mailbox.current_folder = None
            by_project[mailbox.project_id] = by_project.get(mailbox.project_id, 0) + 1

        for project_id, count in by_project.items():
            log_event(
                session, project_id=project_id, level="warning", code="migrate_resume_scheduled",
                message=(
                    f"После перезапуска найдено {count} прерванных ящиков. "
                    f"Перенос возобновится автоматически через {RESUME_DELAY_SECONDS} секунд."
                ),
            )

    if not by_project:
        return

    def _later() -> None:
        time.sleep(RESUME_DELAY_SECONDS)
        for project_id, count in by_project.items():
            project_status = _project_status(project_id)
            if project_status == PROJECT_DONE:
                continue
            start_migration(project_id, resumed=count)

    threading.Thread(target=_later, name="migrate-resume", daemon=True).start()


def _project_status(project_id: int) -> str | None:
    with session_scope() as session:
        project = session.get(Project, project_id)
        return project.status if project else None


# --- вспомогательное -------------------------------------------------------


def _side(endpoint: Endpoint, mailbox_login: str, secret: str | None) -> SideSpec:
    login = mailbox_login
    password = secret or ""

    if endpoint.auth_mode == AUTH_MASTER:
        # Проксирующий логин вида «ящик*администратор»: так умеют Dovecot,
        # Zimbra и Courier. Exchange — нет.
        login = f"{mailbox_login}{endpoint.master_separator}{endpoint.master_username or ''}"
        password = decrypt(endpoint.master_secret_enc) or ""

    return SideSpec(
        host=endpoint.host,
        port=endpoint.port,
        security=endpoint.security,
        verify_cert=endpoint.verify_cert,
        login=login,
        secret=password,
    )


def _noop_note(result) -> str:
    """Объяснение для случая «вышел с нулём, но ничего не сделал»."""
    if result.cgi_context:
        return (
            "imapsync запустился в режиме CGI и вышел, не перенеся ни одного письма. "
            "Так бывает, когда процессу достались переменные окружения веб-сервера. "
            "Обнови образ до последней версии и запусти «Досинхронить»"
        )
    return (
        "imapsync завершился успешно, но не перенёс ни одного письма и не сообщил "
        "ни о переносе, ни об обработке папок. Проверь подробный лог прогона: "
        "возможно, все папки попали в исключения"
    )


def _has_credentials(mailbox: Mailbox, src: Endpoint, dst: Endpoint) -> bool:
    src_ok = bool(mailbox.src_password_enc) or src.auth_mode != "password"
    dst_ok = bool(mailbox.dst_password_enc) or dst.auth_mode != "password"
    return src_ok and dst_ok
