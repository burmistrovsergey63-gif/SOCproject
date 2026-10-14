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


class GitHubWorkflowNotifier:
    """Dispatch a Telegram-only workflow; the synchronizer stays on Selectel."""

    def __init__(
        self,
        token: str,
        repository: str,
        workflow: str,
        ref: str = "main",
        timeout: int = 10,
    ) -> None:
        self.token = token
        self.repository = repository
        self.workflow = workflow
        self.ref = ref
        self.timeout = timeout

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.repository and self.workflow and self.ref)

    def send(self, text: str) -> bool:
        """Queue a GitHub Actions run that forwards this message to Telegram."""
        if not self.enabled:
            return False

        owner, separator, repo = self.repository.partition("/")
        if not separator or not owner or not repo or "/" in repo:
            log.error("GitHub Actions relay: invalid repository setting")
            return False

        from urllib.parse import quote

        workflow = quote(self.workflow, safe="")
        url = (
            f"https://api.github.com/repos/{owner}/{repo}"
            f"/actions/workflows/{workflow}/dispatches"
        )
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "socproject-selectel-notifier",
        }
        payload = {
            "ref": self.ref,
            "inputs": {"notification": text[:3500]},
        }
        try:
            response = requests.post(
                url, headers=headers, json=payload, timeout=self.timeout
            )
            if response.status_code in (200, 204):
                return True
            log.error(
                "GitHub Actions relay: HTTP %s %s",
                response.status_code,
                response.text[:300],
            )
        except Exception:  # noqa: BLE001 — уведомление не должно ронять синхронизацию
            log.exception("GitHub Actions relay: не удалось запустить workflow")
        return False
