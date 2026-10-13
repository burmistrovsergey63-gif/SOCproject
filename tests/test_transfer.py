"""Безопасные переносы сессий между залами (ТЗ 7, ТЗ 11).

Прямой перенос, обмен двух занятых залов, цикл из трёх, изменение времени
в том же зале, очистка технического зала.
"""
from __future__ import annotations

from tests.conftest import (
    make_episode,
    make_hall,
    make_schedule,
    make_speaker,
    ts,
)


def hall_schedule(ep_hall: int, ep_start=None, ep_end=None, ep_id: int = 10):
    """Расписание: эпизод ep_id в зале ep_hall + соседний эпизод всегда в зале 2."""
    start = ep_start or ts(10)
    end = ep_end or ts(11)
    ep1 = make_episode(ep_id, ep_hall, start, end, title="Первый")
    ep2 = make_episode(ep_id + 1, 2, ts(14), ts(15), title="Второй")
    halls = {1: [], 2: []}
    for ep in (ep1, ep2):
        halls[ep.stream_source_id].append(ep)
    return make_schedule(
        halls=tuple(make_hall(hid, f"Зал {hid}", tuple(eps)) for hid, eps in halls.items()),
        speakers=(),
    )


class TestDirectMove:
    def test_move_to_free_hall_single_update(self, make_syncer, api, state):
        """Перенос в свободный зал — ровно один update, без тех. залов (ТЗ 11)."""
        syncer = make_syncer(hall_schedule(1))
        syncer.run_once()
        session_id = state.target_id("session", 10)

        api.clear_ops()
        syncer.comdi.schedule = hall_schedule(2)  # эпизод 10 переехал в зал 2
        stats = syncer.run_once()

        assert stats["failed"] == 0
        assert api.sessions[session_id]["locationsIds"] == [state.target_id("location", 2)]
        session_ops = [op for op, _ in api.ops if "session" in op]
        assert session_ops == ["update_session"], f"один прямой перенос, ушло: {api.ops}"
        temp_ids = [lid for lid in api.locations if lid >= 9_000_000]
        assert temp_ids == [], "технический зал не нужен при свободной цели"

    def test_rename_no_move(self, make_syncer, api, state):
        """Изменение названия — одна update, без temp (ТЗ 11)."""
        sched = hall_schedule(1)
        syncer = make_syncer(sched)
        syncer.run_once()
        session_id = state.target_id("session", 10)

        api.clear_ops()
        renamed = hall_schedule(1)
        eps = []
        for hall in renamed.halls:
            new_eps = tuple(
                ep if ep.source_id != 10 else type(ep)(**{**ep.__dict__, "name_ru": "Новое имя"})
                for ep in hall.episodes
            )
            eps.append(type(hall)(**{**hall.__dict__, "episodes": new_eps}))
        syncer.comdi.schedule = type(renamed)(**{**renamed.__dict__, "halls": tuple(eps)})
        stats = syncer.run_once()

        assert stats["failed"] == 0
        assert api.sessions[session_id]["title"] == "Новое имя"
        assert [op for op, _ in api.ops if "session" in op] == ["update_session"]
        assert all(lid < 9_000_000 for lid in api.locations), "temp не создавался"


class TestSwap:
    def _swap_schedules(self):
        """Два эпизода меняются залами: A(1->2), B(2->1), одно время."""
        ep_a1 = make_episode(10, 1, ts(10), ts(11), title="A")
        ep_b1 = make_episode(11, 2, ts(10), ts(11), title="B")
        first = make_schedule(
            halls=(make_hall(1, "Зал 1", (ep_a1,)), make_hall(2, "Зал 2", (ep_b1,))),
            speakers=(),
        )
        ep_a2 = make_episode(10, 2, ts(10), ts(11), title="A")
        ep_b2 = make_episode(11, 1, ts(10), ts(11), title="B")
        second = make_schedule(
            halls=(make_hall(1, "Зал 1", (ep_b2,)), make_hall(2, "Зал 2", (ep_a2,))),
            speakers=(),
        )
        return first, second

    def test_swap_via_temp_hall(self, make_syncer, api, state):
        """Обмен двух занятых залов: временный зал, без ошибок (ТЗ 7.2/11)."""
        first, second = self._swap_schedules()
        syncer = make_syncer(first)
        syncer.run_once()
        id_a = state.target_id("session", 10)
        id_b = state.target_id("session", 11)
        hall1 = state.target_id("location", 1)
        hall2 = state.target_id("location", 2)

        api.clear_ops()
        syncer.comdi.schedule = second
        stats = syncer.run_once()

        assert stats["failed"] == 0, f"обмен должен пройти без ошибок: {stats}"
        assert api.sessions[id_a]["locationsIds"] == [hall2]
        assert api.sessions[id_b]["locationsIds"] == [hall1]

        # технический зал использован и удалён
        temp_created = [body["id"] for op, body in api.ops if op == "create_location" and body["id"] >= 9_000_000]
        assert temp_created, "для разрыва цикла нужен временный зал"
        temp_deleted = [
            body["id"] for op, body in api.ops if op == "delete_location" and body["id"] >= 9_000_000
        ]
        assert set(temp_deleted) == set(temp_created), "после операций временный зал удалён"
        assert all(lid < 9_000_000 for lid in api.locations), "неиспользуемых temp не осталось"

    def test_cycle_of_three(self, make_syncer, api, state):
        """Цикл из трёх сессий разрывается временным залом (ТЗ 7.3/11)."""
        first = make_schedule(
            halls=(
                make_hall(1, "З1", (make_episode(10, 1, ts(10), ts(11), title="A"),)),
                make_hall(2, "З2", (make_episode(11, 2, ts(10), ts(11), title="B"),)),
                make_hall(3, "З3", (make_episode(12, 3, ts(10), ts(11), title="C"),)),
            ),
            speakers=(),
        )
        # цикл: A->2, B->3, C->1
        second = make_schedule(
            halls=(
                make_hall(1, "З1", (make_episode(12, 1, ts(10), ts(11), title="C"),)),
                make_hall(2, "З2", (make_episode(10, 2, ts(10), ts(11), title="A"),)),
                make_hall(3, "З3", (make_episode(11, 3, ts(10), ts(11), title="B"),)),
            ),
            speakers=(),
        )
        syncer = make_syncer(first)
        syncer.run_once()

        api.clear_ops()
        syncer.comdi.schedule = second
        stats = syncer.run_once()

        assert stats["failed"] == 0, f"цикл должен разорваться: {stats}"
        assert api.sessions[state.target_id("session", 10)]["locationsIds"] == [
            state.target_id("location", 2)
        ]
        assert api.sessions[state.target_id("session", 11)]["locationsIds"] == [
            state.target_id("location", 3)
        ]
        assert api.sessions[state.target_id("session", 12)]["locationsIds"] == [
            state.target_id("location", 1)
        ]
        assert all(lid < 9_000_000 for lid in api.locations), "temp удалён"

    def test_time_change_same_hall(self, make_syncer, api, state):
        """Изменение времени в том же зале при конфликте — дубль зала (ТЗ 6/11)."""
        first = make_schedule(
            halls=(
                make_hall(
                    1,
                    "Зал",
                    (
                        make_episode(10, 1, ts(10), ts(11), title="Утро"),
                        make_episode(11, 1, ts(14), ts(15), title="Вечер"),
                    ),
                ),
            ),
            speakers=(),
        )
        # эпизод 11 переехал на слот, пересекающийся с эпизодом 10
        second = make_schedule(
            halls=(
                make_hall(
                    1,
                    "Зал",
                    (
                        make_episode(10, 1, ts(10), ts(12), title="Утро"),
                        make_episode(11, 1, ts(11), ts(13), title="Вечер"),
                    ),
                ),
            ),
            speakers=(),
        )
        syncer = make_syncer(first)
        syncer.run_once()

        api.clear_ops()
        syncer.comdi.schedule = second
        stats = syncer.run_once()

        assert stats["failed"] == 0, f"конфликт решается дублем зала: {stats}"
        sid10 = state.target_id("session", 10)
        sid11 = state.target_id("session", 11)
        locs10 = set(api.sessions[sid10]["locationsIds"])
        locs11 = set(api.sessions[sid11]["locationsIds"])
        assert not (locs10 & locs11), "пересекающиеся сессии не могут делить зал"
