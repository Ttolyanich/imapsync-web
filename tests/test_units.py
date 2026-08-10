"""Проверка классификации ошибок, опознания папок и пробы к закрытому порту."""

import socket
import ssl
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.errors import classify  # noqa: E402
from app.imap_probe import EndpointConfig, probe  # noqa: E402
from app.presets import (  # noqa: E402
    detect_role,
    load_folder_dictionary,
    load_presets,
    presets_for_side,
)

fails = []


def check(label, got, expected):
    ok = got == expected
    print(f"[{'OK  ' if ok else 'FAIL'}] {label}: {got!r}")
    if not ok:
        fails.append(f"{label}: ожидалось {expected!r}, получено {got!r}")


print("--- классификация ответов серверов ---")
cases = [
    ("[AUTHENTICATIONFAILED] Invalid credentials", "auth_failed"),
    ("LOGIN failed", "auth_failed"),
    ("[ALERT] Application-specific password required", "app_password_required"),
    ("Please use application password", "app_password_required"),
    ("IMAP access is disabled for this user", "imap_disabled"),
    ("Too many simultaneous connections", "too_many_connections"),
    ("[LIMIT] Maximum number of connections", "too_many_connections"),
    ("Account is locked", "account_locked"),
    ("[UNAVAILABLE] Temporary failure", "server_error"),
]
for text, expected in cases:
    check(text[:46], classify(Exception(text)).code, expected)

check("SSL", classify(ssl.SSLError("bad handshake")).code, "tls_error")
check("DNS", classify(socket.gaierror("no such host")).code, "dns_error")
check("таймаут", classify(TimeoutError("timed out")).code, "timeout")

print("\n--- повторы ---")
check("ошибка пароля не повторяется", classify(Exception("LOGIN failed")).is_retriable, False)
check("сетевая ошибка повторяется", classify(TimeoutError("x")).is_retriable, True)
check("ошибка пароля помечена как auth", classify(Exception("LOGIN failed")).is_auth, True)

print("\n--- опознание папок ---")
check("SPECIAL-USE важнее имени", detect_role("Хлам", "\\Junk"), "junk")
check("русское имя", detect_role("Отправленные"), "sent")
check("Exchange", detect_role("Sent Items"), "sent")
check("Deleted Items", detect_role("Deleted Items"), "trash")
check("Junk Email", detect_role("Junk Email"), "junk")
check("регистр не важен", detect_role("корзина"), "trash")
check("пользовательская папка", detect_role("Договоры 2025"), None)

print("\n--- пресеты ---")
presets = load_presets()
check("каталог пресетов загружен", sorted(presets),
      ["exchange-onprem", "generic-imap", "gmail", "mailru", "yandex", "zimbra"])
check("«Другой IMAP» стоит последним в плитках",
      [p.id for p in presets_for_side("source")][-1], "generic-imap")
check("Zimbra умеет мастер-доступ", presets["zimbra"].auth_mode, "master")
check("Яндекс: удалённые, не корзина",
      presets["yandex"].folder_for_role("trash"), "Удалённые")
check("Mail.ru -> Отправленные", presets["mailru"].folder_for_role("sent"), "Отправленные")
check("Exchange -> Sent Items", presets["exchange-onprem"].folder_for_role("sent"), "Sent Items")

print("\n--- проба к закрытому порту ---")
result = probe(EndpointConfig(host="127.0.0.1", port=1, security="none"), "u", "p")
check("проба не упала исключением", result.ok, False)
print(f"       код: {result.error.code}, текст: {result.error.message}")
check("ошибка распознана как сетевая", result.error.is_retriable, True)

print("\n--- предложение по папкам ---")
from types import SimpleNamespace  # noqa: E402

from app.folder_mapping import _propose  # noqa: E402

exchange = load_presets()["exchange-onprem"]
folder_dict = load_folder_dictionary()
project_default = SimpleNamespace(
    migrate_spam=False, migrate_trash=True,
    unknown_folder_policy="create", unknown_folder_container=None,
)


def propose(name, special=None, project=project_default):
    return _propose(name, special, exchange, folder_dict, project)


check("Отправленные -> Sent Items", propose("Отправленные").dst_name, "Sent Items")
check("Корзина переносится", propose("Корзина").dst_name, "Deleted Items")
check("Спам пропускается", propose("Спам").action, "skip")
check("Outbox не трогаем", propose("Outbox").action, "skip")
check("SPECIAL-USE важнее имени", propose("Хлам", "\\Junk").action, "skip")
check("своя папка как есть", propose("Договоры 2025").dst_name, "Договоры 2025")

project_no_trash = SimpleNamespace(
    migrate_spam=False, migrate_trash=False,
    unknown_folder_policy="container", unknown_folder_container="Импортировано",
)
check("корзина выключена настройкой",
      propose("Корзина", project=project_no_trash).action, "skip")
check("своя папка в контейнер",
      propose("Договоры", project=project_no_trash).dst_name, "Импортировано/Договоры")

print("\n--- команда imapsync ---")
import tempfile  # noqa: E402
from pathlib import Path as _Path  # noqa: E402

from app.imapsync_runner import (  # noqa: E402
    EXIT_AUTHENTICATION_FAILURE,
    EXIT_CONNECTION_FAILURE_HOST1,
    EXIT_OVERQUOTA,
    ImapsyncRun,
    RunSpec,
    SideSpec,
    _scrub,
    parse_line,
)

spec = RunSpec(
    mailbox_id=7,
    project_id=3,
    src=SideSpec("imap.example.com", 993, "ssl", True, "ivanov@example.com", "СЕКРЕТ-1"),
    dst=SideSpec("mail.corp.local", 993, "ssl", False, "ivanov@corp.local", "СЕКРЕТ-2"),
    folder_map={"Отправленные": "Sent Items"},
    exclude_folders=("Спам", "Корзина"),
    max_message_bytes=35 * 1024 * 1024,
)
with tempfile.TemporaryDirectory() as tmp:
    p1 = _Path(tmp) / "p1"
    p2 = _Path(tmp) / "p2"
    p1.write_text("x")
    p2.write_text("x")
    cmd = ImapsyncRun(spec)._build_command(p1, p2)

joined = " ".join(cmd)
check("пароля нет в командной строке", "СЕКРЕТ-1" in joined or "СЕКРЕТ-2" in joined, False)
check("пароли переданы файлами", "--passfile1" in cmd and "--passfile2" in cmd, True)
check("кэш по умолчанию выключен", "--nousecache" in cmd, True)

# А включённый кэш должен доезжать до команды.
spec_cached = RunSpec(
    mailbox_id=7, project_id=3, src=spec.src, dst=spec.dst, use_cache=True,
)
with tempfile.TemporaryDirectory() as tmp:
    p1 = _Path(tmp) / "p1"; p1.write_text("x")
    p2 = _Path(tmp) / "p2"; p2.write_text("x")
    cached_cmd = ImapsyncRun(spec_cached)._build_command(p1, p2)
check("включённый кэш попадает в команду", "--usecache" in cached_cmd, True)
check("свой лог, без LOG_imapsync", "--nolog" in cmd, True)
check("автомаппинг папок", "--automap" in cmd, True)
check("явная пара папок", "Отправленные=Sent Items" in cmd, True)
check("исключения собраны в regex", "^(Спам|Корзина)$" in cmd, True)
check("лимит размера письма", str(35 * 1024 * 1024) in cmd, True)
check("SSL на обеих сторонах", "--ssl1" in cmd and "--ssl2" in cmd, True)
check("непроверяемый сертификат только у приёмника",
      "--sslargs2" in cmd and "--sslargs1" not in cmd, True)

# Проверка очистки переменной окружения CGI/Gunicorn для imapsync
import os
os.environ["SERVER_SOFTWARE"] = "gunicorn/23.0.0"
os.environ["HTTP_HOST"] = "127.0.0.1:8090"
clean_env = os.environ.copy()
for k in list(clean_env.keys()):
    if k.startswith("HTTP_") or k in ("SERVER_SOFTWARE", "GATEWAY_INTERFACE"):
        del clean_env[k]
check("SERVER_SOFTWARE отфильтрован", "SERVER_SOFTWARE" not in clean_env, True)
check("HTTP_HOST отфильтрован", "HTTP_HOST" not in clean_env, True)

print("\n--- разбор вывода ---")
check("папка", parse_line("Folder [Отправленные] -> [Sent Items]").kind, "folder")
check("имя папки", parse_line("Folder [Отправленные] -> [Sent Items]").folder, "Отправленные")
copied = parse_line("msg INBOX/42 {12345} copied to INBOX/17")
check("копирование", copied.kind, "copied")
check("размер письма", copied.size, 12345)
check("обычная строка", parse_line("Host1 is imap.example.com").kind, "plain")

print("\n--- скрабинг ---")
check("пароль вычищен", _scrub("password is ХАЛВА", ("ХАЛВА",)), "password is ***")
check("LOGIN замаскирован",
      _scrub("1 LOGIN user@dom s3cret", ()), "1 LOGIN user@dom ***")

print("\n--- боевой лог: прогон, который ничего не сделал ---")
# Настоящий лог с сервера 29.07.2026 (адреса обезличены). imapsync увидел в
# окружении переменные gunicorn, решил, что запущен как CGI-скрипт, напечатал
# HTTP-заголовки и вышел с кодом 0, не тронув ящик. Панель показала «перенесён».
from app.imapsync_runner import CGI_CONTEXT_PATTERN, RunResult  # noqa: E402

fixture = _Path(__file__).parent / "fixtures" / "cgi_noop_run.log"
lines = fixture.read_text(encoding="utf-8").splitlines()

replay = RunResult(exit_code=0)
for line in lines:
    if CGI_CONTEXT_PATTERN.search(line):
        replay.cgi_context = True
    event = parse_line(line)
    if event.kind == "copied":
        replay.copied_messages += 1
        replay.recognised_lines += 1
    elif event.kind == "folder":
        replay.recognised_lines += 1

check("режим CGI распознан", replay.cgi_context, True)
check("писем не перенесено", replay.copied_messages, 0)
check("строк о работе в выводе нет", replay.recognised_lines, 0)
check("прогон помечен как «ничего не сделал»", replay.did_nothing, True)

# Обратная сторона: нормальный прогон не должен попадать под это правило.
normal = RunResult(exit_code=0, copied_messages=5, recognised_lines=12)
check("обычный прогон не считается пустым", normal.did_nothing, False)
# Дельта-прогон: копировать нечего, но папки обработаны — это успех.
delta = RunResult(exit_code=0, copied_messages=0, recognised_lines=7)
check("дельта без новых писем не считается пустой", delta.did_nothing, False)

print("\n--- боевой лог: дельта-прогон, копировать нечего ---")
# Настоящий лог с сервера 30.07.2026: ящик уже перенесён, на приёмнике те же
# письма. imapsync честно отработал и напечатал сводку. Это НЕ поломка, и
# панель не должна показывать ошибку.
delta_run = ImapsyncRun(spec)
for line in (_Path(__file__).parent / "fixtures" / "delta_run.log").read_text(
    encoding="utf-8"
).splitlines():
    delta_run._consume(line)
delta_run.result.exit_code = 0
res = delta_run.result

check("сводка найдена", res.summary_seen, True)
check("перенесено писем", res.copied_messages, 0)
check("уже было на приёмнике", res.skipped_messages, 12)
check("папок обработано", (res.folders_synced, res.folders_total), (5, 5))
check("ошибок нет", res.reported_errors, 0)
check("строки о папках распознаны", res.recognised_lines > 0, True)
check("ложной тревоги нет", res.did_nothing, False)
check("режим CGI не включался", res.cgi_context, False)

# Прогресс ящика — это «сколько писем доехало», а не «сколько уехало за этот
# запуск». Иначе у полностью перенесённого ящика полоса показывала бы проценты
# последнего прогона рядом со статусом «перенесён».
check("на приёмнике писем", res.on_destination_messages, 12)
check("на приёмнике байт", res.on_destination_bytes, 341977)
print(f"       сводка для человека: {res.summary}")

print("\n--- имена папок для командной строки ---")
from app.imap_probe import encode_folder_name  # noqa: E402

# imapsync в своём выводе пишет: «X is the imap foldername you have to use in
# command line options». Читаемое имя доезжает искажённым.
check("Спам", encode_folder_name("Спам"), "&BCEEPwQwBDw-")
check("Отправленные", encode_folder_name("Отправленные"),
      "&BB4EQgQ,BEAEMAQyBDsENQQ9BD0ESwQ1-")
check("Черновики", encode_folder_name("Черновики"), "&BCcENQRABD0EPgQyBDgEOgQ4-")
check("ASCII не трогаем", encode_folder_name("INBOX"), "INBOX")
check("ASCII с пробелом не трогаем", encode_folder_name("Sent Items"), "Sent Items")

print("\n--- сбои окружения ---")
# Боевой случай 10.08.2026: кэш imapsync занял миллион файлов и исчерпал
# inode-ы. Место на диске было (8,7 ГБ), а создать файл не удавалось.
from app.migrator import _describe_environment_error, storage_headroom  # noqa: E402

no_space = OSError(28, "No space left on device")
no_space.errno = 28
text = _describe_environment_error(no_space)
check("кончилось место распознано", "диске сервера" in text, True)
print(f"       текст для человека: {text}")

denied = OSError(13, "Permission denied")
denied.errno = 13
check("нет прав распознано", "прав на запись" in _describe_environment_error(denied), True)

check("любая ошибка получает текст",
      bool(_describe_environment_error(ValueError("бум"))), True)

free_bytes, free_inodes = storage_headroom()
check("запас места измеряется без падения",
      free_bytes is None or free_bytes >= 0, True)

print("\n--- коды возврата ---")
from app.imapsync_runner import FATAL_EXITS, RETRIABLE_EXITS  # noqa: E402

check("ошибка пароля неповторяема", EXIT_AUTHENTICATION_FAILURE in FATAL_EXITS, True)
check("ошибка пароля не в повторяемых",
      EXIT_AUTHENTICATION_FAILURE in RETRIABLE_EXITS, False)
check("обрыв связи повторяем", EXIT_CONNECTION_FAILURE_HOST1 in RETRIABLE_EXITS, True)
check("нехватка квоты не повторяется", EXIT_OVERQUOTA in RETRIABLE_EXITS, False)

print()
if fails:
    print("ПРОВАЛЕНО:")
    for f in fails:
        print(" -", f)
    raise SystemExit(1)
print("Все проверки пройдены.")
