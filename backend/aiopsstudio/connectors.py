"""Read-only source clients. No model supplied URL, shell, or source mutations."""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
import ssl
import time
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from .schema_safety import check_local_schema, offline_validator


class ConnectorError(RuntimeError):
    def __init__(self, message: str, code: str = "source_error"):
        super().__init__(message)
        self.code = code


def _observe(request_observer):
    if request_observer is None:
        return
    observed = request_observer()
    if inspect.isawaitable(observed):
        if inspect.iscoroutine(observed):
            observed.close()
        raise ConnectorError("请求授权观察器必须为同步函数，未发送请求", "request_observer_invalid")


class _BoundedStream(httpx.AsyncByteStream):
    def __init__(self, stream, limit):
        self.stream, self.limit = stream, limit

    async def __aiter__(self):
        size = 0
        async for chunk in self.stream:
            size += len(chunk)
            if size > self.limit:
                raise ConnectorError("MCP 传输响应超过有界读取上限，原响应未完整保存", "response_limit")
            yield chunk

    async def aclose(self):
        await self.stream.aclose()


class _BoundedTransport(httpx.AsyncBaseTransport):
    def __init__(self, context, limit, request_observer=None, *, allowed_urls=None):
        self.transport = httpx.AsyncHTTPTransport(verify=context, retries=0, trust_env=False)
        self.limit = limit
        self.request_observer = request_observer
        self.allowed_urls = None if allowed_urls is None else frozenset(httpx.URL(url) for url in allowed_urls)

    async def handle_async_request(self, request):
        if self.allowed_urls is not None and request.url not in self.allowed_urls:
            raise ConnectorError("实际源请求目标与固定连接目的地不一致，未发送请求", "request_target_changed")
        _observe(self.request_observer)
        response = await self.transport.handle_async_request(request)
        # MCP follows same-origin redirects itself, ignoring the client's
        # follow_redirects=False. Reject the response before the SDK can send
        # its next request with this endpoint's credential and JSON-RPC body.
        if 300 <= response.status_code < 400:
            await response.aclose()
            raise ConnectorError("源返回未批准重定向，未跟随 Location", "redirect_forbidden")
        response.stream = _BoundedStream(response.stream, self.limit)
        return response

    async def aclose(self):
        await self.transport.aclose()


def tool_digest(tool: dict) -> str:
    fields = {k: tool.get(k) for k in ("name", "inputSchema", "outputSchema")}
    return hashlib.sha256(json.dumps(fields, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def tool_alias(server_id: str, name: str) -> str:
    """Stable, provider-safe, collision-resistant and never silently truncated."""
    stem = re.sub(r"[^A-Za-z0-9_-]", "_", name)[:42]
    suffix = hashlib.sha256(f"{server_id}\0{name}".encode()).hexdigest()[:16]
    return f"mcp_{stem}_{suffix}"


def _check_tool_schema(item):
    # A remote schema may describe data, never trigger remote $ref retrieval.
    try:
        for key in ("inputSchema", "outputSchema"):
            if item.get(key) is not None:
                check_local_schema(item[key])
    except Exception:
        raise ConnectorError("工具目录包含无效或远程引用的 JSON Schema", "catalog_schema_invalid") from None


def _tls(config: dict) -> ssl.SSLContext:
    if config.get("verify_tls") is False or config.get("tls_verify") is False:
        raise ConnectorError("不允许关闭 TLS 证书校验", "tls_required")
    return ssl.create_default_context(cafile=config.get("ca_file") or config.get("tls_ca_file") or None)


def _headers(config: dict, vault) -> dict[str, str]:
    # Host override supports the already approved loopback-only Grafana tunnel.
    headers = {k: str(v) for k, v in config.get("headers", {}).items() if k.lower() in {"host", "accept"}}
    auth = config.get("auth_type", "none")
    if auth != "none":
        ref = config.get("secret_id")
        if not ref:
            raise ConnectorError("源连接缺少有效凭据引用", "credential_missing")
        try:
            secret = vault.get(ref)
        except Exception:
            raise ConnectorError("凭据不存在或已经撤销", "credential_revoked") from None
        if auth == "bearer":
            headers["Authorization"] = "Bearer " + secret
        elif auth in {"header", "custom_header"}:
            name = config.get("auth_header") or config.get("header_name") or "Authorization"
            if not re.fullmatch(r"[A-Za-z0-9-]+", name):
                raise ConnectorError("认证头名称无效", "invalid_config")
            headers[name] = secret
        else:
            raise ConnectorError("暂不支持此认证类型", "invalid_config")
    return headers


def _url(config: dict) -> str:
    url = config.get("url") or config.get("base_url") or ""
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ConnectorError("连接地址必须是没有内嵌凭据的 HTTP(S) 地址", "invalid_config")
    return url.rstrip("/")


@asynccontextmanager
async def _session(config: dict, vault, request_observer=None):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    timeout = min(max(float(config.get("timeout_seconds", 30)), 1), 60)
    async with asyncio.timeout(timeout):
        context = _tls(config)
        wire_limit = min(max(int(config.get("max_response_bytes", 1048576)), 1024), 8 * 1048576) + 65536
        async with httpx.AsyncClient(headers={**_headers(config, vault), "Accept-Encoding": "identity"}, verify=context,
                                     transport=_BoundedTransport(context, wire_limit, request_observer, allowed_urls=[_url(config)]),
                                     timeout=httpx.Timeout(timeout), follow_redirects=False,
                                     trust_env=False) as client:
            async with streamable_http_client(_url(config), http_client=client) as (read, write, _):
                async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=timeout)) as session:
                    initialized = await session.initialize()
                    yield session, initialized


_control_locks: dict[str, asyncio.Lock] = {}


async def mcp_discover(config: dict, vault, request_observer=None) -> dict:
    """Official SDK handshake and complete bounded cursor traversal, never calls a tool."""
    key = config.get("id", config.get("url", ""))
    lock = _control_locks.setdefault(key, asyncio.Lock())
    if lock.locked():
        raise ConnectorError("此服务已有连接探测或工具发现请求", "control_busy")
    async with lock:
        async with _session(config, vault, request_observer) as (session, initialized):
            items, seen, cursor = [], set(), None
            for _ in range(50):
                result = await session.list_tools(cursor=cursor)
                for source in result.tools:
                    item = source.model_dump(mode="json", by_alias=True, exclude_none=True)
                    _check_tool_schema(item)
                    if any(t["name"] == item["name"] for t in items):
                        raise ConnectorError("工具目录包含重复名称", "catalog_invalid")
                    item.update(alias=tool_alias(key, item["name"]), definition_digest=tool_digest(item))
                    # Hint alone is never permission to execute.
                    item["readonly"] = False
                    items.append(item)
                if len(items) > 1000:
                    raise ConnectorError("工具目录超过平台上限，发现未完成", "catalog_limit")
                cursor = result.nextCursor
                if not cursor:
                    return {"items": items, "complete": True, "server_info": initialized.serverInfo.model_dump(mode="json"),
                            "protocol_version": initialized.protocolVersion}
                if cursor in seen:
                    raise ConnectorError("工具发现游标重复，目录未查全", "catalog_incomplete")
                seen.add(cursor)
            raise ConnectorError("工具发现分页超过上限，目录未查全", "catalog_incomplete")


def permitted_tool(config: dict, tool_name: str) -> dict:
    if config.get("blocked"):
        raise ConnectorError("该 MCP 服务已立即阻止后续调用", "capability_blocked")
    if tool_name not in config.get("allowed_tools", []):
        raise ConnectorError("工具未列入明确开放的只读白名单", "tool_not_allowed")
    matches = [t for t in config.get("tools", []) if t.get("name") == tool_name]
    if len(matches) != 1 or matches[0].get("readonly") is not True:
        raise ConnectorError("工具尚未登记为经过检查的只读工具", "tool_not_readonly")
    return matches[0]


async def mcp_call(config: dict, vault, tool_name: str, arguments: dict, request_observer=None) -> dict:
    frozen = permitted_tool(config, tool_name)
    offline_validator(frozen.get("inputSchema", {"type": "object"})).validate(arguments)
    async with _session(config, vault, request_observer) as (session, initialized):
        # A fresh connection must recheck remote definitions before dispatch.
        cursor, found, seen, names = None, None, set(), set()
        for _ in range(50):
            listing = await session.list_tools(cursor=cursor)
            for tool in listing.tools:
                if tool.name in names:
                    raise ConnectorError("工具目录包含重复名称，停止调用", "catalog_invalid")
                names.add(tool.name)
                _check_tool_schema(tool.model_dump(mode="json", by_alias=True))
            if len(names) > 1000:
                raise ConnectorError("工具目录超过平台上限，停止调用", "catalog_limit")
            found = next((t for t in listing.tools if t.name == tool_name), found)
            cursor = listing.nextCursor
            if not cursor:
                break
            if cursor in seen:
                raise ConnectorError("工具目录分页不完整，停止调用", "catalog_incomplete")
            seen.add(cursor)
        else:
            raise ConnectorError("工具目录超过分页上限，停止调用", "catalog_incomplete")
        if found is None or tool_digest(found.model_dump(mode="json", by_alias=True)) != tool_digest(frozen):
            raise ConnectorError("远端工具定义与本次固定版本不一致", "tool_definition_changed")
        result = await session.call_tool(tool_name, arguments)
        native = result.model_dump(mode="json", by_alias=True, exclude_none=True)
        native["_source"] = {"server_id": config.get("id"), "tool_name": tool_name,
                             "definition_digest": tool_digest(frozen), "protocol_version": initialized.protocolVersion,
                             "server_info": initialized.serverInfo.model_dump(mode="json")}
        return native


def _ongrid(settings: dict) -> dict:
    cfg = dict(settings.get("ongrid") or {})
    cfg.setdefault("url", settings.get("ongrid_url") or settings.get("ongrid_base_url", ""))
    cfg.setdefault("secret_id", settings.get("ongrid_secret_id"))
    cfg.setdefault("auth_type", settings.get("ongrid_auth_type", "bearer" if cfg.get("secret_id") else "none"))
    cfg.setdefault("ca_file", settings.get("ongrid_ca_file"))
    cfg.setdefault("email", settings.get("ongrid_email"))
    cfg.setdefault("password_secret_id", settings.get("ongrid_password_secret_id"))
    return cfg


_ongrid_tokens: dict[tuple, tuple[str, float]] = {}


async def _get(config: dict, vault, path: str, params: dict | None = None, request_observer=None) -> Any:
    limit = min(int(config.get("max_response_bytes", 1048576)), 8 * 1048576)
    headers = _headers(config, vault)
    password_ref = config.get("password_secret_id")
    token_key = (_url(config), config.get("secret_id"), password_ref)
    # Re-check revocation even when an access token is still cached.
    password = vault.get(password_ref) if password_ref else None
    cached = _ongrid_tokens.get(token_key)
    if cached and cached[1] > time.monotonic():
        headers["Authorization"] = "Bearer " + cached[0]
    context = _tls(config)
    fixed_get = httpx.URL(_url(config) + path, params=params)
    fixed_urls = [fixed_get]
    if password and config.get("email"):
        fixed_urls.append(httpx.URL(_url(config) + "/v1/auth/login"))
    async with httpx.AsyncClient(verify=context, headers={**headers, "Accept-Encoding": "identity"},
                                 transport=_BoundedTransport(context, limit + 65536, request_observer, allowed_urls=fixed_urls),
                                 timeout=min(float(config.get("timeout_seconds", 15)), 60),
                                 follow_redirects=False, trust_env=False) as client:
        async with client.stream("GET", _url(config) + path, params=params) as response:
            if response.status_code == 401 and password and config.get("email"):
                login = await client.post(_url(config) + "/v1/auth/login", json={"email": config["email"], "password": password})
                login.raise_for_status()
                token_data = login.json()
                token = token_data.get("access_token") or token_data.get("token") or (token_data.get("data") or {}).get("access_token")
                if not token:
                    raise ConnectorError("Ongrid 登录未返回 access_token", "authentication_failed")
                _ongrid_tokens[token_key] = (token, time.monotonic() + 300)
                # Keep the refreshed credential in this HTTP client only.
                client.headers["Authorization"] = "Bearer " + token
                async with client.stream("GET", _url(config) + path, params=params) as retry:
                    retry.raise_for_status()
                    bounded = bytearray()
                    async for chunk in retry.aiter_bytes():
                        bounded.extend(chunk)
                        if len(bounded) > limit:
                            raise ConnectorError("源 API 响应超过留存上限", "response_limit")
                    return json.loads(bounded)
            response.raise_for_status()
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > limit:
                    raise ConnectorError("源 API 响应超过留存上限，结果不完整", "response_limit")
            return json.loads(body)


async def alert_list(settings: dict, vault, request_observer=None) -> dict:
    cfg, items, limitations, total = _ongrid(settings), [], [], None
    pages = min(max(int(cfg.get("max_pages", 5)), 1), 20)
    requests = 0
    def count():
        nonlocal requests
        _observe(request_observer)
        requests += 1
    for page in range(1, pages + 1):
        data = await _get(cfg, vault, "/v1/alerts/incidents", {"page": page, "page_size": 100}, count)
        batch = data.get("items", [])
        items.extend(batch)
        total = data.get("total", total)
        if len(batch) < 100:
            break
    else:
        limitations.append("告警列表达到分页上限，未宣称查全")
    return {"items": items, "total": total, "limitations": limitations, "source_request_count": requests}


async def alert_detail(settings: dict, vault, id: str, request_observer=None) -> dict:
    cfg = _ongrid(settings)
    key = quote(str(id), safe="")
    requests = 0
    def count():
        nonlocal requests
        _observe(request_observer)
        requests += 1
    detail = await _get(cfg, vault, f"/v1/alerts/incidents/{key}", request_observer=count)
    limitations = []
    try:
        events = await _get(cfg, vault, f"/v1/alerts/incidents/{key}/events", {"limit": 200}, count)
        timeline = events.get("items", [])
        if len(timeline) >= 200:
            limitations.append("时间线达到源端 limit=200；该 API 无分页游标，未宣称完整")
    except Exception as error:
        timeline = []
        limitations.append(f"告警详情已取得，时间线读取失败（{type(error).__name__}）")
    return {"id": str(id), "detail": detail, "timeline": timeline, "limitations": limitations, "source_request_count": requests}
