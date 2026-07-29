# Инструкции и сведения о правках UI для Claude Code

В данном файле зафиксированы сведения о проведенной модернизации веб-интерфейса **imapsync-web** (29.07.2026), принятых паттернах дизайна и правилах развития интерфейса.

---

## 🎨 Архитектурные правила и принципы UI

1. **Строгое правило Zero-CDN (Автономность)**:
   - Приложение предназначено для работы в том числе в **закрытых корпоративных контурах (air-gapped)** без доступа к интернету.
   - **ЗАПРЕЩЕНО** подключать внешние шрифты, иконки, CSS или JS-библиотеки через CDN (`Google Fonts`, `FontAwesome CDN`, `Tailwind CDN` и т.д.).
   - Все иконки встроены в виде векторных SVG (`inline SVG`), шрифты используют системный стек (-apple-system, BlinkMacSystemFont, Segoe UI, Roboto), стили и скрипты локальны (`app/static/css/style.css`, `app/static/vendor/htmx.min.js`).

2. **Дизайн-система и темы (Dark & Light Mode)**:
   - Дизайн построена на CSS Custom Properties в `app/static/css/style.css`.
   - Автоматическая поддержка тёмной и светлой тем через `@media (prefers-color-scheme: dark)`.
   - **Акцентный цвет**: Electric Blue (`--accent: #2563eb` в светлой / `#3b82f6` в тёмной).
   - **Статусы**: Emerald (`--ok`), Amber (`--warn`), Crimson (`--err`) со светящимися точками-индикаторами (`.badge-dot`).
   - **Стек шрифтов моноширинного вывода**: `ui-monospace, SFMono-Regular, Consolas, monospace`.

3. **Совместимость с HTMX**:
   - Живое обновление прогресса, логов и результатов проверок работает через HTMX (опрос каждые 2–5 сек).
   - **Критичные HTML ID элементов**: `#mailboxes`, `#migrate-progress`, `#check-progress`, `#reconcile-progress`, `#preview`, `#test-{endpoint_id}`, `#log-view`, `#log-mailbox`, `#log-download`. При модификации шаблонов эти ID **должны сохраняться**.

4. **Три правила, которые легко потерять при рефакторинге CSS**:
   - `.actions > form { display: contents; }` — каждая кнопка обёрнута в `<form>` ради CSRF.
     Без этого правила обёртка становится флекс-элементом, растягивается по высоте строки,
     и кнопки в одном ряду получаются разной высоты (было 60 px рядом с 39 px).
   - `[hidden] { display: none !important; }` — авторский `display:block` на `label`
     сильнее браузерного правила для атрибута `hidden`, поэтому без явного `!important`
     скрытые через JS поля продолжают показываться.
   - `?v={{ version }}` у `style.css` и `htmx.min.js` в `base.html` — иначе после
     `docker compose pull` браузер отдаёт пользователю старые стили из кэша.

5. **Поля, зависящие от выбранного режима** (скрываются через атрибут `hidden`):
   - `#auth-extra` в `endpoint_form.html` — блок мастер-доступа. Виден только при
     режимах `master` и `xoauth2`; у `xoauth2` внутри скрываются поля с `data-only="master"`,
     потому что администратора и разделителя в этом режиме не существует.
   - `#unknown-folder-container-field` в `project.html` — только при политике `container`.

6. **Очистка CGI-переменных при вызове imapsync**:
   - `imapsync_runner.py` при запуске subprocess вычищает из `os.environ` переменные веб-сервера Gunicorn/CGI (`SERVER_SOFTWARE`, `GATEWAY_INTERFACE`, `HTTP_*`). Без этого `imapsync` считает, что запущен как CGI-скрипт веб-сервера, отдает заголовки HTTP 200 OK и мгновенно завершается с кодом 0 (`EX_OK`), не выполняя физический перенос писем.


---

## 📁 Структура обновившихся файлов

| Файл | Описание изменений |
|---|---|
| `app/notifier.py` | Модуль фоновой отправки уведомлений в Telegram (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`) и Webhook (`WEBHOOK_URL`). |
| `app/routes/projects.py` | Поиск по e-mail/комментариям, пагинация таблицы ящиков по 100 штук, выгрузка всех логов проекта в ZIP архиве (`/projects/<id>/logs/zip`). |
| `app/templates/project.html` | Добавлена кнопка выгрузки архива всех логов проекта. |
| `app/templates/partials/mailbox_table.html` | Интегрирована поисковая строка HTMX (`hx-trigger="keyup changed delay:300ms"`) и пагинатор страниц. |
| `app/templates/partials/migrate_progress.html` | Добавлен блок отображения параллельно работающих потоков ящиков с их текущей скоростью (MB/s). |

---

## 🧪 Проверка и запуск

- **Запуск unit-тестов**:
  ```bash
  python tests/test_units.py
  # Или в venv:
  .\.venv\Scripts\python.exe tests/test_units.py
  ```
- **Запуск сервера разработки**:
  ```bash
  flask --app app.wsgi run --debug
  ```
