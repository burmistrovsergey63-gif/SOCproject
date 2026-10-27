"""Ядро синхронизации: Comdi (источник) -> Eventicious (приёмник).

Порядок важен: сначала залы и теги (на них ссылаются эпизоды),
затем докладчики, затем сами эпизоды.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .comdi import Episode, Schedule, Speaker, Tag
from dataclasses import dataclass
from .config import Config
from .eventicious import EventiciousClient, EventiciousError
from .state import State, content_hash
from .transfer import Occupancy, Placement

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
        # счётчики реальных изменений за прогон — для Telegram-отчёта
        self.changes: dict[str, int] = {}
        # ошибки/незавершённые операции всех шагов (залы, темы, EN, delete,
        # block, очистка) — попадают в итоговый статус (ТЗ 11)
        self.errors: list[str] = []

    def _bump(self, key: str, n: int = 1) -> None:
        if n:
            self.changes[key] = self.changes.get(key, 0) + n

    def _error(self, msg: str) -> None:
        """Зафиксировать ошибку шага: она обязана попасть в итог прогона."""
        self.errors.append(msg)
        log.error(msg)

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
        self.state.upsert(entity, source_id, target_id, content_hash(payload), payload)

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
            self._error(f"EN {entity} {source_id} не применён: {exc}")
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
                    self._error(f"location {hall.description_ru}: {exc}")
                    continue

                self._upsert("location", hall.source_id, body["id"], body)
                mapping[hall.source_id] = body["id"]
                self._bump(f"location_{action}")
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
                self._error(f"tag {body['name']}: {exc}")
                continue

            self._upsert("tag", tag.source_id, body["id"], body)
            mapping[tag.source_id] = body["id"]
            self._bump(f"tag_{action}")
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

    def sync_users(self, schedule: Schedule) -> dict[int, int]:
        """Синхронизация спикеров/участников.

        Returns:
            mapping source_id -> target_id
        """
        mapping = dict(self.state.list_ids("user"))
        kept_sources: set[int] = set()
        
        # Массовое создание/обновление (максимум 200 за запрос)
        batch: list[dict] = []
        batch_ids: list[int] = []
        
        for speaker in schedule.speakers:
            kept_sources.add(speaker.source_id)
            target_id = mapping.get(speaker.source_id, speaker.source_id + self.cfg.offset_user)
            
            # Формируем payload по контракту Eventicious
            body: dict[str, Any] = {
                "id": target_id,
                "firstName": speaker.name_ru or "",
                "lastName": speaker.lastname_ru or "",
                "company": speaker.company_ru or "",
                "position": speaker.regalia_ru or "",
                "description": _plain_to_html(speaker.description_ru or speaker.biography_ru or ""),
                "telegram": speaker.link_telegram or "",
                "vk": speaker.link_vk or "",
                "socialMediaLink1": speaker.link_dzen or "",
                "externalImagePath": speaker.photo_orig or speaker.photo or "",
                "isSpeaker": True,
            }
            
            # Проверяем, нужно ли обновление
            was_blocked = self.state.is_blocked("user", speaker.source_id)
            if not was_blocked and not self._should_upsert("user", speaker.source_id, body):
                mapping[speaker.source_id] = target_id
                continue

            # реактивация (ТЗ 9): вернувшийся спикер активируется тем же
            # target_id без создания дубля; withdrawn уходит только в отправку,
            # в applied state храним профиль без него (чтобы не зациклить diff)
            if was_blocked:
                batch.append({**body, "withdrawn": False})
                log.info("user reactivate: source_id=%d -> target_id=%d", speaker.source_id, target_id)
            else:
                batch.append(body)
            batch_ids.append(speaker.source_id)
            
            # Отправляем батч по достижении 200 пользователей
            if len(batch) >= 200:
                self._send_user_batch(batch, batch_ids, mapping)
                batch = []
                batch_ids = []
        
        # Отправляем оставшийся батч
        if batch:
            self._send_user_batch(batch, batch_ids, mapping)

        # Блокировка исчезнувших спикеров выполняется ПОСЛЕ синхронизации
        # сессий (ТЗ 9: сначала снимаются связи сессий, затем блокировка).
        return mapping
    
    def _send_user_batch(self, batch: list[dict], source_ids: list[int], mapping: dict[int, int]) -> None:
        """Отправить батч пользователей (create/update)."""
        source_by_target = {user["id"]: source_id for user, source_id in zip(batch, source_ids)}
        try:
            # Определяем, create или update
            new_users = [u for u in batch if source_ids[batch.index(u)] not in mapping]
            existing_users = [u for u in batch if source_ids[batch.index(u)] in mapping]
            
            if new_users:
                self.api.create_users({"users": new_users})
                for user in new_users:
                    source_id = source_by_target[user["id"]]
                    self._upsert("user", source_id, user["id"], user)
                    mapping[source_id] = user["id"]
                    self._bump("user_create")
                    log.info("user create: %s %s (source_id=%d)", user["firstName"], user["lastName"], source_id)
            
            if existing_users:
                self.api.update_users({"users": existing_users})
                for user in existing_users:
                    source_id = source_by_target[user["id"]]
                    # в applied state храним чистый профиль без служебного withdrawn
                    profile = {k: v for k, v in user.items() if k != "withdrawn"}
                    self._upsert("user", source_id, user["id"], profile)
                    mapping[source_id] = user["id"]
                    if user.get("withdrawn") is False:
                        self.state.set_blocked("user", source_id, False)
                        self._bump("user_reactivate")
                        log.info("user reactivate applied: source_id=%d", source_id)
                    else:
                        self._bump("user_update")
                    log.info("user update: %s %s (source_id=%d)", user["firstName"], user["lastName"], source_id)
        
        except EventiciousError as exc:
            log.error("users batch sync failed: %s", exc)
            self._error(f"users batch: {exc}")
    
    def _block_missing_users(self, kept_sources: set[int], mapping: dict[int, int]) -> None:
        """Заблокировать спикеров, которых нет в новом полном списке Comdi.

        Блокируется только уже управляемый профиль (из applied state);
        повторная одинаковая выгрузка не шлёт block повторно (ТЗ 9),
        ошибка block не сохраняет blocked.
        """
        existing_users = self.state.list_ids("user")
        missing_sources = set(existing_users.keys()) - kept_sources

        if not missing_sources:
            return

        # Блокируем через update с withdrawn=True (не delete)
        active_targets = {target_id for source_id, target_id in mapping.items() if source_id in kept_sources}
        block_sources = []
        to_block = []
        for source_id in missing_sources:
            if self.state.is_blocked("user", source_id):
                continue  # уже подтверждённо заблокирован — повтор не нужен
            target_id = existing_users[source_id]
            if target_id in active_targets:
                continue
            block_sources.append(source_id)
            to_block.append({"id": target_id, "withdrawn": True})
            log.info("user block: source_id=%d -> target_id=%d", source_id, target_id)

        for offset in range(0, len(to_block), 200):
            batch = to_block[offset:offset + 200]
            try:
                self.api.update_users({"users": batch})
            except EventiciousError as exc:
                log.error("users block failed: %s", exc)
                self._error(f"users block: {exc}")
                return  # blocked не сохраняем: операция незавершена (ТЗ 9)
            self._bump("user_block", len(batch))
            for source_id in block_sources[offset:offset + 200]:
                self.state.set_blocked("user", source_id, True)

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
        users: dict[int, int],
    ) -> list[tuple[int, str]]:
        """Синхронизация эпизодов. Возвращает список упавших (source_id, ошибка).

        Порядок (ТЗ 7):
        1) согласованные удаления — до переносов, чтобы слоты освобождались
           в модели только после подтверждённого удаления;
        2) контентные обновления (без смены зала/времени) — напрямую;
        3) create и переносы — через планировщик с проверкой занятости и
           разрывом циклов техническим временным залом;
        4) очистка неиспользуемых технических залов.
        Fallback «409 -> загрузить без зала» удалён по ТЗ: 409 вопреки модели
        означает расхождение модели с сервером и фиксируется как ошибка.
        """
        mapping = dict(self.state.list_ids("session"))
        kept_sources: set[int] = set()
        failures: list[tuple[int, str]] = []

        # 1) удаления исчезнувших эпизодов — до моделирования переносов
        if self.cfg.delete_removed:
            self._remove_missing("session", {ep.source_id for ep in schedule.all_episodes}, self.api.delete_session)

        # 2) желаемые payload'ы (desired state по данным Comdi)
        desired: dict[int, dict] = {}
        en_payloads: dict[int, dict | None] = {}
        for ep in schedule.all_episodes:
            kept_sources.add(ep.source_id)
            desired[ep.source_id] = self._session_body(ep, locations, tags, session_locations, users)
            en_payloads[ep.source_id] = self._en_session_payload(ep)

        # 3) модель занятости из applied state. Заодно добиваем payload у сессий,
        #    чей хэш совпал с желаемым: совпадение хэша доказывает, что в
        #    Eventicious ровно этот payload — baseline не выдумываем (ТЗ 10).
        occupancy = Occupancy()
        for sid, body in desired.items():
            stored = self.state.get("session", sid)
            if stored is None:
                continue
            if stored["content_hash"] == content_hash(body) and stored["content_payload"] is None:
                self.state.upsert("session", sid, body["id"], content_hash(body), body)
            payload = self.state.get_payload("session", sid)
            if payload is not None:
                occupancy.place(sid, Placement.from_payload(payload))

        # 4) классификация: пропуск / контент-апдейт / перенос или create
        content_updates: dict[int, dict] = {}
        pending: dict[int, dict] = {}
        for sid, body in desired.items():
            stored = self.state.get("session", sid)
            if stored is not None and stored["content_hash"] == content_hash(body):
                # основной payload не менялся, но EN-версия могла не уйти раньше
                self._sync_en("session", sid, body["id"], en_payloads[sid])
                continue
            applied = occupancy.placement_of(sid) if stored is not None else None
            if applied is not None and applied == Placement.from_payload(body):
                content_updates[sid] = body
            else:
                pending[sid] = body

        # 5) контентные обновления: зал и время не меняются — конфликтов нет
        for sid, body in content_updates.items():
            try:
                self.api.update_session(body["id"], {k: v for k, v in body.items() if k != "id"})
            except EventiciousError as exc:
                failures.append((sid, str(exc)))
                log.error("session %s не синхронизирована: %s", sid, exc)
                continue
            self._upsert("session", sid, body["id"], body)
            mapping[sid] = body["id"]
            self._bump("session_update")
            log.info("session update: [%s] %s", sid, (body.get("title") or "")[:60])
            self._sync_en("session", sid, body["id"], en_payloads[sid])

        # 6) create и переносы — в безопасном порядке
        if pending:
            failures.extend(self._execute_planned(pending, occupancy, mapping, en_payloads))

        # 7) технические залы больше не нужны — удаляем
        self._cleanup_temp_halls(occupancy, failures)

        if failures:
            log.error(
                "Не синхронизировано эпизодов: %d из %d -> %s",
                len(failures),
                len(schedule.all_episodes),
                [f[0] for f in failures],
            )
        return failures

    def _execute_planned(
        self,
        pending: dict[int, dict],
        occupancy: Occupancy,
        mapping: dict[int, int],
        en_payloads: dict[int, dict | None],
    ) -> list[tuple[int, str]]:
        """Выполняет create/переносы в порядке, безопасном для занятости.

        Цикл переносов (А хочет слот Б, Б хочет слот А) разрывается уводом
        одной сессии во временный технический зал. При ошибке шага зависимые
        шаги не выполняются — applied state сохраняет место, где остановились,
        и следующий прогон продолжит с этого же места (ТЗ 7.3).
        """
        failures: list[tuple[int, str]] = []
        parked: set[int] = set()

        while pending:
            progressed = False
            for sid in sorted(pending):
                body = pending[sid]
                target = Placement.from_payload(body)
                if not occupancy.is_free(target, exclude={sid}):
                    continue
                exists = sid in mapping
                try:
                    self._push_session(body, exists)
                except EventiciousError as exc:
                    # 409 вопреки модели = расхождение модели с сервером; не
                    # маскируем, останавливаем зависимые шаги (ТЗ 7.3)
                    log.error(
                        "session %s: перенос не применился (модель расходится с сервером): %s",
                        sid,
                        exc,
                    )
                    failures.append((sid, str(exc)))
                    failures.extend(
                        (other, "не выполнено: зависим от упавшего переноса")
                        for other in sorted(pending)
                        if other != sid
                    )
                    return failures
                self._upsert("session", sid, body["id"], body)
                occupancy.place(sid, target)
                mapping[sid] = body["id"]
                self._bump("session_create" if not exists else "session_move")
                log.info(
                    "session %s: [%s] -> зал %s (%s)",
                    "create" if not exists else "move/update",
                    sid,
                    target.location_id,
                    (body.get("title") or "")[:50],
                )
                self._sync_en("session", sid, body["id"], en_payloads.get(sid))
                del pending[sid]
                parked.discard(sid)
                progressed = True

            if progressed:
                continue

            # тупик — цикл переносов. Разрываем уводом одной сессии во временный зал.
            candidates = [s for s in sorted(pending) if s in mapping and s not in parked]
            if not candidates:
                msg = "тупик: нет кандидатов для разрыва цикла переносов"
                log.error("session %s: %s", sorted(pending), msg)
                failures.extend((s, msg) for s in sorted(pending))
                return failures
            sid = candidates[0]
            if not self._park_to_temp(sid, pending[sid], occupancy):
                msg = "не удалось увести сессию во временный зал"
                failures.append((sid, msg))
                failures.extend(
                    (other, "не выполнено: зависим от незавершённого переноса")
                    for other in sorted(pending)
                    if other != sid
                )
                return failures
            parked.add(sid)

        return failures

    # смещение id для технических временных залов (обмен занятыми залами)
    TEMP_HALL_BASE = 9_000_000
    TEMP_HALL_POSITION = 999
    TEMP_HALL_POOL = 5

    def _temp_hall_candidates(self) -> list[int]:
        return [self.TEMP_HALL_BASE + i for i in range(self.TEMP_HALL_POOL)]

    def _ensure_temp_hall(self, temp_id: int) -> None:
        """Создаёт технический зал однократно (идемпотентно через state)."""
        if self.state.get_temporary_hall(temp_id) is not None:
            return
        body = {
            "id": temp_id,
            "position": self.TEMP_HALL_POSITION,
            "name": f"Технический зал {temp_id}",
        }
        try:
            self.api.create_location(body)  # при конфликте id сам уйдёт в update
        except EventiciousError as exc:
            log.error("Не удалось создать технический зал %s: %s", temp_id, exc)
            raise
        self.state.create_temporary_hall(temp_id, body["name"])
        log.warning("Создан технический временный зал %s", temp_id)

    def _park_to_temp(self, sid: int, final_body: dict, occupancy: Occupancy) -> bool:
        """Уводит сессию во временный зал (разрыв цикла переносов).

        Время сессии не меняется — только зал. Применённое промежуточное
        состояние сохраняется, чтобы после сбоя продолжить с того же места.
        """
        start, end = final_body.get("startTime"), final_body.get("endTime")
        for temp_id in self._temp_hall_candidates():
            target = Placement(temp_id, start, end)
            if not occupancy.is_free(target, exclude={sid}):
                continue
            try:
                self._ensure_temp_hall(temp_id)
            except EventiciousError:
                return False
            park_body = {**final_body, "locationsIds": [temp_id]}
            try:
                self.api.update_session(
                    final_body["id"], {k: v for k, v in park_body.items() if k != "id"}
                )
            except EventiciousError as exc:
                log.error("session %s: не удалось увести во временный зал: %s", sid, exc)
                return False
            self._upsert("session", sid, final_body["id"], park_body)
            self.state.mark_temporary_hall_in_use(temp_id)
            occupancy.place(sid, target)
            self._bump("temp_park")
            log.warning(
                "session %s: уведена во временный зал %s (разрыв цикла переносов)",
                sid,
                temp_id,
            )
            return True
        log.error("session %s: нет свободного технического зала", sid)
        return False

    def _cleanup_temp_halls(self, occupancy: Occupancy, failures: list[tuple[int, str]]) -> None:
        """Удаляет технические залы, в которых не осталось сессий (ТЗ 7.2 п.5)."""
        for hall in self.state.list_temporary_halls():
            temp_id = int(hall["hall_id"])
            occupied = occupancy.sessions_in(temp_id)
            if occupied:
                log.warning(
                    "Технический зал %s ещё занят сессиями %s — оставляем", temp_id, sorted(occupied)
                )
                continue
            try:
                self.api.delete_location(temp_id)
            except EventiciousError as exc:
                if exc.status == 404:
                    pass  # уже удалён
                else:
                    failures.append((temp_id, f"очистка временного зала: {exc}"))
                    log.error("Технический зал %s не удалён: %s", temp_id, exc)
                    continue
            self.state.cleanup_temporary_halls_id(temp_id)
            log.info("Технический зал %s удалён", temp_id)

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
        # ТЗ 6: не повторять отправку неизменного дубля — сверяемся с applied state
        stored = self.state.get("location_dup", hall_source_id * 100 + lane)
        if stored is not None and stored["content_hash"] == content_hash(body):
            return dup_id
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
        users: dict[int, int] | None = None,
    ) -> dict[str, Any]:
        """Собирает payload эпизода.

        `session_locations` = {ep_source_id: location_id} — куда именно грузить
        эпизод (учтены дубли залов). Если маппинга нет (dry-run без плана) —
        берём основной зал из `locations`.
        
        `users` = {source_id: target_id} — маппинг спикеров
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
        speaker_ids: list[int] = []
        if ep.speaker_ids and users:
            # Используем только спикеров, которые успешно синхронизированы
            speaker_ids = [users[s] for s in ep.speaker_ids if s in users]
        if speaker_ids:
            body["speakersIds"] = speaker_ids
        else:
            # ТЗ 9: исчезновение последнего спикера сессии — явная очистка
            # связей пустым массивом. Ключ держим стабильным: раз он уже был
            # в applied state, desired всегда содержит его (иначе diff
            # зациклится и update уйдёт каждый прогон).
            prev = self.state.get_payload("session", ep.source_id)
            if prev and "speakersIds" in prev:
                body["speakersIds"] = []
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
                self._bump(f"{entity}_delete")
                log.info("%s: удалён исчезнувший объект %s (target_id=%s)", entity, source_id, target_id)
            except EventiciousError as exc:
                if exc.status == 404:
                    self.state.delete(entity, source_id)
                else:
                    log.error("%s: не удалось удалить %s: %s", entity, source_id, exc)
                    self._error(f"{entity} delete {source_id}: {exc}")

    # ---------- запуск ----------

    def run_once(self) -> dict[str, int]:
        self.changes = {}  # счётчики за текущий прогон
        self.errors = []  # ошибки/незавершённые операции шагов за прогон
        schedule = self.comdi.fetch_schedule()
        # конфликты по времени считаем заранее по данным Comdi, а не по 409 от API
        plan = self._plan_halls(schedule)
        locations, lane_map = self.sync_halls(schedule, plan)
        # сводим к «куда грузить каждый эпизод» — одна плоская карта без путаницы ключей
        session_locations = self._flatten_locations(plan, lane_map)
        tags = self.sync_tags(schedule)
        users = self.sync_users(schedule)  # синхронизация спикеров
        failures = self.sync_sessions(schedule, locations, tags, session_locations, users)

        # Блокировка исчезнувших спикеров — только после успешных изменений
        # сессий (ТЗ 9): при ошибке зависимого шага откладываем и фиксируем.
        if self.cfg.delete_removed:
            kept_sources = {s.source_id for s in schedule.speakers}
            if failures:
                self._error(
                    "блокировка спикеров отложена: есть незавершённые операции сессий"
                )
            else:
                self._block_missing_users(kept_sources, users)

        stats = {
            "halls": len(schedule.halls),
            "tags": len(schedule.tags),
            "episodes": len(schedule.all_episodes),
            "users": len(schedule.speakers),
            "failed": len(failures) + len(self.errors),
            "changes": dict(self.changes),
            "errors": (self.errors + [f"session {sid}: {msg}" for sid, msg in failures])[:20],
        }
        if failures:
            log.error("Синхронизация завершена с ошибками: %s", stats)
        else:
            log.info("Синхронизация завершена: %s", stats)
        return stats
