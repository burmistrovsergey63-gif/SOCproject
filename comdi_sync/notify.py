"""Уведомления в Telegram о сбоях синхронизации.

Работает через обычный Bot API (api.telegram.org), без сторонних библиотек.
Чтобы включить: заведите бота у @BotFather, получите chat_id (например, через
@userinfobot) и задайте TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID в окружении.
Если токен или chat_id не заданы — уведомления молча отключены.
"""
from __future__ import annotations

import logging

import requests

log = logging.getLogger("comdi_sync.notify")

API_BASE = "https://api.telegram.org"


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str, timeout: int = 10) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.timeout = timeout

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    def send(self, text: str) -> bool:
        """Отправить сообщение. False — если уведомления выключены или отправка упала.

        Ошибка отправки не должна ломать саму синхронизацию, поэтому
        любые исключения здесь гасятся (с логом).
        """
        if not self.enabled:
            return False
        try:
            resp = requests.post(
                f"{API_BASE}/bot{self.bot_token}/sendMessage",
                json={"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"},
                timeout=self.timeout,
            )
            if resp.status_code == 200:
                return True
            log.error("Telegram: HTTP %s %s", resp.status_code, resp.text[:300])
        except Exception:  # noqa: BLE001 — уведомление не должно ронять процесс
            log.exception("Telegram: не удалось отправить уведомление")
        return False
