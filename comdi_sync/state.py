"""Локальное состояние синхронизации: маппинг source_id -> target_id + хэш содержимого.

Нужно, потому что в Eventicious нет GET для расписания/залов/тегов —
читать текущее состояние обратно невозможно. Всё, что мы создали, помним сами.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS objects (
    entity     TEXT NOT NULL,   -- 'location' | 'tag' | 'session' | 'user'
    source_id  INTEGER NOT NULL,-- id в Comdi
    target_id  INTEGER NOT NULL,-- id, который мы передали в Eventicious
    content_hash TEXT NOT NULL, -- хэш последнего успешно отправленного состояния
    en_hash TEXT,               -- хэш отправленной EN-версии (NULL = ещё не ушла)
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (entity, source_id)
);
CREATE TABLE IF NOT EXISTS sync_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    status TEXT NOT NULL,
    detail TEXT
);
"""


def content_hash(payload: dict[str, Any]) -> str:
    """Стабильный хэш payload — чтобы не слать update, если ничего не изменилось."""
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class State:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Добавляет колонки, появившиеся в более новых версиях схемы."""
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(objects)")}
        if "en_hash" not in cols:
            self._conn.execute("ALTER TABLE objects ADD COLUMN en_hash TEXT")

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "State":
        return self

    def __exit__(self, *exc: object) -> None:
        self._conn.commit()
        self.close()

    def get(self, entity: str, source_id: int) -> sqlite3.Row | None:
        cur = self._conn.execute(
            "SELECT * FROM objects WHERE entity=? AND source_id=?", (entity, source_id)
        )
        return cur.fetchone()

    def target_id(self, entity: str, source_id: int) -> int | None:
        row = self.get(entity, source_id)
        return int(row["target_id"]) if row else None

    def upsert(self, entity: str, source_id: int, target_id: int, payload_hash: str) -> None:
        self._conn.execute(
            """
            INSERT INTO objects(entity, source_id, target_id, content_hash, updated_at)
            VALUES(?,?,?,?,datetime('now'))
            ON CONFLICT(entity, source_id) DO UPDATE SET
                target_id=excluded.target_id,
                content_hash=excluded.content_hash,
                updated_at=datetime('now')
            """,
            (entity, source_id, target_id, payload_hash),
        )
        self._conn.commit()

    def delete(self, entity: str, source_id: int) -> None:
        self._conn.execute(
            "DELETE FROM objects WHERE entity=? AND source_id=?", (entity, source_id)
        )
        self._conn.commit()

    def list_ids(self, entity: str) -> dict[int, int]:
        """{source_id: target_id} для всех известных объектов данного типа."""
        cur = self._conn.execute(
            "SELECT source_id, target_id FROM objects WHERE entity=?", (entity,)
        )
        return {int(r["source_id"]): int(r["target_id"]) for r in cur.fetchall()}

    def list_hashes(self, entity: str) -> dict[int, str]:
        cur = self._conn.execute(
            "SELECT source_id, content_hash FROM objects WHERE entity=?", (entity,)
        )
        return {int(r["source_id"]): str(r["content_hash"]) for r in cur.fetchall()}

    def en_hash(self, entity: str, source_id: int) -> str | None:
        """Хэш последней успешно отправленной EN-версии (None — ещё не уходила)."""
        row = self.get(entity, source_id)
        if row is None or row["en_hash"] is None:
            return None
        return str(row["en_hash"])

    def mark_en_sent(self, entity: str, source_id: int, payload_hash: str) -> None:
        self._conn.execute(
            "UPDATE objects SET en_hash=?, updated_at=datetime('now') "
            "WHERE entity=? AND source_id=?",
            (payload_hash, entity, source_id),
        )
        self._conn.commit()

    def log(self, status: str, detail: str = "") -> None:
        self._conn.execute(
            "INSERT INTO sync_log(status, detail) VALUES(?,?)", (status, detail[:4000])
        )
        self._conn.commit()
