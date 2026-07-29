"""Сквозная проверка этапа 1 через HTTP: вход -> сервер -> проект -> импорт."""

import http.cookiejar
import os
import re
import sys
import urllib.parse
import urllib.request

BASE = os.environ.get("SMOKE_BASE", "http://127.0.0.1:5010")
ADMIN = os.environ.get("SMOKE_ADMIN", "admin")
# Пароля по умолчанию здесь нет намеренно: это публичный репозиторий, и
# захардкоженный пароль в нём рано или поздно окажется чьим-то настоящим.
PASSWORD = os.environ.get("SMOKE_PASSWORD", "")

jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


def get(path):
    with opener.open(BASE + path) as r:
        return r.status, r.read().decode("utf-8"), r.geturl()


def post(path, fields, files=None):
    if files:
        boundary = "----smoke"
        parts = []
        for k, v in fields.items():
            parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
        for k, (fname, data) in files.items():
            parts.append(
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; "
                f"filename=\"{fname}\"\r\nContent-Type: text/csv\r\n\r\n".encode() + data + b"\r\n"
            )
        parts.append(f"--{boundary}--\r\n".encode())
        body = b"".join(parts)
        req = urllib.request.Request(BASE + path, data=body)
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    else:
        req = urllib.request.Request(
            BASE + path, data=urllib.parse.urlencode(fields).encode("utf-8")
        )
    with opener.open(req) as r:
        return r.status, r.read().decode("utf-8"), r.geturl()


def get_raw(path):
    with opener.open(BASE + path) as r:
        return r.status, r.read()


def csrf(html):
    m = CSRF_RE.search(html)
    assert m, "csrf token not found"
    return m.group(1)


def check(label, condition, extra=""):
    mark = "OK  " if condition else "FAIL"
    print(f"[{mark}] {label} {extra}")
    if not condition:
        sys.exit(1)


if not PASSWORD:
    print(
        "Задай пароль администратора для проверки:\n"
        "    SMOKE_PASSWORD=... python tests/smoke_http.py\n"
        "Запускать только по тестовой базе — сценарий создаёт и меняет данные."
    )
    sys.exit(2)


# 1. защита: без входа редиректит на логин
status, html, url = get("/")
check("аноним редиректится на вход", "/login" in url)

# 2. неверный пароль
token = csrf(html)
try:
    post("/login", {"csrf_token": token, "username": "admin", "password": "wrong"})
    check("неверный пароль отвергнут", False)
except urllib.error.HTTPError as e:
    check("неверный пароль отвергнут", e.code == 401, f"(HTTP {e.code})")

# 3. CSRF: POST без токена
try:
    post("/login", {"username": ADMIN, "password": PASSWORD})
    check("POST без CSRF отклонён", False)
except urllib.error.HTTPError as e:
    check("POST без CSRF отклонён", e.code == 400, f"(HTTP {e.code})")

# 4. вход
status, html, url = get("/login")
status, html, url = post(
    "/login", {"csrf_token": csrf(html), "username": ADMIN, "password": PASSWORD}
)
check("вход выполнен", "Проекты" in html, )

# 5. создание сервера-источника из пресета mailru
status, html, _ = get("/endpoints/new?preset=mailru")
check("форма сервера открылась с пресетом", "imap.mail.ru" in html)
check("подсказки пресета показаны", "пароль приложения" in html)
status, html, _ = post("/endpoints/new", {
    "csrf_token": csrf(html), "preset": "mailru", "name": "Mail.ru (тест)",
    "host": "imap.mail.ru", "port": "993", "security": "ssl", "verify_cert": "on",
    "auth_mode": "password", "max_parallel": "3", "master_separator": "*",
})
check("сервер-источник создан", "Mail.ru (тест)" in html)

# 6. сервер-приёмник
status, html, _ = get("/endpoints/new?preset=exchange-onprem")
status, html, _ = post("/endpoints/new", {
    "csrf_token": csrf(html), "preset": "exchange-onprem", "name": "Exchange (тест)",
    "host": "mail.example.local", "port": "993", "security": "ssl", "verify_cert": "on",
    "auth_mode": "password", "max_parallel": "5", "master_separator": "*",
})
check("сервер-приёмник создан", "Exchange (тест)" in html)

ids = re.findall(r"/endpoints/(\d+)/edit", html)
check("id серверов найдены", len(ids) >= 2, f"({ids})")
src_id, dst_id = ids[0], ids[1]

# 7. проект
status, html, _ = get("/wizard/new")
status, html, url = post("/wizard/new", {"csrf_token": csrf(html), "name": "Смоук-проект"})
check("мастер дошёл до выбора источника", "Откуда переносим" in html)
project_id = re.search(r"/wizard/(\d+)/endpoint", url).group(1)

status, html, url = post(f"/wizard/{project_id}/endpoint/source",
                         {"csrf_token": csrf(html), "endpoint_id": src_id})
check("источник выбран", "Куда переносим" in html)
status, html, url = post(f"/wizard/{project_id}/endpoint/destination",
                         {"csrf_token": csrf(html), "endpoint_id": dst_id})
check("приёмник выбран, дошли до импорта", "Список ящиков" in html)

# 8. импорт «excel-подобного» CSV: cp1251, разделитель ;, хвостовой пробел, дубль
csv_text = (
    "Почта откуда;Пароль откуда;Почта куда;Пароль куда;ФИО\r\n"
    "ivanov@mail.ru;appPass1;ivanov@corp.local;Dst!1;Иванов Иван\r\n"
    "petrov@mail.ru;appPass2 ;petrov@corp.local;Dst!2;Петров Пётр\r\n"
    "ivanov@mail.ru;appPass3;dubl@corp.local;Dst!3;Дубль\r\n"
    "broken-address;appPass4;sidorov@corp.local;Dst!4;Кривой адрес\r\n"
)
status, html, url = post(
    f"/wizard/{project_id}/import",
    {"csrf_token": csrf(html), "mode": "file"},
    files={"file": ("spisok.csv", csv_text.encode("cp1251"))},
)
check("кодировка cp1251 определена", "cp1251" in html, )
# Проверяем сам факт, а не формулировку: тексты в шаблонах меняются.
check("разделитель ; определён", "«;»" in html)
check("колонки угаданы", "Почта откуда" in html)
check("дубль найден", "уже встречался" in html)
check("кривой адрес найден", "выглядит некорректно" in html)
check("пробел в пароле найден", "пробел в начале или в конце" in html)
check("пароли не показаны", "appPass1" not in html)

# 9. коммит с обрезкой пробелов
status, html, url = post(f"/wizard/{project_id}/commit", {
    "csrf_token": csrf(html),
    "col_src_email": "0", "col_src_password": "1",
    "col_dst_email": "2", "col_dst_password": "3", "col_note": "4",
    "trim_passwords": "1",
})
check("импортировано 2 из 4 строк (дубль и кривой адрес отброшены)",
      "Импортировано 2 ящиков" in html)
check("на карточке проекта видны ящики", "ivanov@mail.ru" in html)
check("плашка про календари на месте", "Календари, контакты" in html)

# 10. вкладки и партиалы
status, html, _ = get(f"/projects/{project_id}?tab=log")
check("вкладка лога открывается", 'id="log-view"' in html)
status, html, _ = get(f"/projects/{project_id}/events")
check("лента событий отдаётся", "Импортировано 2 ящиков" in html)
status, html, _ = get(f"/projects/{project_id}/mailboxes?failed=1")
check("фильтр «только упавшие» работает", "Упавших ящиков нет" in html)
status, html, _ = get(f"/projects/{project_id}/check/progress")
check("партиал прогресса отдаётся", status == 200)

# 11. перенос: предохранитель до проверки доступов
status, html, _ = get(f"/projects/{project_id}")
status, html, _ = post(f"/projects/{project_id}/migrate/start", {"csrf_token": csrf(html)})
check("перенос не стартует без подтверждённого доступа",
      "нет ящиков с подтверждённым доступом" in html)

status, html, _ = get(f"/projects/{project_id}")
status, html, _ = post(f"/projects/{project_id}/migrate/resync", {"csrf_token": csrf(html)})
check("досинхрон принят", status == 200)

status, html, _ = get(f"/projects/{project_id}/migrate/progress")
check("партиал прогресса переноса отдаётся", status == 200)

import json
import time as _time

_time.sleep(1.5)
status, html, _ = get(f"/projects/{project_id}/events")
check("отсутствие imapsync отражено в ленте", "imapsync не найден" in html)

mailbox_id = re.search(r"/mailboxes/(\d+)/unlock", html)
status, body, _ = get(f"/projects/{project_id}/mailboxes/1/log")
check("лог отдаёт корректный json", json.loads(body).get("missing") is True)

# 12. настройки переноса
status, html, _ = get(f"/projects/{project_id}?tab=settings")
status, html, _ = post(f"/projects/{project_id}/settings", {
    "csrf_token": csrf(html), "max_parallel": "4", "migrate_trash": "on",
    "unknown_folder_policy": "create", "max_message_size_mb": "35",
})
check("настройки переноса сохранены", "Настройки сохранены" in html)
check("лимит размера письма записан", 'value="35"' in html)

# 13. папки, сверка, отчёт
status, html, _ = get(f"/projects/{project_id}/folders")
check("экран папок открывается", "Соответствие папок" in html)
check("без инвентаризации честно сообщаем, что папок нет",
      "появятся здесь после проверки доступов" in html)

status, html, _ = get(f"/projects/{project_id}?tab=report")
check("вкладка отчёта открывается", "Сверка с приёмником" in html)
check("на вкладке есть запуск сверки и выгрузка",
      f"/projects/{project_id}/reconcile" in html and "report.xlsx" in html)

status, html, _ = post(f"/projects/{project_id}/reconcile", {"csrf_token": csrf(html)})
check("сверка запускается", "Сверка запущена" in html)

status, body = get_raw(f"/projects/{project_id}/report.xlsx")
check("отчёт xlsx отдаётся", body[:2] == b"PK", f"({len(body)} байт)")

# 14. пользователи и роли
status, html, _ = get("/settings/")
check("экран настроек открывается", "/settings/users" in html)
check("журнал уже что-то записал", "создан проект" in html)

check("над своей строкой действий нет", "is_me" in html or "badge ok" in html)

# Запрет на действия над собой держится на сервере, а не только в шаблоне:
# свой пароль меняется в отдельном блоке, с подтверждением текущего.
status, html, _ = post("/settings/users/1/reset", {"csrf_token": csrf(html)})
check("сброс своего пароля отклонён", "Свой пароль меняется" in html)
status, html, _ = post("/settings/users/1/toggle", {"csrf_token": csrf(html)})
check("отключение самого себя отклонено", "Нельзя отключить самого себя" in html)

status, html, _ = post("/settings/users", {
    "csrf_token": csrf(html), "username": "operator1", "role": "operator", "password": "",
})
check("оператор создан с временным паролем", "Временный пароль" in html)
operator_password = re.search(r"Временный пароль: (\S+)", html).group(1)

# 15. права оператора
status, html, _ = get("/settings/")
post("/logout", {"csrf_token": csrf(html)})

status, html, _ = get("/login")
status, html, _ = post("/login", {
    "csrf_token": csrf(html), "username": "operator1", "password": operator_password,
})
check("оператор вошёл", "Проекты" in html)

status, html, _ = get("/settings/")
check("оператору не видно управление пользователями", "/settings/users" not in html)
check("оператору доступна смена своего пароля", "pwd-modal" in html or "/settings/password" in html)

try:
    get("/endpoints/new")
    check("оператору закрыто создание серверов", False)
except urllib.error.HTTPError as e:
    check("оператору закрыто создание серверов", e.code == 403, f"(HTTP {e.code})")

status, html, _ = get(f"/projects/{project_id}")
check("чужой проект оператору виден", "Смоук-проект" in html)

# возвращаемся администратором
status, html, _ = get("/settings/")
post("/logout", {"csrf_token": csrf(html)})
status, html, _ = get("/login")
status, html, _ = post("/login", {
    "csrf_token": csrf(html), "username": ADMIN, "password": PASSWORD,
})
check("вернулись администратором", "Проекты" in html)

# 16. очистка кредов
status, html, _ = get(f"/projects/{project_id}?tab=settings")
status, html, _ = post(f"/projects/{project_id}/purge-credentials", {"csrf_token": csrf(html)})
check("пароли стёрты", "Пароли стёрты" in html)

print("\nВсе проверки пройдены.")
