"""Валидация полноты ответа Comdi (ТЗ 5, ТЗ 11).

Полная пустая программа (пустые списки) валидна и разрешает удаления/блокировки.
Неполный ответ (нет ключа, неверный тип, дубли id) валится ДО операций и не
должен вызывать массовые delete/block.
"""
from __future__ import annotations

import pytest

from comdi_sync.comdi import SourceValidationError, normalize

from tests.conftest import (
    FakeComdi,
    make_episode,
    make_hall,
    make_schedule,
    make_speaker,
    ts,
)


VALID_EMPTY = {"nameRu": "Событие", "stream": [], "members": []}


class TestNormalizeValidation:
    def test_full_empty_schedule_is_valid(self):
        """Полная пустая программа — валидный ответ (разрешает удаления)."""
        sched = normalize(VALID_EMPTY)
        assert sched.halls == ()
        assert sched.speakers == ()
        assert sched.all_episodes == []

    def test_missing_stream_is_error(self):
        with pytest.raises(SourceValidationError):
            normalize({"nameRu": "x", "members": []})

    def test_missing_members_is_error(self):
        """Отсутствие списка спикеров не даёт права блокировать (ТЗ 9)."""
        with pytest.raises(SourceValidationError):
            normalize({"nameRu": "x", "stream": []})

    def test_wrong_type_is_error(self):
        with pytest.raises(SourceValidationError):
            normalize({"nameRu": "x", "stream": {}, "members": []})
        with pytest.raises(SourceValidationError):
            normalize({"nameRu": "x", "stream": [], "members": "nope"})

    def test_duplicate_hall_id_is_error(self):
        hall = {"id": 1, "descriptionRu": "Зал", "descriptionEn": "", "episode": []}
        with pytest.raises(SourceValidationError):
            normalize({"nameRu": "x", "stream": [hall, dict(hall)], "members": []})

    def test_duplicate_episode_id_is_error(self):
        ep = {"id": 5, "streamId": 1, "timeStart": 0, "timeEnd": 60}
        hall = {"id": 1, "descriptionRu": "З", "descriptionEn": "", "episode": [ep, dict(ep)]}
        with pytest.raises(SourceValidationError):
            normalize({"nameRu": "x", "stream": [hall], "members": []})

    def test_duplicate_member_id_is_error(self):
        member = {"id": 7, "nameRu": "И", "nameEn": ""}
        with pytest.raises(SourceValidationError):
            normalize({"nameRu": "x", "stream": [], "members": [member, dict(member)]})


class TestNoOperationsOnInvalidSource:
    """Неполная выгрузка не должна вызывать удаления/блокировки (ТЗ 11)."""

    def test_run_fails_before_any_write(self, make_syncer, api, state):
        # baseline: два спикера и сессия уже синхронизированы
        sched = make_schedule(
            halls=(make_hall(1, "Зал", (make_episode(10, 1, ts(10), ts(11), speakers=(100,)),)),),
            speakers=(make_speaker(100), make_speaker(200)),
        )
        syncer = make_syncer(sched)
        syncer.run_once()
        assert state.list_ids("session")
        assert state.list_ids("user")

        # неполный ответ источника -> SourceValidationError, операций нет
        syncer.comdi = FakeComdi(error=SourceValidationError("нет members"))
        api.clear_ops()
        with pytest.raises(SourceValidationError):
            syncer.run_once()
        assert api.ops == [], "неполная выгрузка не должна порождать операций"
        # applied state не тронут
        assert len(state.list_ids("user")) == 2
        assert not state.is_blocked("user", 200)

    def test_full_empty_runs_deletions(self, make_syncer, api, state):
        """Полная пустая программа — валидна: удаления и блокировки выполняются."""
        sched = make_schedule(
            halls=(make_hall(1, "Зал", (make_episode(10, 1, ts(10), ts(11), speakers=(100,)),)),),
            speakers=(make_speaker(100), make_speaker(200)),
        )
        syncer = make_syncer(sched)
        syncer.run_once()

        syncer.comdi = FakeComdi(schedule=make_schedule())  # пусто, но полный ответ
        stats = syncer.run_once()

        assert stats["failed"] == 0
        assert state.list_ids("session") == {}
        assert state.list_ids("location") == {}
        # оба спикера заблокированы (удалённых из полного списка)
        assert state.is_blocked("user", 100)
        assert state.is_blocked("user", 200)
