"""Durable private conversations and receipts independent of execution attempts."""
from __future__ import annotations

import asyncio
import copy
import hashlib
from datetime import datetime, timezone
import secrets

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from .identity import actor_id, resource_owner
from .access_policy import PolicyError
from .store import Store, utcnow

TERMINAL = {"succeeded", "partial", "failed", "needs_input", "cancelled"}
CONTEXT_FIELDS = {"skill_id", "skill_version", "model_id", "target", "incident_id", "analysis_start", "analysis_end", "environment", "cluster", "namespace", "target_type", "target_identity", "windows", "baseline_window", "change_window", "validation_window"}


class ConversationsService:
    def __init__(self, app, store: Store):
        self.app, self.store = app, store
        self._locks: dict[str, asyncio.Lock] = {}

    @staticmethod
    def context(value):
        if not isinstance(value, dict):
            raise HTTPException(422, "分析上下文必须为对象")
        if value.get("tool_scope") not in (None, "", []):
            raise HTTPException(422, "工具范围由已发布 Skill 绑定和服务端白名单决定，暂不支持自定义范围")
        result = {}
        for key in CONTEXT_FIELDS:
            if key not in value:
                continue
            field = value[key]
            if key in {"target_identity", "windows", "baseline_window", "change_window", "validation_window"}:
                from .material_reading import canonical
                if field is not None and (not isinstance(field, dict) or len(canonical(field).encode()) > 8192):
                    raise HTTPException(422, "结构化上下文字段必须是有界JSON对象")
                result[key] = copy.deepcopy(field)
                continue
            limit = 4096 if key == "target" else 512
            if field is not None and (not isinstance(field, str) or len(field) > limit):
                raise HTTPException(422, f"上下文字段 {key} 必须为不超过 {limit} 字符的文本或 null")
            result[key] = field.strip() if isinstance(field, str) else None
        return result

    def require_owned(self, id, owner_id, *, allow_legacy_archive=False):
        item = self.store.get("conversation", id)
        if not item or item.get("deleted_at") or resource_owner(item) != owner_id:
            raise HTTPException(404, "会话不存在")
        self._authorize_history(item, owner_id, allow_legacy_archive=allow_legacy_archive)
        return item

    def _authorize_history(self, conversation, owner_id, *, allow_legacy_archive=False):
        """No saved assistant body/title bypasses a later source revocation."""
        from .legacy_archive import archived, deny_execution
        if allow_legacy_archive and archived(self.store, "conversation", conversation, owner_id):
            return
        deny_execution(self.store, "conversation", conversation)
        references = {message.get("run_id") for message in conversation.get("messages", []) if message.get("run_id")}
        if conversation.get("active_run_id"):
            references.add(conversation["active_run_id"])
        runs = [run for run in self.store.list("run") if run.get("conversation_id") == conversation["id"] or run.get("thread_id") == conversation["id"] or run.get("id") in references]
        if references - {run["id"] for run in runs} or any(message.get("role") == "assistant" and not message.get("run_id") for message in conversation.get("messages", [])):
            raise HTTPException(404, "会话不存在或当前不可访问")
        if not runs:
            return
        runtime = getattr(self.app.state, "runtime", None)
        authorize = getattr(runtime, "authorize_history", None)
        if not callable(authorize):
            raise PolicyError("policy_unavailable_history")
        for run in runs:
            if resource_owner(run) != owner_id:
                raise HTTPException(404, "会话不存在或当前不可访问")
            try:
                authorize(run, owner_id)
            except PolicyError as error:
                if error.code.startswith(("policy_missing", "policy_unavailable")):
                    raise
                raise HTTPException(404, "会话不存在或当前不可访问") from None

    def lifecycle(self):
        from .lifecycle import LifecycleService
        return LifecycleService(self.app)

    def list_page(self, owner_id, *, search="", archived=False, status=None, cursor=None, limit=50, start=None, end=None):
        if not isinstance(search, str) or len(search) > 200 or not 1 <= limit <= 100:
            raise HTTPException(422, "历史搜索或分页参数无效")
        for stamp in (start, end):
            if stamp is not None:
                try:
                    parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                    if parsed.tzinfo is None:
                        raise ValueError()
                except (TypeError, ValueError):
                    raise HTTPException(422, "历史时间范围须使用带时区的绝对时间") from None
        if start and end and datetime.fromisoformat(start.replace("Z", "+00:00")) > datetime.fromisoformat(end.replace("Z", "+00:00")):
            raise HTTPException(422, "历史时间范围开始晚于结束")
        start = datetime.fromisoformat(start.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat() if start else None
        end = datetime.fromisoformat(end.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat() if end else None
        visible, current = [], cursor
        while len(visible) < limit:
            try:
                page = self.store.query_documents("conversation", owner_id=owner_id, search=search, archived=archived,
                    status=status, cursor=current, limit=1, created_from=start, created_to=end)
            except ValueError:
                raise HTTPException(422, "分页游标无效或不属于当前查询") from None
            if not page["items"]:
                current = None
                break
            conversation = page["items"][0]
            current = page.get("next_cursor")
            try:
                self._authorize_history(conversation, owner_id, allow_legacy_archive=True)
            except (HTTPException, PolicyError):
                if current:
                    continue
                break
            from .legacy_archive import archived as legacy_archived, label
            visible.append(label(conversation) if legacy_archived(self.store, "conversation", conversation, owner_id) else conversation)
            if not current:
                break
        return {"items": visible, "next_cursor": current}

    def list(self, owner_id):
        return self.list_page(owner_id, limit=100)["items"]

    def create(self, owner_id, payload):
        key = payload.get("client_request_id")
        if not isinstance(key, str) or not key.strip() or len(key) > 200:
            raise HTTPException(422, "创建会话需要 client_request_id")
        title = payload.get("title") or "新分析"
        if not isinstance(title, str) or not title.strip() or len(title) > 200:
            raise HTTPException(422, "会话标题不能超过 200 个字符")
        context = self.context(payload.get("context") or {})
        def create():
            existing = next((c for c in self.store.list("conversation") if resource_owner(c) == owner_id and (c.get("client_request_id") == key or c.get("client_request_sha256") == hashlib.sha256(key.encode()).hexdigest())), None)
            if existing:
                return self.require_owned(existing["id"], owner_id)
            return self.store.save("conversation", "conv_" + secrets.token_hex(16), {"owner_id": owner_id, "title": title.strip(), "context": context,
                    "messages": [], "active_run_id": None, "client_request_id": key, "lifecycle_version": 2, "archived": False})
        return self.store.atomic(create)

    def update(self, id, owner_id, payload):
        def update():
            item = self.require_owned(id, owner_id)
            if item.get("archived"):
                raise HTTPException(409, "归档会话须先恢复后修改")
            if "title" in payload:
                if not isinstance(payload["title"], str) or not payload["title"].strip() or len(payload["title"]) > 200:
                    raise HTTPException(422, "会话标题须为 1 至 200 个字符")
                item["title"] = payload["title"].strip()
            if "context" in payload:
                item["context"] = self.context(payload["context"])
            return self.store.save("conversation", id, item, payload.get("expected_revision", item["revision"]))
        return self.store.atomic(update)

    def append_message(self, id, owner_id, role, content, client_message_id=None, run_id=None, attachment_ids=None, status="accepted", **fields):
        def append():
            conversation = self.require_owned(id, owner_id)
            existing = next((m for m in conversation["messages"] if client_message_id and m.get("client_message_id") == client_message_id and m.get("role") == role), None)
            if existing:
                return existing
            message = {"id": "msg_" + secrets.token_hex(16), "conversation_id": id, "owner_id": owner_id, "role": role, "content": content,
                       "client_message_id": client_message_id, "run_id": run_id, "attachment_ids": list(attachment_ids or []), "status": status, "created_at": utcnow(), **copy.deepcopy(fields)}
            conversation["messages"].append(message)
            self.store.save("conversation", id, conversation, conversation["revision"])
            return message
        return self.store.atomic(append)

    def _update_message(self, id, owner_id, message_id, **changes):
        def update():
            conversation = self.require_owned(id, owner_id)
            message = next(m for m in conversation["messages"] if m["id"] == message_id)
            message.update(copy.deepcopy(changes))
            if changes.get("run_id"):
                linked = self.store.get("run", changes["run_id"])
                if linked and linked.get("status") not in TERMINAL:
                    conversation["active_run_id"] = changes["run_id"]
                elif conversation.get("active_run_id") == changes["run_id"]:
                    conversation["active_run_id"] = None
            saved = self.store.save("conversation", id, conversation, conversation["revision"])
            return next(m for m in saved["messages"] if m["id"] == message_id)
        return self.store.atomic(update)

    @staticmethod
    def _canonical(value, key):
        if value in (None, ""):
            return None
        if key in {"analysis_start", "analysis_end"}:
            try:
                instant = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                return instant.astimezone(timezone.utc).isoformat() if instant.tzinfo else value
            except ValueError:
                pass
        return value

    def _same_context(self, run, context):
        requested = run.get("_request") or {}
        return all(self._canonical(requested.get(k), k) == self._canonical(context.get(k), k) for k in CONTEXT_FIELDS)

    def _receipt(self, id, owner_id, message):
        run = self.store.get("run", message.get("run_id")) if message.get("run_id") else None
        if run and resource_owner(run) != owner_id:
            raise HTTPException(404, "分析不存在")
        runtime = getattr(self.app.state, "runtime", None)
        return {"message": message, "run": runtime.public(run) if run and runtime else None, "conversation": self.require_owned(id, owner_id)}

    async def send_message(self, id, owner_id, payload):
        client_id, content = payload.get("client_message_id"), payload.get("content", "")
        attachment_ids = payload.get("attachment_ids") or []
        if not isinstance(client_id, str) or not client_id.strip() or len(client_id) > 200:
            raise HTTPException(422, "发送消息需要 client_message_id")
        if not isinstance(content, str) or len(content) > 64000 or (not content.strip() and not attachment_ids):
            raise HTTPException(422, "消息正文不能为空且不能超过 64000 个字符")
        async with self._locks.setdefault(id, asyncio.Lock()):
            conversation = self.require_owned(id, owner_id)
            if conversation.get("archived"):
                raise HTTPException(409, "归档会话须先恢复后发送消息")
            existing = next((m for m in conversation["messages"] if m.get("client_message_id") == client_id and m.get("role") == "user"), None)
            if existing and existing.get("status") != "pending":
                return self._receipt(id, owner_id, existing)
            mode = existing.get("mode", "analyze") if existing else payload.get("mode", "analyze")
            if mode not in {"analyze", "explain", "followup"}:
                raise HTTPException(422, "消息模式须为 analyze、explain 或 followup")
            if mode == "explain" and attachment_ids:
                raise HTTPException(422, "解释已有结果不能添加新材料，请使用补查模式")
            runs = sorted([r for r in self.store.list("run") if not r.get("deleted_at") and resource_owner(r) == owner_id and (r.get("conversation_id") or r.get("thread_id")) == id], key=lambda r: r.get("created_at", ""))
            parent_id = existing.get("parent_run_id") if existing else payload.get("parent_run_id")
            parent = None
            if mode in {"explain", "followup"}:
                if parent_id is None:
                    parent = next((r for r in reversed(runs) if r.get("status") in TERMINAL), None)
                    parent_id = parent.get("id") if parent else None
                else:
                    if not isinstance(parent_id, str):
                        raise HTTPException(422, "父轮标识格式无效")
                    parent = next((r for r in runs if r["id"] == parent_id), None)
                if not parent or parent.get("status") not in TERMINAL:
                    raise HTTPException(404, "可解释或补查的已完成父轮不存在")
                self.app.state.runtime.require_owned(parent_id, owner_id)
            if existing:
                message = existing
                try:
                    context = self.context(existing["analysis_context"])
                except HTTPException as error:
                    self._update_message(id, owner_id, message["id"], status="not_executed", error={"code": "invalid_context", "detail": error.detail}, hint=error.detail)
                    raise
                attachment_ids, content = existing["attachment_ids"], existing["content"]
            else:
                inherited = {k: v for k, v in (parent.get("_request") or {}).items() if k in CONTEXT_FIELDS} if parent else {}
                context = {**inherited, **conversation.get("context", {}), **self.context(payload.get("run_input") or {})}
                # Validate references before accepting a message; never persist a foreign attachment ID.
                self.app.state.attachments.reading_context(owner_id, id, attachment_ids)
                message = self.append_message(id, owner_id, "user", content.strip(), client_id, attachment_ids=attachment_ids, status="pending", analysis_context=context, mode=mode, parent_run_id=parent_id)
                conversation = self.update(id, owner_id, {"context": context})
            runtime = getattr(self.app.state, "runtime", None)
            hint = None
            if not context.get("skill_id"):
                hint = "请选择已发布的 Skill，再补充目标或告警与固定分析时间窗后重新发送。"
            elif not runtime:
                hint = "分析执行器尚未装配，请稍后重新发送。"
            if hint:
                message = self._update_message(id, owner_id, message["id"], status="not_executed", error={"code": "context_required", "detail": hint}, hint=hint)
                return self._receipt(id, owner_id, message)
            key = "chat:" + id + ":" + message["id"]
            linked = next((r for r in runs if r.get("client_request_id") == key or
                           any(m.get("role") == "user" and m.get("client_message_id") == message["id"] for m in r.get("messages", []))), None)
            active = next((r for r in reversed(runs) if r["status"] not in TERMINAL), None)
            try:
                if linked:
                    run = linked
                elif mode == "analyze" and active and active.get("mode", "analyze") == "analyze" and self._same_context(active, context):
                    await runtime.message(active["id"], {"content": content, "client_message_id": message["id"], "attachment_ids": attachment_ids,
                        "context": {"run_id": active["id"], **{k: active.get(k) for k in CONTEXT_FIELDS if k in active}}}, owner_id=owner_id)
                    run = self.store.get("run", active["id"])
                else:
                    if context.get("skill_version") == "draft":
                        raise HTTPException(422, "Chat 分析请选择已发布的 Skill 版本")
                    submitted = {k: v for k, v in context.items() if k in CONTEXT_FIELDS}
                    submitted.update(purpose="investigation", thread_id=id, conversation_id=id, message=content, attachment_ids=attachment_ids,
                                     client_request_id=key, conversation_message_id=message["id"], mode=mode, parent_run_id=parent_id or (runs[-1]["id"] if runs else None))
                    run = await runtime.submit(submitted, owner_id=owner_id)
                run = self.store.get("run", run["id"]) or run
                changes = {"status": "accepted", "run_id": run["id"], "error": None, "hint": None}
                if run.get("status") in TERMINAL:
                    consumed = next((m for m in run.get("messages", []) if m.get("role") == "user" and m.get("client_message_id") == message["id"]), {})
                    changes["status"] = consumed.get("status") if consumed.get("status") in {"processed", "completed", "unprocessed"} else "unprocessed"
                    if changes["status"] == "unprocessed":
                        changes["reason"] = consumed.get("reason") or "本轮结束前未消费该消息；可新建轮次继续分析"
                message = self._update_message(id, owner_id, message["id"], **changes)
            except HTTPException as error:
                hint = str(error.detail)
                message = self._update_message(id, owner_id, message["id"], status="not_executed", error={"code": "submission_rejected", "detail": hint}, hint=hint)
            return self._receipt(id, owner_id, message)

    def finish_run(self, run):
        owner_id = resource_owner(run)
        conversation_id = run.get("conversation_id") or run.get("thread_id")
        conversation = self.store.get("conversation", conversation_id) if conversation_id else None
        if not conversation or conversation.get("deleted_at") or run.get("deleted_at"):
            return None
        self.require_owned(conversation_id, owner_id)
        if run.get("status") not in TERMINAL:
            return None
        attachment, attachment_error = None, None
        try:
            attachment = self.app.state.attachments.save_report(run)
        except Exception as error:
            attachment_error = {"code": "report_attachment_failed", "detail": str(error.detail) if isinstance(error, HTTPException) else "报告附件保存失败，分析记录仍保留"}
        report = run.get("report") or {}
        text = report.get("analysis") or report.get("summary") or "本轮分析已结束，请查看执行状态与限制。"
        self.append_message(conversation_id, owner_id, "assistant", text, "run:" + run["id"], run_id=run["id"],
                            attachment_ids=[attachment["id"]] if attachment else [], status="completed" if run["status"] == "succeeded" else run["status"],
                            report_attachment=attachment, error=attachment_error, run_status=run["status"])
        def update():
            current = self.require_owned(conversation_id, owner_id)
            if current.get("active_run_id") == run["id"]:
                current["active_run_id"] = None
            for message in current["messages"]:
                if message.get("role") == "assistant" and message.get("run_id") == run["id"]:
                    # Preserve the original assistant identity and report body;
                    # a retried artifact write only repairs its delivery metadata.
                    message.update(report_attachment=attachment, attachment_ids=[attachment["id"]] if attachment else [], error=attachment_error)
                if message.get("role") == "user" and message.get("run_id") == run["id"] and message.get("status") in {"accepted", "pending", "processing"}:
                    consumed = next((m for m in run.get("messages", []) if m.get("client_message_id") == message["id"]), None)
                    message["status"] = consumed.get("status", "unprocessed") if consumed else "unprocessed"
                    if not consumed or message["status"] not in {"processed", "completed"}:
                        message["reason"] = (consumed or {}).get("reason") or "本轮结束前未消费该消息；可新建轮次继续分析"
            self.store.save("conversation", conversation_id, current, current["revision"])
            latest = self.store.get("run", run["id"])
            if latest:
                latest["report_attachment"] = attachment
                latest["delivery_status"] = "failed" if attachment_error else "ready"
                if attachment_error:
                    latest["report_attachment_error"] = attachment_error
                    latest["artifact_error"] = attachment_error
                else:
                    latest.pop("report_attachment_error", None)
                    latest.pop("artifact_error", None)
                self.store.save("run", run["id"], latest, latest["revision"])
        self.store.atomic(update)
        return attachment

    def repair_report(self, run_id, owner_id):
        runtime = getattr(self.app.state, "runtime", None)
        if runtime is None:
            raise HTTPException(503, "分析执行器尚未装配")
        run = runtime.require_owned(run_id, owner_id)
        if run.get("status") not in TERMINAL:
            raise HTTPException(409, "只有已完成轮次可以修复报告文件")
        self.finish_run(run)
        latest = self.store.get("run", run_id)
        return {"delivery_status": latest.get("delivery_status", "pending"), "report_attachment": latest.get("report_attachment"),
                "error": latest.get("report_attachment_error")}

    def detail(self, id, owner_id):
        result = self.require_owned(id, owner_id, allow_legacy_archive=True)
        runtime = getattr(self.app.state, "runtime", None)
        from .legacy_archive import archived, label
        if archived(self.store, "conversation", result, owner_id):
            result = label(result)
            runs = [r for r in self.store.list("run") if resource_owner(r) == owner_id and (r.get("conversation_id") or r.get("thread_id")) == id]
            result["runs"] = [runtime.public(r, allow_legacy_archive=True) for r in sorted(runs, key=lambda r: r.get("created_at", ""))] if runtime else []
            result["attachments"] = self.app.state.attachments.list(owner_id, id)
            return result
        runs = [r for r in self.store.list("run") if resource_owner(r) == owner_id and (r.get("conversation_id") or r.get("thread_id")) == id]
        for run in runs:
            represented = any(m.get("role") == "assistant" and m.get("run_id") == run["id"] for m in result.get("messages", []))
            pending = any(m.get("role") == "user" and m.get("run_id") == run["id"] and m.get("status") in {"accepted", "processing", "pending"} for m in result.get("messages", []))
            assistant = next((m for m in result.get("messages", []) if m.get("role") == "assistant" and m.get("run_id") == run["id"]), {})
            artifact_pending = bool(run.get("artifact_error") or run.get("report_attachment_error") or assistant.get("error"))
            artifact_outdated = bool(run.get("report_attachment") and assistant.get("report_attachment") != run["report_attachment"])
            if run.get("status") in TERMINAL and (not represented or pending or artifact_pending or artifact_outdated):
                self.finish_run(run)
        result = self.require_owned(id, owner_id)
        runs = [self.store.get("run", run["id"]) or run for run in runs]
        result["runs"] = [runtime.public(r) if runtime else {k: v for k, v in r.items() if k not in {"_request", "config_snapshot", "history_context", "user_materials"}} for r in sorted(runs, key=lambda r: r.get("created_at", ""))]
        result["attachments"] = self.app.state.attachments.list(owner_id, id)
        return result


def configure_conversations(app):
    service = app.state.conversations
    from .lifecycle import LifecycleConflict

    @app.exception_handler(LifecycleConflict)
    async def lifecycle_conflict(request, error):
        return JSONResponse({"detail": error.detail, "code": error.code}, status_code=409)

    async def body(request):
        data = await request.json()
        if not isinstance(data, dict):
            raise HTTPException(422, "会话请求必须为 JSON 对象")
        return data

    @app.get("/api/conversations")
    async def listing(request: Request, search: str = "", archived: bool = False, status: str | None = None,
                      cursor: str | None = None, limit: int = 50, start: str | None = None, end: str | None = None):
        return service.list_page(actor_id(request), search=search, archived=archived, status=status, cursor=cursor, limit=limit, start=start, end=end)

    @app.post("/api/conversations")
    async def create(request: Request):
        return service.create(actor_id(request), await body(request))

    @app.get("/api/conversations/{id}")
    async def detail(id: str, request: Request):
        return service.detail(id, actor_id(request))

    @app.put("/api/conversations/{id}")
    async def update(id: str, request: Request):
        return service.update(id, actor_id(request), await body(request))

    @app.post("/api/conversations/{id}/messages")
    async def message(id: str, request: Request):
        return await service.send_message(id, actor_id(request), await body(request))

    @app.post("/api/conversations/{id}/archive")
    async def archive(id: str, request: Request):
        return service.lifecycle().archive(id, actor_id(request), await body(request))

    @app.get("/api/conversations/{id}/export")
    async def export(id: str, request: Request):
        return service.lifecycle().export(id, actor_id(request))

    @app.post("/api/conversations/{id}/deletion-preview")
    async def deletion_preview(id: str, request: Request):
        return service.lifecycle().preview("conversation", id, actor_id(request))

    @app.post("/api/conversations/{id}/delete")
    async def deletion(id: str, request: Request):
        payload = await body(request)
        lifecycle = service.lifecycle()
        result = lifecycle.delete("conversation", id, actor_id(request), payload)
        return await lifecycle.finish_cleanup(result, payload["preview_token"], actor_id(request))

    @app.post("/api/runs/{id}/repair-report")
    async def repair(id: str, request: Request):
        return service.repair_report(id, actor_id(request))
