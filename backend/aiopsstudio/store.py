"""Durable JSON documents and the database-owned singleton execution lease."""
from __future__ import annotations

import copy
import base64
import hashlib
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Callable, Any

from .compat import TABLE_SUFFIXES, TABLE_PREFIX, LEGACY_TABLE_PREFIX, table_namespace


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class StoreConflict(Exception):
    pass


class LeaseLost(StoreConflict):
    """A previous execution epoch must never publish late results."""


EXECUTION_LEASE = ContextVar("aiopsstudio_execution_lease", default=None)


class Store:
    def __init__(self, database_url: str, lease_seconds: int = 45):
        self.database_url = database_url
        self.lease_seconds = lease_seconds
        self.sqlite = database_url.startswith("sqlite:")
        self._lock = threading.RLock()
        self._depth = 0
        self._rollback_actions = []
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
        if self.sqlite:
            columns = {r[1] for r in self._execute(f"PRAGMA table_info({self._worker_slot})")}
        else:
            columns = {r[0] for r in self._execute("SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name=?", (self._worker_slot,))}
        if "generation" not in columns:
            self._execute(f"ALTER TABLE {self._worker_slot} ADD COLUMN generation INTEGER NOT NULL DEFAULT 0")
        owner = "COALESCE(NULLIF(json_extract(data,'$.owner_id'),''),'admin')" if self.sqlite else "COALESCE(NULLIF(data->>'owner_id',''),'admin')"
        created = "COALESCE(json_extract(data,'$.created_at'),'')" if self.sqlite else "COALESCE(data->>'created_at','')"
        self._execute(f"CREATE INDEX IF NOT EXISTS {self.namespace}_documents_owner_created_m2 ON {self._documents} (kind,({owner}),({created}),id)")
        # Schema metadata is separate from business records; old rows stay intact.
        if self.get("schema_migration", "m2-storage-1") is None:
            self.save("schema_migration", "m2-storage-1", {"version": 1, "changes": ["worker_generation", "owner_created_index"]})

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
                self.assert_execution()
                return callback()
            self._execute("BEGIN IMMEDIATE" if self.sqlite else "BEGIN")
            self._depth += 1
            self._rollback_actions = []
            try:
                if not self.sqlite:
                    self._execute("SELECT pg_advisory_xact_lock(784302116)")
                self.assert_execution()
                result = callback()
                self.assert_execution()
                self._execute("COMMIT")
                return result
            except BaseException:
                self._execute("ROLLBACK")
                for undo in reversed(self._rollback_actions):
                    try:
                        undo()
                    except OSError:
                        pass
                raise
            finally:
                self._rollback_actions = []
                self._depth -= 1

    def on_rollback(self, callback):
        if not self._depth:
            raise RuntimeError("文件回滚动作必须在事务中登记")
        self._rollback_actions.append(callback)

    def get(self, kind: str, id: str) -> dict | None:
        with self._lock:
            row = self._execute(f"SELECT data FROM {self._documents} WHERE kind=? AND id=?", (kind, id)).fetchone()
            return self._unpack(row[0]) if row else None

    def list(self, kind: str) -> list[dict]:
        with self._lock:
            rows = self._execute(f"SELECT data FROM {self._documents} WHERE kind=? ORDER BY id", (kind,)).fetchall()
            return [self._unpack(row[0]) for row in rows]

    @contextmanager
    def execution_scope(self, run):
        """Only claimed workers are fenced; ordinary HTTP changes are separate."""
        binding = (self, run["id"], run.get("lease_owner"), run.get("lease_generation")) if run.get("lease_generation") is not None else None
        token = EXECUTION_LEASE.set(binding)
        try:
            self.assert_execution()
            yield
        finally:
            EXECUTION_LEASE.reset(token)

    def assert_execution(self):
        bound = EXECUTION_LEASE.get()
        if not bound or bound[0] is not self:
            return
        row = self._execute(f"SELECT run_id,owner,expires_at,generation FROM {self._worker_slot} WHERE id=1").fetchone()
        if not row or row[0] != bound[1] or row[1] != bound[2] or row[3] != bound[3] or row[2] <= time.time():
            raise LeaseLost("执行租约或代次已失效，拒绝保存旧执行结果")

    def query_documents(self, kind, *, owner_id=None, search="", status=None, cursor=None, limit=50,
                        archived=None, include_deleted=False, created_from=None, created_to=None,
                        conversation_id=None, parent_run_id=None):
        """Bounded SQL filtering; cursors cannot change scope or broaden owner."""
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError("分页数量须为1至100")
        if not isinstance(search, str) or len(search) > 200:
            raise ValueError("搜索条件过长")
        def instant(value):
            if value is None:
                return None
            try:
                parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    raise ValueError()
                return parsed.astimezone(timezone.utc).isoformat()
            except (ValueError, TypeError):
                raise ValueError("筛选时间须为带时区的ISO绝对时间") from None
        created_from, created_to = instant(created_from), instant(created_to)
        if created_from and created_to and created_from > created_to:
            raise ValueError("筛选开始时间晚于结束时间")
        conditions = {"kind": kind, "owner_id": owner_id, "search": search, "status": status,
            "archived": archived, "include_deleted": include_deleted, "created_from": created_from,
            "created_to": created_to, "conversation_id": conversation_id, "parent_run_id": parent_run_id}
        fingerprint = hashlib.sha256(json.dumps(conditions, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        state = None
        if cursor:
            try:
                state = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
                if set(state) != {"v", "scope", "last", "ceiling"} or state["v"] != 1 or state["scope"] != fingerprint:
                    raise ValueError()
                if any(not isinstance(pair, list) or len(pair) != 2 or not all(isinstance(v, str) for v in pair) for pair in (state["last"], state["ceiling"])):
                    raise ValueError()
            except Exception:
                raise ValueError("分页游标无效或不属于当前查询") from None
        def field(name):
            return f"json_extract(data,'$.{name}')" if self.sqlite else f"data->>'{name}'"
        created = f"COALESCE({field('created_at')},'')"
        where, args = ["kind=?"], [kind]
        if owner_id is not None:
            where.append(f"COALESCE(NULLIF({field('owner_id')},''),'admin')=?"); args.append(owner_id)
        if not include_deleted:
            where.append(f"COALESCE({field('deleted_at')},'')=''")
        if archived is not None:
            where.append(f"COALESCE({field('archived_at')},'')" + ("<>''" if archived else "=''"))
        for name, value in (("status", status), ("conversation_id", conversation_id), ("parent_run_id", parent_run_id)):
            if value is not None:
                selected = f"COALESCE({field('conversation_id')},{field('thread_id')})" if name == "conversation_id" and kind == "run" else field(name)
                where.append(f"{selected}=?"); args.append(value)
        if created_from:
            where.append(f"julianday({created})>=julianday(?)" if self.sqlite else f"CAST(NULLIF({created},'') AS TIMESTAMPTZ)>=CAST(? AS TIMESTAMPTZ)"); args.append(created_from)
        if created_to:
            where.append(f"julianday({created})<=julianday(?)" if self.sqlite else f"CAST(NULLIF({created},'') AS TIMESTAMPTZ)<=CAST(? AS TIMESTAMPTZ)"); args.append(created_to)
        if search:
            needle = "%" + search.lower().replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"
            fields = "id || ' ' || " + " || ' ' || ".join(f"COALESCE({field(k)},'')" for k in ("title", "name", "display_name", "description"))
            where.append(f"LOWER({fields}) LIKE ? ESCAPE '!'"); args.append(needle)
        if state:
            for pair, operator in ((state["last"], "<"), (state["ceiling"], "<=")):
                where.append(f"({created},id) {operator} (?,?)"); args.extend(pair)
        with self._lock:
            rows = self._execute(f"SELECT data FROM {self._documents} WHERE {' AND '.join(where)} ORDER BY {created} DESC,id DESC LIMIT ?", (*args, limit + 1)).fetchall()
        items = [self._unpack(r[0]) for r in rows[:limit]]
        ceiling = state["ceiling"] if state else [items[0].get("created_at", ""), items[0]["id"]] if items else ["", ""]
        def position(item):
            value = {"v": 1, "scope": fingerprint, "ceiling": ceiling, "last": [item.get("created_at", ""), item["id"]]}
            return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).decode().rstrip("=")
        positions = [position(item) for item in items]
        return {"items": items, "next_cursor": positions[-1] if len(rows) > limit else None, "item_cursors": positions}

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
            sql = f"SELECT owner,run_id,expires_at,generation FROM {self._worker_slot} WHERE id=1"
            slot = self._execute(sql if self.sqlite else sql + " FOR UPDATE").fetchone()
            now = time.time()
            if slot[1] and slot[2] > now:
                # The owner still finishes conversation/artifact delivery after
                # setting the run's terminal state. Only its release (or expiry)
                # may transfer this epoch to the next job.
                return None
            if slot[1] and slot[2] <= now:
                interrupted = self.get("run", slot[1])
                if interrupted and interrupted.get("status") == "running":
                    interrupted.update(status="failed", error={"code": "execution_interrupted", "detail": "执行租约已过期，结果未知，请显式重试"})
                    interrupted["completed_at"] = utcnow()
                    for invocation in self.list("invocation"):
                        if invocation.get("run_id") == interrupted["id"] and invocation.get("status") == "running":
                            invocation.update(status="cancelled", completed_at=utcnow(), error={"code": "execution_interrupted", "message": "旧执行器结果未知，未自动重放"})
                            dispatched = interrupted.get("usage", {}).get("model_http_request_count", 0)
                            if invocation.get("source") == "model" and dispatched > invocation.get("model_http_requests_before", 0):
                                interrupted["usage"]["token_usage_known"] = False
                            self.save("invocation", invocation["id"], invocation)
                    self.save("run", interrupted["id"], interrupted)
                    self.event(interrupted["id"], "failed", interrupted["error"])
            queued = sorted((r for r in self.list("run") if r.get("status") == "queued"), key=lambda r: (r.get("created_at", ""), r["id"]))
            if not queued:
                self._execute(f"UPDATE {self._worker_slot} SET owner=NULL,run_id=NULL,expires_at=0 WHERE id=1")
                return None
            run = queued[0]
            generation = slot[3] + 1
            run.update(status="running", lease_owner=owner, lease_generation=generation, lease_expires_at=now + self.lease_seconds)
            saved = self.save("run", run["id"], run)
            self._execute(f"UPDATE {self._worker_slot} SET owner=?,run_id=?,expires_at=?,generation=? WHERE id=1", (owner, run["id"], now + self.lease_seconds, generation))
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
