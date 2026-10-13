"""Локальное состояние синхронизации: три вида состояния по ТЗ.

1. Desired state - нормализованная выгрузка Comdi (актуальные данные источника)
2. Applied state - последнее подтверждённое состояние в Eventicious
3. Execution state - план выполнения операций с их статусами
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCHEMA = """
-- Applied state: последнее подтверждённое состояние в Eventicious
CREATE TABLE IF NOT EXISTS objects (
    entity     TEXT NOT NULL,   -- 'location' | 'tag' | 'session' | 'user'
    source_id  INTEGER NOT NULL,-- id в Comdi
    target_id  INTEGER NOT NULL,-- id, который мы передали в Eventicious
    content_hash TEXT NOT NULL, -- хэш последнего успешно отправленного состояния
    content_payload TEXT,       -- полный payload для планирования переносов
    en_hash TEXT,               -- хэш отправленной EN-версии (NULL = ещё не ушла)
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (entity, source_id)
);

-- Desired state: нормализованная выгрузка Comdi
CREATE TABLE IF NOT EXISTS desired_state (
    run_id TEXT NOT NULL,
    entity TEXT NOT NULL,
    source_id INTEGER NOT NULL,
    payload TEXT NOT NULL,  -- полный JSON payload
    received_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (run_id, entity, source_id)
);

-- Execution state: план выполнения операций
CREATE TABLE IF NOT EXISTS execution_plan (
    run_id TEXT NOT NULL,
    operation TEXT NOT NULL,  -- 'create' | 'update' | 'delete' | 'move' | 'block' | 'reactivate'
    entity TEXT NOT NULL,
    source_id INTEGER NOT NULL,
    target_id INTEGER,
    payload TEXT NOT NULL,
    dependencies TEXT,  -- JSON массив зависимых operation_id
    status TEXT NOT NULL DEFAULT 'pending',  -- 'pending' | 'running' | 'succeeded' | 'failed' | 'unknown'
    error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT,
    PRIMARY KEY (run_id, operation, entity, source_id)
);

-- Временные залы для безопасного переноса сессий
CREATE TABLE IF NOT EXISTS temporary_halls (
    hall_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    in_use BOOLEAN NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (hall_id)
);

-- Журнал синхронизаций
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
        """Миграция существующей схемы на новую версию."""
        # Миграция v1 -> v2: добавление en_hash
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(objects)")}
        if "en_hash" not in cols:
            self._conn.execute("ALTER TABLE objects ADD COLUMN en_hash TEXT")
        # Миграция: полный payload для планирования переносов (занятость залов)
        if "content_payload" not in cols:
            self._conn.execute("ALTER TABLE objects ADD COLUMN content_payload TEXT")
        # Миграция: флаг подтверждённой блокировки (ТЗ 9 — повтор отсутствия не повторяет block)
        if "blocked" not in cols:
            self._conn.execute("ALTER TABLE objects ADD COLUMN blocked INTEGER NOT NULL DEFAULT 0")
        self._conn.commit()
        # Миграция v2 -> v3: новые таблицы
        tables = {r["name"] for r in self._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "desired_state" not in tables:
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS desired_state (
                    run_id TEXT NOT NULL,
                    entity TEXT NOT NULL,
                    source_id INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    received_at TEXT NOT NULL DEFAULT (datetime('now')),
                    PRIMARY KEY (run_id, entity, source_id)
                );
            """)
        if "execution_plan" not in tables:
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS execution_plan (
                    run_id TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    entity TEXT NOT NULL,
                    source_id INTEGER NOT NULL,
                    target_id INTEGER,
                    payload TEXT NOT NULL,
                    dependencies TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    error TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    completed_at TEXT,
                    PRIMARY KEY (run_id, operation, entity, source_id)
                );
            """)
        if "temporary_halls" not in tables:
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS temporary_halls (
                    hall_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    in_use BOOLEAN NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    PRIMARY KEY (hall_id)
                );
            """)

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

    def upsert(
        self,
        entity: str,
        source_id: int,
        target_id: int,
        payload_hash: str,
        payload: dict | None = None,
    ) -> None:
        """Подтвердить применённое состояние.

        `payload` — полный последний подтверждённый payload: он нужен, чтобы
        моделировать занятость залов при планировании переносов. Если payload
        не передан, прежнее значение сохраняется (не затирается пустым).
        """
        self._conn.execute(
            """
            INSERT INTO objects(entity, source_id, target_id, content_hash, content_payload, updated_at)
            VALUES(?,?,?,?,?,datetime('now'))
            ON CONFLICT(entity, source_id) DO UPDATE SET
                target_id=excluded.target_id,
                content_hash=excluded.content_hash,
                content_payload=COALESCE(excluded.content_payload, objects.content_payload),
                updated_at=datetime('now')
            """,
            (
                entity,
                source_id,
                target_id,
                payload_hash,
                json.dumps(payload, ensure_ascii=False) if payload is not None else None,
            ),
        )
        self._conn.commit()

    def get_payload(self, entity: str, source_id: int) -> dict | None:
        """Полный подтверждённый payload объекта (None — не хранится/нет записи)."""
        row = self.get(entity, source_id)
        if row is None or row["content_payload"] is None:
            return None
        return json.loads(row["content_payload"])

    def list_payloads(self, entity: str) -> dict[int, dict]:
        """{source_id: payload} всех подтверждённых объектов данного типа."""
        cur = self._conn.execute(
            "SELECT source_id, content_payload FROM objects "
            "WHERE entity=? AND content_payload IS NOT NULL",
            (entity,),
        )
        return {int(r["source_id"]): json.loads(r["content_payload"]) for r in cur.fetchall()}

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

    def is_blocked(self, entity: str, source_id: int) -> bool:
        """Подтверждённо заблокирован ли объект (ТЗ 9: повтор отсутствия не шлёт block повторно)."""
        row = self.get(entity, source_id)
        return bool(row and row["blocked"])

    def set_blocked(self, entity: str, source_id: int, value: bool) -> None:
        self._conn.execute(
            "UPDATE objects SET blocked=?, updated_at=datetime('now') "
            "WHERE entity=? AND source_id=?",
            (1 if value else 0, entity, source_id),
        )
        self._conn.commit()

    def log(self, status: str, detail: str = "") -> None:
        self._conn.execute(
            "INSERT INTO sync_log(status, detail) VALUES(?,?)", (status, detail[:4000])
        )
        self._conn.commit()

    def last_alert_at(self, kind: str) -> str | None:
        """Когда последний раз уходило уведомление данного вида (UTC, ISO) или None."""
        cur = self._conn.execute(
            "SELECT MAX(started_at) FROM sync_log WHERE status=?", (f"alert:{kind}",)
        )
        row = cur.fetchone()
        return str(row[0]) if row and row[0] else None

    def mark_alert(self, kind: str) -> None:
        self.log(f"alert:{kind}")

    # ---------- Desired state (актуальная выгрузка Comdi) ----------
    
    def save_desired_state(self, run_id: str, entity: str, source_id: int, payload: dict) -> None:
        """Сохранить объект из новой выгрузки Comdi."""
        self._conn.execute(
            """
            INSERT OR REPLACE INTO desired_state(run_id, entity, source_id, payload, received_at)
            VALUES(?,?,?,?,datetime('now'))
            """,
            (run_id, entity, source_id, json.dumps(payload, ensure_ascii=False)),
        )
        self._conn.commit()
    
    def get_desired_state(self, run_id: str, entity: str) -> dict[int, dict]:
        """Получить все объекты данного типа из выгрузки."""
        cur = self._conn.execute(
            "SELECT source_id, payload FROM desired_state WHERE run_id=? AND entity=?",
            (run_id, entity)
        )
        return {int(r["source_id"]): json.loads(r["payload"]) for r in cur.fetchall()}
    
    def clear_desired_state(self, run_id: str) -> None:
        """Очистить desired state для прогона."""
        self._conn.execute("DELETE FROM desired_state WHERE run_id=?", (run_id,))
        self._conn.commit()

    # ---------- Execution state (план операций) ----------
    
    def create_execution_plan(self, run_id: str) -> None:
        """Создать новый план выполнения."""
        self._conn.execute("DELETE FROM execution_plan WHERE run_id=?", (run_id,))
        self._conn.commit()
    
    def add_operation(
        self,
        run_id: str,
        operation: str,
        entity: str,
        source_id: int,
        target_id: int | None,
        payload: dict,
        dependencies: list[int] | None = None
    ) -> int:
        """Добавить операцию в план. Возвращает operation_id."""
        op_id = hash(f"{run_id}{operation}{entity}{source_id}") % 1000000000
        self._conn.execute(
            """
            INSERT INTO execution_plan(run_id, operation, entity, source_id, target_id, payload, dependencies, status, created_at)
            VALUES(?,?,?,?,?,?,?,?,?,'pending',datetime('now'))
            """,
            (run_id, operation, entity, source_id, target_id, json.dumps(payload, ensure_ascii=False),
             json.dumps(dependencies) if dependencies else None),
        )
        self._conn.commit()
        return op_id
    
    def update_operation_status(self, run_id: str, operation: str, entity: str, source_id: int, 
                               status: str, error: str | None = None) -> None:
        """Обновить статус операции."""
        completed_at = "datetime('now')" if status in ('succeeded', 'failed', 'unknown') else None
        self._conn.execute(
            """
            UPDATE execution_plan 
            SET status=?, error=?, completed_at=?
            WHERE run_id=? AND operation=? AND entity=? AND source_id=?
            """,
            (status, error, completed_at, run_id, operation, entity, source_id),
        )
        self._conn.commit()
    
    def get_operation_status(self, run_id: str, operation: str, entity: str, source_id: int) -> str:
        """Получить статус операции."""
        cur = self._conn.execute(
            "SELECT status FROM execution_plan WHERE run_id=? AND operation=? AND entity=? AND source_id=?",
            (run_id, operation, entity, source_id)
        )
        row = cur.fetchone()
        return row["status"] if row else "pending"
    
    def get_running_operations(self, run_id: str) -> list:
        """Получить незавершённые операции плана."""
        cur = self._conn.execute(
            "SELECT * FROM execution_plan WHERE run_id=? AND status IN ('pending', 'running')",
            (run_id,)
        )
        return [dict(r) for r in cur.fetchall()]
    
    def clear_execution_plan(self, run_id: str) -> None:
        """Очистить план выполнения."""
        self._conn.execute("DELETE FROM execution_plan WHERE run_id=?", (run_id,))
        self._conn.commit()

    # ---------- Temporary halls (временные залы для переноса) ----------
    
    def create_temporary_hall(self, hall_id: int, name: str) -> None:
        """Создать или получить временный зал."""
        self._conn.execute(
            "INSERT OR IGNORE INTO temporary_halls(hall_id, name, in_use, created_at) VALUES(?,?,0,datetime('now'))",
            (hall_id, name),
        )
        self._conn.commit()
    
    def mark_temporary_hall_in_use(self, hall_id: int, in_use: bool = True) -> None:
        """Отметить временный зал как используемый или свободный."""
        self._conn.execute(
            "UPDATE temporary_halls SET in_use=? WHERE hall_id=?",
            (in_use, hall_id),
        )
        self._conn.commit()
    
    def get_temporary_hall(self, hall_id: int) -> dict | None:
        """Получить информацию о временном зале."""
        cur = self._conn.execute("SELECT * FROM temporary_halls WHERE hall_id=?", (hall_id,))
        row = cur.fetchone()
        return dict(row) if row else None

    def list_temporary_halls(self) -> list[dict]:
        """Все известные временные залы."""
        cur = self._conn.execute("SELECT * FROM temporary_halls ORDER BY hall_id")
        return [dict(r) for r in cur.fetchall()]
    
    def cleanup_temporary_halls(self) -> list[int]:
        """Удалить неиспользуемые временные залы. Возвращает список ID удалённых."""
        cur = self._conn.execute("SELECT hall_id FROM temporary_halls WHERE in_use=0")
        to_delete = [r[0] for r in cur.fetchall()]
        for hall_id in to_delete:
            self._conn.execute("DELETE FROM temporary_halls WHERE hall_id=?", (hall_id,))
        self._conn.commit()
        return to_delete

    def cleanup_temporary_halls_id(self, hall_id: int) -> None:
        """Забыть один временный зал после его удаления через API."""
        self._conn.execute("DELETE FROM temporary_halls WHERE hall_id=?", (hall_id,))
        self._conn.commit()
