"""Сбои на каждом шаге переноса и восстановление (ТЗ 7.3, ТЗ 11).

Инъекция сбоев в подставной API (в т.ч. «сервер применил, ответ не дошёл»).
После каждого сбоя прогон обязан честно сообщать об ошибке, не портить
applied state и на следующем прогоне доводить перенос до конца без потери
целостности (без занятых временных залов, без 409).
"""
from __future__ import annotations

import pytest

from comdi_sync.eventicious import EventiciousError

from tests.conftest import make_episode, make_hall, make_schedule, ts


def swap_schedules():
    first = make_schedule(
        halls=(
            make_hall(1, "Зал 1", (make_episode(10, 1, ts(10), ts(11), title="A"),)),
            make_hall(2, "Зал 2", (make_episode(11, 2, ts(10), ts(11), title="B"),)),
        ),
        speakers=(),
    )
    second = make_schedule(
        halls=(
            make_hall(1, "Зал 1", (make_episode(11, 1, ts(10), ts(11), title="B"),)),
            make_hall(2, "Зал 2", (make_episode(10, 2, ts(10), ts(11), title="A"),)),
        ),
        speakers=(),
    )
    return first, second


def run_swap_with_fault(make_syncer, api, state, method: str, apply_first: bool):
    """Базовый прогон + обмен с инъекцией сбоя. Возвращает (stats, state_after)."""
    first, second = swap_schedules()
    syncer = make_syncer(first)
    assert syncer.run_once()["failed"] == 0

    api.clear_ops()
    api.fail_next(method, apply_first=apply_first)
    syncer.comdi.schedule = second
    failed_stats = syncer.run_once()

    # честный статус: ошибка обязательно отражена
    assert failed_stats["failed"] > 0, f"сбой {method} обязан попасть в статус"

    # следующий прогон без сбоев доводит до конца
    api.clear_ops()
    final_stats = syncer.run_once()
    return failed_stats, final_stats


class TestSwapCrashRecovery:
    @pytest.mark.parametrize("step", ["create_location", "update_session", "delete_location"])
    @pytest.mark.parametrize("apply_first", [False, True])
    def test_crash_at_each_step_then_recover(self, make_syncer, api, state, step, apply_first):
        first, _ = swap_schedules()
        syncer = make_syncer(first)
        assert syncer.run_once()["failed"] == 0
        id_a = state.target_id("session", 10)
        id_b = state.target_id("session", 11)
        hall1 = state.target_id("location", 1)
        hall2 = state.target_id("location", 2)

        failed_stats, final_stats = run_swap_with_fault(
            make_syncer, api, state, step, apply_first
        )

        # восстановление: следующий прогон завершает обмен без ошибок
        assert final_stats["failed"] == 0, f"восстановление не удалось: {final_stats}"
        assert api.sessions[id_a]["locationsIds"] == [hall2]
        assert api.sessions[id_b]["locationsIds"] == [hall1]

        # временные залы удалены — занятого temp не осталось
        leftover_temp = [lid for lid in api.locations if lid >= 9_000_000]
        assert leftover_temp == [], f"не удалён временный зал: {leftover_temp}"

        # applied state согласован с сервером
        from comdi_sync.transfer import Placement

        payload_a = state.get_payload("session", 10)
        assert Placement.from_payload(payload_a).location_id == hall2

    def test_409_means_model_divergence(self, make_syncer, api, state):
        """409 вопреки модели — ошибка, а не «загрузка без зала» (ТЗ 7.3)."""
        first, second = swap_schedules()
        syncer = make_syncer(first)
        assert syncer.run_once()["failed"] == 0
        hall1 = state.target_id("location", 1)

        # на сервере появился слот, неизвестный модели: чужая сессия заняла
        # зал 1 в нужный интервал. Модель считает слот свободным.
        api.sessions[999_999] = {
            "id": 999_999,
            "title": "Чужая",
            "locationsIds": [hall1],
            "startTime": "2026-10-10T13:00",
            "endTime": "2026-10-10T14:00",
        }

        syncer.comdi.schedule = second
        stats = syncer.run_once()
        assert stats["failed"] > 0, "409 вопреки модели — ошибка в статусе"
        assert any("409" in str(e) for e in stats.get("errors", [])), (
            "расхождение модели с сервером должно быть явно в статусе"
        )


class TestSourceCrash:
    def test_comdi_unavailable_no_writes(self, make_syncer, api, state):
        """Сбой Comdi: прогон падает до операций, applied state цел (ТЗ 11)."""
        from tests.conftest import FakeComdi

        first, _ = swap_schedules()
        syncer = make_syncer(first)
        assert syncer.run_once()["failed"] == 0
        before_sessions = dict(state.list_ids("session"))

        api.clear_ops()
        syncer.comdi = FakeComdi(error=RuntimeError("Comdi недоступен"))
        with pytest.raises(RuntimeError):
            syncer.run_once()
        assert api.ops == [], "сбой источника не должен слать записи"
        assert dict(state.list_ids("session")) == before_sessions


class TestTokenFailure:
    def test_token_503_limited_retry_no_state_change(self, cfg, monkeypatch):
        """503 при выдаче токена: ограниченный retry, applied state не меняется (ТЗ 11)."""
        import comdi_sync.eventicious as ev
        from comdi_sync.state import State
        from comdi_sync.sync import Syncer

        monkeypatch.setattr(ev.time, "sleep", lambda *_: None)  # не ждём в тестах

        class Resp:
            status_code = 503
            text = "service unavailable"

            def json(self):
                return {}

        calls = {"n": 0}

        class Session:
            def post(self, *a, **k):
                calls["n"] += 1
                return Resp()

            def request(self, *a, **k):
                calls["n"] += 1
                return Resp()

        client = ev.EventiciousClient("https://example.test", "id", "secret")
        client._session = Session()

        st = State(cfg.state_db_path)
        try:
            syncer = Syncer(cfg, None, client, st)
            api_before = dict(st.list_ids("session"))
            with pytest.raises(EventiciousError):
                syncer.api.create_location({"id": 1, "name": "x"})
            assert calls["n"] <= cfg.max_retries, "retry ограничен"
            assert dict(st.list_ids("session")) == api_before, "applied state не изменился"
        finally:
            st.close()
