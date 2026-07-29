"""Отправка уведомлений в Telegram / Webhook при завершении проверок и миграций."""

from __future__ import annotations

import json
import logging
import os
import threading
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)


def notify(message: str) -> None:
    """Отправка уведомления в фоновом демоническом потоке."""
    thread = threading.Thread(target=_send, args=(message,), daemon=True)
    thread.start()


def _send(message: str) -> None:
    # Telegram Bot Notification
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if bot_token and chat_id:
        try:
            url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            data = urllib.parse.urlencode({
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "HTML",
            }).encode("utf-8")
            req = urllib.request.Request(
                url, data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"}
            )
            with urllib.request.urlopen(req, timeout=10):
                pass
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось отправить уведомление в Telegram: %s", exc)

    # Webhook Notification
    webhook_url = os.environ.get("WEBHOOK_URL")
    if webhook_url:
        try:
            payload = json.dumps({
                "text": message,
                "event": "imapsync_notification",
            }).encode("utf-8")
            req = urllib.request.Request(
                webhook_url, data=payload,
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=10):
                pass
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось отправить Webhook: %s", exc)
