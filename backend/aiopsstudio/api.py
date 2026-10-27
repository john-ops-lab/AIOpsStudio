"""Authenticated management API; execution endpoints are attached by runtime."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import secrets
import time
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .config import ConfigError, ConfigService, redact, digest
from .store import StoreConflict, utcnow
from .identity import actor_id, require_admin, resource_owner, public_user
from .compat import COOKIE_NAME, LEGACY_COOKIE_NAME, legacy_cookie_expiry
from .access_policy import PolicyError, PolicyService


def configure_api(app: FastAPI):
    store, vault = app.state.store, app.state.vault
    service: ConfigService = app.state.config
    control_locks: dict[str, asyncio.Lock] = {}
    login_failures: dict[str, list[float]] = {}
    cookie_name = COOKIE_NAME

    async def json_body(request):
        value = await request.json()
        if not isinstance(value, dict):
            raise ConfigError("请求正文必须为 JSON 对象", "invalid_request")
        return value

    def public(value):
        return redact(value, vault)

    def catalog_view(kind, item, request):
        if request.state.user.get("role") == "admin":
            return public(item)
        if not item.get("current_version") or item.get("enabled") is False or item.get("blocked"):
            return None
        if kind == "mcp":
            try:
                PolicyService(store).authorize_source(subject_id=actor_id(request), source_id=item["id"],
                    tool_name="$catalog", config=item, arguments={}, target=None,
                    environment=(store.get("settings", "main") or {}).get("environment"), purpose="discovery")
            except PolicyError:
                return None
        fields = {"id", "name", "display_name", "enabled", "blocked", "current_version"}
        if kind == "model":
            fields |= {"model", "provider"}
        return {key: value for key, value in item.items() if key in fields}

    def history_visible(record, owner):
        if resource_owner(record) != owner:
            return False
        authorize = getattr(getattr(app.state, "runtime", None), "authorize_history", None)
        if not callable(authorize):
            return not (record.get("evidence") or record.get("history_context") or record.get("report"))
        try:
            authorize(record, owner, allow_legacy_archive=True)
            return True
        except (HTTPException, PolicyError):
            return False

    @app.exception_handler(ConfigError)
    async def configuration_error(request, error):
        return JSONResponse(public({"detail": error.detail, "code": error.code}), status_code=404 if error.code == "not_found" else 422)

    @app.exception_handler(StoreConflict)
    async def conflict_error(request, error):
        return JSONResponse({"detail": str(error), "code": "revision_conflict"}, status_code=409)

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        if not request.url.path.startswith("/api/"):
            return await call_next(request)
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin", "")
            expected_origin = app.state.public_origin.rstrip("/")
            if origin.rstrip("/") != expected_origin or request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"detail": "变更请求必须来自工作台同源页面", "code": "origin_rejected"}, status_code=403)
            if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
                return JSONResponse({"detail": "变更请求必须使用 application/json", "code": "content_type_rejected"}, status_code=415)
            upload = request.url.path.startswith("/api/conversations/") and request.url.path.endswith("/attachments")
            body_limit = (8 if upload else 2) * 1024 * 1024
            content_length = request.headers.get("content-length")
            if content_length and (not content_length.isdigit() or int(content_length) > body_limit):
                return JSONResponse({"detail": "请求正文超过允许的 " + str(body_limit // (1024 * 1024)) + " MiB"}, status_code=413)
            chunks, received = [], 0
            async for chunk in request.stream():
                received += len(chunk)
                if received > body_limit:
                    return JSONResponse({"detail": "实际请求正文超过允许的大小"}, status_code=413)
                chunks.append(chunk)
            request._body = b"".join(chunks)
        token = request.cookies.get(cookie_name, "")
        old_token = request.cookies.get(LEGACY_COOKIE_NAME, "")
        if token and old_token and token != old_token:
            response = JSONResponse({"detail": "检测到新旧登录会话冲突，请重新登录", "code": "session_cookie_conflict"}, status_code=401)
            response.delete_cookie(cookie_name, path="/")
            response.delete_cookie(LEGACY_COOKIE_NAME, path="/")
            return response
        using_legacy = not token and bool(old_token)
        token = token or old_token
        sid = hashlib.sha256(token.encode()).hexdigest() if token else ""
        user = app.state.identity.session_user(sid)
        legacy_expires = legacy_cookie_expiry(store.get("session", sid)) if using_legacy else 0
        if using_legacy and legacy_expires <= time.time():
            user = None
        request.state.user = user
        request.state.authenticated = user is not None
        request.state.session_id = sid
        if request.url.path not in {"/api/session", "/api/login"} and not request.state.authenticated:
            return JSONResponse({"detail": "请先登录工作台", "code": "authentication_required"}, status_code=401)
        restored = store.get("restore_guard", "main")
        if user and restored and not restored.get("verified") and request.url.path not in {"/api/login", "/api/session", "/api/logout", "/api/restore-status"}:
            return JSONResponse({"detail": "恢复实例尚未核对最新授权，工作空间暂不可访问", "code": "restore_unverified"}, status_code=503)
        if user and user["role"] != "admin":
            management = ("/api/skills", "/api/mcp-servers", "/api/model-configs", "/api/settings", "/api/secrets", "/api/config-audit", "/api/users", "/api/tool-tests")
            admin_read = ("/api/secrets", "/api/config-audit", "/api/users")
            restricted = any(request.url.path == p or request.url.path.startswith(p + "/") for p in (admin_read if request.method in {"GET", "HEAD"} else management))
            if restricted:
                return JSONResponse({"detail": "此操作仅管理员可用"}, status_code=403)
        response = await call_next(request)
        if old_token:
            response.delete_cookie(LEGACY_COOKIE_NAME, path="/")
        if user and using_legacy and request.url.path not in {"/api/login", "/api/logout"}:
            response.set_cookie(cookie_name, token, httponly=True, secure=app.state.public_origin.startswith("https://"),
                samesite="strict", max_age=max(0, int(legacy_expires - time.time())), path="/")
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        return response

    @app.get("/api/restore-status")
    async def restore_status(request: Request):
        actor_id(request)
        guard = store.get("restore_guard", "main")
        return {"authorization_verified": not guard or guard.get("verified") is True}

    @app.get("/api/session")
    async def session(request: Request):
        return {"authenticated": request.state.authenticated, "user": request.state.user}

    @app.post("/api/login")
    async def login(request: Request):
        address = request.client.host if request.client else "unknown"
        now = time.time()
        failures = [t for t in login_failures.get(address, []) if now - t < 300]
        login_failures[address] = failures
        if len(failures) >= 10:
            return JSONResponse({"detail": "登录尝试过多，请 5 分钟后重试", "code": "login_rate_limited"}, status_code=429)
        data = await json_body(request)
        user = app.state.identity.authenticate(data.get("username", "admin"), data.get("password", ""))
        if not user:
            failures.append(now)
            service.audit("login_failed", "session", "admin")
            return JSONResponse({"detail": "登录口令错误", "code": "invalid_credentials"}, status_code=401)
        login_failures.pop(address, None)
        if request.state.session_id:
            store.delete("session", request.state.session_id)
        token = secrets.token_urlsafe(40)
        sid = hashlib.sha256(token.encode()).hexdigest()
        store.save("session", sid, {"user_id": user["id"], "user": user["id"], "expires_at": now + 12 * 3600})
        service.audit("login", "session", user["id"], actor=user["id"])
        response = JSONResponse({"authenticated": True, "user": user})
        response.set_cookie(cookie_name, token, httponly=True, secure=app.state.public_origin.startswith("https://"), samesite="strict", max_age=12 * 3600, path="/")
        response.delete_cookie(LEGACY_COOKIE_NAME, path="/")
        return response

    @app.post("/api/logout")
    async def logout(request: Request):
        store.delete("session", request.state.session_id)
        service.audit("logout", "session", actor_id(request), actor=actor_id(request))
        response = JSONResponse({"authenticated": False, "user": None})
        response.delete_cookie(cookie_name, path="/")
        response.delete_cookie(LEGACY_COOKIE_NAME, path="/")
        return response

    @app.get("/api/users")
    async def users(request: Request):
        require_admin(request)
        return {"items": [public_user(user) for user in store.list("user")]}

    @app.post("/api/users")
    async def create_user(request: Request):
        require_admin(request)
        user = app.state.identity.create(await json_body(request))
        service.audit("create", "user", user["id"], {"username": user["username"], "role": user["role"]}, actor=actor_id(request))
        return user

    @app.post("/api/users/{id}/state")
    async def user_state(id: str, request: Request):
        require_admin(request)
        user = app.state.identity.state(id, (await json_body(request)).get("enabled"))
        service.audit("state", "user", id, {"enabled": user["enabled"]}, actor=actor_id(request))
        return user

    def register(kind, path):
        @app.get(path, name=f"list_{kind}")
        async def listing(request: Request):
            return {"items": [view for item in store.list(kind) if (view := catalog_view(kind, item, request)) is not None]}

        @app.post(path, name=f"create_{kind}")
        async def create(request: Request):
            return public(service.save(kind, await json_body(request)))

        @app.get(path + "/{id}", name=f"get_{kind}")
        async def read(id: str, request: Request):
            view = catalog_view(kind, service.require(kind, id), request)
            if view is None:
                raise HTTPException(404, "配置不存在或当前不可访问")
            return view

        @app.put(path + "/{id}", name=f"save_{kind}")
        async def update(id: str, request: Request):
            return public(service.save(kind, await json_body(request), id))

        @app.post(path + "/{id}/publish", name=f"publish_{kind}")
        async def publish(id: str, request: Request):
            payload = await json_body(request)
            if "expected_revision" not in payload:
                raise ConfigError("发布必须提供 expected_revision", "revision_required")
            result = await asyncio.to_thread(service.publish, kind, id, payload["expected_revision"],
                channel=payload.get("channel", "formal"), evaluation_id=payload.get("evaluation_id"))
            return public(result)

        @app.post(path + "/{id}/state", name=f"state_{kind}")
        async def state(id: str, request: Request):
            return public(service.state(kind, id, await json_body(request)))

        @app.get(path + "/{id}/versions", name=f"versions_{kind}")
        async def versions(id: str, request: Request):
            require_admin(request)
            return {"items": public(service.require(kind, id).get("versions", []))}

        @app.post(path + "/{id}/rollback", name=f"rollback_{kind}")
        async def rollback(id: str, request: Request):
            data = await json_body(request)
            return public(service.rollback(kind, id, data.get("version_id")))

        @app.post(path + "/{id}/validate", name=f"validate_{kind}")
        async def validate(id: str, request: Request):
            data = await json_body(request)
            item = service.require(kind, id)
            if data.get("expected_revision", item["revision"]) != item["revision"]:
                raise StoreConflict("校验时配置已变化，请重新读取")
            return public(service.validate(kind, item))

    register("skill", "/api/skills")
    register("mcp", "/api/mcp-servers")
    register("model", "/api/model-configs")

    async def control(id: str, discover: bool, request: Request):
        from . import connectors
        require_admin(request)
        lock = control_locks.setdefault(id, asyncio.Lock())
        if lock.locked():
            return JSONResponse({"detail": "该服务已有连接检查正在进行", "code": "control_request_in_progress"}, status_code=409)
        async with lock:
            config = service.require("mcp", id)
            expected = config["revision"]
            snapshot = copy.deepcopy(config)
            policy = getattr(app.state, "policy", None) or PolicyService(store)
            def authorize():
                try:
                    actor = actor_id(request)
                    identity = app.state.identity.session_user(request.state.session_id)
                    if not identity or identity.get("id") != actor:
                        raise PolicyError("policy_source_subject_revoked")
                    current = service.require("mcp", id)
                    if current.get("blocked"):
                        raise PolicyError("policy_source_blocked")
                    if any(current.get(key) != snapshot.get(key) for key in ("url", "secret_id", "password_secret_id", "auth_type", "auth_header", "headers", "verify_tls", "ca_file", "tls_ca_file")):
                        raise PolicyError("policy_source_configuration_changed")
                    for field in ("secret_id", "password_secret_id"):
                        if snapshot.get(field):
                            try:
                                vault.get(snapshot[field])
                            except ConfigError:
                                raise PolicyError("policy_source_credential_revoked") from None
                    return policy.authorize_source(subject_id=actor, source_id=id, tool_name="$catalog", config=snapshot,
                        arguments={}, target=None, environment=(store.get("settings", "main") or {}).get("environment"), purpose="discovery")
                except PolicyError as error:
                    service.audit("source_access_denied", "mcp", id, {"code": error.code, "purpose": "discovery"}, actor=actor_id(request))
                    raise
            authorize()
            start = time.monotonic()
            try:
                result = await asyncio.wait_for(connectors.mcp_discover(snapshot, vault, request_observer=authorize), timeout=min(config.get("timeout_seconds", 30), 120) + 5)
                authorize()  # A response in flight cannot bypass a newer revoke.
                complete = result.get("complete", False)
                check = {"status": "succeeded" if complete else "partial", "complete": complete, "checked_at": utcnow(), "duration_ms": round((time.monotonic() - start) * 1000), "server_info": result.get("server_info"), "protocol_version": result.get("protocol_version"), "configuration_digest": digest(snapshot)}
                config["last_probe"] = check
                if discover:
                    previous = {t["name"]: t for t in config.get("tools", [])}
                    tools = []
                    for raw in result.get("items", []):
                        tool = copy.deepcopy(raw)
                        name = tool.get("name", "")
                        old = previous.get(name, {})
                        tool["alias"] = old.get("alias") or "m_" + hashlib.sha256((id + ":" + name).encode()).hexdigest()[:24]
                        same_schema = old.get("inputSchema") == tool.get("inputSchema") and old.get("outputSchema") == tool.get("outputSchema")
                        tool["readonly"] = bool(old.get("readonly") and same_schema)
                        tools.append(tool)
                    config["candidate_tools"] = tools
                    config["discovery"] = check
                    if complete:
                        config["tools"] = tools
                        config["allowed_tools"] = [name for name in config.get("allowed_tools", []) if any(t.get("name") == name and t.get("readonly") for t in tools)]
                config["status"] = "checked" if complete else "partial"
                saved = store.save("mcp", id, config, expected)
                service.audit("discover" if discover else "probe", "mcp", id, check)
                return public({**check, "items": config.get("candidate_tools", []) if discover else [], "config": saved})
            except (StoreConflict, PolicyError):
                raise
            except Exception as error:
                check = {"status": "failed", "complete": False, "checked_at": utcnow(), "duration_ms": round((time.monotonic() - start) * 1000), "error": "源连接、协议或目录核对失败；内部错误未公开"}
                latest = store.get("mcp", id)
                if latest and latest["revision"] == expected:
                    latest.update(last_probe=check)
                    store.save("mcp", id, latest, expected)
                service.audit("discover_failed" if discover else "probe_failed", "mcp", id, check)
                return JSONResponse({"detail": "MCP 连接检查失败", "code": "mcp_probe_failed", "check": check}, status_code=502)

    @app.post("/api/mcp-servers/{id}/probe")
    async def probe(id: str, request: Request):
        return await control(id, False, request)

    @app.post("/api/mcp-servers/{id}/discover")
    async def discover(id: str, request: Request):
        return await control(id, True, request)

    @app.get("/api/mcp-servers/{id}/tools")
    async def tools(id: str, request: Request):
        require_admin(request)
        config = service.require("mcp", id)
        return {"items": public(config.get("tools", [])), "complete": config.get("discovery", {}).get("complete", False)}

    @app.post("/api/model-configs/{id}/test")
    async def model_test(id: str, request: Request):
        require_admin(request)
        config = service.require("model", id)
        data = await json_body(request)
        if not data.get("client_request_id"):
            raise ConfigError("模型测试必须提供 client_request_id")
        if data.get("expected_revision", config["revision"]) != config["revision"]:
            raise StoreConflict("模型配置已变化，请重新读取后试运行")
        result = await app.state.runtime.submit({"purpose": "model_test", "model_id": id, "config_version": data.get("config_version", "draft"), "client_request_id": data["client_request_id"], "title": "模型中文工具调用样本"}, owner_id=actor_id(request))
        return public(result)

    @app.get("/api/settings")
    async def settings(request: Request):
        item = service.require("settings", "main")
        if request.state.user.get("role") == "admin":
            return public(item)
        default = store.get("model", item.get("default_model_id", ""))
        return {"default_model_id": default["id"] if default and catalog_view("model", default, request) else None}

    @app.put("/api/settings")
    async def update_settings(request: Request):
        return public(service.settings(await json_body(request)))

    @app.get("/api/config-audit")
    async def audit():
        return {"items": public(sorted(store.list("audit"), key=lambda a: a.get("time", ""), reverse=True)[:500])}

    @app.get("/api/secrets")
    async def secret_metadata():
        return {"items": [{"id": item["id"], "created_at": item.get("created_at"), "revoked": item.get("revoked", False)} for item in store.list("secret")]}

    @app.post("/api/secrets/{id}/revoke")
    async def revoke(id: str):
        vault.revoke(id)
        service.audit("revoke", "secret", id)
        return {"id": id, "revoked": True}

    @app.get("/api/overview")
    async def overview(request: Request):
        owner_id = actor_id(request)
        runs = sorted([r for r in store.list("run") if history_visible(r, owner_id)], key=lambda r: r.get("created_at", ""), reverse=True)
        running = [r for r in runs if r.get("status") == "running"]
        queued = [r for r in runs if r.get("status") == "queued"]
        owned_runs = {run["id"] for run in runs}
        evidence = [item for item in store.list("evidence") if item.get("run_id") in owned_runs]
        storage_bytes = sum(int(e.get("bytes", e.get("byte_count", e.get("size_bytes", 0))) or 0) for e in evidence)
        def run_projection(run):
            if run is None:
                return None
            runtime = getattr(app.state, "runtime", None)
            source = runtime.public(run, allow_legacy_archive=True) if runtime else run
            allowed = {"id", "thread_id", "conversation_id", "attempt_id", "purpose", "title", "status", "skill_id", "skill_version", "target", "incident_id", "analysis_start", "analysis_end", "created_at", "updated_at", "error", "usage", "cancel_requested", "report_attachment", "legacy_archive"}
            return {key: value for key, value in source.items() if key in allowed}
        visible_mcps = [m for item in store.list("mcp") if (m := catalog_view("mcp", item, request)) is not None]
        counts = {"skills": sum(catalog_view("skill", item, request) is not None for item in store.list("skill")), "mcp_servers": len(visible_mcps),
            "models": sum(catalog_view("model", item, request) is not None for item in store.list("model")), "runs": len(runs), "evidence": len(evidence)}
        return public({"counts": counts, "queue": {"running": len(running), "queued": len(queued), "waiting": len(queued), "limit": int(service.require("settings", "main").get("queue_limit", 10)), "current": run_projection(running[0]) if running else None}, "services": [{"id": m["id"], "name": m["name"], "type": "mcp", "status": m.get("last_probe", {}).get("status", "unchecked"), "enabled": m.get("enabled", False), "checked_at": m.get("last_probe", {}).get("checked_at")} for m in visible_mcps] + [{"id": "glpi", "name": "GLPI", "type": "ticket", "status": "not_connected", "detail": "未接入"}], "recent_runs": [run_projection(r) for r in runs[:10]], "storage": {"evidence_bytes": storage_bytes, "quota_bytes": service.require("settings", "main")["storage_quota_bytes"], "database": "sqlite_test" if store.sqlite else "postgresql", "measurement": "stored_evidence_metadata"}})
