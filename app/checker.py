"""Фаза проверки доступов и инвентаризации.

Самая опасная фаза во всём инструменте, и опасна она не тем, что может не
сработать, а тем, что может сработать слишком хорошо:

* в домене обычно пять неудачных попыток входа до блокировки учётной записи
  на час. Съехавшая на одну строку колонка паролей в списке из двухсот ящиков
  заблокирует двести доменных учёток — люди не войдут в компьютеры;
* почтовые сервисы ограничивают число подключений с одного IP-адреса, и за
  корпоративным NAT это адрес всего офиса, а не только мигратора.

Отсюда четыре предохранителя, и их нельзя ослаблять «чтобы побыстрее»:

1. ноль повторов при ошибке аутентификации (сетевые ошибки повторяем);
2. автоматическая остановка, если доля неуспешных превысила порог;
3. липкий флаг на упавших строках — повторная проверка их пропускает;
4. параллельность ограничена минимумом из лимитов проекта и обоих серверов.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.config import (
    AUTH_FAILURE_ABORT_MIN_CHECKED,
    AUTH_FAILURE_ABORT_RATIO,
)
from app.crypto import decrypt
from app.db import session_scope
from app.errors import ClassifiedError, human
from app.imap_probe import EndpointConfig, ProbeResult, probe
from app.journal import log_event
from app.models import (
    MB_CHECK_FAILED,
    MB_CHECK_OK,
    MB_CHECKING,
    Endpoint,
    Mailbox,
    MailboxFolder,
    Project,
)

log = logging.getLogger(__name__)

# Пауза между логинами к одному и тому же серверу. Дешёвая страховка от
# «слишком много подключений»: ограничение обычно на IP, а не на учётку.
MIN_INTERVAL_PER_HOST = 0.4
NETWORK_RETRIES = 2
RETRY_BACKOFF_SECONDS = (3, 10)


@dataclass
class CheckProgress:
    """Состояние прогона для интерфейса. Обновляется под замком."""

    project_id: int
    total: int = 0
    checked: int = 0
    ok: int = 0
    failed: int = 0
    skipped: int = 0
    running: bool = True
    aborted: bool = False
    abort_reason: str | None = None
    current: str = ""
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None

    @property
    def percent(self) -> int:
        if not self.total:
            return 0
        return int(round(100 * (self.checked + self.skipped) / self.total))


class _HostThrottle:
    """Не даём двум потокам ломиться на один хост чаще, чем раз в интервал."""

    def __init__(self, interval: float = MIN_INTERVAL_PER_HOST) -> None:
        self._interval = interval
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, host: str) -> None:
        with self._lock:
            now = time.monotonic()
            previous = self._last.get(host, 0.0)
            delay = previous + self._interval - now
            if delay > 0:
                self._last[host] = previous + self._interval
            else:
                delay = 0.0
                self._last[host] = now
        if delay:
            time.sleep(delay)


class CheckRunner:
    """Один прогон проверки по одному проекту."""

    def __init__(self, project_id: int, *, force: bool = False, measure_size: bool = True) -> None:
        self.project_id = project_id
        self.force = force
        self.measure_size = measure_size
        self.progress = CheckProgress(project_id=project_id)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._throttle = _HostThrottle()
        self._thread: threading.Thread | None = None

    # -- управление ---------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name=f"check-project-{self.project_id}", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    @property
    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # -- собственно прогон --------------------------------------------------

    def _run(self) -> None:
        try:
            plan = self._load_plan()
        except Exception:  # noqa: BLE001
            log.exception("Не удалось подготовить проверку проекта %s", self.project_id)
            self._finish()
            return

        if plan is None:
            self._finish()
            return

        mailbox_ids, src_cfg, dst_cfg, parallel = plan

        with ThreadPoolExecutor(max_workers=parallel, thread_name_prefix="check") as pool:
            futures = []
            for mailbox_id in mailbox_ids:
                if self._stop.is_set():
                    break
                futures.append(pool.submit(self._check_one, mailbox_id, src_cfg, dst_cfg))
            for future in futures:
                try:
                    future.result()
                except Exception:  # noqa: BLE001
                    log.exception("Проверка ящика завершилась исключением")

        self._finish()

    def _load_plan(self):
        with session_scope() as session:
            project = session.get(Project, self.project_id)
            if project is None:
                return None

            src = session.get(Endpoint, project.src_endpoint_id) if project.src_endpoint_id else None
            dst = session.get(Endpoint, project.dst_endpoint_id) if project.dst_endpoint_id else None
            if src is None or dst is None:
                log_event(
                    session,
                    project_id=self.project_id,
                    code="check_no_endpoints",
                    level="error",
                    message="Проверка не запущена: не выбраны серверы источника или приёмника.",
                )
                return None

            query = session.query(Mailbox).filter(Mailbox.project_id == self.project_id)
            mailboxes = query.order_by(Mailbox.id).all()

            selected: list[int] = []
            skipped = 0
            for mailbox in mailboxes:
                # Липкий флаг: строка уже упала по паролю. Повторная проверка —
                # это ещё одна попытка входа, а их до блокировки всего пять.
                if mailbox.auth_locked and not self.force:
                    skipped += 1
                    continue
                selected.append(mailbox.id)

            parallel = max(1, min(project.max_parallel, src.max_parallel, dst.max_parallel))

            with self._lock:
                self.progress.total = len(mailboxes)
                self.progress.skipped = skipped

            binding = _binding_note(project.max_parallel, src, dst, parallel)
            log_event(
                session,
                project_id=self.project_id,
                code="check_started",
                message=(
                    f"Проверка доступов запущена: {len(selected)} ящиков, "
                    f"параллельно {parallel}{binding}."
                    + (f" Пропущено с прошлой ошибкой пароля: {skipped}." if skipped else "")
                ),
            )

            src_cfg = _endpoint_config(src)
            dst_cfg = _endpoint_config(dst)

        return selected, src_cfg, dst_cfg, parallel

    def _check_one(self, mailbox_id: int, src_cfg: EndpointConfig, dst_cfg: EndpointConfig) -> None:
        if self._stop.is_set():
            return

        with session_scope() as session:
            mailbox = session.get(Mailbox, mailbox_id)
            if mailbox is None:
                return
            mailbox.status = MB_CHECKING
            src_login, src_secret = mailbox.src_email, decrypt(mailbox.src_password_enc)
            dst_login, dst_secret = mailbox.dst_email, decrypt(mailbox.dst_password_enc)

        self._throttle.wait(src_cfg.host)
        src_result = self._probe_with_retries(
            src_cfg, src_login, src_secret, inventory=True, measure_size=self.measure_size
        )

        dst_result: ProbeResult | None = None
        if not self._stop.is_set():
            self._throttle.wait(dst_cfg.host)
            # На приёмнике инвентарь не нужен — там обычно пусто. Нужны только
            # факт входа и квота, чтобы предупредить о нехватке места до старта.
            dst_result = self._probe_with_retries(
                dst_cfg, dst_login, dst_secret, inventory=False, measure_size=False, read_quota=True
            )

        self._store(mailbox_id, src_result, dst_result)

    def _probe_with_retries(
        self,
        cfg: EndpointConfig,
        login: str,
        secret: str | None,
        *,
        inventory: bool,
        measure_size: bool,
        read_quota: bool = False,
    ) -> ProbeResult:
        attempt = 0
        while True:
            result = probe(
                cfg,
                login,
                secret,
                inventory=inventory,
                measure_size=measure_size,
                read_quota=read_quota,
            )
            if result.ok or result.error is None:
                return result

            # Здесь и находится главный предохранитель: ошибка пароля
            # не повторяется никогда. Пять попыток — и учётка заблокирована.
            if result.error.is_auth or not result.error.is_retriable:
                return result

            if attempt >= NETWORK_RETRIES or self._stop.is_set():
                return result

            time.sleep(RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)])
            attempt += 1

    def _store(
        self, mailbox_id: int, src_result: ProbeResult, dst_result: ProbeResult | None
    ) -> None:
        with session_scope() as session:
            mailbox = session.get(Mailbox, mailbox_id)
            if mailbox is None:
                return

            mailbox.checked_at = datetime.now(timezone.utc)
            mailbox.src_auth_attempts += 1
            if dst_result is not None:
                mailbox.dst_auth_attempts += 1

            mailbox.src_check_result = "ok" if src_result.ok else (
                src_result.error.code if src_result.error else "unknown"
            )
            mailbox.src_check_detail = None if src_result.ok else (
                src_result.error.detail if src_result.error else None
            )

            if dst_result is None:
                mailbox.dst_check_result = None
            else:
                mailbox.dst_check_result = "ok" if dst_result.ok else (
                    dst_result.error.code if dst_result.error else "unknown"
                )
                mailbox.dst_check_detail = None if dst_result.ok else (
                    dst_result.error.detail if dst_result.error else None
                )

            auth_problem = _auth_error(src_result) or _auth_error(dst_result)
            if auth_problem is not None:
                mailbox.auth_locked = True

            both_ok = src_result.ok and (dst_result is not None and dst_result.ok)
            mailbox.status = MB_CHECK_OK if both_ok else MB_CHECK_FAILED

            if src_result.ok:
                self._store_inventory(session, mailbox, src_result)
            if dst_result is not None and dst_result.ok:
                mailbox.dst_quota_limit_bytes = dst_result.quota_limit_bytes
                mailbox.dst_quota_used_bytes = dst_result.quota_used_bytes

            self._write_event(session, mailbox, src_result, dst_result, both_ok)

        self._account(ok=both_ok, label=self._label(mailbox_id))

    def _store_inventory(self, session, mailbox: Mailbox, result: ProbeResult) -> None:
        session.query(MailboxFolder).filter(
            MailboxFolder.mailbox_id == mailbox.id, MailboxFolder.side == "src"
        ).delete(synchronize_session=False)

        for folder in result.folders:
            session.add(
                MailboxFolder(
                    mailbox_id=mailbox.id,
                    side="src",
                    name_raw=folder.name_raw,
                    name_display=folder.name_display,
                    delimiter=folder.delimiter,
                    special_use=folder.special_use,
                    selectable=folder.selectable,
                    messages=folder.messages,
                    size_bytes=folder.size_bytes,
                    uidvalidity=folder.uidvalidity,
                )
            )

        mailbox.total_messages = result.total_messages
        mailbox.total_bytes = result.total_bytes

    def _write_event(
        self,
        session,
        mailbox: Mailbox,
        src_result: ProbeResult,
        dst_result: ProbeResult | None,
        both_ok: bool,
    ) -> None:
        if both_ok:
            size = _human_size(mailbox.total_bytes)
            count = mailbox.total_messages if mailbox.total_messages is not None else "?"
            message = f"{mailbox.src_email}: доступ есть, {count} писем, {size}"
            if mailbox.quota_will_overflow:
                message += (
                    "  ВНИМАНИЕ: на приёмнике меньше свободного места, чем объём ящика"
                )
                log_event(
                    session,
                    project_id=mailbox.project_id,
                    mailbox_id=mailbox.id,
                    level="warning",
                    code="quota_overflow",
                    message=message,
                )
                return
            log_event(
                session,
                project_id=mailbox.project_id,
                mailbox_id=mailbox.id,
                code="check_ok",
                message=message,
            )
            return

        problems = []
        if not src_result.ok and src_result.error:
            problems.append(f"источник — {src_result.error.message.lower()}")
        if dst_result is not None and not dst_result.ok and dst_result.error:
            problems.append(f"приёмник — {dst_result.error.message.lower()}")

        log_event(
            session,
            project_id=mailbox.project_id,
            mailbox_id=mailbox.id,
            level="error",
            code="check_failed",
            message=f"{mailbox.src_email}: " + ", ".join(problems or ["неизвестная ошибка"]),
        )

    def _label(self, mailbox_id: int) -> str:
        with session_scope() as session:
            mailbox = session.get(Mailbox, mailbox_id)
            return mailbox.src_email if mailbox else str(mailbox_id)

    def _account(self, *, ok: bool, label: str) -> None:
        with self._lock:
            self.progress.checked += 1
            if ok:
                self.progress.ok += 1
            else:
                self.progress.failed += 1
            self.progress.current = label
            checked = self.progress.checked
            failed = self.progress.failed

        # Второй предохранитель: массовый провал почти всегда означает съехавшие
        # колонки или не тот сервер, а не двести неправильных паролей подряд.
        if checked >= AUTH_FAILURE_ABORT_MIN_CHECKED and failed / checked > AUTH_FAILURE_ABORT_RATIO:
            self._abort(
                f"Остановлено автоматически: не прошли {failed} из {checked} проверенных ящиков. "
                "Похоже, съехали колонки при импорте или выбран не тот сервер. "
                "Продолжение рискует заблокировать учётные записи."
            )

    def _abort(self, reason: str) -> None:
        with self._lock:
            if self.progress.aborted:
                return
            self.progress.aborted = True
            self.progress.abort_reason = reason
        self._stop.set()

        with session_scope() as session:
            log_event(
                session,
                project_id=self.project_id,
                level="error",
                code="check_aborted",
                message=reason,
            )

    def _finish(self) -> None:
        with self._lock:
            self.progress.running = False
            self.progress.finished_at = datetime.now(timezone.utc)
            summary = (
                f"Проверка завершена: доступны {self.progress.ok}, "
                f"с ошибками {self.progress.failed}"
                + (f", пропущено {self.progress.skipped}" if self.progress.skipped else "")
            )
            aborted = self.progress.aborted

        if not aborted:
            with session_scope() as session:
                log_event(
                    session, project_id=self.project_id, code="check_finished", message=summary
                )


# --------------------------------------------------------------------------
# Реестр прогонов: по одному на проект
# --------------------------------------------------------------------------

_runners: dict[int, CheckRunner] = {}
_registry_lock = threading.Lock()


def start_check(project_id: int, *, force: bool = False, measure_size: bool = True) -> CheckRunner:
    with _registry_lock:
        existing = _runners.get(project_id)
        if existing and existing.is_alive:
            return existing
        runner = CheckRunner(project_id, force=force, measure_size=measure_size)
        _runners[project_id] = runner
    runner.start()
    return runner


def get_check(project_id: int) -> CheckRunner | None:
    with _registry_lock:
        return _runners.get(project_id)


def stop_check(project_id: int) -> None:
    with _registry_lock:
        runner = _runners.get(project_id)
    if runner:
        runner.stop()


# --------------------------------------------------------------------------
# Вспомогательное
# --------------------------------------------------------------------------


def _endpoint_config(endpoint: Endpoint) -> EndpointConfig:
    return EndpointConfig(
        host=endpoint.host,
        port=endpoint.port,
        security=endpoint.security,
        verify_cert=endpoint.verify_cert,
        auth_mode=endpoint.auth_mode,
        master_username=endpoint.master_username,
        master_secret=decrypt(endpoint.master_secret_enc),
        master_separator=endpoint.master_separator,
    )


def _auth_error(result: ProbeResult | None) -> ClassifiedError | None:
    if result is None or result.ok or result.error is None:
        return None
    return result.error if result.error.is_auth else None


def _binding_note(project_limit: int, src: Endpoint, dst: Endpoint, effective: int) -> str:
    """Человек должен понимать, почему выбранное им число не применилось —
    иначе он решит, что настройка сломана."""
    if effective >= project_limit:
        return ""
    if src.max_parallel == effective:
        return f" (ограничено сервером «{src.name}» до {effective})"
    if dst.max_parallel == effective:
        return f" (ограничено сервером «{dst.name}» до {effective})"
    return ""


def _human_size(value: int | None) -> str:
    if value is None:
        return "объём неизвестен"
    size = float(value)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if size < 1024 or unit == "ТБ":
            return f"{size:.0f} {unit}" if unit in ("Б", "КБ") else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} ТБ"


def describe_check_result(code: str | None) -> str:
    if code in (None, "ok"):
        return "" if code is None else "доступ есть"
    return human(code)
