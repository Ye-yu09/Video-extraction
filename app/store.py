"""SQLite 任务持久化：任务记录落盘，服务重启后自动恢复。"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from pathlib import Path

from .models import TaskRecord

logger = logging.getLogger(__name__)


class TaskStore:
    """极简 SQLite 存储：整条 TaskRecord 以 JSON 存单列，读时反序列化。"""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id   TEXT PRIMARY KEY,
                    payload   TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def upsert(self, rec: TaskRecord) -> None:
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO tasks (task_id, payload, updated_at) VALUES (?, ?, ?)",
                    (rec.task_id, rec.model_dump_json(), rec.updated_at),
                )

    def get(self, task_id: str) -> TaskRecord | None:
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT payload FROM tasks WHERE task_id = ?", (task_id,)
                ).fetchone()
        if not row:
            return None
        try:
            return TaskRecord.model_validate_json(row[0])
        except Exception:  # noqa: BLE001
            logger.warning("任务 %s 记录损坏，跳过恢复", task_id)
            return None

    def delete(self, task_id: str) -> None:
        with self._lock:
            with self._connect() as conn:
                conn.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))

    def load_all(self) -> dict[str, TaskRecord]:
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute("SELECT task_id, payload FROM tasks").fetchall()
        out: dict[str, TaskRecord] = {}
        for task_id, payload in rows:
            try:
                out[task_id] = TaskRecord.model_validate_json(payload)
            except Exception:  # noqa: BLE001
                logger.warning("任务 %s 记录损坏，跳过恢复", task_id)
        return out

    def count(self) -> int:
        with self._lock:
            with self._connect() as conn:
                return conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]