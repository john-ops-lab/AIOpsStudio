"""Authenticated trust-policy editing and saved fragment navigation only."""
from __future__ import annotations

import copy
import hashlib
import inspect
import json
import re

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from .access_policy import PolicyError, PolicyService
from .analysis_context import ContextError
from .config import ConfigError, redact
from .identity import actor_id, require_admin
from .material_reading import ReadingError, fingerprint
from .store import StoreConflict

_META = {"id", "revision", "created_at", "updated_at", "expected_revision", "configured"}


def _policy_content(value):
    return {key: copy.deepcopy(item) for key, item in value.items() if key not in _META}


def configure_trust_api(app):
    """Call after Runtime assignment. Idempotent for explicit test assembly."""
    if getattr(app.state, "trust_api_configured", False):
        return
    app.state.trust_api_configured = True
    store = app.state.store
    if not getattr(app.state, "policy", None):
        app.state.policy = PolicyService(store)
    def authorize_run(run_id, owner):
        run = store.get("run", run_id)
        if not run or (run.get("owner_id") or "admin") != owner:
            raise HTTPException(404, "材料不存在或不可访问")
        runtime = getattr(app.state, "runtime", None)
        authorize = getattr(runtime, "authorize_history", None)
        if not callable(authorize):
            raise PolicyError("policy_unavailable_history")
        authorize(run, owner)
        return True
    app.state.attachments.authorize_run = authorize_run

    def public(value):
        return redact(value, app.state.vault)

    @app.exception_handler(PolicyError)
    async def policy_error(request, error):
        if error.code.startswith(("policy_missing", "policy_unavailable")):
            status, detail = 503, "当前策略未就绪，操作未派发"
        elif error.code.startswith("policy_material"):
            status, detail = 404, "材料不存在或不可访问"
        elif error.code.startswith("policy_invalid"):
            status, detail = 422, "授权或出站策略配置无效"
        else:
            status, detail = 403, "当前授权或数据出站规则不允许此操作"
        return JSONResponse({"detail": detail, "code": error.code}, status_code=status)

    @app.exception_handler(ContextError)
    async def context_error(request, error):
        return JSONResponse({"detail": "分析对象、窗口或查询目录未通过核对", "code": error.code}, status_code=422)

    @app.exception_handler(ReadingError)
    async def reading_error(request, error):
        if error.code == "reading_budget":
            status, detail = 429, "本轮材料读取预算已耗尽"
        elif error.code in {"not_found", "material_unavailable", "anchor_invalid", "material_changed"}:
            status, detail = 404, "材料不存在、已变化或不可访问"
        else:
            status, detail = 422, "材料读取位置或请求格式无效"
        return JSONResponse({"detail": detail, "code": error.code}, status_code=status)

    def register_policy(name, kind):
        @app.get("/api/access-policies/" + name, name="get_" + kind)
        async def get_policy(request: Request):
            require_admin(request)
            current = store.get(kind, "main")
            return public({"configured": True, "revision": current["revision"], "policy": current}) if current else {"configured": False, "revision": 0}

        @app.put("/api/access-policies/" + name, name="put_" + kind)
        async def put_policy(request: Request):
            require_admin(request)
            body = await request.json()
            if not isinstance(body, dict) or type(body.get("expected_revision")) is not int or body["expected_revision"] < 0:
                raise ConfigError("策略保存须提供非负整数 expected_revision", "revision_required")
            candidate = body.get("policy", body)
            if not isinstance(candidate, dict):
                raise ConfigError("策略必须为 JSON 对象", "invalid_policy")
            candidate = _policy_content(candidate)
            app.state.policy.validate_document(kind, candidate)
            if public(candidate) != candidate:
                raise ConfigError("策略只允许凭据引用，不能保存凭据原文", "policy_secret_input")
            # Field-name protection complements known-value removal. References
            # secret_id/secret_ids are allowed; actual secret-bearing keys are not.
            app.state.config._reject_embedded_secrets(candidate)
            encoded = json.dumps(candidate, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
            digest = hashlib.sha256(encoded.encode()).hexdigest()
            version_key = name + ":" + candidate["version"]
            def save():
                previous = store.get(kind, "main")
                if body["expected_revision"] != (previous or {}).get("revision", 0):
                    raise StoreConflict("策略已变化，请重新读取后保存")
                history = store.get("policy_version", version_key)
                if history and (history.get("policy_kind") != kind or history.get("digest") != digest):
                    raise StoreConflict("策略版本已使用，修改内容必须使用新版本")
                if previous and previous.get("version") == candidate["version"]:
                    if _policy_content(previous) != candidate:
                        raise StoreConflict("策略版本已使用，修改内容必须使用新版本")
                    return previous
                # Register a seeded current version as well, so a later edit
                # cannot reuse it after another version became current.
                if previous:
                    prior_content = _policy_content(previous)
                    prior_key = name + ":" + str(previous.get("version"))
                    if not store.get("policy_version", prior_key):
                        prior_digest = hashlib.sha256(json.dumps(prior_content, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
                        store.save("policy_version", prior_key, {"policy_kind": kind, "version": previous.get("version"), "digest": prior_digest})
                saved = store.save(kind, "main", candidate, body["expected_revision"])
                if not history:
                    store.save("policy_version", version_key, {"policy_kind": kind, "version": candidate["version"], "digest": digest})
                app.state.config.audit("save_policy", kind, "main", {"version": candidate["version"], "revision": saved["revision"], "policy_digest": digest}, actor=actor_id(request))
                return saved
            saved = store.atomic(save)
            return public({"configured": True, "revision": saved["revision"], "policy": saved})

    register_policy("source", "access_policy")
    register_policy("egress", "egress_policy")

    async def read(run_id, owner, body):
        try:
            result = app.state.runtime.material_read(run_id, owner, body, consumer="user")
            return await result if inspect.isawaitable(result) else result
        except PolicyError as error:
            if error.code.startswith(("policy_missing", "policy_unavailable")):
                raise
            raise ReadingError("not_found", "材料不存在或不可访问") from None

    @app.post("/api/runs/{id}/material-reads")
    async def material_read(id: str, request: Request):
        owner = actor_id(request)
        body = await request.json()
        if not isinstance(body, dict):
            raise ReadingError("selector_invalid", "材料读取请求必须为对象")
        return public(await read(id, owner, body))

    @app.get("/api/fragments/{anchor_id}")
    async def fragment(anchor_id: str, request: Request):
        owner = actor_id(request)
        if not re.fullmatch(r"frag_[a-f0-9]{32}", anchor_id):
            raise ReadingError("not_found", "材料不存在或不可访问")
        receipt = next((item for item in store.list("material_read") if item.get("anchor_id") == anchor_id and item.get("owner_id") == owner), None)
        if not receipt:
            raise ReadingError("not_found", "材料不存在或不可访问")
        run = store.get("run", receipt.get("run_id", ""))
        if not run or run.get("owner_id") != owner or run.get("attempt_id") != receipt.get("attempt_id"):
            raise ReadingError("not_found", "材料不存在或不可访问")
        anchor = receipt.get("anchor") or {}
        kind = anchor.get("resource_type")
        fields = ("evidence_id",) if kind == "evidence" else ("skill_id", "version_id", "path") if kind == "skill_reference" else None
        if not fields or any(key not in anchor for key in fields) or not isinstance(anchor.get("selector"), dict):
            raise ReadingError("anchor_invalid", "材料片段锚点无效")
        body = {"resource_type": kind, **{key: anchor[key] for key in fields}, "selector": copy.deepcopy(anchor["selector"])}
        result = await read(run["id"], owner, body)
        if result.get("anchor_id") != anchor_id or fingerprint(result.get("data")) != anchor.get("data_digest"):
            raise ReadingError("material_changed", "材料已经变化，不能替换旧片段")
        return public(result)
