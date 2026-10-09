"""Конфигурация синхронизации Comdi -> Eventicious.

Все настройки читаются из переменных окружения (можно загрузить .env).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv()
except ImportError:  # python-dotenv опционален
    pass


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw else default


@dataclass(frozen=True)
class Config:
    # источник
    comdi_event_url: str = os.getenv(
        "COMDI_EVENT_URL", "https://broadcast.comdi.com/api/player/event/erzo3310"
    )

    # приёмник
    eventicious_base_url: str = os.getenv(
        "EVENTICIOUS_BASE_URL", "https://api-integration.eventicious.ru"
    ).rstrip("/")
    client_id: str = os.getenv("EVENTICIOUS_CLIENT_ID", "")
    client_secret: str = os.getenv("EVENTICIOUS_CLIENT_SECRET", "")

    event_timezone: str = os.getenv("EVENT_TIMEZONE", "Europe/Moscow")

    # поведение
    sync_interval_seconds: int = _env_int("SYNC_INTERVAL_SECONDS", 300)
    delete_removed: bool = _env_bool("DELETE_REMOVED", True)
    sync_en: bool = _env_bool("SYNC_EN", True)

    # смещения ID, чтобы не пересечься с уже существующими объектами в Eventicious
    offset_location: int = _env_int("ID_OFFSET_LOCATION", 100_000)
    offset_tag: int = _env_int("ID_OFFSET_TAG", 200_000)
    offset_session: int = _env_int("ID_OFFSET_SESSION", 300_000)
    offset_user: int = _env_int("ID_OFFSET_USER", 400_000)

    # Telegram-уведомления о сбоях (пустые строки = выключены)
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")

    state_db: str = os.getenv("STATE_DB", "sync_state.sqlite3")

    http_timeout: int = _env_int("HTTP_TIMEOUT", 30)
    max_retries: int = _env_int("MAX_RETRIES", 4)

    log_level: str = os.getenv("LOG_LEVEL", "INFO")

    # не чаще, чем раз в N часов слать одно и то же Telegram-уведомление о сбое
    alert_cooldown_hours: int = _env_int("ALERT_COOLDOWN_HOURS", 6)

    def __post_init__(self) -> None:
        pass  # проверка кредов — в main(), чтобы dry-run работал без них

    def require_credentials(self) -> None:
        if not self.client_id or not self.client_secret:
            raise ValueError(
                "Не заданы EVENTICIOUS_CLIENT_ID / EVENTICIOUS_CLIENT_SECRET. "
                "Скопируйте .env.example в .env и заполните."
            )

    @property
    def state_db_path(self) -> Path:
        return Path(self.state_db).expanduser()


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


def load_config() -> Config:
    return Config()
