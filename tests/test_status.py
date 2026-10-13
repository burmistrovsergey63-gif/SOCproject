"""Итоговый статус прогона и единственный исполнитель (ТЗ 5.1/5.9, ТЗ 11).

Ошибки любых шагов (залы, темы, профили, EN, delete, block, очистка временных
залов) обязаны попадать в итоговый статус. При сбое записи в БД после
успешного HTTP состояние расходится — следующий прогон восстанавливает
конвергенцию без небезопасных переносов. Два одновключённых запуска не
должны работать параллельно (concurrency в workflow).
"""
from __future__ import annotations

from pathlib import Path

from comdi_sync.comdi import Tag

from tests.conftest import make_episode, make_hall, make_schedule, make_speaker, ts


def simple_schedule(title: str = "Доклад", tag_color: str = "#90baff"):
    return make_schedule(
        halls=(make_hall(1, "Зал", (make_episode(10, 1, ts(10), ts(11), title=title),)),),
        tags=(Tag(50, "Тема", "", tag_color),),
        speakers=(make_speaker(100),),
    )


class TestErrorsInStatus:
    def test_hall_error_in_status(self, make_syncer, api):
        syncer = make_syncer(simple_schedule())
        api.fail_next("create_location")
        stats = syncer.run_once()
        assert stats["failed"] > 0
        assert any("location" in e for e in stats["errors"])

    def test_tag_error_in_status(self, make_syncer, api):
        syncer = make_syncer(simple_schedule())
        api.fail_next("create_tag")
        stats = syncer.run_once()
        assert stats["failed"] > 0
        assert any("tag" in e for e in stats["errors"])

    def test_user_error_in_status(self, make_syncer, api):
        syncer = make_syncer(simple_schedule())
        api.fail_next("create_users")
        stats = syncer.run_once()
        assert stats["failed"] > 0
        assert any("user" in e for e in stats["errors"])

    def test_delete_error_in_status(self, make_syncer, api, state):
        syncer = make_syncer(simple_schedule())
        assert syncer.run_once()["failed"] == 0

        api.fail_next("delete_session")
        syncer.comdi.schedule = make_schedule(
            halls=(make_hall(1, "Зал"),),
            tags=(Tag(50, "Тема", "", "#90baff"),),
            speakers=(make_speaker(100),),
        )
        stats = syncer.run_once()
        assert stats["failed"] > 0, "ошибка delete обязана попасть в статус"

    def test_block_error_in_status(self, make_syncer, api):
        syncer = make_syncer(simple_schedule())
        assert syncer.run_once()["failed"] == 0

        api.fail_next("update_users")
        syncer.comdi.schedule = make_schedule(
            halls=(make_hall(1, "Зал", (make_episode(10, 1, ts(10), ts(11)),)),),
            tags=(Tag(50, "Тема", "", "#90baff"),),
            speakers=(),  # спикер исчез -> block
        )
        stats = syncer.run_once()
        assert stats["failed"] > 0, "ошибка block обязана попасть в статус"


class TestEnInStatus:
    def test_en_error_reflected(self, make_syncer, api, state):
        """EN-ошибка попадает в итоговый статус (ТЗ 11)."""
        from dataclasses import replace

        # эпизод с EN-переводом, чтобы EN-версия реально уходила
        sched = make_schedule(
            halls=(
                make_hall(
                    1,
                    "Зал",
                    (make_episode(10, 1, ts(10), ts(11), title="Доклад", title_en="Talk"),),
                ),
            ),
            speakers=(),
        )
        syncer = make_syncer(sched)
        syncer.cfg = replace(syncer.cfg, sync_en=True)

        # EN-версия сессии уходит через update_session — роняем именно его
        api.fail_next("update_session")
        stats = syncer.run_once()
        assert stats["failed"] > 0, "EN-ошибка обязана попасть в статус"


class TestConcurrentRuns:
    def test_workflow_single_executor(self):
        """Два одновключённых запуска не должны работать параллельно (ТЗ 5.1).

        В GitHub Actions это гарантирует concurrency-группа workflow.
        """
        repo = Path(__file__).resolve().parent.parent
        workflows = sorted((repo / ".github" / "workflows").glob("*.yml")) + sorted(
            (repo / ".github" / "workflows").glob("*.yaml")
        )
        assert workflows, "workflow синхронизации должен существовать"
        sync_workflows = [w for w in workflows if "sync" in w.read_text().lower()]
        assert sync_workflows, "должен быть workflow синхронизации"
        for wf in sync_workflows:
            text = wf.read_text()
            assert "concurrency" in text, (
                f"{wf.name}: без concurrency-группы возможны два параллельных исполнителя"
            )

    def test_one_fetch_per_run_plan_stability(self, make_syncer):
        """Новая выгрузка не подмешивается в выполняемый план: план строится
        по одному получению Comdi за прогон (ТЗ 5.2/11)."""
        syncer = make_syncer(simple_schedule())
        assert syncer.comdi.fetch_count == 0
        syncer.run_once()
        assert syncer.comdi.fetch_count == 1, "ровно один запрос Comdi за прогон"
        syncer.run_once()
        assert syncer.comdi.fetch_count == 2, "каждый прогон — новая выгрузка целиком"


class TestDbFailure:
    def test_db_write_failure_after_http_recovers(self, make_syncer, api, state):
        """Успешный HTTP + сбой записи в БД: applied state не переписывается
        желаемыми данными; следующий прогон доводит конвергенцию (ТЗ 5.7/11)."""
        syncer = make_syncer(simple_schedule())

        # сбой записи БД в момент подтверждения сессии
        calls = {"n": 0}
        real_upsert = state.upsert

        def flaky_upsert(entity, source_id, target_id, payload_hash, payload=None):
            if entity == "session":
                calls["n"] += 1
                if calls["n"] == 1:
                    import sqlite3

                    raise sqlite3.OperationalError("disk I/O error")
            return real_upsert(entity, source_id, target_id, payload_hash, payload)

        state.upsert = flaky_upsert
        try:
            import sqlite3

            try:
                syncer.run_once()
            except sqlite3.OperationalError:
                pass  # прогон упал — честно
        finally:
            state.upsert = real_upsert

        # сессия могла создаться на сервере, но applied state её не подтвердил
        # -> следующий прогон безопасно повторяет операцию (идемпотентный create)
        stats = syncer.run_once()
        assert stats["failed"] == 0, f"конвергенция после сбоя БД: {stats}"
        assert len(api.sessions) == 1, "без дублей сессии"
