"""Подставной Eventicious API для локальных проверок (ТЗ 11).

Модель:
- applied state в памяти: locations / tags / sessions / users по target_id;
- 409 на пересечения сессий в одном зале (интервалы [start, end));
- инъекция сбоев: `fail_next(method, exc, apply_first=...)` — один раз
  уронить операцию, опционально применив её на сервере (модель «таймаут с
  уже применённым запросом»);
- журнал операций `ops` для проверки порядка и отсутствия лишних записей.
"""
from __future__ import annotations

from typing import Any, Callable

from comdi_sync.eventicious import EventiciousError


class FakeEventicious:
    def __init__(self) -> None:
        self.locations: dict[int, dict] = {}
        self.tags: dict[int, dict] = {}
        self.sessions: dict[int, dict] = {}
        self.users: dict[int, dict] = {}
        # журнал: ("method", payload) — для проверок порядка и идемпотентности
        self.ops: list[tuple[str, dict]] = []
        # инъекции сбоев: method -> (exc, apply_first)
        self._faults: dict[str, tuple[EventiciousError, bool]] = {}

    # ---------- инъекция сбоев ----------

    def fail_next(
        self,
        method: str,
        exc: EventiciousError | None = None,
        apply_first: bool = False,
    ) -> None:
        """Один раз уронить следующий вызов `method`.

        apply_first=True — операция сначала применяется на «сервере»,
        затем падает (модель «сервер применил, но ответ не дошёл»).
        """
        self._faults[method] = (
            exc or EventiciousError("POST", method, 500, "injected failure"),
            apply_first,
        )

    def _maybe_fail(self, method: str, apply: Callable[[], None]) -> None:
        fault = self._faults.pop(method, None)
        if fault is None:
            apply()
            return
        exc, apply_first = fault
        if apply_first:
            apply()
        raise exc

    # ---------- залы ----------

    def create_location(self, body: dict) -> dict:
        def apply() -> None:
            # реальный клиент при конфликте id превращает create в update
            if body["id"] in self.locations:
                self.locations[body["id"]].update(body)
                self.ops.append(("update_location", dict(body)))
            else:
                self.locations[body["id"]] = dict(body)
                self.ops.append(("create_location", dict(body)))

        self._maybe_fail("create_location", apply)
        return {}

    def update_location(self, location_id: int, body: dict) -> dict:
        def apply() -> None:
            if location_id not in self.locations:
                raise EventiciousError("PUT", "locations", 404, "not found")
            self.locations[location_id].update(body)
            self.ops.append(("update_location", {"id": location_id, **body}))

        self._maybe_fail("update_location", apply)
        return {}

    def delete_location(self, location_id: int) -> None:
        def apply() -> None:
            if location_id not in self.locations:
                raise EventiciousError("DELETE", "locations", 404, "not found")
            del self.locations[location_id]
            self.ops.append(("delete_location", {"id": location_id}))

        self._maybe_fail("delete_location", apply)

    # ---------- темы ----------

    def create_tag(self, body: dict) -> dict:
        def apply() -> None:
            self.tags[body["id"]] = dict(body)
            self.ops.append(("create_tag", dict(body)))

        self._maybe_fail("create_tag", apply)
        return {}

    def update_tag(self, tag_id: int, body: dict) -> dict:
        def apply() -> None:
            if tag_id not in self.tags:
                raise EventiciousError("PUT", "tags", 404, "not found")
            self.tags[tag_id].update(body)
            self.ops.append(("update_tag", {"id": tag_id, **body}))

        self._maybe_fail("update_tag", apply)
        return {}

    def delete_tag(self, tag_id: int) -> None:
        def apply() -> None:
            if tag_id not in self.tags:
                raise EventiciousError("DELETE", "tags", 404, "not found")
            del self.tags[tag_id]
            self.ops.append(("delete_tag", {"id": tag_id}))

        self._maybe_fail("delete_tag", apply)

    # ---------- сессии (с проверкой занятости залов) ----------

    def _assert_free(self, body: dict, self_id: int | None = None) -> None:
        """409, если слот в одном из залов занят другой сессией (ТЗ 7)."""
        loc_ids = body.get("locationsIds") or []
        start, end = body.get("startTime"), body.get("endTime")
        if not loc_ids or not start or not end:
            return
        for sid, sess in self.sessions.items():
            if self_id is not None and sid == self_id:
                continue
            if not (set(sess.get("locationsIds") or []) & set(loc_ids)):
                continue
            s_start, s_end = sess.get("startTime"), sess.get("endTime")
            # интервалы [start, end): соприкосновение не пересечение
            if start < s_end and s_start < end:
                raise EventiciousError(
                    "POST", "sessions", 409,
                    f"location busy: session {sid} {s_start}..{s_end}",
                )

    def _merged_session(self, session_id: int, body: dict) -> dict:
        merged = dict(self.sessions.get(session_id, {}))
        merged.update(body)
        merged["id"] = session_id
        return merged

    def create_session(self, body: dict) -> dict:
        def apply() -> None:
            if body["id"] in self.sessions:
                merged = self._merged_session(body["id"], body)
                self._assert_free(merged, self_id=body["id"])
                self.sessions[body["id"]] = merged
                self.ops.append(("update_session", dict(body)))
                return
            self._assert_free(body)
            self.sessions[body["id"]] = dict(body)
            self.ops.append(("create_session", dict(body)))

        self._maybe_fail("create_session", apply)
        return {}

    def update_session(self, session_id: int, body: dict) -> dict:
        def apply() -> None:
            if session_id not in self.sessions:
                raise EventiciousError("PUT", "sessions", 404, "not found")
            merged = self._merged_session(session_id, body)
            self._assert_free(merged, self_id=session_id)
            self.sessions[session_id] = merged
            self.ops.append(("update_session", {"id": session_id, **body}))

        self._maybe_fail("update_session", apply)
        return {}

    def delete_session(self, session_id: int) -> None:
        def apply() -> None:
            if session_id not in self.sessions:
                raise EventiciousError("DELETE", "sessions", 404, "not found")
            del self.sessions[session_id]
            self.ops.append(("delete_session", {"id": session_id}))

        self._maybe_fail("delete_session", apply)

    # ---------- пользователи ----------

    @staticmethod
    def _users_list(payload: Any) -> list[dict]:
        if isinstance(payload, dict):
            payload = payload.get("users") or []
        return list(payload)

    def create_users(self, payload: dict) -> dict:
        def apply() -> None:
            for user in self._users_list(payload):
                # реальный клиент при конфликте id превращает create в update
                if user["id"] in self.users:
                    self.users[user["id"]].update(user)
                    self.ops.append(("update_user", {"id": user["id"]}))
                else:
                    self.users[user["id"]] = dict(user)
                    self.ops.append(("create_user", {"id": user["id"]}))

        self._maybe_fail("create_users", apply)
        return {}

    def update_users(self, payload: dict) -> dict:
        def apply() -> None:
            for user in self._users_list(payload):
                if user["id"] not in self.users:
                    raise EventiciousError("PATCH", "users", 404, "not found")
                self.users[user["id"]].update(user)
                self.ops.append(("update_user", {"id": user["id"], **user}))

        self._maybe_fail("update_users", apply)
        return {}

    def delete_users(self, ids: list[int]) -> dict:
        """Блокировка не должна сюда попадать — ТЗ 9 запрещает delete."""

        def apply() -> None:
            for uid in ids:
                self.users.pop(uid, None)
                self.ops.append(("delete_user", {"id": uid}))

        self._maybe_fail("delete_users", apply)
        return {}

    # ---------- проверки для тестов ----------

    def write_ops(self) -> list[tuple[str, dict]]:
        """Операции записи (все текущие ops — все и являются записями)."""
        return list(self.ops)

    def clear_ops(self) -> None:
        self.ops.clear()
