"""PostgreSQL checkpoints sharing the execution epoch boundary.

Synchronous transactions run in a worker thread. Holding an async PostgreSQL
lock while the event loop enters a synchronous Store transaction could otherwise
deadlock the loop; this bridge keeps the lock holder independently runnable.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
import time

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres import PostgresSaver, _internal
from psycopg.rows import dict_row

from .store import EXECUTION_LEASE, LeaseLost


class _FencedPostgresSaver(PostgresSaver):
    _initializing = False

    def setup(self):
        self._initializing = True
        try:
            return super().setup()
        finally:
            self._initializing = False

    @contextmanager
    def _cursor(self, *, pipeline=False):
        binding = EXECUTION_LEASE.get()
        if self._initializing or not binding:
            with super()._cursor(pipeline=pipeline) as cursor:
                yield cursor
            return
        with self.lock, _internal.get_connection(self.conn) as conn, conn.transaction():
            with conn.cursor(binary=True, row_factory=dict_row) as cursor:
                cursor.execute("SET LOCAL statement_timeout='5s'")
                cursor.execute("SELECT pg_advisory_xact_lock(784302116)")
                def check():
                    cursor.execute(f"SELECT owner,run_id,expires_at,generation FROM {binding[0]._worker_slot} WHERE id=1")
                    row = cursor.fetchone()
                    if (not row or row["owner"] != binding[2] or row["run_id"] != binding[1]
                            or row["generation"] != binding[3] or row["expires_at"] <= time.time()):
                        raise LeaseLost("执行代次已失效，拒绝保存旧检查点")
                check()
                yield cursor
                check()


class FencedAsyncPostgresSaver(BaseCheckpointSaver):
    def __init__(self, sync):
        super().__init__(serde=sync.serde)
        self.sync = sync
        self.pending = set()

    @classmethod
    @asynccontextmanager
    async def from_conn_string(cls, conn_string):
        context = _FencedPostgresSaver.from_conn_string(conn_string)
        sync = await asyncio.to_thread(context.__enter__)
        bridge = cls(sync)
        try:
            yield bridge
        finally:
            if bridge.pending:
                await asyncio.gather(*bridge.pending, return_exceptions=True)
            await asyncio.to_thread(context.__exit__, None, None, None)

    async def _run(self, method, *args, **kwargs):
        task = asyncio.create_task(asyncio.to_thread(method, *args, **kwargs))
        self.pending.add(task)
        try:
            return await asyncio.shield(task)
        finally:
            if task.done():
                self.pending.discard(task)

    async def setup(self):
        return await self._run(self.sync.setup)

    @property
    def config_specs(self):
        return self.sync.config_specs

    def get_next_version(self, current, channel):
        return self.sync.get_next_version(current, channel)

    async def aget_tuple(self, config):
        return await self._run(self.sync.get_tuple, config)

    async def alist(self, config, *, filter=None, before=None, limit=None):
        rows = await self._run(lambda: list(self.sync.list(config, filter=filter, before=before, limit=min(limit or 100, 100))))
        for row in rows:
            yield row

    async def aput(self, config, checkpoint, metadata, new_versions):
        return await self._run(self.sync.put, config, checkpoint, metadata, new_versions)

    async def aput_writes(self, config, writes, task_id, task_path=""):
        return await self._run(self.sync.put_writes, config, writes, task_id, task_path)

    async def adelete_thread(self, thread_id):
        return await self._run(self.sync.delete_thread, thread_id)
