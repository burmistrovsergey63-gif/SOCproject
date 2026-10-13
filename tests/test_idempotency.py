"""Повтор одной и той же выгрузки не должен слать записи (ТЗ 5.8, ТЗ 11).

Проверяется полная идемпотентность: залы, темы, пользователи, сессии,
EN-версии, дубли залов, блокировки — повторная выгрузка без изменений даёт
ноль операций записи.
"""
from __future__ import annotations

from tests.conftest import (
    Schedule,
    make_episode,
    make_hall,
    make_schedule,
    make_speaker,
    ts,
)


def overlapping_schedule() -> Schedule:
    """Расписание с пересечением в одном зале -> создаётся дубль зала."""
    ep1 = make_episode(10, 1, ts(10), ts(12), title="Утро", speakers=(100,), title_en="Morning")
    ep2 = make_episode(11, 1, ts(11), ts(13), title="Полдень", title_en="Noon")
    return make_schedule(
        halls=(make_hall(1, "Большой", (ep1, ep2)),),
        speakers=(make_speaker(100),),
    )


class TestRepeatRun:
    def test_repeat_makes_no_writes(self, make_syncer, api, state):
        syncer = make_syncer(overlapping_schedule())
        first = syncer.run_once()
        assert first["failed"] == 0
        assert api.ops, "первый прогон должен создать объекты"

        api.clear_ops()
        second = syncer.run_once()
        assert second["failed"] == 0
        assert api.ops == [], f"повтор не должен слать записи, ушло: {api.ops}"
        assert second["changes"] == {}, "реальных изменений нет"

    def test_dup_hall_not_recreated(self, make_syncer, api):
        """ТЗ 6: неизменный дубль зала не переотправляется каждый прогон."""
        syncer = make_syncer(overlapping_schedule())
        syncer.run_once()
        creates = [op for op, _ in api.ops if op == "create_location"]
        n_creates = len(creates)
        api.clear_ops()

        syncer.run_once()
        assert len([op for op, _ in api.ops if op == "create_location"]) == 0
        assert len(
            [op for op, _ in api.ops if op == "update_location"]
        ) == 0, "дубль зала не должен обновляться при неизменных данных"
        assert n_creates >= 2  # основной зал + дубль созданы в первом прогоне

    def test_block_not_repeated(self, make_syncer, api, state):
        """ТЗ 9: повторная одинаковая выгрузка не повторяет блокировку."""
        full = make_schedule(
            halls=(make_hall(1, "Зал", (make_episode(10, 1, ts(10), ts(11), speakers=(100,)),)),),
            speakers=(make_speaker(100),),
        )
        syncer = make_syncer(full)
        syncer.run_once()

        # спикер исчез из полного списка -> блокировка
        reduced = make_schedule(
            halls=(make_hall(1, "Зал", (make_episode(10, 1, ts(10), ts(11)),)),),
            speakers=(),
        )
        syncer.comdi.schedule = reduced
        syncer.run_once()
        assert state.is_blocked("user", 100)
        assert len([op for op, _ in api.ops if op == "update_user"]) == 1

        api.clear_ops()
        stats = syncer.run_once()  # та же выгрузка повторно
        assert stats["failed"] == 0
        assert api.ops == [], "повторное отсутствие не должно повторять block"
