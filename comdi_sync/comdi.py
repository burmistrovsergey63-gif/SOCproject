"""Клиент Comdi: забирает «боевую программу» по API и нормализует её.

Исходный payload (~1 МБ) содержит много лишнего — оставляем только то,
что нужно для расписания: залы (stream), эпизоды (episode), теги.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import requests

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Tag:
    source_id: int
    title_ru: str
    title_en: str
    color: str = ""


@dataclass(frozen=True)
class Episode:
    source_id: int
    stream_source_id: int
    name_ru: str
    name_en: str
    info_ru: str
    info_en: str
    start_utc: datetime | None
    stop_utc: datetime | None
    tag_ids: tuple[int, ...] = ()
    speaker_ids: tuple[int, ...] = ()
    hidden: bool = False
    color: str = ""


@dataclass(frozen=True)
class Speaker:
    source_id: int
    name_ru: str
    name_en: str
    lastname_ru: str
    lastname_en: str
    patronymic_ru: str
    patronymic_en: str
    company_ru: str
    company_en: str
    regalia_ru: str
    regalia_en: str
    description_ru: str
    description_en: str
    biography_ru: str
    biography_en: str
    link_telegram: str | None
    link_vk: str | None
    link_dzen: str | None
    photo: str
    photo_orig: str
    hide: bool = False


@dataclass(frozen=True)
class Hall:
    source_id: int
    description_ru: str
    description_en: str
    order: int
    episodes: tuple[Episode, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Schedule:
    event_name_ru: str
    event_name_en: str
    halls: tuple[Hall, ...]
    tags: tuple[Tag, ...]
    speakers: tuple[Speaker, ...]

    @property
    def all_episodes(self) -> list[Episode]:
        return [ep for hall in self.halls for ep in hall.episodes]


def _ts(value: Any) -> datetime | None:
    """Unix-seconds (UTC) -> aware datetime. Некорректные значения -> None."""
    if value in (None, "", 0):
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def _as_list(value: Any) -> list:
    if isinstance(value, list):
        return value
    return []


class ComdiClient:
    def __init__(self, url: str, timeout: int = 30, max_retries: int = 4) -> None:
        self.url = url
        self.timeout = timeout
        self.max_retries = max_retries
        self._session = requests.Session()
        self._session.headers.update(
            {"Accept": "application/json", "User-Agent": "comdi-eventicious-sync/1.0"}
        )

    def fetch_raw(self) -> dict:
        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self._session.get(self.url, timeout=self.timeout)
                resp.raise_for_status()
                return resp.json()
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                log.warning(
                    "Comdi: попытка %d/%d не удалась: %s",
                    attempt,
                    self.max_retries,
                    exc,
                )
        raise RuntimeError(f"Comdi недоступен после {self.max_retries} попыток") from last_error

    def fetch_schedule(self) -> Schedule:
        return normalize(self.fetch_raw())


def normalize(payload: dict) -> Schedule:
    """Превращает сырой ответ Comdi в нормализованное расписание.

    ВАЖНО: у части залов `episode` приходит пустым массивом — это норма,
    зал всё равно должен попасть в систему.
    """
    tags_by_id: dict[int, Tag] = {}
    halls: list[Hall] = []
    speakers: list[Speaker] = []

    # Нормализация спикеров из members
    for member in _as_list(payload.get("members")):
        member_id = member.get("id")
        if member_id is None:
            continue
        
        speakers.append(
            Speaker(
                source_id=int(member_id),
                name_ru=member.get("nameRu") or "",
                name_en=member.get("nameEn") or "",
                lastname_ru=member.get("lastnameRu") or "",
                lastname_en=member.get("lastnameEn") or "",
                patronymic_ru=member.get("patronymicRu") or "",
                patronymic_en=member.get("patronymicEn") or "",
                company_ru=member.get("companyRu") or "",
                company_en=member.get("companyEn") or "",
                regalia_ru=member.get("regaliaRu") or "",
                regalia_en=member.get("regaliaEn") or "",
                description_ru=member.get("descriptionRu") or "",
                description_en=member.get("descriptionEn") or "",
                biography_ru=member.get("biographyRu") or "",
                biography_en=member.get("biographyEn") or "",
                link_telegram=member.get("linkTelegram"),
                link_vk=member.get("linkVk"),
                link_dzen=member.get("linkDzen"),
                photo=member.get("photo") or "",
                photo_orig=member.get("photoOrig") or "",
                hide=bool(member.get("hide", False)),
            )
        )

    for order, stream in enumerate(_as_list(payload.get("stream"))):
        stream_id = stream.get("id")
        if stream_id is None:
            log.warning("Зал без id пропущен: %r", stream.get("description_ru"))
            continue

        episodes: list[Episode] = []
        for ep in _as_list(stream.get("episode")):
            ep_id = ep.get("id")
            if ep_id is None:
                log.warning("Эпизод без id пропущен в зале %s", stream_id)
                continue

            tag_ids: list[int] = []
            for tag in _as_list(ep.get("tags")):
                tag_id = tag.get("id")
                if tag_id is None:
                    continue
                tag_ids.append(int(tag_id))
                tags_by_id.setdefault(
                    int(tag_id),
                    Tag(
                        source_id=int(tag_id),
                        title_ru=tag.get("titleRu") or "",
                        title_en=tag.get("titleEn") or "",
                        color=tag.get("color") or "",
                    ),
                )

            speaker_ids = [
                int(s["id"]) if isinstance(s, dict) else int(s)
                for s in _as_list(ep.get("speakers"))
                if (isinstance(s, dict) and s.get("id") is not None) or isinstance(s, int)
            ]
            # Основная привязка спикеров в Comdi — через groups с ролями
            # («Спикер», «Модератор»); поле speakers у эпизодов часто пустое.
            if not speaker_ids:
                seen: set[int] = set()
                for group in _as_list(ep.get("groups")):
                    role = (group.get("role") or {}).get("titleRu") or ""
                    if role and role != "Спикер":
                        continue  # модераторы и прочие роли — не спикеры
                    for gm in _as_list(group.get("members")):
                        member = gm.get("member") if isinstance(gm, dict) else None
                        mid = member.get("id") if isinstance(member, dict) else None
                        if mid is None and isinstance(gm, dict):
                            mid = gm.get("id")
                        if mid is not None and int(mid) not in seen:
                            seen.add(int(mid))
                            speaker_ids.append(int(mid))

            episodes.append(
                Episode(
                    source_id=int(ep_id),
                    stream_source_id=int(stream_id),
                    name_ru=ep.get("name_ru") or "",
                    name_en=ep.get("name_en") or "",
                    info_ru=ep.get("info_ru") or "",
                    info_en=ep.get("info_en") or "",
                    start_utc=_ts(ep.get("timeStart")),
                    stop_utc=_ts(ep.get("timeStop")),
                    tag_ids=tuple(tag_ids),
                    speaker_ids=tuple(speaker_ids),
                    hidden=bool(ep.get("hidden")),
                    color=ep.get("color") or "",
                )
            )

        episodes.sort(key=lambda e: (e.start_utc or datetime.max.replace(tzinfo=timezone.utc), e.source_id))
        halls.append(
            Hall(
                source_id=int(stream_id),
                description_ru=stream.get("description_ru") or "",
                description_en=stream.get("description_en") or "",
                order=order,
                episodes=tuple(episodes),
            )
        )

    log.info(
        "Comdi: залов=%d, эпизодов=%d, тегов=%d, спикеров=%d",
        len(halls),
        sum(len(h.episodes) for h in halls),
        len(tags_by_id),
        len(speakers),
    )
    return Schedule(
        event_name_ru=payload.get("name_ru") or "",
        event_name_en=payload.get("name_en") or "",
        halls=tuple(halls),
        tags=tuple(sorted(tags_by_id.values(), key=lambda t: t.source_id)),
        speakers=tuple(speakers),
    )
