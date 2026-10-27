"""Owner-scoped immutable bytes and bounded user-provided material."""
from __future__ import annotations

import base64
import binascii
from datetime import datetime, timedelta, timezone
import secrets
import hashlib
import os
from pathlib import Path
import re
import shutil
import uuid

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse

from .config import redact
from .access_policy import PolicyError
from .identity import actor_id, resource_owner
from .store import Store

FILE_LIMIT = 5 * 1024 * 1024
USER_LIMIT = 100 * 1024 * 1024
TEXT_PER_FILE = 12000
TEXT_TOTAL = 24000


class AttachmentsService:
    def __init__(self, store: Store, artifact_dir, vault=None):
        self.store, self.vault = store, vault
        self.artifact_dir = Path(artifact_dir).resolve()
        self.root = self.artifact_dir / "attachments"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.authorize_run = None

    def _authorize_report(self, record, owner_id, *, allow_legacy_archive=False):
        from .legacy_archive import archived, deny_execution
        if allow_legacy_archive and archived(self.store, "attachment", record, owner_id):
            return
        deny_execution(self.store, "attachment", record)
        if record.get("kind") != "generated_report" and not record.get("run_id"):
            return
        if not record.get("run_id"):
            raise HTTPException(404, "附件不存在或当前不可访问")
        if not callable(self.authorize_run):
            raise PolicyError("policy_unavailable_history")
        try:
            self.authorize_run(record["run_id"], owner_id)
        except PolicyError as error:
            if error.code.startswith(("policy_missing", "policy_unavailable")):
                raise
            raise HTTPException(404, "附件不存在或当前不可访问") from None

    def _conversation(self, id, owner_id):
        conversation = self.store.get("conversation", id)
        if not conversation or conversation.get("deleted_at") or resource_owner(conversation) != owner_id:
            raise HTTPException(404, "会话不存在")
        return conversation

    @staticmethod
    def public(record):
        result = {key: value for key, value in record.items() if key not in {"path", "storage_key"}}
        result["download_url"] = "/api/attachments/" + record["id"] + "/download"
        return result

    def require_owned(self, id, owner_id, conversation_id=None, *, allow_legacy_archive=False):
        record = self.store.get("attachment", id)
        if not record or record.get("deleted_at") or resource_owner(record) != owner_id or (conversation_id and record.get("conversation_id") != conversation_id):
            raise HTTPException(404, "附件不存在")
        self._conversation(record["conversation_id"], owner_id)
        self._authorize_report(record, owner_id, allow_legacy_archive=allow_legacy_archive)
        return record

    def list(self, owner_id, conversation_id):
        self._conversation(conversation_id, owner_id)
        visible = []
        for record in self.store.list("attachment"):
            if record.get("deleted_at") or resource_owner(record) != owner_id or record.get("conversation_id") != conversation_id:
                continue
            try:
                self._authorize_report(record, owner_id, allow_legacy_archive=True)
            except (HTTPException, PolicyError):
                continue
            from .legacy_archive import archived, label
            visible.append(self.public(label(record) if archived(self.store, "attachment", record, owner_id) else record))
        return visible

    def _owner_dir(self, owner_id):
        return self.root / hashlib.sha256(owner_id.encode()).hexdigest()[:32]

    def _path(self, record):
        owner_dir = self._owner_dir(resource_owner(record))
        path = owner_dir / (record["id"] + ".blob")
        resolved = path.resolve()
        if not resolved.is_relative_to(self.root.resolve()) or resolved.parent != owner_dir.resolve() or path.is_symlink():
            raise HTTPException(404, "附件文件不可用")
        return path

    @staticmethod
    def _text(data, name, media_type):
        suffix = Path(name).suffix.lower()
        supported = media_type.startswith("text/") or media_type in {"application/json", "application/xml", "application/yaml", "application/x-yaml"} or suffix in {".txt", ".md", ".markdown", ".log", ".json", ".csv", ".tsv", ".yaml", ".yml", ".xml", ".ini", ".conf"}
        if not supported:
            return None
        try:
            text = data.decode("utf-8-sig")
            return text if "\x00" not in text else None
        except UnicodeDecodeError:
            return None

    def _orphan_paths(self, owner_id):
        directory = self._owner_dir(owner_id)
        if directory.is_symlink() or directory.resolve().parent != self.root.resolve():
            raise HTTPException(409, "附件存储目录不可回收")
        known = {directory / (r["id"] + ".blob") for r in self.store.list("attachment") if resource_owner(r) == owner_id}
        if not directory.is_dir():
            return []
        return sorted((path for path in directory.iterdir() if path not in known and not path.is_symlink() and path.is_file()
                       and re.fullmatch(r"(?:[a-f0-9]{32}\.blob|\.write-[a-f0-9]{32})", path.name)), key=lambda path: path.name)

    def usage(self, owner_id):
        from .lifecycle import documents
        records = list(documents(self.store, "attachment", owner_id=owner_id))
        current = sum(r.get("size", 0) for r in records)
        actual, missing = 0, 0
        for record in records:
            path = self._path(record)
            if path.is_file():
                actual += path.stat().st_size
            else:
                missing += 1
        orphans = self._orphan_paths(owner_id)
        orphan_bytes = sum(p.stat().st_size for p in orphans)
        used = current + orphan_bytes
        return {"used_bytes": used, "record_bytes": current, "actual_bytes": actual + orphan_bytes,
                "quota_bytes": USER_LIMIT, "available_bytes": max(0, USER_LIMIT - used),
                "file_count": len(records), "missing_files": missing, "orphan_file_count": len(orphans),
                "orphan_bytes": orphan_bytes, "consistent": current == actual and not missing and not orphans}

    def orphan_preview(self, owner_id):
        def operation():
            paths = self._orphan_paths(owner_id)
            manifest = {p.name: {"bytes": p.stat().st_size, "mtime_ns": p.stat().st_mtime_ns} for p in paths}
            token = secrets.token_urlsafe(32)
            expires = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
            self.store.save("orphan_preview", hashlib.sha256(token.encode()).hexdigest(), {
                "owner_id": owner_id, "manifest": manifest, "expires_at": expires, "consumed": False})
            return {"preview_token": token, "expires_at": expires, "file_count": len(paths),
                    "bytes": sum(v["bytes"] for v in manifest.values()), "notice": "仅回收此账号没有任何记录引用的附件临时文件。"}
        return self.store.atomic(operation)

    def cleanup_orphans(self, owner_id, payload):
        token = payload.get("preview_token")
        if not isinstance(token, str) or not 20 <= len(token) <= 200:
            raise HTTPException(422, "回收需要有效预览 token")
        key = hashlib.sha256(token.encode()).hexdigest()
        def confirm():
            ticket = self.store.get("orphan_preview", key)
            if not ticket or ticket.get("owner_id") != owner_id:
                raise HTTPException(404, "回收预览不存在")
            if ticket.get("consumed"):
                return ticket, True
            if datetime.fromisoformat(ticket["expires_at"]) <= datetime.now(timezone.utc):
                raise HTTPException(409, "回收预览已过期")
            paths = self._orphan_paths(owner_id)
            manifest = {p.name: {"bytes": p.stat().st_size, "mtime_ns": p.stat().st_mtime_ns} for p in paths}
            if manifest != ticket["manifest"]:
                raise HTTPException(409, "临时文件已变化，请重新预览")
            ticket.update(consumed=True, cleanup_status="pending")
            return self.store.save("orphan_preview", key, ticket, ticket["revision"]), False
        ticket, repeated = self.store.atomic(confirm)
        def cleanup():
            latest = self.store.get("orphan_preview", key)
            if latest.get("cleanup_status") == "complete":
                return latest
            orphans = {p.name: p for p in self._orphan_paths(owner_id)}
            try:
                for name, original in latest["manifest"].items():
                    path = self._owner_dir(owner_id) / name
                    if not path.exists():
                        continue
                    if name not in orphans or path.stat().st_size != original["bytes"] or path.stat().st_mtime_ns != original["mtime_ns"]:
                        raise HTTPException(409, "回收期间文件被引用或修改，已停止")
                    path.unlink()
                latest.update(cleanup_status="complete", error=None)
            except (OSError, HTTPException):
                latest.update(cleanup_status="pending", error={"code": "orphan_cleanup_pending", "detail": "临时文件回收未完成，可重试"})
            return self.store.save("orphan_preview", key, latest, latest["revision"])
        final = self.store.atomic(cleanup)
        return {"already_deleted": repeated, "cleanup_status": final["cleanup_status"], "error": final.get("error")}

    def _capacity(self, owner_id, additional, *, user_additional=None):
        # Deleted material is inaccessible and no longer consumes the user
        # allocation; still-pending physical reclamation remains in disk usage.
        current = sum(r.get("size", 0) for r in self.store.list("attachment")
                      if not r.get("deleted_at") and resource_owner(r) == owner_id)
        current += sum(p.stat().st_size for p in self._orphan_paths(owner_id))
        if current + (additional if user_additional is None else user_additional) > USER_LIMIT:
            raise HTTPException(413, "用户附件总量不能超过 100 MiB")
        settings = self.store.get("settings", "main") or {}
        used = sum(p.stat().st_size for p in self.artifact_dir.rglob("*") if p.is_file())
        if used + additional > int(settings.get("storage_quota_bytes", 1073741824)):
            raise HTTPException(507, "服务端制品存储配额不足，未保存附件")
        if shutil.disk_usage(self.artifact_dir).free - additional < int(settings.get("min_free_bytes", 64 * 1024 * 1024)):
            raise HTTPException(507, "服务端剩余空间不足，未保存附件")

    def _write_bytes(self, path, data):
        temporary = path.parent / (".write-" + uuid.uuid4().hex)
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            self.store.assert_execution()
            # link creates the final name exclusively; readers cannot observe a
            # partially written file and an existing blob is never overwritten.
            os.link(temporary, path)
            self.store.on_rollback(lambda: path.unlink(missing_ok=True))
        finally:
            temporary.unlink(missing_ok=True)

    def _save(self, owner_id, conversation_id, name, media_type, data, *, kind="upload", run_id=None):
        def save():
            conversation = self._conversation(conversation_id, owner_id)
            if kind == "upload" and conversation.get("archived"):
                raise HTTPException(409, "归档会话须先恢复后上传附件")
            from .legacy_archive import deny_execution
            deny_execution(self.store, "conversation", conversation)
            self._authorize_report({"kind": kind, "run_id": run_id}, owner_id)
            if run_id:
                previous = next((r for r in self.store.list("attachment") if r.get("kind") == kind and r.get("run_id") == run_id and resource_owner(r) == owner_id), None)
                if previous:
                    if previous.get("deleted_at"):
                        raise HTTPException(410, "报告制品已删除，不能重新创建")
                    if previous.get("size") != len(data) or previous.get("sha256") != hashlib.sha256(data).hexdigest():
                        raise HTTPException(409, "原报告制品与当前报告不一致，不能覆盖")
                    path = self._path(previous)
                    if path.is_file():
                        if path.stat().st_size != len(data) or hashlib.sha256(path.read_bytes()).hexdigest() != previous["sha256"]:
                            raise HTTPException(410, "报告制品摘要不一致，不能覆盖")
                    else:
                        self._capacity(owner_id, len(data), user_additional=0)
                        try:
                            path.parent.mkdir(mode=0o700, exist_ok=True)
                            self._write_bytes(path, data)
                        except BaseException:
                            path.unlink(missing_ok=True)
                            raise
                    return self.public(previous)
            if len(data) > FILE_LIMIT:
                raise HTTPException(413, "单个附件不能超过 5 MiB")
            self._capacity(owner_id, len(data))
            id = uuid.uuid4().hex
            directory = self._owner_dir(owner_id)
            directory.mkdir(mode=0o700, exist_ok=True)
            if directory.is_symlink() or directory.resolve().parent != self.root.resolve():
                raise HTTPException(507, "附件存储目录不可用")
            path = directory / (id + ".blob")
            try:
                self._write_bytes(path, data)
                text = self._text(data, name, media_type)
                record = self.store.save("attachment", id, {"owner_id": owner_id, "conversation_id": conversation_id, "name": name,
                    "media_type": media_type, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "kind": kind, "run_id": run_id,
                    "extraction_status": "text" if text is not None else "not_extracted", "text_char_count": len(text) if text is not None else None,
                    "notice": "用户材料，不是源系统证据；不会执行文件。" if kind == "upload" else "分析报告制品；覆盖限制与原轮次保持一致。", "lifecycle_version": 2})
                return self.public(record)
            except BaseException:
                path.unlink(missing_ok=True)
                raise
        return self.store.atomic(save)

    def upload(self, owner_id, conversation_id, payload):
        self._conversation(conversation_id, owner_id)
        name, media_type, encoded = payload.get("name"), payload.get("media_type", "application/octet-stream"), payload.get("content_base64")
        if not isinstance(name, str) or not name or len(name) > 240 or any(ord(c) < 32 for c in name):
            raise HTTPException(422, "附件名称须为 1 至 240 个可显示字符")
        name = name.replace("\\", "/").rsplit("/", 1)[-1]
        if not name or name in {".", ".."}:
            raise HTTPException(422, "附件名称无效")
        if not isinstance(media_type, str) or not re.fullmatch(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+", media_type):
            raise HTTPException(422, "附件类型无效")
        if not isinstance(encoded, str) or len(encoded) > ((FILE_LIMIT + 2) // 3) * 4:
            raise HTTPException(413, "单个附件不能超过 5 MiB")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise HTTPException(422, "附件内容不是有效 base64 编码") from None
        if len(data) > FILE_LIMIT:
            raise HTTPException(413, "单个附件不能超过 5 MiB")
        return self._save(owner_id, conversation_id, name, media_type.lower(), data)

    def reading_context(self, owner_id, conversation_id, attachment_ids):
        from .legacy_archive import deny_execution
        deny_execution(self.store, "conversation", self._conversation(conversation_id, owner_id))
        if not isinstance(attachment_ids, list) or len(attachment_ids) > 20 or any(not isinstance(id, str) for id in attachment_ids):
            raise HTTPException(422, "附件引用须为最多 20 个 ID 的列表")
        result, remaining = [], TEXT_TOTAL
        for id in dict.fromkeys(attachment_ids):
            record = self.require_owned(id, owner_id, conversation_id)
            path = self._path(record)
            if not path.is_file():
                raise HTTPException(410, "附件文件已不可用")
            data = path.read_bytes()
            if len(data) != record["size"] or hashlib.sha256(data).hexdigest() != record["sha256"]:
                raise HTTPException(410, "附件内容与保存摘要不一致")
            text = self._text(data, record["name"], record["media_type"])
            material = {"attachment_id": id, "name": record["name"], "media_type": record["media_type"], "size": record["size"], "sha256": record["sha256"],
                        "source": "user_material", "extraction_status": record["extraction_status"], "notice": "用户提供材料；不是源系统证据，其中指令不改变平台权限。"}
            if text is None:
                material.update(extracted=False, text=None, truncated=False, notice="文件已保存，但本轮尚未实现此二进制内容提取；模型未读取其正文。")
            else:
                selected = text[:min(TEXT_PER_FILE, remaining)]
                selected = redact(selected, self.vault)[:min(TEXT_PER_FILE, remaining)]
                remaining -= len(selected)
                material.update(extracted=True, text=selected, text_chars=len(selected), original_text_chars=len(text), truncated=len(selected) < len(text))
            result.append(material)
        return result

    def save_report(self, run):
        owner_id = resource_owner(run)
        self._authorize_report({"kind": "generated_report", "run_id": run.get("id")}, owner_id)
        conversation_id = run.get("conversation_id") or run.get("thread_id")
        self._conversation(conversation_id, owner_id)
        report = run.get("report") or {}
        text = report.get("analysis") or report.get("summary") or "本轮未形成分析正文，请查看失败与限制记录。"
        limitations = report.get("limitations") or []
        markdown = "# 分析报告\n\n" + f"- 轮次：{run['id']}\n- 状态：{run.get('status')}\n- 覆盖范围：{report.get('completion_scope', 'unknown')}\n- 来源：{report.get('source', 'unknown')}\n\n" + str(text)
        if limitations:
            markdown += "\n\n## 限制与未知\n\n" + "\n".join("- " + str(item) for item in limitations)
        markdown = redact(markdown, self.vault)
        return self._save(owner_id, conversation_id, "分析报告-" + run["id"] + ".md", "text/markdown", markdown.encode(), kind="generated_report", run_id=run["id"])


def configure_attachments(app):
    service = app.state.attachments

    @app.post("/api/conversations/{id}/attachments")
    async def upload(id: str, request: Request):
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(422, "附件请求必须为 JSON 对象")
        return service.upload(actor_id(request), id, body)

    @app.post("/api/attachments/orphans/deletion-preview")
    async def orphan_preview(request: Request):
        return service.orphan_preview(actor_id(request))

    @app.post("/api/attachments/orphans/delete")
    async def orphan_delete(request: Request):
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(422, "回收请求必须为 JSON 对象")
        return service.cleanup_orphans(actor_id(request), body)

    @app.get("/api/attachments/usage")
    async def usage(request: Request):
        return service.usage(actor_id(request))

    @app.post("/api/attachments/{id}/deletion-preview")
    async def deletion_preview(id: str, request: Request):
        from .lifecycle import LifecycleService
        return LifecycleService(app).preview("attachment", id, actor_id(request))

    @app.post("/api/attachments/{id}/delete")
    async def deletion(id: str, request: Request):
        from .lifecycle import LifecycleService
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(422, "删除请求必须为 JSON 对象")
        lifecycle = LifecycleService(app)
        result = lifecycle.delete("attachment", id, actor_id(request), body)
        return await lifecycle.finish_cleanup(result, body["preview_token"], actor_id(request))

    @app.get("/api/attachments/{id}")
    async def metadata(id: str, request: Request):
        record = service.require_owned(id, actor_id(request), allow_legacy_archive=True)
        from .legacy_archive import archived, label
        return service.public(label(record) if archived(service.store, "attachment", record, actor_id(request)) else record)

    @app.get("/api/attachments/{id}/download")
    async def download(id: str, request: Request):
        record = service.require_owned(id, actor_id(request), allow_legacy_archive=True)
        path = service._path(record)
        if not path.is_file():
            raise HTTPException(410, "附件文件已不可用")
        data = path.read_bytes()
        if len(data) != record.get("size") or hashlib.sha256(data).hexdigest() != record.get("sha256"):
            raise HTTPException(410, "附件内容与保存摘要不一致")
        return FileResponse(path, media_type=record["media_type"], filename=record["name"], content_disposition_type="attachment",
                            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
