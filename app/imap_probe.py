"""Проба IMAP: проверка доступа и инвентаризация ящика.

Это единственное место, где мы сами разговариваем с IMAP. Перенос писем делает
imapsync — там двадцать лет отлаженных краевых случаев, повторять их не надо.
А вот чтение метаданных простое и безопасное, и без него не построить ни
проверку доступов, ни знаменатель прогресса, ни отчёт о сверке.

Порядок опознания спецпапок принципиален: сначала флаги SPECIAL-USE от самого
сервера, и только если их нет — словарь имён. Иначе на приёмнике окажутся
одновременно «Sent Items» и «Отправленные» с размазанными по ним письмами.
"""

from __future__ import annotations

import logging
import ssl
from dataclasses import dataclass, field

from imapclient import IMAPClient
from imapclient.exceptions import IMAPClientError

from app.config import IMAP_TIMEOUT_SECONDS
from app.errors import ClassifiedError, classify
from app.presets import detect_role, load_folder_dictionary

log = logging.getLogger(__name__)

AUTH_PASSWORD = "password"
AUTH_MASTER = "master"
AUTH_XOAUTH2 = "xoauth2"

SPECIAL_USE_FLAGS = {
    b"\\sent", b"\\drafts", b"\\trash", b"\\junk", b"\\archive", b"\\all", b"\\flagged"
}

# Размер ящика: способ зависит от того, что умеет сервер.
SIZE_VIA_STATUS = "status-size"   # RFC 8438, одна команда на папку
SIZE_VIA_FETCH = "fetch"          # FETCH RFC822.SIZE, дороже, но работает везде
SIZE_UNKNOWN = "none"


@dataclass
class FolderInfo:
    name_raw: str
    name_display: str
    delimiter: str | None = None
    special_use: str | None = None
    selectable: bool = True
    role: str | None = None
    messages: int | None = None
    size_bytes: int | None = None
    uidvalidity: int | None = None
    skipped_reason: str | None = None


@dataclass
class ProbeResult:
    ok: bool = False
    error: ClassifiedError | None = None
    capabilities: list[str] = field(default_factory=list)
    folders: list[FolderInfo] = field(default_factory=list)
    delimiter: str | None = None
    namespace_prefix: str | None = None
    size_method: str = SIZE_UNKNOWN
    quota_limit_bytes: int | None = None
    quota_used_bytes: int | None = None

    @property
    def total_messages(self) -> int | None:
        counted = [f.messages for f in self.folders if f.messages is not None]
        return sum(counted) if counted else None

    @property
    def total_bytes(self) -> int | None:
        counted = [f.size_bytes for f in self.folders if f.size_bytes is not None]
        return sum(counted) if counted else None

    @property
    def has_special_use(self) -> bool:
        """Объявляет ли сервер спецпапки сам. Полезно знать: у части серверов
        (в том числе, возможно, локального Exchange) этого нет, и всё держится
        на словаре имён."""
        return any(f.special_use for f in self.folders)


@dataclass
class EndpointConfig:
    """То, что нужно для подключения. Отдельно от модели БД, чтобы пробу можно
    было вызвать из теста или из формы «проверить соединение» без сохранения."""

    host: str
    port: int = 993
    security: str = "ssl"          # ssl | starttls | none
    verify_cert: bool = True
    auth_mode: str = AUTH_PASSWORD
    master_username: str | None = None
    master_secret: str | None = None
    master_separator: str = "*"


def _ssl_context(verify: bool) -> ssl.SSLContext:
    context = ssl.create_default_context()
    if not verify:
        # Осознанное послабление для локальных серверов с самоподписанным
        # сертификатом. Включается только вручную и только в настройках сервера.
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


def _connect(cfg: EndpointConfig) -> IMAPClient:
    use_ssl = cfg.security == "ssl"
    client = IMAPClient(
        cfg.host,
        port=cfg.port,
        ssl=use_ssl,
        ssl_context=_ssl_context(cfg.verify_cert) if use_ssl else None,
        timeout=IMAP_TIMEOUT_SECONDS,
    )
    if cfg.security == "starttls":
        client.starttls(_ssl_context(cfg.verify_cert))
    return client


def _authenticate(client: IMAPClient, cfg: EndpointConfig, username: str, secret: str | None) -> None:
    if cfg.auth_mode == AUTH_XOAUTH2:
        token = cfg.master_secret or secret or ""
        client.oauth2_login(username, token)
        return

    if cfg.auth_mode == AUTH_MASTER:
        # Проксирующий логин: 'ящик*администратор' у Dovecot, Zimbra, Courier.
        # Exchange так не умеет — там нужен пароль от каждого ящика.
        login = f"{username}{cfg.master_separator}{cfg.master_username or ''}"
        client.login(login, cfg.master_secret or "")
        return

    client.login(username, secret or "")


def probe(
    cfg: EndpointConfig,
    username: str,
    secret: str | None = None,
    *,
    inventory: bool = True,
    measure_size: bool = True,
    read_quota: bool = False,
) -> ProbeResult:
    """Подключиться, проверить доступ и (по желанию) собрать инвентарь.

    Никаких повторов внутри: решение о повторе принимает вызывающий код,
    и для ошибок аутентификации ответ всегда «нет».
    """
    result = ProbeResult()
    client: IMAPClient | None = None

    try:
        client = _connect(cfg)
        _authenticate(client, cfg, username, secret)
        result.ok = True
        result.capabilities = [c.decode("ascii", "replace") for c in client.capabilities()]

        if inventory:
            _collect_folders(client, result, measure_size=measure_size)
        if read_quota:
            _collect_quota(client, result)

    except Exception as exc:  # noqa: BLE001 — классифицируем всё, что прилетело
        result.ok = False
        result.error = classify(exc)
        log.info(
            "Проба %s@%s: %s (%s)",
            username, cfg.host, result.error.code, result.error.detail[:200],
        )
    finally:
        if client is not None:
            try:
                client.logout()
            except Exception:  # noqa: BLE001 — на закрытии уже неважно
                pass

    return result


def _collect_folders(client: IMAPClient, result: ProbeResult, *, measure_size: bool) -> None:
    capabilities = {c.upper() for c in result.capabilities}
    supports_status_size = "STATUS=SIZE" in capabilities

    try:
        result.namespace_prefix = _personal_prefix(client)
    except Exception:  # noqa: BLE001 — NAMESPACE есть не у всех, это не повод падать
        result.namespace_prefix = None

    dictionary = load_folder_dictionary()

    for flags, delimiter, name in client.list_folders():
        delim = _as_text(delimiter)
        if result.delimiter is None and delim:
            result.delimiter = delim

        flag_set = {f.lower() for f in flags}
        special = next((_as_text(f) for f in flags if f.lower() in SPECIAL_USE_FLAGS), None)
        selectable = b"\\noselect" not in flag_set

        info = FolderInfo(
            name_raw=_encode_folder(name),
            name_display=name,
            delimiter=delim,
            special_use=special,
            selectable=selectable,
            role=detect_role(name, special),
        )

        if not selectable:
            info.skipped_reason = "служебная папка без писем"
        elif dictionary.is_never_migrate(name):
            # Outbox, календари и прочее — это не почта пользователя.
            info.skipped_reason = "не почтовая папка, не переносится"
        else:
            _collect_folder_stats(
                client, info, supports_status_size=supports_status_size, measure_size=measure_size
            )

        result.folders.append(info)

    result.size_method = (
        SIZE_VIA_STATUS if supports_status_size
        else SIZE_VIA_FETCH if measure_size
        else SIZE_UNKNOWN
    )


def _collect_folder_stats(
    client: IMAPClient,
    info: FolderInfo,
    *,
    supports_status_size: bool,
    measure_size: bool,
) -> None:
    items = [b"MESSAGES", b"UIDVALIDITY"]
    if supports_status_size:
        items.append(b"SIZE")

    try:
        status = client.folder_status(info.name_display, items)
    except (IMAPClientError, OSError) as exc:
        info.skipped_reason = f"не удалось прочитать: {exc}"
        return

    info.messages = _as_int(status.get(b"MESSAGES"))
    info.uidvalidity = _as_int(status.get(b"UIDVALIDITY"))

    if supports_status_size:
        info.size_bytes = _as_int(status.get(b"SIZE"))
        return

    if not measure_size or not info.messages:
        info.size_bytes = 0 if info.messages == 0 else None
        return

    # Сервер не умеет STATUS=SIZE (RFC 8438) — считаем сами. Тянем только
    # размеры, не тела писем: одна команда на папку, ответ в виде списка чисел.
    try:
        client.select_folder(info.name_display, readonly=True)
        sizes = client.fetch("1:*", [b"RFC822.SIZE"])
        info.size_bytes = sum(int(v.get(b"RFC822.SIZE", 0) or 0) for v in sizes.values())
    except (IMAPClientError, OSError, ValueError) as exc:
        log.debug("Размер папки %s не посчитан: %s", info.name_display, exc)
        info.size_bytes = None


def _collect_quota(client: IMAPClient, result: ProbeResult) -> None:
    """Квота приёмника нужна, чтобы предупредить о нехватке места ДО старта,
    а не узнать об этом на четвёртом часу переноса."""
    if not client.has_capability("QUOTA"):
        return
    try:
        quotas = client.get_quota(mailbox="INBOX")
    except (IMAPClientError, OSError) as exc:
        log.debug("Квота не прочитана: %s", exc)
        return

    for quota in quotas or []:
        if str(getattr(quota, "resource", "")).upper() != "STORAGE":
            continue
        # В протоколе значения STORAGE указываются в килобайтах.
        result.quota_used_bytes = int(quota.usage) * 1024
        result.quota_limit_bytes = int(quota.limit) * 1024
        break


def _personal_prefix(client: IMAPClient) -> str | None:
    """Часть серверов держит все папки внутри INBOX. — без учёта этого на
    приёмнике появляется INBOX/INBOX/Работа."""
    ns = client.namespace()
    personal = getattr(ns, "personal", None)
    if personal:
        prefix = personal[0][0]
        return prefix or None
    return None


def _as_text(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("ascii", "replace")
    return str(value)


def _as_int(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def encode_folder_name(name: str) -> str:
    """Имя папки так, как оно выглядит в протоколе IMAP (modified UTF-7).

    Именно это имя нужно передавать imapsync в параметрах командной строки —
    он сам об этом пишет в своём выводе: «X is the imap foldername you have to
    use in command line options». Если отдать ему читаемое «Спам», аргумент
    приедет искажённым и не совпадёт ни с одной папкой.
    """
    if name.isascii():
        return name
    try:
        from imapclient import imap_utf7

        return imap_utf7.encode(name).decode("ascii", "replace")
    except Exception:  # noqa: BLE001 — не критично
        return name


# Прежнее внутреннее имя оставлено, чтобы не трогать вызовы внутри модуля.
_encode_folder = encode_folder_name
