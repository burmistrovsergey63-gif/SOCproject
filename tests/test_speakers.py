"""Спикеры: создание, обновление, блокировка, реактивация (ТЗ 8, 9, 11)."""
from __future__ import annotations

from tests.conftest import (
    make_episode,
    make_hall,
    make_schedule,
    make_speaker,
    ts,
)


def schedule_with(*speaker_ids: int, episode_speakers: tuple[int, ...] | None = None):
    eps = ()
    if episode_speakers is not None:
        eps = (make_episode(10, 1, ts(10), ts(11), speakers=episode_speakers),)
    return make_schedule(
        halls=(make_hall(1, "Зал", eps),),
        speakers=tuple(make_speaker(sid) for sid in speaker_ids),
    )


class TestSpeakerCreation:
    def test_new_speaker_created_before_session_link(self, make_syncer, api):
        """Новый спикер создаётся до привязки к сессии (ТЗ 11)."""
        sched = schedule_with(100, episode_speakers=(100,))
        syncer = make_syncer(sched)
        syncer.run_once()

        methods = [op for op, _ in api.ops]
        assert methods.index("create_user") < methods.index(
            "create_session"
        ), "спикер должен быть создан раньше сессии"

    def test_profile_change_updates_same_target(self, make_syncer, api, state):
        """Изменение профиля обновляет тот же target_id (ТЗ 11)."""
        syncer = make_syncer(schedule_with(100))
        syncer.run_once()
        target_id = state.target_id("user", 100)

        api.clear_ops()
        changed = make_schedule(
            halls=(make_hall(1, "Зал"),),
            speakers=(make_speaker(100, name="Пётр"),),
        )
        syncer.comdi.schedule = changed
        syncer.run_once()

        assert state.target_id("user", 100) == target_id, "target_id не должен меняться"
        creates = [op for op, _ in api.ops if op == "create_user"]
        assert creates == [], "обновление не создаёт дубль"
        assert ("update_user", {"id": target_id}) in api.ops or any(
            op == "update_user" and body["id"] == target_id for op, body in api.ops
        )

    def test_unchanged_profile_not_sent(self, make_syncer, api):
        """Неизменный профиль не отправляется (ТЗ 11)."""
        syncer = make_syncer(schedule_with(100, 200))
        syncer.run_once()
        api.clear_ops()
        syncer.run_once()
        assert [op for op, _ in api.ops if "user" in op] == []


class TestSpeakerBlock:
    def test_block_not_delete(self, make_syncer, api, state):
        """Исчезновение спикера блокирует профиль, а не удаляет (ТЗ 9)."""
        syncer = make_syncer(schedule_with(100, 200))
        syncer.run_once()
        target_id = state.target_id("user", 200)

        api.clear_ops()
        syncer.comdi.schedule = schedule_with(100)
        syncer.run_once()

        assert state.is_blocked("user", 200)
        assert target_id in api.users, "профиль остаётся, не удаляется"
        assert api.users[target_id].get("withdrawn") is True
        assert [op for op, _ in api.ops if op == "delete_user"] == []

    def test_block_error_does_not_save_blocked(self, make_syncer, api, state):
        """Ошибка block не сохраняет blocked (ТЗ 11)."""
        syncer = make_syncer(schedule_with(100, 200))
        syncer.run_once()

        api.fail_next("update_users")
        syncer.comdi.schedule = schedule_with(100)
        stats = syncer.run_once()

        assert stats["failed"] > 0, "ошибка block обязана попасть в статус"
        assert not state.is_blocked("user", 200), "неподтверждённая блокировка не сохраняется"

        # повтор прогона без сбоя доводит блокировку
        stats = syncer.run_once()
        assert stats["failed"] == 0
        assert state.is_blocked("user", 200)

    def test_returning_speaker_reactivates_same_id(self, make_syncer, api, state):
        """Возвращение спикера реактивирует тот же профиль (ТЗ 9/11)."""
        syncer = make_syncer(schedule_with(100, 200))
        syncer.run_once()
        target_id = state.target_id("user", 200)

        syncer.comdi.schedule = schedule_with(100)
        syncer.run_once()
        assert state.is_blocked("user", 200)

        api.clear_ops()
        syncer.comdi.schedule = schedule_with(100, 200)
        stats = syncer.run_once()

        assert stats["failed"] == 0
        assert not state.is_blocked("user", 200), "спикер снова активен"
        assert state.target_id("user", 200) == target_id
        assert [op for op, _ in api.ops if op == "create_user"] == [], "без дублей"
        assert any(
            op == "update_user" and body["id"] == target_id
            for op, body in api.ops
        ), "reactivate через update того же профиля"


class TestSpeakerLinks:
    def test_last_speaker_removal_clears_link(self, make_syncer, api, state):
        """Исчезновение последнего спикера сессии явно очищает связь (ТЗ 9/11)."""
        sched = schedule_with(100, episode_speakers=(100,))
        syncer = make_syncer(sched)
        syncer.run_once()
        session_id = state.target_id("session", 10)

        # спикер остался в Comdi, но ушёл из сессии
        api.clear_ops()
        syncer.comdi.schedule = schedule_with(100, episode_speakers=())
        stats = syncer.run_once()

        assert stats["failed"] == 0
        assert api.sessions[session_id].get("speakersIds") == [], (
            "связь должна быть очищена явно пустым массивом"
        )
        # спикер остаётся активным: он всё ещё в полном списке Comdi
        assert not state.is_blocked("user", 100)
