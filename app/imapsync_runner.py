"""Запуск imapsync на один ящик.

Здесь мы не пересказываем логику переноса — её делает imapsync, у которого
двадцать лет отлаженных краевых случаев. Наша задача ровно три вещи:

1. собрать корректную команду и **не отдать пароли через argv** — иначе их
   увидит любой пользователь хоста в выводе `ps`;
2. читать вывод построчно, класть в файл и вытаскивать из него прогресс;
3. превратить код возврата в понятный человеку исход.

Про коды возврата: они у imapsync документированы, и это надёжнее любых
регулярных выражений. Особенно важен 16 — ошибка аутентификации: такой ящик
повторять нельзя, иначе упрёмся в блокировку учётной записи.

Про разбор вывода: формат построчного вывода в документации не зафиксирован,
поэтому парсер намеренно защитный. Если ни одна строка не опознана, прогресс
не врёт, а просто остаётся на уровне «ящик N из M» — см. MigrationProgress.
"""

from __future__ import annotations

import gzip
import logging
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.config import IMAPSYNC_CACHE_DIR, RAW_LOG_DIR

log = logging.getLogger(__name__)

IMAPSYNC_BIN = os.environ.get("IMAPSYNC_BIN", "imapsync")

# --- коды возврата imapsync ------------------------------------------------

EXIT_OK = 0
EXIT_CONNECTION_FAILURE = 10
EXIT_TLS_FAILURE = 12
EXIT_AUTHENTICATION_FAILURE = 16
EXIT_CONNECTION_FAILURE_HOST1 = 101
EXIT_CONNECTION_FAILURE_HOST2 = 102
EXIT_WITH_ERRORS = 111
EXIT_WITH_ERRORS_MAX = 112
EXIT_OVERQUOTA = 113

# Повторять осмысленно: проблема снаружи и, скорее всего, временная.
RETRIABLE_EXITS = frozenset(
    {
        EXIT_CONNECTION_FAILURE,
        EXIT_CONNECTION_FAILURE_HOST1,
        EXIT_CONNECTION_FAILURE_HOST2,
        69,   # сервис недоступен
    }
)

# Повторять нельзя ни при каких обстоятельствах: следующая попытка только
# приблизит блокировку учётной записи.
FATAL_EXITS = frozenset({EXIT_AUTHENTICATION_FAILURE, EXIT_TLS_FAILURE, 64, 66})

EXIT_MESSAGES = {
    EXIT_OK: "перенос завершён",
    EXIT_CONNECTION_FAILURE: "не удалось подключиться",
    EXIT_CONNECTION_FAILURE_HOST1: "не удалось подключиться к источнику",
    EXIT_CONNECTION_FAILURE_HOST2: "не удалось подключиться к приёмнику",
    EXIT_TLS_FAILURE: "ошибка TLS",
    EXIT_AUTHENTICATION_FAILURE: "неверный логин или пароль",
    EXIT_WITH_ERRORS: "завершено, но часть писем не перенеслась",
    EXIT_WITH_ERRORS_MAX: "слишком много ошибок, прогон остановлен",
    EXIT_OVERQUOTA: "на приёмнике закончилось место",
    6: "процесс остановлен",
}


def describe_exit(code: int | None) -> str:
    if code is None:
        return "прогон не завершён"
    return EXIT_MESSAGES.get(code, f"imapsync завершился с кодом {code}")


# --- разбор вывода ---------------------------------------------------------

# Формат вывода imapsync не документирован построчно, поэтому шаблоны собраны
# в одном месте и рассчитаны на то, что часть из них может не сработать.
# Если после первого боевого прогона окажется, что формат другой — правится
# только этот блок.
FOLDER_PATTERNS = (
    re.compile(r"^\s*(?:\++\s*)?(?:Folder|folder)\s+\[(?P<src>[^\]]*)\]\s*->\s*\[(?P<dst>[^\]]*)\]"),
    re.compile(r"^\s*From folder\s+\[(?P<src>[^\]]*)\]"),
)
COPIED_PATTERN = re.compile(r"\bcopied\b", re.IGNORECASE)
SIZE_PATTERN = re.compile(r"\{(\d+)\}")
SKIPPED_PATTERN = re.compile(r"\bskip(?:ped|ping)\b", re.IGNORECASE)
ERROR_PATTERN = re.compile(r"^\s*(?:Err|ERROR|Error)\b|\bfailure\b", re.IGNORECASE)


@dataclass
class LineEvent:
    kind: str                 # folder | copied | skipped | error | plain
    folder: str | None = None
    size: int | None = None


def parse_line(line: str) -> LineEvent:
    for pattern in FOLDER_PATTERNS:
        match = pattern.search(line)
        if match:
            return LineEvent("folder", folder=match.group("src").strip())

    if COPIED_PATTERN.search(line):
        size_match = SIZE_PATTERN.search(line)
        return LineEvent("copied", size=int(size_match.group(1)) if size_match else None)

    if SKIPPED_PATTERN.search(line):
        return LineEvent("skipped")

    if ERROR_PATTERN.search(line):
        return LineEvent("error")

    return LineEvent("plain")


# --- параметры прогона -----------------------------------------------------


@dataclass
class SideSpec:
    host: str
    port: int
    security: str          # ssl | starttls | none
    verify_cert: bool
    login: str
    secret: str


@dataclass
class RunSpec:
    mailbox_id: int
    project_id: int
    src: SideSpec
    dst: SideSpec
    # Пары «папка источника -> папка приёмника», посчитанные по ролям.
    folder_map: dict[str, str] = field(default_factory=dict)
    # Папки источника, которые не переносим (Спам, Корзина, служебные).
    exclude_folders: tuple[str, ...] = ()
    max_message_bytes: int | None = None
    dry_run: bool = False


@dataclass
class RunResult:
    exit_code: int | None = None
    copied_messages: int = 0
    copied_bytes: int = 0
    error_lines: int = 0
    log_path: Path | None = None
    stopped: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == EXIT_OK

    @property
    def retriable(self) -> bool:
        return self.exit_code in RETRIABLE_EXITS

    @property
    def fatal(self) -> bool:
        return self.exit_code in FATAL_EXITS


def is_available() -> bool:
    """Есть ли imapsync в системе. В образе он есть; при локальной разработке
    его может не быть, и об этом надо сказать внятно, а не падать."""
    return shutil.which(IMAPSYNC_BIN) is not None


class ImapsyncRun:
    """Один процесс imapsync. Потокобезопасен для чтения прогресса."""

    def __init__(self, spec: RunSpec, on_progress=None) -> None:
        self.spec = spec
        self.on_progress = on_progress
        self.result = RunResult()
        self._process: subprocess.Popen | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._current_folder: str | None = None
        self._recognised_lines = 0
        self._last_line = ""
        # Скорость считаем по скользящему окну: средняя за весь прогон врёт
        # тем сильнее, чем дольше он идёт.
        self._window: list[tuple[float, int]] = []

    # -- состояние для интерфейса ------------------------------------------

    @property
    def current_folder(self) -> str | None:
        with self._lock:
            return self._current_folder

    @property
    def last_line(self) -> str:
        with self._lock:
            return self._last_line

    @property
    def output_recognised(self) -> bool:
        """Понял ли парсер хоть что-нибудь. Если нет — интерфейс не должен
        показывать нули как факт, лучше честно опереться на счётчик ящиков."""
        with self._lock:
            return self._recognised_lines > 0

    @property
    def speed_bytes_per_second(self) -> float:
        now = time.monotonic()
        with self._lock:
            window = [(t, b) for t, b in self._window if now - t <= 300]
            self._window = window
        if len(window) < 2:
            return 0.0
        span = window[-1][0] - window[0][0]
        if span <= 0:
            return 0.0
        return sum(b for _, b in window[1:]) / span

    def stop(self) -> None:
        self._stop.set()
        process = self._process
        if process and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass

    # -- запуск -------------------------------------------------------------

    def run(self) -> RunResult:
        log_path = _log_path(self.spec)
        self.result.log_path = log_path
        secrets = (self.spec.src.secret, self.spec.dst.secret)

        with tempfile.TemporaryDirectory(prefix="imapsync-creds-") as creds_dir:
            pass1 = _write_secret(Path(creds_dir) / "p1", self.spec.src.secret)
            pass2 = _write_secret(Path(creds_dir) / "p2", self.spec.dst.secret)
            command = self._build_command(pass1, pass2)

            with log_path.open("w", encoding="utf-8", errors="replace") as log_file:
                log_file.write(
                    "# " + " ".join(_display_command(command)) + "\n"
                    "# пароли переданы файлами, в командной строке их нет\n\n"
                )
                self._execute(command, log_file, secrets)

        _compress(log_path)
        self.result.log_path = log_path.with_suffix(log_path.suffix + ".gz")
        return self.result

    def _execute(self, command: list[str], log_file, secrets: tuple[str, str]) -> None:
        try:
            self._process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except FileNotFoundError:
            message = (
                f"Не найден исполняемый файл «{IMAPSYNC_BIN}». "
                "В контейнере он ставится при сборке; при локальном запуске "
                "поставь imapsync или укажи путь в переменной IMAPSYNC_BIN."
            )
            log.error(message)
            log_file.write(message + "\n")
            self.result.exit_code = 66
            return

        assert self._process.stdout is not None
        for raw in self._process.stdout:
            line = raw.rstrip("\n")
            # Пароль не должен попасть в файл ни при какой ошибке и ни в каком
            # режиме отладки — вычищаем по фактическим значениям.
            clean = _scrub(line, secrets)
            log_file.write(clean + "\n")
            self._consume(clean)

            if self._stop.is_set():
                break

        self._process.wait()
        self.result.exit_code = self._process.returncode
        self.result.stopped = self._stop.is_set()

    def _consume(self, line: str) -> None:
        event = parse_line(line)

        with self._lock:
            self._last_line = line.strip()[:300]

        if event.kind == "folder":
            with self._lock:
                self._current_folder = event.folder
                self._recognised_lines += 1
        elif event.kind == "copied":
            size = event.size or 0
            self.result.copied_messages += 1
            self.result.copied_bytes += size
            with self._lock:
                self._recognised_lines += 1
                self._window.append((time.monotonic(), size))
        elif event.kind == "error":
            self.result.error_lines += 1

        if self.on_progress is not None:
            self.on_progress(self, event)

    # -- команда ------------------------------------------------------------

    def _build_command(self, pass1: Path, pass2: Path) -> list[str]:
        spec = self.spec
        command = [
            IMAPSYNC_BIN,
            "--host1", spec.src.host, "--port1", str(spec.src.port),
            "--user1", spec.src.login, "--passfile1", str(pass1),
            "--host2", spec.dst.host, "--port2", str(spec.dst.port),
            "--user2", spec.dst.login, "--passfile2", str(pass2),
            # Свой лог мы ведём сами, каталог LOG_imapsync не нужен.
            "--nolog",
            # Размеры папок мы уже посчитали на фазе проверки — незачем платить
            # за это ещё раз в начале и в конце прогона.
            "--nofoldersizes", "--nofoldersizesatend",
            # Кэш ускоряет повторные прогоны: без него каждый «досинхронить»
            # заново вычитывает заголовки всего ящика.
            "--usecache", "--tmpdir", str(_cache_dir(spec)),
            # Догадка о системных папках самим imapsync — поверх неё ниже идут
            # явные пары, посчитанные по нашим пресетам.
            "--automap",
        ]

        command += _security_flags(spec.src, "1")
        command += _security_flags(spec.dst, "2")

        for src_name, dst_name in spec.folder_map.items():
            if src_name and dst_name and src_name != dst_name:
                command += ["--f1f2", f"{src_name}={dst_name}"]

        if spec.exclude_folders:
            pattern = "|".join(re.escape(name) for name in spec.exclude_folders)
            command += ["--exclude", f"^({pattern})$"]

        if spec.max_message_bytes:
            command += ["--maxsize", str(spec.max_message_bytes)]

        if spec.dry_run:
            command.append("--dry")

        return command


# --- вспомогательное -------------------------------------------------------


def _security_flags(side: SideSpec, index: str) -> list[str]:
    if side.security == "ssl":
        flags = [f"--ssl{index}"]
    elif side.security == "starttls":
        flags = [f"--tls{index}"]
    else:
        return [f"--nossl{index}"]

    if not side.verify_cert:
        # Осознанное послабление для локальных серверов с самоподписанным
        # сертификатом; включается только галкой в настройках сервера.
        flags += [f"--sslargs{index}", "SSL_verify_mode=0"]
    return flags


def _write_secret(path: Path, secret: str) -> Path:
    """Пароль уходит файлом, а не аргументом: argv виден в `ps` всем."""
    path.write_text((secret or "") + "\n", encoding="utf-8")
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    return path


def _display_command(command: list[str]) -> list[str]:
    """Команда для лога: пути к файлам паролей заменяем, чтобы не подсказывать,
    где они лежали."""
    shown = []
    skip_next = False
    for item in command:
        if skip_next:
            shown.append("<файл с паролем>")
            skip_next = False
            continue
        shown.append(item)
        if item in ("--passfile1", "--passfile2"):
            skip_next = True
    return shown


def _scrub(line: str, secrets: tuple[str, ...]) -> str:
    for secret in secrets:
        if secret and secret in line:
            line = line.replace(secret, "***")
    # На случай режима отладки IMAP, где в лог попадает сама команда LOGIN.
    return re.sub(r"(LOGIN\s+\S+\s+)\S+", r"\1***", line, flags=re.IGNORECASE)


def _cache_dir(spec: RunSpec) -> Path:
    path = IMAPSYNC_CACHE_DIR / f"project_{spec.project_id}" / f"mailbox_{spec.mailbox_id}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _log_path(spec: RunSpec) -> Path:
    folder = RAW_LOG_DIR / f"project_{spec.project_id}"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return folder / f"mailbox_{spec.mailbox_id}_{stamp}.log"


def _compress(path: Path) -> None:
    """Строка на письмо — это 15-20 МБ на крупный ящик. Текст жмётся раз в
    двадцать, поэтому сжимаем сразу по завершении."""
    try:
        with path.open("rb") as src, gzip.open(str(path) + ".gz", "wb") as dst:
            shutil.copyfileobj(src, dst)
        path.unlink()
    except OSError as exc:
        log.warning("Не удалось сжать лог %s: %s", path, exc)


def read_log_tail(path: Path, offset: int = 0, limit: int = 64_000) -> tuple[str, int]:
    """Хвост лога с указанной позиции — для опроса из интерфейса.

    Опрос с offset выбран вместо SSE осознанно: образ публичный, его ставят за
    неизвестным прокси, а потоковые ответы регулярно ломаются о буферизацию.
    Здесь же разрыв не страшен — следующий запрос продолжит с той же позиции.
    """
    if path.suffix == ".gz" or not path.exists():
        gz = path if path.suffix == ".gz" else Path(str(path) + ".gz")
        if gz.exists():
            with gzip.open(gz, "rt", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            return text[offset:offset + limit], min(len(text), offset + limit)
        return "", offset

    with path.open("r", encoding="utf-8", errors="replace") as fh:
        fh.seek(offset)
        chunk = fh.read(limit)
        return chunk, fh.tell()
