"""Ядро синхронизации: Comdi (источник) -> Eventicious (приёмник).

Порядок важен: сначала залы и теги (на них ссылаются эпизоды),
затем докладчики, затем сами эпизоды.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .comdi import Episode, Schedule, Tag
from .config import Config
from .eventicious import EventiciousClient, EventiciousError
from .state import State, content_hash

log = logging.getLogger(__name__)


def _color(raw: str) -> str | None:
    """'90baff' / '#90baff' -> '#90baff'; пустое/мусорное -> None."""
    if not raw:
        return None
    value = raw.strip()
    if not value.startswith("#"):
        value = f"#{value}"
    if len(value) != 7:
        return None
    try:
        int(value[1:], 16)
    except ValueError:
        return None
    return value


def _plain_to_html(text: str) -> str:
    """Eventicious принимает HTML. Простой текст превращаем в валидную разметку."""
    if not text:
        return ""
    if "<" in text:  # уже разметка
        return text
    import html as html_mod

    escaped = html_mod.escape(text)
    paragraphs = "".join(f"<p>{line.strip()}</p>" for line in escaped.splitlines() if line.strip())
    return paragraphs or f"<p>{escaped}</p>"


class Syncer:
    def __init__(self, cfg: Config, comdi, api: EventiciousClient, state: State) -> None:
        self.cfg = cfg
        self.comdi = comdi
        self.api = api
        self.state = state
        self.tz = ZoneInfo(cfg.event_timezone)

    # ---------- вспомогательные ----------

    def _local(self, dt: datetime | None) -> str | None:
        if dt is None:
            return None
        return dt.astimezone(self.tz).strftime("%Y-%m-%dT%H:%M")

    def _should_upsert(self, entity: str, source_id: int, payload: dict) -> bool:
        """Правим объект только если его содержимое реально изменилось."""
        stored = self.state.get(entity, source_id)
        return stored is None or stored["content_hash"] != content_hash(payload)

    def _upsert(self, entity: str, source_id: int, target_id: int, payload: dict) -> None:
        self.state.upsert(entity, source_id, target_id, content_hash(payload))

    def _put_en(self, entity: str, target_id: int, payload: dict) -> None:
        body = {**payload, "language": "en-US"}
        if entity == "tag":
            self.api.update_tag(target_id, body)
        elif entity == "session":
            self.api.update_session(target_id, body)
        else:
            raise ValueError(f"нет EN-обработчика для {entity}")

    def _sync_en(
        self, entity: str, source_id: int, target_id: int, payload: dict | None
    ) -> None:
        """Отправка EN-версии отдельным PUT с language=en-US.

        Отслеживается своим хэшем, чтобы при сбое (например, 404 сразу после
        create) повторить отправку на следующем прогоне, а не потерять перевод.
        """
        if not self.cfg.sync_en or not payload:
            return

        payload_hash = content_hash(payload)
        if self.state.en_hash(entity, source_id) == payload_hash:
            return  # перевод уже ушёл и не менялся

        try:
            self._put_en(entity, target_id, payload)
        except EventiciousError as exc:
            if exc.status == 404:
                # объект ещё не виден API — уйдёт на следующем прогоне
                log.warning("%s %s: EN-версия, 404 — повторю позже", entity, source_id)
            else:
                log.warning("%s %s: EN-версия не применилась: %s", entity, source_id, exc)
            return

        self.state.mark_en_sent(entity, source_id, payload_hash)

    # ---------- шаги ----------

    def _plan_halls(self, schedule: Schedule) -> dict[int, dict[int, int]]:
        """Раскладывает эпизоды по «дорожкам» внутри зала.

        Eventicious запрещает два события в одном зале в одно время (409).
        Конфликтность считаем ПО ДАННЫМ Comdi, а не по ответу API: иначе
        обычный обмен докладов местами даёт ложный 409 — оба слота заняты
        до того, как мы успели кого-то сдвинуть — и в системе без нужды
        появляется дубль зала.

        Результат: {hall_source_id: {номер_дорожки: target_id}}.
        Дорожка 0 — основный зал, 1 и выше — его дубли («(2)», «(3)»…).
        Жадный алгоритм по возрастанию начала: первый эпизод слота остаётся
        в основном зале, все последующие пересекающиеся уходят на новую дорожку.
        """
        plan: dict[int, dict[int, int]] = {}
        far_future = datetime.max.replace(tzinfo=timezone.utc)

        for hall in schedule.halls:
            lane_end: list[datetime] = []  # конец последнего эпизода на дорожке
            lanes: dict[int, int] = {}

            for ep in sorted(hall.episodes, key=lambda e: (e.start_utc or far_future, e.source_id)):
                if ep.start_utc is None or ep.stop_utc is None:
                    lanes[ep.source_id] = 0
                    continue
                for idx, occupied_until in enumerate(lane_end):
                    if ep.start_utc >= occupied_until:  # дорожка свободна
                        lane_end[idx] = ep.stop_utc
                        lanes[ep.source_id] = idx
                        break
                else:
                    lane_end.append(ep.stop_utc)
                    lanes[ep.source_id] = len(lane_end) - 1

            plan[hall.source_id] = lanes
            moved = {e: l for e, l in lanes.items() if l > 0}
            if moved:
                log.warning(
                    "Зал %r: %d эпизод(ов) пересекаются по времени -> на дубли зала: %s",
                    hall.description_ru,
                    len(moved),
                    moved,
                )
        return plan

    @staticmethod
    def _dup_hall_id(hall_source_id: int, offset_location: int, lane: int) -> int:
        """Детерминированный id дубля зала: основной id + смещение дорожки."""
        return hall_source_id + offset_location + lane * Syncer.DUP_HALL_OFFSET

    def sync_halls(
        self, schedule: Schedule, plan: dict[int, dict[int, int]]
    ) -> tuple[dict[int, int], dict[int, dict[int, int]]]:
        """Залы -> locations. Создаёт основные залы и нужные дубли.

        Возвращает ({source_id: target_id}, {hall: {lane: target_id}}).
        """
        mapping = dict(self.state.list_ids("location"))
        lane_map: dict[int, dict[int, int]] = {}
        kept_sources: set[int] = set()

        for hall in schedule.halls:
            kept_sources.add(hall.source_id)
            body = {
                "id": hall.source_id + self.cfg.offset_location,
                "position": hall.order + 1,
                "name": hall.description_ru or f"Зал {hall.source_id}",
            }
            if not self._should_upsert("location", hall.source_id, body):
                mapping[hall.source_id] = body["id"]
            else:
                try:
                    if hall.source_id in mapping:
                        self.api.update_location(
                            mapping[hall.source_id], {k: v for k, v in body.items() if k != "id"}
                        )
                        action = "update"
                    else:
                        self.api.create_location(body)
                        action = "create"
                except EventiciousError as exc:
                    log.error("location %s не синхронизирован: %s", hall.description_ru, exc)
                    continue

                self._upsert("location", hall.source_id, body["id"], body)
                mapping[hall.source_id] = body["id"]
                log.info("location %s: %s (%s)", action, hall.description_ru, body["id"])

            # дорожки >0 = дубли зала, нужны для эпизодов с пересечением времени
            lane_map[hall.source_id] = {0: body["id"]}
            for lane in sorted(set(plan.get(hall.source_id, {}).values()) - {0}):
                lane_map[hall.source_id][lane] = self._ensure_dup_hall(
                    hall.source_id, hall.description_ru or f"Зал {hall.source_id}", lane
                )

        self._remove_missing("location", kept_sources, self.api.delete_location)
        return mapping, lane_map

    def sync_tags(self, schedule: Schedule) -> dict[int, int]:
        mapping = dict(self.state.list_ids("tag"))
        kept_sources: set[int] = set()

        for tag in schedule.tags:
            kept_sources.add(tag.source_id)
            body: dict[str, Any] = {
                "id": tag.source_id + self.cfg.offset_tag,
                "name": tag.title_ru or tag.title_en or f"Тег {tag.source_id}",
                "color": _color(tag.color) or "#888888",
                "visibilityFlag": 1,
            }
            if not self._should_upsert("tag", tag.source_id, body):
                mapping[tag.source_id] = body["id"]
                # EN-версия могла не уйти на прошлом прогоне — повторяем
                self._sync_en(
                    entity="tag",
                    source_id=tag.source_id,
                    target_id=body["id"],
                    payload={"name": tag.title_en} if tag.title_en else None,
                )
                continue

            try:
                if tag.source_id in mapping:
                    self.api.update_tag(
                        mapping[tag.source_id], {k: v for k, v in body.items() if k != "id"}
                    )
                    action = "update"
                else:
                    self.api.create_tag(body)
                    action = "create"
            except EventiciousError as exc:
                log.error("tag %s не синхронизирован: %s", body["name"], exc)
                continue

            self._upsert("tag", tag.source_id, body["id"], body)
            mapping[tag.source_id] = body["id"]
            log.info("tag %s: %s", action, body["name"])

            # английская версия — отдельным PUT с language=en-US
            self._sync_en(
                entity="tag",
                source_id=tag.source_id,
                target_id=body["id"],
                payload={"name": tag.title_en} if tag.title_en else None,
            )

        self._remove_missing("tag", kept_sources, self.api.delete_tag)
        return mapping

    @staticmethod
    def _en_session_payload(ep: Episode) -> dict | None:
        if not (ep.name_en or ep.info_en):
            return None
        return {"title": ep.name_en, "description": _plain_to_html(ep.info_en)}

    def sync_sessions(
        self,
        schedule: Schedule,
        locations: dict[int, int],
        tags: dict[int, int],
        session_locations: dict[int, int],
    ) -> list[tuple[int, str]]:
        """Синхронизация эпизодов. Возвращает список упавших (source_id, ошибка).

        Ошибка на одном эпизоде не должна ронять остальные 81 — поэтому
        исключения собираются, а не пробрасываются наружу.
        """
        mapping = dict(self.state.list_ids("session"))
        kept_sources: set[int] = set()
        failures: list[tuple[int, str]] = []

        for ep in schedule.all_episodes:
            kept_sources.add(ep.source_id)
            try:
                self._sync_one_session(ep, mapping, locations, tags, session_locations)
            except EventiciousError as exc:
                failures.append((ep.source_id, str(exc)))
                log.error(
                    "session %s не синхронизирована: %s [%s]",
                    ep.source_id,
                    ep.name_ru[:50],
                    exc,
                )
            except Exception as exc:  # noqa: BLE001 — не даём одному эпизоду уронить прогон
                failures.append((ep.source_id, repr(exc)))
                log.exception("session %s: непредвиденная ошибка", ep.source_id)

        if failures:
            log.error(
                "Не синхронизировано эпизодов: %d из %d -> %s",
                len(failures),
                len(schedule.all_episodes),
                [f[0] for f in failures],
            )

        if self.cfg.delete_removed:
            self._remove_missing("session", kept_sources, self.api.delete_session)
        return failures

    def _sync_one_session(
        self,
        ep: Episode,
        mapping: dict[int, int],
        locations: dict[int, int],
        tags: dict[int, int],
        session_locations: dict[int, int],
    ) -> None:
        body = self._session_body(ep, locations, tags, session_locations)
        en_payload = self._en_session_payload(ep)

        if not self._should_upsert("session", ep.source_id, body):
            mapping[ep.source_id] = body["id"]
            # основной payload не менялся, но EN-версия могла не уйти раньше
            self._sync_en(
                entity="session",
                source_id=ep.source_id,
                target_id=body["id"],
                payload=en_payload,
            )
            return

        exists = ep.source_id in mapping
        # зал для эпизода уже подобран планом _plan_halls — дубль создаётся заранее,
        # поэтому 409 здесь означает, что планировщик что-то не учёл
        try:
            self._push_session(body, exists)
            action = "update" if exists else "create"
        except EventiciousError as exc:
            if exc.status != 409 or not body.get("locationsIds"):
                raise
            # страховка: планировщик не предвидел конфликт. Не теряем эпизод —
            # грузим без зала и громко сообщаем, чтобы это поправили в данных Comdi
            log.error(
                "session %s: неожиданный 409 (планировщик не увидел пересечения). "
                "Эпизод загружен БЕЗ зала, проверьте данные Comdi: %s [%s]",
                ep.source_id,
                ep.name_ru[:50],
                exc,
            )
            body = {**body, "locationsIds": []}
            self._push_session(body, exists)
            action = "update" if exists else "create"

        self._upsert("session", ep.source_id, body["id"], body)
        mapping[ep.source_id] = body["id"]
        log.info("session %s: [%s] %s", action, ep.source_id, ep.name_ru[:60])

        # английская версия — отдельным PUT с language=en-US
        self._sync_en(
            entity="session",
            source_id=ep.source_id,
            target_id=body["id"],
            payload=en_payload,
        )

    def _push_session(self, body: dict, exists: bool) -> None:
        """Отправка эпизода: update для известного, create для нового."""
        if exists:
            self.api.update_session(body["id"], {k: v for k, v in body.items() if k != "id"})
        else:
            self.api.create_session(body)

    # смещение id для дублей залов, чтобы не пересечься с обычными залами
    DUP_HALL_OFFSET = 1_000_000

    def _ensure_dup_hall(self, hall_source_id: int, hall_name: str, lane: int) -> int:
        """Создаёт (однократно) дубль зала для эпизодов с пересечением времени.

        Не хранит override — id детерминирован (дорожка), а нужность дубля
        каждый прогон пересчитывается из данных Comdi через _plan_halls.
        Поэтому, когда клиент уберёт пересечение в источнике, эпизод сам
        вернётся в основной зал: дорожка снова станет 0.
        """
        dup_id = self._dup_hall_id(hall_source_id, self.cfg.offset_location, lane)
        body = {
            "id": dup_id,
            "position": lane + 1,
            "name": f"{hall_name} ({lane + 1})",
        }
        try:
            self.api.create_location(body)  # при конфликте id сам уйдёт в update
        except EventiciousError as exc:
            log.error("Не удалось создать дубль зала %r: %s", body["name"], exc)
            raise

        self.state.upsert("location_dup", hall_source_id * 100 + lane, dup_id, content_hash(body))
        log.warning("Зал-дубль %r (id=%s) используется для дорожки %d", body["name"], dup_id, lane)
        return dup_id

    @staticmethod
    def _flatten_locations(
        plan: dict[int, dict[int, int]], lane_map: dict[int, dict[int, int]]
    ) -> dict[int, int]:
        """Сводит план и маппинг залов к одному словарю: {ep_source_id: location_id}.

        Разделяем источники, чтобы не путать ключи: в `plan` ключи — id эпизодов,
        в `lane_map` — номера дорожек. Промежуточная путаница здесь приводит к
        отправке эпизода в чужой зал и ложному 409 от API.
        """
        flat: dict[int, int] = {}
        for hall_id, lanes in plan.items():
            ids = lane_map.get(hall_id, {})
            for ep_id, lane in lanes.items():
                location_id = ids.get(lane)
                if location_id is not None:
                    flat[ep_id] = location_id
        return flat

    def _session_body(
        self,
        ep: Episode,
        locations: dict[int, int],
        tags: dict[int, int],
        session_locations: dict[int, int] | None = None,
    ) -> dict[str, Any]:
        """Собирает payload эпизода.

        `session_locations` = {ep_source_id: location_id} — куда именно грузить
        эпизод (учтены дубли залов). Если маппинга нет (dry-run без плана) —
        берём основной зал из `locations`.
        """
        location_id = (session_locations or {}).get(ep.source_id)
        if location_id is None:
            location_id = locations.get(ep.stream_source_id)
        body: dict[str, Any] = {
            "id": ep.source_id + self.cfg.offset_session,
            "title": ep.name_ru or ep.name_en or f"Эпизод {ep.source_id}",
            "description": _plain_to_html(ep.info_ru),
            "startTime": self._local(ep.start_utc),
            "endTime": self._local(ep.stop_utc),
            "tagIds": [tags[t] for t in ep.tag_ids if t in tags],
            "locationsIds": [location_id] if location_id is not None else [],
            "aclGroupsIds": [],
            "type": 0,
        }
        if ep.speaker_ids:
            body["speakersIds"] = [s + self.cfg.offset_user for s in ep.speaker_ids]
        color = _color(ep.color)
        if color:
            body["color"] = color
        return body

    def _remove_missing(self, entity: str, kept: set[int], deleter) -> None:
        if not self.cfg.delete_removed:
            return
        for source_id, target_id in list(self.state.list_ids(entity).items()):
            if source_id in kept:
                continue
            try:
                deleter(target_id)
                self.state.delete(entity, source_id)
                log.info("%s: удалён исчезнувший объект %s (target_id=%s)", entity, source_id, target_id)
            except EventiciousError as exc:
                if exc.status == 404:
                    self.state.delete(entity, source_id)
                else:
                    log.error("%s: не удалось удалить %s: %s", entity, source_id, exc)

    # ---------- запуск ----------

    def run_once(self) -> dict[str, int]:
        schedule = self.comdi.fetch_schedule()
        # конфликты по времени считаем заранее по данным Comdi, а не по 409 от API
        plan = self._plan_halls(schedule)
        locations, lane_map = self.sync_halls(schedule, plan)
        # сводим к «куда грузить каждый эпизод» — одна плоская карта без путаницы ключей
        session_locations = self._flatten_locations(plan, lane_map)
        tags = self.sync_tags(schedule)
        failures = self.sync_sessions(schedule, locations, tags, session_locations)

        stats = {
            "halls": len(schedule.halls),
            "tags": len(schedule.tags),
            "episodes": len(schedule.all_episodes),
            "failed": len(failures),
        }
        if failures:
            log.error("Синхронизация завершена с ошибками: %s", stats)
        else:
            log.info("Синхронизация завершена: %s", stats)
        return stats
