"""Durable JSON documents and the database-owned singleton execution lease."""
from __future__ import annotations

import copy
import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Any

from .compat import TABLE_SUFFIXES, TABLE_PREFIX, LEGACY_TABLE_PREFIX, table_namespace


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class StoreConflict(Exception):
    pass


class Store:
    def __init__(self, database_url: str, lease_seconds: int = 45):
        self.database_url = database_url
        self.lease_seconds = lease_seconds
        self.sqlite = database_url.startswith("sqlite:")
        self._lock = threading.RLock()
        self._depth = 0
        if self.sqlite:
            path = database_url.removeprefix("sqlite:///")
            self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=30)
            self.conn.execute("PRAGMA busy_timeout=30000")
            self.conn.execute("PRAGMA journal_mode=WAL")
        elif database_url.startswith(("postgresql://", "postgres://")):
            import psycopg
            self.conn = psycopg.connect(database_url, autocommit=True)
        else:
            raise ValueError("数据库地址必须使用 PostgreSQL；SQLite 仅用于测试")
        try:
            self.atomic(self._initialize_schema)
        except BaseException:
            self.conn.close()
            raise

    def _initialize_schema(self):
        names = [f"{prefix}_{suffix}" for prefix in (TABLE_PREFIX, LEGACY_TABLE_PREFIX) for suffix in TABLE_SUFFIXES]
        if self.sqlite:
            rows = self._execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            existing = {row[0] for row in rows}
        else:
            existing = {name for name in names if self._execute("SELECT to_regclass(?)", (name,)).fetchone()[0] is not None}
        self.namespace = table_namespace(existing)
        self._documents, self._events, self._worker_slot = [f"{self.namespace}_{suffix}" for suffix in TABLE_SUFFIXES]
        data_type = "TEXT" if self.sqlite else "JSONB"
        self._execute(f"CREATE TABLE IF NOT EXISTS {self._documents} (kind TEXT NOT NULL, id TEXT NOT NULL, data {data_type} NOT NULL, PRIMARY KEY(kind,id))")
        self._execute(f"CREATE TABLE IF NOT EXISTS {self._events} (run_id TEXT NOT NULL, seq INTEGER NOT NULL, data {data_type} NOT NULL, PRIMARY KEY(run_id,seq))")
        self._execute(f"CREATE TABLE IF NOT EXISTS {self._worker_slot} (id INTEGER PRIMARY KEY, owner TEXT, run_id TEXT, expires_at DOUBLE PRECISION NOT NULL DEFAULT 0)")
        self._execute(f"INSERT INTO {self._worker_slot}(id, expires_at) VALUES (1,0) ON CONFLICT(id) DO NOTHING")

    def _execute(self, sql: str, args=()):
        return self.conn.execute(sql if self.sqlite else sql.replace("?", "%s"), args)

    def _pack(self, value):
        if self.sqlite:
            return json.dumps(value, ensure_ascii=False, allow_nan=False)
        from psycopg.types.json import Jsonb
        return Jsonb(value)

    @staticmethod
    def _unpack(value):
        return json.loads(value) if isinstance(value, str) else copy.deepcopy(value)

    def atomic(self, callback: Callable[..., Any]):
        """Serialize mutations across processes. Callback accepts no arguments."""
        with self._lock:
            if self._depth:
                return callback()
            self._execute("BEGIN IMMEDIATE" if self.sqlite else "BEGIN")
            self._depth += 1
            try:
                if not self.sqlite:
                    self._execute("SELECT pg_advisory_xact_lock(784302116)")
                result = callback()
                self._execute("COMMIT")
                return result
            except BaseException:
                self._execute("ROLLBACK")
                raise
            finally:
                self._depth -= 1

    def get(self, kind: str, id: str) -> dict | None:
        with self._lock:
            row = self._execute(f"SELECT data FROM {self._documents} WHERE kind=? AND id=?", (kind, id)).fetchone()
            return self._unpack(row[0]) if row else None

    def list(self, kind: str) -> list[dict]:
        with self._lock:
            rows = self._execute(f"SELECT data FROM {self._documents} WHERE kind=? ORDER BY id", (kind,)).fetchall()
            return [self._unpack(row[0]) for row in rows]

    def save(self, kind: str, id: str, data: dict, expected_revision: int | None = None) -> dict:
        def write():
            previous = self.get(kind, id)
            revision = previous.get("revision", 0) if previous else 0
            if expected_revision is not None and expected_revision != revision:
                raise StoreConflict("配置已被其他页面修改，请重新读取后保存")
            value = copy.deepcopy(data)
            value.update(id=id, revision=revision + 1, updated_at=utcnow())
            value.setdefault("created_at", previous.get("created_at", utcnow()) if previous else utcnow())
            self._execute(f"INSERT INTO {self._documents}(kind,id,data) VALUES (?,?,?) ON CONFLICT(kind,id) DO UPDATE SET data=excluded.data", (kind, id, self._pack(value)))
            return copy.deepcopy(value)
        return self.atomic(write)

    def delete(self, kind: str, id: str):
        return self.atomic(lambda: self._execute(f"DELETE FROM {self._documents} WHERE kind=? AND id=?", (kind, id)))

    def event(self, run_id: str, type: str, payload: dict) -> dict:
        def write():
            last = self._execute(f"SELECT COALESCE(MAX(seq),0) FROM {self._events} WHERE run_id=?", (run_id,)).fetchone()[0]
            cursor = self.get("event_cursor", run_id) or {}
            seq = max(last, cursor.get("last_seq", 0)) + 1
            self.save("event_cursor", run_id, {"last_seq": seq})
            item = {"id": seq, "seq": seq, "run_id": run_id, "type": type, "payload": copy.deepcopy(payload), "created_at": utcnow()}
            self._execute(f"INSERT INTO {self._events}(run_id,seq,data) VALUES (?,?,?)", (run_id, seq, self._pack(item)))
            return item
        return self.atomic(write)

    def events(self, run_id: str, after: int = 0) -> list[dict]:
        with self._lock:
            return [self._unpack(row[0]) for row in self._execute(f"SELECT data FROM {self._events} WHERE run_id=? AND seq>? ORDER BY seq", (run_id, after)).fetchall()]

    def purge_events(self, run_id: str) -> None:
        """Remove expired payloads while retaining monotonic SSE cursor metadata."""
        def purge():
            last = self._execute(f"SELECT COALESCE(MAX(seq),0) FROM {self._events} WHERE run_id=?", (run_id,)).fetchone()[0]
            cursor = self.get("event_cursor", run_id) or {}
            self.save("event_cursor", run_id, {"last_seq": max(last, cursor.get("last_seq", 0))})
            self._execute(f"DELETE FROM {self._events} WHERE run_id=?", (run_id,))
        return self.atomic(purge)

    def claim_job(self, owner: str) -> dict | None:
        def claim():
            sql = f"SELECT owner,run_id,expires_at FROM {self._worker_slot} WHERE id=1"
            slot = self._execute(sql if self.sqlite else sql + " FOR UPDATE").fetchone()
            now = time.time()
            if slot[1] and slot[2] > now:
                run = self.get("run", slot[1])
                if run and run.get("status") == "running":
                    return None
            if slot[1] and slot[2] <= now:
                interrupted = self.get("run", slot[1])
                if interrupted and interrupted.get("status") == "running":
                    interrupted.update(status="failed", error={"code": "execution_interrupted", "detail": "执行租约已过期，结果未知，请显式重试"})
                    self.save("run", interrupted["id"], interrupted)
                    self.event(interrupted["id"], "failed", interrupted["error"])
            queued = sorted((r for r in self.list("run") if r.get("status") == "queued"), key=lambda r: (r.get("created_at", ""), r["id"]))
            if not queued:
                self._execute(f"UPDATE {self._worker_slot} SET owner=NULL,run_id=NULL,expires_at=0 WHERE id=1")
                return None
            run = queued[0]
            run.update(status="running", lease_owner=owner, lease_expires_at=now + self.lease_seconds)
            saved = self.save("run", run["id"], run)
            self._execute(f"UPDATE {self._worker_slot} SET owner=?,run_id=?,expires_at=? WHERE id=1", (owner, run["id"], now + self.lease_seconds))
            return saved
        return self.atomic(claim)

    def renew_job(self, id: str, owner: str) -> bool:
        def renew():
            cursor = self._execute(f"UPDATE {self._worker_slot} SET expires_at=? WHERE id=1 AND owner=? AND run_id=? AND expires_at>?", (time.time() + self.lease_seconds, owner, id, time.time()))
            return cursor.rowcount == 1
        return self.atomic(renew)

    def release_job(self, id: str, owner: str) -> bool:
        def release():
            cursor = self._execute(f"UPDATE {self._worker_slot} SET owner=NULL,run_id=NULL,expires_at=0 WHERE id=1 AND owner=? AND run_id=?", (owner, id))
            return cursor.rowcount == 1
        return self.atomic(release)

    def close(self):
        with self._lock:
            self.conn.close()
