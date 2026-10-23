"""AG-UI is a projection of database-owned tasks, never a second executor."""
from __future__ import annotations

import asyncio
import hashlib
import json
from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse

from .identity import actor_id, require_admin
from .runtime import resource_owner, stream_authorized


def protocol_binding_id(owner_id: str, protocol_id: str) -> str:
    return hashlib.sha256(json.dumps([owner_id, protocol_id], separators=(",", ":")).encode()).hexdigest()


def configure_agui(app):
    runtime, store = app.state.runtime, app.state.store
    terminal = {"succeeded", "partial", "failed", "needs_input", "cancelled"}

    def require_owned(record, owner_id):
        if not record or resource_owner(record) != owner_id:
            raise HTTPException(404, "分析会话不存在")
        return record

    def binding_for(protocol_id, owner_id):
        binding = store.get("agui_binding", protocol_binding_id(owner_id, protocol_id))
        if not binding and owner_id == "admin":
            legacy = store.get("agui_binding", protocol_id)
            if legacy and resource_owner(legacy) == owner_id:
                binding = legacy
        if binding:
            require_owned(binding, owner_id)
            require_owned(store.get("run", binding.get("business_run_id", "")), owner_id)
        return binding

    def find(thread_id, owner_id):
        conversation = store.get("conversation", thread_id)
        if conversation:
            require_owned(conversation, owner_id)
        matches = [r for r in store.list("run") if r.get("thread_id") == thread_id or r["id"] == thread_id]
        if matches and any(resource_owner(r) != owner_id for r in matches):
            raise HTTPException(404, "分析会话不存在")
        return max(matches, key=lambda r: r.get("created_at", "")) if matches else None

    def event(kind, **fields):
        return 'data: ' + json.dumps({"type": kind, **fields}, ensure_ascii=False) + '\n\n'

    async def stream(request, business_id, protocol_run_id, owner_id):
        if not stream_authorized(request, owner_id):
            return
        try:
            current = runtime.detail(business_id, owner_id=owner_id)
        except HTTPException:
            yield event("RUN_ERROR", message="分析不存在或当前不可访问", code="access_revoked")
            return
        yield event("RUN_STARTED", threadId=current["thread_id"], runId=protocol_run_id)
        seen_messages, seen_seq = set(), -1
        while True:
            if not stream_authorized(request, owner_id):
                return
            try:
                current = runtime.detail(business_id, owner_id=owner_id)
            except HTTPException:
                yield event("RUN_ERROR", message="分析不存在或当前不可访问", code="access_revoked")
                return
            events = current.get("events", [])
            seq = max((e.get("seq", 0) for e in events), default=0)
            if seq != seen_seq:
                yield event("STATE_SNAPSHOT", snapshot={"investigation": current})
                messages = [{"id": m["id"], "role": m["role"], "content": m.get("content", "")}
                            for m in current.get("messages", []) if m.get("role") in {"user", "assistant"}]
                initial_snapshot = seen_seq == -1
                seen_seq = seq
                for message in messages:
                    if message["role"] != "assistant" or message["id"] in seen_messages:
                        continue
                    seen_messages.add(message["id"])
                    if initial_snapshot:
                        continue  # Reconnect replays a snapshot, not a second copy of its text.
                    yield event("TEXT_MESSAGE_START", messageId=message["id"], role="assistant")
                    yield event("TEXT_MESSAGE_CONTENT", messageId=message["id"], delta=str(message["content"]))
                    yield event("TEXT_MESSAGE_END", messageId=message["id"])
                yield event("MESSAGES_SNAPSHOT", messages=messages)
            if current["status"] in terminal:
                # A terminal business failure remains explicit in the state and report.
                yield event("RUN_FINISHED", threadId=current["thread_id"], runId=protocol_run_id)
                return
            if await request.is_disconnected():
                return  # Disconnect only drops this subscription, never runtime.cancel.
            await asyncio.sleep(.35)

    async def attach(request, body, connect=False):
        if not isinstance(body, dict):
            raise HTTPException(422, "AG-UI 请求必须为对象")
        owner_id = actor_id(request)
        thread_id, protocol_id = body.get("threadId"), body.get("runId")
        if not isinstance(thread_id, str) or not isinstance(protocol_id, str) or not thread_id or not protocol_id or len(thread_id) > 200 or len(protocol_id) > 200:
            raise HTTPException(422, "AG-UI 请求需要 threadId 与 runId")
        context = body.get("forwardedProps") or {}
        if not isinstance(context, dict):
            raise HTTPException(422, "页面上下文格式无效")
        current_thread = find(thread_id, owner_id)
        requested_explicit = context.get("business_run_id")
        if requested_explicit is not None and (not isinstance(requested_explicit, str) or len(requested_explicit) > 200):
            raise HTTPException(422, "分析轮次标识格式无效")
        if requested_explicit:
            requested_record = require_owned(store.get("run", requested_explicit), owner_id)
            if requested_record.get("thread_id") != thread_id and requested_record["id"] != thread_id:
                raise HTTPException(409, "分析和协议会话不匹配")
        binding = binding_for(protocol_id, owner_id)
        if binding and binding.get("thread_id") != thread_id:
            raise HTTPException(409, "协议运行标识已绑定到其他会话")
        if binding and requested_explicit and requested_explicit not in {binding.get("business_run_id"), binding.get("requested_business_run_id")}:
            raise HTTPException(409, "协议运行标识已绑定到其他分析轮次")
        explicit = binding.get("business_run_id") if binding else context.get("business_run_id")
        record = require_owned(store.get("run", explicit), owner_id) if explicit else current_thread
        if record and record["thread_id"] != thread_id and record["id"] != thread_id:
            raise HTTPException(409, "分析和协议会话不匹配")
        if connect:
            if not record:
                raise HTTPException(404, "分析会话不存在，请先提交分析")
        elif not binding:
            messages = body.get("messages", [])
            if not isinstance(messages, list) or any(not isinstance(m, dict) for m in messages):
                raise HTTPException(422, "协议消息必须为列表")
            incoming = [m for m in messages if m.get("role") == "user"]
            latest = incoming[-1] if incoming else None
            if latest:
                message_id = latest.get("id") or protocol_id
                if not isinstance(message_id, str) or not message_id.strip() or len(message_id) > 200:
                    raise HTTPException(422, "协议用户消息需要有效 id")
                if context.get("purpose", "investigation") != "investigation" or context.get("skill_version") == "draft":
                    require_admin(request)
                previous = [r for r in store.list("run") if resource_owner(r) == owner_id and r.get("thread_id") == thread_id
                            and any(m.get("role") == "user" and message_id in {m.get("id"), m.get("client_message_id")}
                                    for m in r.get("messages", []))]
                conversation_id = (record or {}).get("conversation_id") or thread_id
                conversation = store.get("conversation", conversation_id)
                if previous:
                    record = max(previous, key=lambda r: r.get("created_at", ""))
                elif conversation:
                    require_owned(conversation, owner_id)
                    service = app.state.conversations
                    existing = next((m for m in conversation.get("messages", []) if m.get("role") == "user"
                                     and message_id in {m.get("id"), m.get("client_message_id")}), None)
                    client_message_id = (existing.get("client_message_id") or existing["id"]) if existing else message_id
                    receipt = await service.send_message(conversation_id, owner_id, {
                        "content": latest.get("content", ""), "client_message_id": client_message_id,
                        "attachment_ids": context.get("attachment_ids") or latest.get("attachment_ids") or [], "run_input": context})
                    record = receipt.get("run")
                    if not record:
                        message = receipt.get("message") or {}
                        raise HTTPException(422, message.get("hint") or "消息已保存但尚未执行，请补充分析上下文")
                elif record:
                    if record.get("purpose", "investigation") != "investigation":
                        require_admin(request)
                    if record["status"] in terminal:
                        record = await runtime.retry(record["id"], {"message": latest.get("content", ""),
                            "client_request_id": protocol_id, "protocol_message_id": message_id}, owner_id=owner_id)
                    else:
                        fields = {key: context[key] for key in ("skill_id", "skill_version", "model_id", "target", "incident_id", "analysis_start", "analysis_end", "environment") if key in context}
                        await runtime.message(record["id"], {"content": latest.get("content", ""), "client_message_id": message_id,
                            "context": {"run_id": record["id"], **fields}}, owner_id=owner_id)
                else:
                    if not context.get("skill_id"):
                        raise HTTPException(422, "请先在工作台选择 Skill、对象和时间窗")
                    safe_context = {k: v for k, v in context.items() if k not in {"conversation_message_id", "protocol_message_id"}}
                    record = await runtime.submit({**safe_context, "thread_id": thread_id,
                        "message": latest.get("content", ""), "client_request_id": protocol_id,
                        "protocol_message_id": message_id}, owner_id=owner_id)
            elif not record:
                raise HTTPException(404, "分析会话不存在，请先提交分析")
        # Authenticate source history before starting the 200/SSE response.
        runtime.require_owned(record["id"], owner_id)
        if not binding:
            store.save("agui_binding", protocol_binding_id(owner_id, protocol_id), {"owner_id": owner_id, "protocol_run_id": protocol_id,
                       "thread_id": thread_id, "business_run_id": record["id"], "requested_business_run_id": requested_explicit})
        return StreamingResponse(stream(request, record["id"], protocol_id, owner_id), media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @app.post("/api/ag-ui")
    async def run(request: Request):
        return await attach(request, await request.json())

    @app.post("/api/ag-ui/connect")
    async def connect(request: Request):
        return await attach(request, await request.json(), True)

    @app.get("/api/ag-ui/status/{thread_id}")
    async def status(thread_id: str, request: Request):
        record = find(thread_id, actor_id(request))
        if record:
            runtime.require_owned(record["id"], actor_id(request))
        return {"running": bool(record and record["status"] not in terminal), "business_run_id": record["id"] if record else None}

    @app.post("/api/ag-ui/stop/{thread_id}")
    async def stop(thread_id: str, request: Request):
        owner_id = actor_id(request)
        body = await request.json()
        binding = binding_for(body.get("runId", ""), owner_id) if body.get("runId") else None
        if body.get("runId") and (not binding or binding.get("thread_id") != thread_id):
            raise HTTPException(404, "协议运行不存在")
        record = require_owned(store.get("run", binding["business_run_id"]), owner_id) if binding else find(thread_id, owner_id)
        if not record:
            raise HTTPException(404, "分析会话不存在")
        if record.get("thread_id") != thread_id and record["id"] != thread_id:
            raise HTTPException(404, "分析会话不存在")
        return await runtime.cancel(record["id"], owner_id=owner_id)
