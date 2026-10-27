"""Explicit recovery helpers. No model or source replay occurs here."""
from __future__ import annotations

import re


async def purge_checkpoint(store, attempt_id):
    """Delete only a declared deleted/expired attempt's exact checkpoint thread."""
    if not isinstance(attempt_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", attempt_id):
        raise ValueError("检查点标识格式无效")
    run = next((r for r in store.list("run") if r.get("attempt_id") == attempt_id), None)
    if not run or not (run.get("deleted_at") or run.get("artifacts_expired_at")):
        raise ValueError("仅可回收已删除或已过期任务的检查点")
    if store.sqlite or store._execute("SELECT to_regclass('checkpoints')").fetchone()[0] is None:
        return {"deleted": False, "reason": "no_checkpoint_tables"}
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    async with AsyncPostgresSaver.from_conn_string(store.database_url) as saver:
        await saver.adelete_thread(attempt_id)
    return {"deleted": True}
