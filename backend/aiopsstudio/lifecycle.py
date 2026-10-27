"""Explicit owner-scoped lifecycle operations; no implicit retention cleanup."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import secrets

from fastapi import HTTPException

from .identity import resource_owner
from .store import utcnow

TERMINAL = {"succeeded", "partial", "failed", "needs_input", "cancelled"}
EXPORT_LIMIT = 16 * 1024 * 1024
PREVIEW_SECONDS = 300


class LifecycleConflict(Exception):
    def __init__(self, detail, code):
        self.detail, self.code = detail, code
        super().__init__(detail)


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def documents(store, kind, *, owner_id=None, **filters):
    """Internal bounded database pages; never return an unfiltered user list."""
    cursor = None
    while True:
        page = store.query_documents(kind, owner_id=owner_id, limit=100, cursor=cursor, **filters)
        yield from page["items"]
        cursor = page.get("next_cursor")
        if not cursor:
            break


class LifecycleService:
    def __init__(self, app):
        self.app, self.store = app, app.state.store

    def require(self, kind, id, owner, *, deleted=False):
        record = self.store.get(kind, id)
        if not record or resource_owner(record) != owner or (record.get("deleted_at") and not deleted):
            raise HTTPException(404, "会话或附件不存在")
        if not record.get("deleted_at"):
            if kind == "conversation":
                self.app.state.conversations.require_owned(id, owner, allow_legacy_archive=True)
            else:
                self.app.state.attachments.require_owned(id, owner, allow_legacy_archive=True)
        return record

    def _mutable(self, kind, record, owner):
        from .legacy_archive import archived
        if archived(self.store, kind, record, owner):
            raise LifecycleConflict("旧版档案保持只读，不能修改原档案", "legacy_archive_readonly")

    def archive(self, id, owner, payload):
        if type(payload.get("archived")) is not bool or type(payload.get("expected_revision")) is not int:
            raise HTTPException(422, "归档操作需要 archived 和 expected_revision")
        def operation():
            record = self.require("conversation", id, owner)
            self._mutable("conversation", record, owner)
            if any(r.get("status") not in TERMINAL for r in self._runs(id, owner)):
                raise HTTPException(409, "运行中的会话不能归档")
            record.update(archived=payload["archived"], archived_at=utcnow() if payload["archived"] else None, lifecycle_version=2)
            return self.store.save("conversation", id, record, payload["expected_revision"])
        return self.store.atomic(operation)

    def _runs(self, conversation_id, owner):
        return list(documents(self.store, "run", owner_id=owner, conversation_id=conversation_id))

    def _scope(self, kind, id, owner):
        record = self.require(kind, id, owner)
        self._mutable(kind, record, owner)
        conversations, runs, attachments = [], [], []
        reasons = []
        if kind == "conversation":
            conversations = [record]
            runs = self._runs(id, owner)
            attachments = [a for a in documents(self.store, "attachment", owner_id=owner, conversation_id=id)]
            if any(r.get("status") not in TERMINAL for r in runs):
                reasons.append("active_run")
        else:
            attachments = [record]
        run_ids = {r["id"] for r in runs}
        attachment_ids = {a["id"] for a in attachments}
        evidence_ids = {e["id"] for r in runs for e in r.get("evidence", []) if e.get("id")}
        identifiers = run_ids | attachment_ids | evidence_ids
        inspected = []
        # References can exist outside the current conversation. Internal
        # scanning is authorized by the service, but no foreign metadata leaves
        # this boundary; users only receive a generic conflict category.
        for other in documents(self.store, "run"):
            if other["id"] in run_ids:
                continue
            refs = set(_strings({key: other.get(key) for key in (
                "parent_run_id", "history_context", "attachment_ids", "user_materials", "report", "report_attachment", "_request")}))
            if identifiers & refs:
                inspected.append(("run", other["id"], other.get("revision", 0)))
                reasons.append("retained_reference")
        for other in documents(self.store, "conversation"):
            if kind == "conversation" and other["id"] == id:
                continue
            if attachment_ids & set(_strings(other.get("messages", []))):
                inspected.append(("conversation", other["id"], other.get("revision", 0)))
                reasons.append("retained_reference")
        if kind == "attachment":
            conversation = self.app.state.conversations.require_owned(record["conversation_id"], owner, allow_legacy_archive=True)
            inspected.append(("conversation", conversation["id"], conversation.get("revision", 0)))
            if attachment_ids & set(_strings(conversation.get("messages", []))):
                reasons.append("retained_reference")
            if any(r.get("status") not in TERMINAL for r in self._runs(record["conversation_id"], owner)):
                reasons.append("active_run")
        plan = {"conversation": conversations, "run": runs, "attachment": attachments}
        fingerprint = hashlib.sha256(_canonical({"records": {
            k: [(r["id"], r.get("revision", 0), r.get("sha256")) for r in v] for k, v in plan.items()},
            "references": sorted(inspected), "reasons": sorted(set(reasons))}).encode()).hexdigest()
        return plan, sorted(set(reasons)), fingerprint

    def preview(self, kind, id, owner):
        def operation():
            plan, reasons, fingerprint = self._scope(kind, id, owner)
            expires = (datetime.now(timezone.utc) + timedelta(seconds=PREVIEW_SECONDS)).isoformat()
            token = None
            if not reasons:
                token = secrets.token_urlsafe(32)
                self.store.save("deletion_preview", hashlib.sha256(token.encode()).hexdigest(), {
                    "owner_id": owner, "target_kind": kind, "target_id": id, "fingerprint": fingerprint,
                    "expires_at": expires, "consumed": False})
            return {"preview_token": token, "expires_at": expires,
                "counts": {"conversations": len(plan["conversation"]), "runs": len(plan["run"]),
                           "attachments": len(plan["attachment"]), "attachment_bytes": sum(a.get("size", 0) for a in plan["attachment"])},
                "blocked": bool(reasons), "reasons": reasons,
                "notice": "确认后将删除这些私有记录和文件，已保留引用或运行中任务会阻止删除。"}
        return self.store.atomic(operation)

    def delete(self, kind, id, owner, payload):
        token = payload.get("preview_token")
        if not isinstance(token, str) or not 20 <= len(token) <= 200:
            raise HTTPException(422, "删除需要有效的预览确认 token")
        key = hashlib.sha256(token.encode()).hexdigest()
        def operation():
            ticket = self.store.get("deletion_preview", key)
            if not ticket or (ticket.get("owner_id"), ticket.get("target_kind"), ticket.get("target_id")) != (owner, kind, id):
                raise HTTPException(404, "删除预览不存在")
            target = self.require(kind, id, owner, deleted=True)
            if ticket.get("consumed"):
                if not target.get("deleted_at"):
                    raise HTTPException(409, "删除记录状态不一致")
                return self.store.get("lifecycle_cleanup", key), True
            if datetime.fromisoformat(ticket["expires_at"]) <= datetime.now(timezone.utc):
                raise HTTPException(409, "删除预览已过期，请重新预览")
            plan, reasons, fingerprint = self._scope(kind, id, owner)
            if reasons or fingerprint != ticket["fingerprint"]:
                raise HTTPException(409, "记录或引用已变化，请重新预览")
            stamp = utcnow()
            job = {"owner_id": owner, "target_kind": kind, "target_id": id, "status": "pending",
                   "attachments": [a["id"] for a in plan["attachment"]], "runs": [r["id"] for r in plan["run"]],
                   "evidence": [e["id"] for r in plan["run"] for e in r.get("evidence", []) if e.get("id")],
                   "attempts": [r["attempt_id"] for r in plan["run"] if r.get("attempt_id")],
                   "checkpoints_complete": self.store.sqlite or not plan["run"]}
            for attachment in plan["attachment"]:
                # Keep immutable byte metadata for idempotent private cleanup,
                # but user access is denied before paths or content are read.
                minimal = {k: attachment[k] for k in ("owner_id", "conversation_id", "kind", "run_id", "size", "sha256", "created_at") if k in attachment}
                minimal.update(deleted_at=stamp, lifecycle_version=2)
                self.store.save("attachment", attachment["id"], minimal, attachment["revision"])
            for run in plan["run"]:
                minimal = {k: run[k] for k in ("owner_id", "conversation_id", "thread_id", "attempt_id", "created_at", "status", "completed_at", "trust_version", "lease_owner", "lease_generation") if k in run}
                minimal.update(deleted_at=stamp, messages=[], inputs={}, user_materials=[], history_context={},
                    evidence=[{k: e[k] for k in ("id", "path", "sha256", "status") if k in e} for e in run.get("evidence", [])],
                    report={"expired": True, "summary": "此分析已由用户删除"})
                self.store.save("run", run["id"], minimal, run["revision"])
                self.store.purge_events(run["id"])
                for related_kind in ("invocation", "evidence", "material_read"):
                    for related in documents(self.store, related_kind, include_deleted=True):
                        if related.get("run_id") == run["id"] or related_kind == "evidence" and related["id"] in job["evidence"]:
                            keep = {k: related[k] for k in ("owner_id", "run_id", "access_scopes", "source", "created_at") if k in related}
                            keep.update(deleted_at=stamp, expired=True)
                            self.store.save(related_kind, related["id"], keep, related["revision"])
            for conversation in plan["conversation"]:
                minimal = {k: conversation[k] for k in ("owner_id", "created_at") if k in conversation}
                if conversation.get("client_request_id"):
                    minimal["client_request_sha256"] = hashlib.sha256(conversation["client_request_id"].encode()).hexdigest()
                minimal.update(deleted_at=stamp, lifecycle_version=2, title="", context={}, messages=[], active_run_id=None)
                self.store.save("conversation", conversation["id"], minimal, conversation["revision"])
            ticket.update(consumed=True, consumed_at=stamp)
            self.store.save("deletion_preview", key, ticket, ticket["revision"])
            return self.store.save("lifecycle_cleanup", key, job), False
        job, repeated = self.store.atomic(operation)
        if job:
            self._cleanup(job)
        latest = self.store.get("lifecycle_cleanup", key) or {}
        return {"id": id, "deleted": True, "already_deleted": repeated, "cleanup_status": latest.get("status", "complete")}

    def _safe_unlink(self, path, root):
        path, root = Path(path), Path(root).resolve()
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError("Unsafe lifecycle path")
        path.unlink(missing_ok=True)

    def _cleanup(self, job):
        if job.get("status") == "complete":
            return
        try:
            attachments = self.app.state.attachments
            for id in job.get("attachments", []):
                record = self.store.get("attachment", id)
                if record and record.get("deleted_at"):
                    self._safe_unlink(attachments._path(record), attachments.root)
            runtime = getattr(self.app.state, "runtime", None)
            root = Path(getattr(runtime, "artifact_dir", attachments.artifact_dir)).resolve()
            for run_id in job.get("runs", []):
                run = self.store.get("run", run_id) or {}
                for record in run.get("evidence", []):
                    if record.get("path"):
                        self._safe_unlink(record["path"], root / "evidence")
                attempt = run.get("attempt_id", "")
                if re.fullmatch(r"attempt_[a-f0-9]{32}", attempt):
                    directory = root / "skills" / attempt
                    if directory.is_symlink():
                        raise ValueError("Unsafe lifecycle directory")
                    if directory.is_dir():
                        for path in directory.rglob("*"):
                            if path.is_file() or path.is_symlink():
                                self._safe_unlink(path, root / "skills")
                        for path in sorted(directory.rglob("*"), key=lambda p: len(p.parts), reverse=True):
                            if path.is_dir():
                                path.rmdir()
                        directory.rmdir()
            for run_id in job.get("runs", []):
                latest = self.store.get("run", run_id)
                if latest and latest.get("deleted_at") and latest.get("evidence"):
                    latest["evidence"] = []
                    self.store.save("run", run_id, latest, latest["revision"])
            job.update(files_complete=True, status="complete" if job.get("checkpoints_complete") else "pending", completed_at=utcnow(), error=None)
        except (OSError, ValueError, HTTPException):
            job.update(status="pending", error={"code": "cleanup_pending", "detail": "内容访问已撤回，文件回收待重试"})
        self.store.save("lifecycle_cleanup", job["id"], job)

    async def finish_cleanup(self, result, token, owner):
        key = hashlib.sha256(token.encode()).hexdigest()
        job = self.store.get("lifecycle_cleanup", key)
        if job and job.get("owner_id") == owner and not job.get("checkpoints_complete"):
            try:
                from .recovery import purge_checkpoint
                for attempt in job.get("attempts", []):
                    await purge_checkpoint(self.store, attempt)
                job.update(checkpoints_complete=True)
                if job.get("files_complete"):
                    job.update(status="complete", completed_at=utcnow(), error=None)
            except Exception:
                job.update(status="pending", error={"code": "checkpoint_cleanup_pending", "detail": "内容访问已撤回，检查点回收待重试"})
            self.store.save("lifecycle_cleanup", key, job)
        return {**result, "cleanup_status": (job or {}).get("status", result["cleanup_status"])}

    def export(self, id, owner):
        detail = self.app.state.conversations.detail(id, owner)
        payload = {"schema": "aiopsstudio-conversation-export/1", "exported_at": utcnow(),
                   "conversation": {k: detail.get(k) for k in ("id", "title", "created_at", "updated_at", "archived", "context", "messages")},
                   "runs": detail.get("runs", []), "attachments": []}
        total = len(_canonical(payload).encode())
        if total > EXPORT_LIMIT:
            raise HTTPException(413, "会话导出超过 16 MiB，请缩小导出范围")
        for meta in detail.get("attachments", []):
            record = self.app.state.attachments.require_owned(meta["id"], owner, id, allow_legacy_archive=True)
            path = self.app.state.attachments._path(record)
            if not path.is_file() or path.stat().st_size != record["size"]:
                raise HTTPException(410, "附件文件不完整，不能导出")
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != record["sha256"]:
                raise HTTPException(410, "附件摘要不一致，不能导出")
            encoded = base64.b64encode(data).decode()
            total += len(encoded) + len(_canonical(meta).encode())
            if total > EXPORT_LIMIT:
                raise HTTPException(413, "会话导出超过 16 MiB，请分开管理附件")
            payload["attachments"].append({**meta, "content_base64": encoded})
        # Public run projections and attachment metadata have no server paths;
        # messages may include only their existing user-visible fields.
        return payload
