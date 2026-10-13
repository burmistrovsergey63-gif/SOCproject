"""Модель занятости залов для безопасных переносов сессий (Часть 2 ТЗ).

Eventicious запрещает два события в одном зале одновременно (409). Простой
update с новым залом падает, если целевой слот занят — например, при обмене
двух докладов местами. Поэтому переносы планируются по модели занятости,
построенной из applied state (последние подтверждённые payload'ы), а не по
ответам API.

Интервалы трактуются как [start, end): соприкосновение концом и началом
пересечением не является. Время сравнивается строками формата
"YYYY-MM-DDTHH-MM" (локальное EVENT_TIMEZONE) — лексикографическое сравнение
эквивалентно хронологическому для этого формата.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Placement:
    """Фактическое размещение сессии: зал + интервал."""

    location_id: int | None
    start: str | None
    end: str | None

    @classmethod
    def from_payload(cls, payload: dict) -> "Placement":
        """Размещение из payload'а сессии (locationsIds, startTime, endTime)."""
        locations = payload.get("locationsIds") or []
        return cls(
            location_id=locations[0] if locations else None,
            start=payload.get("startTime"),
            end=payload.get("endTime"),
        )

    @property
    def has_time(self) -> bool:
        return self.start is not None and self.end is not None


def overlaps(a: Placement, b: Placement) -> bool:
    """Пересекаются ли два размещения [start, end) в одном зале."""
    if a.location_id is None or a.location_id != b.location_id:
        return False
    if not a.has_time or not b.has_time:
        return False  # сессии без времени слоты не занимают
    return a.start < b.end and b.start < a.end


class Occupancy:
    """Модель текущей занятости: {source_id сессии: размещение}.

    Строится из applied state; обновляется после каждого подтверждённого
    переноса, чтобы следующая операция видела актуальную картину.
    """

    def __init__(self) -> None:
        self._sessions: dict[int, Placement] = {}

    def place(self, source_id: int, placement: Placement) -> None:
        self._sessions[source_id] = placement

    def remove(self, source_id: int) -> None:
        self._sessions.pop(source_id, None)

    def placement_of(self, source_id: int) -> Placement | None:
        return self._sessions.get(source_id)

    def occupant(self, placement: Placement, exclude: set[int] | None = None) -> int | None:
        """source_id сессии, занимающей слот, или None если свободен.

        При проверке назначения саму переносимую сессию исключаем.
        """
        exclude = exclude or set()
        for source_id, other in self._sessions.items():
            if source_id in exclude:
                continue
            if overlaps(placement, other):
                return source_id
        return None

    def is_free(self, placement: Placement, exclude: set[int] | None = None) -> bool:
        return self.occupant(placement, exclude) is None

    def sessions_in(self, location_id: int) -> set[int]:
        """Сессии, фактически находящиеся в данном зале."""
        return {
            source_id
            for source_id, pl in self._sessions.items()
            if pl.location_id == location_id
        }
