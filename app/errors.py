"""Классификация ошибок IMAP.

Человеку в интерфейсе нужен диагноз, а не строка протокола. Разница между
«неверный пароль», «IMAP выключен в настройках ящика» и «нужен пароль
приложения» — это разница между «исправь опечатку», «позвони админу» и
«пользователь должен сам сходить в настройки». Показывать вместо этого
сырой ответ сервера — значит перекладывать разбор на человека.

Отдельно важно отличать ошибки аутентификации от сетевых: сетевые можно
ретраить, аутентификацию — категорически нельзя, иначе пять попыток подряд
заблокируют доменную учётную запись.
"""

from __future__ import annotations

import re
import socket
import ssl
from dataclasses import dataclass

# --- коды ------------------------------------------------------------------

AUTH_FAILED = "auth_failed"
AUTH_APP_PASSWORD_REQUIRED = "app_password_required"
AUTH_IMAP_DISABLED = "imap_disabled"
AUTH_ACCOUNT_LOCKED = "account_locked"
AUTH_ACCOUNT_DISABLED = "account_disabled"
MAILBOX_NOT_FOUND = "mailbox_not_found"

TOO_MANY_CONNECTIONS = "too_many_connections"
RATE_LIMITED = "rate_limited"

CONN_DNS = "dns_error"
CONN_REFUSED = "connection_refused"
CONN_TIMEOUT = "timeout"
CONN_TLS = "tls_error"
SERVER_ERROR = "server_error"
UNKNOWN = "unknown"

# Ошибки аутентификации: ретраить нельзя ни при каких обстоятельствах.
AUTH_CODES = frozenset(
    {
        AUTH_FAILED,
        AUTH_APP_PASSWORD_REQUIRED,
        AUTH_IMAP_DISABLED,
        AUTH_ACCOUNT_LOCKED,
        AUTH_ACCOUNT_DISABLED,
    }
)

# Ошибки, при которых повтор осмыслен (но не сразу и не бесконечно).
RETRIABLE_CODES = frozenset(
    {CONN_DNS, CONN_REFUSED, CONN_TIMEOUT, SERVER_ERROR, TOO_MANY_CONNECTIONS, RATE_LIMITED}
)

HUMAN_MESSAGES = {
    AUTH_FAILED: "Неверный логин или пароль",
    AUTH_APP_PASSWORD_REQUIRED: "Нужен пароль приложения, обычный пароль не подходит",
    AUTH_IMAP_DISABLED: "Доступ по IMAP отключён в настройках ящика",
    AUTH_ACCOUNT_LOCKED: "Учётная запись заблокирована",
    AUTH_ACCOUNT_DISABLED: "Учётная запись отключена",
    MAILBOX_NOT_FOUND: "Ящик не найден на сервере",
    TOO_MANY_CONNECTIONS: "Слишком много одновременных подключений",
    RATE_LIMITED: "Сервер ограничивает частоту обращений",
    CONN_DNS: "Не удалось разрешить имя сервера",
    CONN_REFUSED: "Сервер отказал в подключении",
    CONN_TIMEOUT: "Сервер не ответил вовремя",
    CONN_TLS: "Ошибка TLS или сертификата",
    SERVER_ERROR: "Сервер вернул ошибку",
    UNKNOWN: "Неопознанная ошибка",
}

# Подсказка «что делать» — то, ради чего классификация и затевалась.
HINTS = {
    AUTH_APP_PASSWORD_REQUIRED: (
        "Пользователь должен включить двухфакторную аутентификацию и создать "
        "пароль приложения в настройках своего ящика."
    ),
    AUTH_IMAP_DISABLED: "Включить IMAP в настройках почтового ящика.",
    AUTH_ACCOUNT_LOCKED: (
        "Обычно это следствие нескольких неудачных попыток входа. Дождись снятия "
        "блокировки и убедись, что пароль верный, прежде чем пробовать снова."
    ),
    TOO_MANY_CONNECTIONS: (
        "Снизь параллельность для этого сервера. Ограничение чаще всего "
        "действует на IP-адрес, а не на учётную запись."
    ),
    CONN_TLS: "Проверь порт, режим шифрования и срок действия сертификата.",
}

# Порядок важен: более частные образцы должны стоять раньше общих.
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"application[- ]specific password|пароль для внешнего приложения|"
                r"application password|app password", re.I), AUTH_APP_PASSWORD_REQUIRED),
    (re.compile(r"imap.{0,20}(disabled|отключ)|(disabled|отключ).{0,20}imap|"
                r"imap access is disabled", re.I), AUTH_IMAP_DISABLED),
    (re.compile(r"account (is )?(locked|blocked)|заблокирован", re.I), AUTH_ACCOUNT_LOCKED),
    (re.compile(r"account (is )?disabled|отключена", re.I), AUTH_ACCOUNT_DISABLED),
    (re.compile(r"too many (simultaneous )?connections|maximum number of connections|"
                r"\[LIMIT\]|connection limit", re.I), TOO_MANY_CONNECTIONS),
    (re.compile(r"too many requests|rate limit|\[THROTTLED\]|try again later", re.I), RATE_LIMITED),
    (re.compile(r"authenticationfailed|authentication failed|login failed|"
                r"invalid credentials|authenticate failed|\[AUTHENTICATIONFAILED\]|"
                r"invalid user or password|logon failure", re.I), AUTH_FAILED),
    (re.compile(r"\[UNAVAILABLE\]|\[SERVERBUG\]|internal error|temporary failure", re.I), SERVER_ERROR),
    (re.compile(r"(mailbox|user) (does not exist|not found|unknown)", re.I), MAILBOX_NOT_FOUND),
)


@dataclass(frozen=True)
class ClassifiedError:
    code: str
    message: str
    detail: str
    hint: str | None = None

    @property
    def is_auth(self) -> bool:
        return self.code in AUTH_CODES

    @property
    def is_retriable(self) -> bool:
        return self.code in RETRIABLE_CODES


def classify(exc: BaseException) -> ClassifiedError:
    """Превратить исключение в понятный человеку диагноз."""
    detail = str(exc).strip() or exc.__class__.__name__

    if isinstance(exc, ssl.SSLError) or isinstance(exc, ssl.CertificateError):
        return _build(CONN_TLS, detail)
    if isinstance(exc, socket.gaierror):
        return _build(CONN_DNS, detail)
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return _build(CONN_TIMEOUT, detail)
    if isinstance(exc, ConnectionRefusedError):
        return _build(CONN_REFUSED, detail)

    for pattern, code in _PATTERNS:
        if pattern.search(detail):
            return _build(code, detail)

    if isinstance(exc, (ConnectionError, OSError)):
        return _build(CONN_REFUSED, detail)

    return _build(UNKNOWN, detail)


def _build(code: str, detail: str) -> ClassifiedError:
    return ClassifiedError(
        code=code,
        message=HUMAN_MESSAGES.get(code, HUMAN_MESSAGES[UNKNOWN]),
        detail=detail,
        hint=HINTS.get(code),
    )


def human(code: str | None) -> str:
    if not code:
        return ""
    return HUMAN_MESSAGES.get(code, code)
