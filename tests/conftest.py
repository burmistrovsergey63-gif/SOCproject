"""Общие фикстуры: подставной Comdi, подставной Eventicious, SQLite во временном
каталоге. Никаких сетевых вызовов и production-данных (ТЗ 11, ТЗ 2)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from comdi_sync.comdi import Episode, Hall, Schedule, Speaker, Tag
from comdi_sync.config import Config
from comdi_sync.state import State
from comdi_sync.sync import Syncer

from tests.fake_eventicious import FakeEventicious


class FakeComdi:
    """Отдаёт заранее заданное расписание; может падать (сбой источника)."""

    def __init__(self, schedule: Schedule | None = None, error: Exception | None = None):
        self.schedule = schedule
        self.error = error
        self.fetch_count = 0

    def fetch_schedule(self) -> Schedule:
        self.fetch_count += 1
        if self.error is not None:
            raise self.error
        assert self.schedule is not None
        return self.schedule


def ts(hour: int, minute: int = 0, day: int = 10) -> datetime:
    """Московское время -> aware UTC datetime (для эпизодов)."""
    return datetime(2026, 10, day, hour, minute, tzinfo=timezone.utc)


def make_speaker(source_id: int, name: str = "Иван", lastname: str = "Иванов") -> Speaker:
    return Speaker(
        source_id=source_id,
        name_ru=name,
        name_en="",
        lastname_ru=lastname,
        lastname_en="",
        patronymic_ru="",
        patronymic_en="",
        company_ru="",
        company_en="",
        regalia_ru="",
        regalia_en="",
        description_ru="",
        description_en="",
        biography_ru="",
        biography_en="",
        link_telegram=None,
        link_vk=None,
        link_dzen=None,
        photo="",
        photo_orig="",
    )


def make_episode(
    source_id: int,
    hall_id: int,
    start: datetime,
    end: datetime,
    title: str = "Доклад",
    speakers: tuple[int, ...] = (),
    tags: tuple[int, ...] = (),
    title_en: str = "",
) -> Episode:
    return Episode(
        source_id=source_id,
        stream_source_id=hall_id,
        name_ru=title,
        name_en=title_en,
        info_ru="",
        info_en="",
        start_utc=start,
        stop_utc=end,
        tag_ids=tags,
        speaker_ids=speakers,
    )


def make_hall(source_id: int, name: str, episodes: tuple[Episode, ...] = ()) -> Hall:
    return Hall(
        source_id=source_id,
        description_ru=name,
        description_en="",
        order=source_id,
        episodes=episodes,
    )


def make_schedule(
    halls: tuple[Hall, ...] = (),
    tags: tuple[Tag, ...] = (),
    speakers: tuple[Speaker, ...] = (),
) -> Schedule:
    return Schedule(
        event_name_ru="Событие",
        event_name_en="Event",
        halls=halls,
        tags=tags,
        speakers=speakers,
    )


@pytest.fixture
def cfg(tmp_path) -> Config:
    return Config(
        state_db=str(tmp_path / "state.sqlite3"),
        delete_removed=True,
        sync_en=False,  # EN-переводы проверяются отдельными тестами
        telegram_bot_token="",
        telegram_chat_id="",
    )


@pytest.fixture
def state(cfg) -> State:
    st = State(cfg.state_db_path)
    yield st
    st.close()


@pytest.fixture
def api() -> FakeEventicious:
    return FakeEventicious()


@pytest.fixture
def make_syncer(cfg, state, api):
    """Фабрика синхронизаторов: make_syncer(schedule, error=None) -> (syncer, comdi)."""

    def _make(schedule: Schedule | None = None, error: Exception | None = None) -> Syncer:
        comdi = FakeComdi(schedule, error)
        return Syncer(cfg, comdi, api, state)

    return _make
