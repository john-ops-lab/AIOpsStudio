"""Explicit offline upgrade archives; never an execution authorization."""
from __future__ import annotations

import copy
import hashlib
import json

from fastapi import HTTPException

from .identity import resource_owner

KIND, KEY = "legacy_archive", "upgrade-v1"
TERMINAL = {"succeeded", "partial", "failed", "needs_input", "cancelled"}
NOTICE = "升级前历史只读档案：原内容未改，来源未按新契约核验；继续分析请新建会话，旧材料不会自动送入模型。"


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _entry(kind, record):
    return {"kind": kind, "id": record["id"], "owner_id": resource_owner(record), "content_digest": digest(record)}


def plan_archive(store):
    """Read-only plan. Caller must stop workers before registration/cutover."""
    runs = store.list("run")
    old = [r for r in runs if not r.get("trust_version")]
    if any(r.get("status") not in TERMINAL for r in old):
        raise ValueError("旧分析尚未全部结束；停止新提交并等候执行完成后再生成档案清单")
    selected = {r["id"]: r for r in old}
    entries = [_entry("run", r) for r in old]
    for conversation in store.list("conversation"):
        related = [r for r in runs if r.get("conversation_id") == conversation["id"] or r.get("thread_id") == conversation["id"]]
        references = {m.get("run_id") for m in conversation.get("messages", []) if m.get("run_id")}
        if conversation.get("active_run_id"):
            references.add(conversation["active_run_id"])
        touched = any(r["id"] in selected for r in related) or bool(references & selected.keys())
        # Pre-run assistant material also needs an explicit archive, never implicit trust.
        touched = touched or any(m.get("role") == "assistant" and not m.get("run_id") for m in conversation.get("messages", []))
        if not touched:
            continue
        if any(r["id"] not in selected or resource_owner(r) != resource_owner(conversation) for r in related) or any(
                r not in selected or resource_owner(selected[r]) != resource_owner(conversation) for r in references):
            raise ValueError("旧会话关联不完整或混入新运行/其他owner，档案登记停止")
        entries.append(_entry("conversation", conversation))
    for attachment in store.list("attachment"):
        if attachment.get("kind") != "generated_report":
            continue
        parent = selected.get(attachment.get("run_id"))
        if parent:
            if resource_owner(parent) != resource_owner(attachment):
                raise ValueError("旧报告owner与运行不符，档案登记停止")
            entries.append(_entry("attachment", attachment))
    entries.sort(key=lambda e: (e["kind"], e["id"]))
    return {"schema": "legacy-readonly-archive/v1", "entries": entries, "plan_digest": digest(entries)}


def register_archive(store, plan):
    """One-time, idempotent registration; only a separate manifest row is written."""
    def register():
        if not isinstance(plan, dict) or plan.get("schema") != "legacy-readonly-archive/v1" or plan.get("plan_digest") != digest(plan.get("entries")):
            raise ValueError("升级档案计划无效")
        previous = store.get(KIND, KEY)
        if previous:
            if previous.get("plan_digest") != plan["plan_digest"] or previous.get("entries") != plan["entries"]:
                raise ValueError("升级档案已登记，不能扩容或替换")
            if any(not archived(store, e["kind"], store.get(e["kind"], e["id"]), e["owner_id"]) for e in plan["entries"]):
                raise ValueError("已登记的旧记录已经变化，档案完整性核对失败")
            return previous
        current = plan_archive(store)
        if current != plan:
            raise ValueError("生成计划后旧数据已经变化；未登记，请重新核对")
        return store.save(KIND, KEY, copy.deepcopy(plan), 0)
    return store.atomic(register)


def archived(store, kind, record, owner):
    if not record or resource_owner(record) != owner:
        return False
    manifest = store.get(KIND, KEY)
    if not manifest or manifest.get("schema") != "legacy-readonly-archive/v1":
        return False
    entry = next((e for e in manifest.get("entries", []) if e.get("kind") == kind and e.get("id") == record.get("id")), None)
    if not entry or entry.get("owner_id") != owner or entry.get("content_digest") != digest(record):
        return False
    if kind == "attachment":
        return archived(store, "run", store.get("run", record.get("run_id", "")), owner)
    return True


def registered(store, kind, record):
    """Detect registered IDs even after tampering; mutations never escape read-only mode."""
    manifest = store.get(KIND, KEY) or {}
    return bool(record and any(e.get("kind") == kind and e.get("id") == record.get("id") for e in manifest.get("entries", [])))


def deny_execution(store, kind, record):
    if registered(store, kind, record):
        raise HTTPException(409, NOTICE)


def label(record):
    return {**copy.deepcopy(record), "legacy_archive": {"status": "legacy_unverified", "read_only": True, "notice": NOTICE}}
