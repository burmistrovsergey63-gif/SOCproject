"""Клиент Eventicious External API v2 (приёмник расписания).

Аутентификация: client_credentials -> bearer token -> Authorization: Bearer {token}.
Все записи идемпотентны: объекты создаём со СВОИМ id, дальше — update по этому id.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable

import requests

log = logging.getLogger(__name__)


class EventiciousError(RuntimeError):
    def __init__(self, method: str, url: str, status: int, body: str) -> None:
        super().__init__(f"{method} {url} -> {status}: {body[:500]}")
        self.status = status


class EventiciousClient:
    def __init__(
        self,
        base_url: str,
        client_id: str,
        client_secret: str,
        timeout: int = 30,
        max_retries: int = 4,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.timeout = timeout
        self.max_retries = max_retries
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self._session = requests.Session()

    # ---------- auth ----------

    def get_token(self) -> str:
        if self._token and time.time() < self._token_expires_at - 60:
            return self._token

        # Ограниченный retry только для сетевых ошибок и 5xx (ТЗ 11):
        # 4xx не ретраим; при исчерпании попыток — ошибка без изменения applied state.
        attempts = min(self.max_retries, 4)
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                resp = self._session.post(
                    f"{self.base_url}/connect/token",
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                    },
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                last_exc = exc
                log.warning("POST /connect/token: сетевая ошибка (%d/%d): %s", attempt, attempts, exc)
                time.sleep(min(2**attempt, 10))
                continue

            if resp.status_code == 200:
                data = resp.json()
                self._token = data["access_token"]
                self._token_expires_at = time.time() + int(data.get("expires_in", 3600))
                log.info("Eventicious: токен получен, действует %s с", data.get("expires_in"))
                return self._token

            if resp.status_code >= 500 and attempt < attempts:
                log.warning(
                    "POST /connect/token: HTTP %d (%d/%d), повторяю",
                    resp.status_code, attempt, attempts,
                )
                time.sleep(min(2**attempt, 10))
                continue

            raise EventiciousError("POST", "/connect/token", resp.status_code, resp.text)

        raise EventiciousError(
            "POST", "/connect/token", 0,
            f"нет токена после {attempts} попыток: {last_exc}",
        )

    # ---------- http ----------

    def _request(
        self,
        method: str,
        path: str,
        json_body: dict | None = None,
        params: dict | None = None,
        *,
        retry_4xx: bool = False,
        retry_404: bool = False,
    ) -> dict:
        url = f"{self.base_url}{path}"
        last_exc: Exception | None = None
        # у API бывает задержка консистентности: create уже вернул 200,
        # а update сразу после — ещё 404
        attempts = 3 if retry_404 else self.max_retries

        for attempt in range(1, attempts + 1):
            try:
                resp = self._session.request(
                    method,
                    url,
                    json=json_body,
                    params=params,
                    headers={"Authorization": f"Bearer {self.get_token()}"},
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                last_exc = exc
                log.warning("%s %s: сетевая ошибка (%d/%d): %s", method, path, attempt, attempts, exc)
                time.sleep(min(2**attempt, 30))
                continue

            # токен мог протухнуть между запросами — берём новый один раз
            if resp.status_code == 401 and attempt == 1:
                log.info("%s %s: 401, обновляю токен", method, path)
                self._token = None
                continue

            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError:
                    return {}

            # 404 на update сразу после create — задержка консистентности, повторяем
            if retry_404 and resp.status_code == 404 and attempt < attempts:
                log.info("%s %s: 404, повтор %d/%d (консистентность)", method, path, attempt, attempts)
                time.sleep(2)
                continue

            # 4xx (кроме 401) ретраим только если явно разрешено: обычно это бизнес-ошибка
            if resp.status_code >= 500 or (retry_4xx and resp.status_code >= 400):
                last_exc = EventiciousError(method, url, resp.status_code, resp.text)
                log.warning(
                    "%s %s: %d, повтор %d/%d", method, path, resp.status_code, attempt, attempts
                )
                time.sleep(min(2**attempt, 30))
                continue

            raise EventiciousError(method, url, resp.status_code, resp.text)

        raise last_exc if last_exc else RuntimeError("запрос не выполнен")

    # ---------- сущности ----------

    def _create(self, path: str, body: dict, update: Callable[[dict], dict] | None = None) -> dict:
        """Создать объект.

        Если id уже занят (409 / «already exists») — запись может быть устаревшей
        (например, создана раньше и с тех пор изменена). В этом случае делаем
        update тем же payload'ом, чтобы привести объект к актуальному состоянию.
        """
        try:
            return self._request("POST", path, body)
        except EventiciousError as exc:
            conflict = exc.status == 409 or "already exists" in str(exc).lower()
            if not conflict or update is None:
                raise
            log.debug("id уже занят, выполняю update: %s", path)
            return update({k: v for k, v in body.items() if k != "id"})

    # locations (залы)
    def create_location(self, body: dict) -> dict:
        return self._create(
            "/api/external/v2/locations/create",
            body,
            update=lambda b: self.update_location(body["id"], b),
        )

    def update_location(self, entity_id: int, body: dict) -> dict:
        return self._request("PUT", f"/api/external/v2/locations/update/{entity_id}", body, retry_404=True)

    def delete_location(self, entity_id: int) -> dict:
        return self._request("DELETE", f"/api/external/v2/locations/delete/{entity_id}")

    # tags (темы)
    def create_tag(self, body: dict) -> dict:
        return self._create(
            "/api/external/v2/tags/create",
            body,
            update=lambda b: self.update_tag(body["id"], b),
        )

    def update_tag(self, entity_id: int, body: dict) -> dict:
        return self._request("PUT", f"/api/external/v2/tags/update/{entity_id}", body, retry_404=True)

    def delete_tag(self, entity_id: int) -> dict:
        return self._request("DELETE", f"/api/external/v2/tags/delete/{entity_id}")

    # sessions (эпизоды расписания)
    def create_session(self, body: dict) -> dict:
        return self._create(
            "/api/external/v2/sessions/create",
            body,
            update=lambda b: self.update_session(body["id"], b),
        )

    def update_session(self, entity_id: int, body: dict) -> dict:
        return self._request("PUT", f"/api/external/v2/sessions/update/{entity_id}", body, retry_404=True)

    def delete_session(self, entity_id: int) -> dict:
        return self._request("DELETE", f"/api/external/v2/sessions/delete/{entity_id}")

    # users (докладчики)
    def create_users(self, body: dict) -> dict:
        return self._create(
            "/api/external/v2/users/create",
            body,
            update=lambda b: self.update_users(b),
        )

    def update_users(self, body: dict) -> dict:
        return self._request("PATCH", "/api/external/v2/users/update", body)

    def delete_users(self, user_ids: list[int]) -> dict:
        return self._request("DELETE", "/api/external/v2/users/delete", {"userIds": user_ids})
