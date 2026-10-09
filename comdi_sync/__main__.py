"""Точка входа.

Режимы:
  python -m comdi_sync once   — один прогон (удобно для cron)
  python -m comdi_sync loop   — постоянный опрос по SYNC_INTERVAL_SECONDS
  python -m comdi_sync dry    — только забрать у Comdi и показать, что уйдёт в API
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time

from .comdi import ComdiClient
from .config import load_config, setup_logging
from .eventicious import EventiciousClient
from .state import State
from .sync import Syncer

log = logging.getLogger("comdi_sync")


def cmd_once(cfg, syncer, state) -> int:
    try:
        stats = syncer.run_once()
        state.log("ok" if not stats.get("failed") else "partial", json.dumps(stats, ensure_ascii=False))
        # частичный результат — тоже сигнал ненулевого кода для cron/мониторинга
        return 1 if stats.get("failed") else 0
    except Exception as exc:  # noqa: BLE001 — фиксируем любой сбой и отдаём ненулевой код
        log.exception("Синхронизация не удалась")
        state.log("error", repr(exc))
        return 1


def cmd_dry(cfg, comdi) -> int:
    """Показать payload, который ушёл бы в API. Ничего не пишем."""
    schedule = comdi.fetch_schedule()
    syncer = Syncer(cfg, comdi, api=None, state=None)  # type: ignore[arg-type]

    # в dry-run маппинга ещё нет, но ID детерминированы (source_id + offset)
    fake_locations = {h.source_id: h.source_id + cfg.offset_location for h in schedule.halls}
    fake_tags = {t.source_id: t.source_id + cfg.offset_tag for t in schedule.tags}
    # конфликты по времени считаем и в dry — чтобы заранее увидеть дубли залов
    plan = syncer._plan_halls(schedule)
    fake_lane_map = {
        hall_id: {
            lane: Syncer._dup_hall_id(hall_id, cfg.offset_location, lane)
            for lane in sorted(set(lanes.values()))
        }
        for hall_id, lanes in plan.items()
    }
    # lane 0 — сам основной зал
    for hall_id, lanes in fake_lane_map.items():
        lanes[0] = fake_locations[hall_id]
    fake_session_locations = syncer._flatten_locations(plan, fake_lane_map)

    dup_halls = [
        {
            "id": Syncer._dup_hall_id(h_id, cfg.offset_location, lane),
            "name": f"{h_name} ({lane + 1})",
            "reason": "пересечение времени внутри зала",
        }
        for h_id, h_name in ((h.source_id, h.description_ru) for h in schedule.halls)
        for lane in sorted(set(plan.get(h_id, {}).values()) - {0})
    ]

    out = {
        "locations": [
            {
                "id": h.source_id + cfg.offset_location,
                "position": h.order + 1,
                "name": h.description_ru,
                "episodes": len(h.episodes),
            }
            for h in schedule.halls
        ],
        "dup_locations": dup_halls,
        "tags": [
            {
                "id": t.source_id + cfg.offset_tag,
                "name": t.title_ru,
                "name_en": t.title_en,
            }
            for t in schedule.tags
        ],
        "sessions": [
            syncer._session_body(ep, fake_locations, fake_tags, fake_session_locations)
            for ep in schedule.all_episodes
        ],
    }
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    log.info("dry-run: отправлено не было ничего")
    return 0


def cmd_loop(cfg, syncer, state) -> int:
    log.info("Циклический опрос каждые %d с. Ctrl+C — остановить.", cfg.sync_interval_seconds)
    while True:
        started = time.monotonic()
        cmd_once(cfg, syncer, state)
        elapsed = time.monotonic() - started
        time.sleep(max(cfg.sync_interval_seconds - elapsed, 1))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="comdi_sync", description="Comdi -> Eventicious sync")
    parser.add_argument(
        "mode",
        choices=["once", "loop", "dry"],
        nargs="?",
        default="once",
        help="once — один прогон, loop — постоянный опрос, dry — только показать payload",
    )
    args = parser.parse_args(argv)

    cfg = load_config()
    setup_logging(cfg.log_level)

    comdi = ComdiClient(cfg.comdi_event_url, cfg.http_timeout, cfg.max_retries)

    if args.mode == "dry":
        return cmd_dry(cfg, comdi)

    api = EventiciousClient(
        cfg.eventicious_base_url,
        cfg.client_id,
        cfg.client_secret,
        cfg.http_timeout,
        cfg.max_retries,
    )
    with State(cfg.state_db_path) as state:
        syncer = Syncer(cfg, comdi, api, state)
        if args.mode == "loop":
            try:
                return cmd_loop(cfg, syncer, state)
            except KeyboardInterrupt:
                log.info("Остановлено")
                return 0
        return cmd_once(cfg, syncer, state)


if __name__ == "__main__":
    sys.exit(main())
