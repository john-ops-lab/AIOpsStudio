"""Mock HTTP wire proves the sync guard runs before every actual request."""
import json
import ssl

import httpx
import pytest

from aiopsstudio import connectors


class Denied(RuntimeError):
    pass


@pytest.fixture
def wire(monkeypatch):
    events = []
    handler = None
    def network(request):
        assert events and events[-1][0] == "authorize", "network ran before latest authorization"
        events.append(("network", request.method, request.url.path))
        return handler(request)
    monkeypatch.setattr(connectors.httpx, "AsyncHTTPTransport", lambda **kwargs: httpx.MockTransport(network))
    monkeypatch.setattr(connectors, "_ongrid_tokens", {})
    def configure(responder):
        nonlocal handler
        handler = responder
    return events, configure


def guard(events, deny_at=None, exception=Denied):
    count = 0
    def observe():
        nonlocal count
        count += 1
        events.append(("authorize", count))
        if count == deny_at:
            raise exception("synthetic authorization refusal")
    return observe


@pytest.mark.asyncio
async def test_transport_rechecks_before_each_request_and_denies_zero_outbound(wire):
    events, configure = wire
    configure(lambda request: httpx.Response(200, json={"ok": True}))
    async with httpx.AsyncClient(transport=connectors._BoundedTransport(ssl.create_default_context(), 1024, guard(events))) as client:
        await client.get("https://synthetic.invalid/a")
        await client.post("https://synthetic.invalid/b", json={})
    assert [e[0] for e in events] == ["authorize", "network", "authorize", "network"]
    events.clear()
    async with httpx.AsyncClient(transport=connectors._BoundedTransport(ssl.create_default_context(), 1024, guard(events, 1))) as client:
        with pytest.raises(Denied):
            await client.get("https://synthetic.invalid/never")
    assert events == [("authorize", 1)]


@pytest.mark.asyncio
async def test_observer_typeerror_is_not_retried_without_the_guard(wire):
    events, configure = wire
    configure(lambda request: httpx.Response(200, json={"unexpected": True}))
    async with httpx.AsyncClient(transport=connectors._BoundedTransport(ssl.create_default_context(), 1024, guard(events, 1, TypeError))) as client:
        with pytest.raises(TypeError):
            await client.get("https://synthetic.invalid/never")
    assert events == [("authorize", 1)]


@pytest.mark.asyncio
async def test_async_observer_cannot_be_accidentally_treated_as_authorized(wire):
    events, configure = wire
    configure(lambda request: httpx.Response(200))
    async def wrong_protocol():
        raise AssertionError("must never execute async observer")
    async with httpx.AsyncClient(transport=connectors._BoundedTransport(ssl.create_default_context(), 1024, wrong_protocol)) as client:
        with pytest.raises(connectors.ConnectorError) as exc:
            await client.get("https://synthetic.invalid/never")
    assert exc.value.code == "request_observer_invalid" and events == []


@pytest.mark.asyncio
async def test_response_bound_is_preserved_after_authorization(wire):
    events, configure = wire
    class WireStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"x" * 2048
    configure(lambda request: httpx.Response(200, stream=WireStream()))
    async with httpx.AsyncClient(transport=connectors._BoundedTransport(ssl.create_default_context(), 1024, guard(events))) as client:
        with pytest.raises(connectors.ConnectorError) as exc:
            await client.get("https://synthetic.invalid/large")
    assert exc.value.code == "response_limit" and len(events) == 2


TOOL = {"name": "read_fixture", "description": "Synthetic read", "inputSchema": {"type": "object", "properties": {}}, "readonly": True}
CONFIG = {"id": "synthetic-wire", "url": "https://synthetic.invalid/mcp", "auth_type": "none", "timeout_seconds": 2,
          "tools": [TOOL], "allowed_tools": ["read_fixture"]}


def mcp_responder(methods, *, session=False):
    def response(request):
        if request.method == "GET":
            methods.append("stream")
            return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=b"")
        if request.method == "DELETE":
            methods.append("terminate")
            return httpx.Response(204)
        body = json.loads(request.content)
        method = body["method"]
        methods.append(method)
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "initialize":
            result = {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}}, "serverInfo": {"name": "synthetic-mock", "version": "1"}}
        elif method == "tools/list":
            result = {"tools": [{k: v for k, v in TOOL.items() if k != "readonly"}]}
        else:
            assert method == "tools/call"
            result = {"content": [{"type": "text", "text": "synthetic result"}], "isError": False}
        return httpx.Response(200, headers={"Mcp-Session-Id": "synthetic-session"} if session and method == "initialize" else {},
                              json={"jsonrpc": "2.0", "id": body["id"], "result": result})
    return response


@pytest.mark.asyncio
async def test_real_mcp_sdk_initialization_listing_call_stream_and_termination_are_guarded(wire):
    events, configure = wire
    methods = []
    configure(mcp_responder(methods, session=True))
    result = await connectors.mcp_call(CONFIG, None, "read_fixture", {}, request_observer=guard(events))
    assert result["isError"] is False
    assert {"initialize", "notifications/initialized", "tools/list", "tools/call", "terminate"}.issubset(methods)
    assert "stream" in methods
    assert len([e for e in events if e[0] == "authorize"]) == len([e for e in events if e[0] == "network"])


@pytest.mark.asyncio
async def test_real_mcp_discovery_has_no_tool_dispatch_and_all_requests_guarded(wire):
    events, configure = wire
    methods = []
    configure(mcp_responder(methods))
    result = await connectors.mcp_discover(CONFIG, None, request_observer=guard(events))
    assert result["complete"] is True and "tools/call" not in methods
    assert methods == ["initialize", "notifications/initialized", "tools/list"]
    assert len(events) == 6


@pytest.mark.asyncio
@pytest.mark.parametrize("function", ["discover", "call"])
async def test_mcp_guard_refusal_before_initialization_sends_nothing(wire, function):
    events, configure = wire
    methods = []
    configure(mcp_responder(methods))
    with pytest.raises(BaseException):
        if function == "discover":
            await connectors.mcp_discover(CONFIG, None, request_observer=guard(events, 1))
        else:
            await connectors.mcp_call(CONFIG, None, "read_fixture", {}, request_observer=guard(events, 1))
    assert methods == [] and events == [("authorize", 1)]


@pytest.mark.asyncio
async def test_mcp_revocation_after_initialize_prevents_any_further_dispatch(wire):
    events, configure = wire
    methods = []
    configure(mcp_responder(methods))
    with pytest.raises(BaseException):
        await connectors.mcp_call(CONFIG, None, "read_fixture", {}, request_observer=guard(events, 2))
    assert methods == ["initialize"]
    assert [event[0] for event in events] == ["authorize", "network", "authorize"]


@pytest.mark.asyncio
async def test_ongrid_login_retry_and_timeline_keep_the_composed_request_count(wire):
    events, configure = wire
    attempts = []
    def response(request):
        attempts.append((request.method, request.url.path))
        if len(attempts) == 1:
            return httpx.Response(401, json={"detail": "synthetic expired token"})
        if request.method == "POST":
            assert json.loads(request.content)["password"] == "synthetic-password"
            return httpx.Response(200, json={"access_token": "synthetic-token"})
        if request.url.path.endswith("/events"):
            return httpx.Response(200, json={"items": []})
        return httpx.Response(200, json={"id": "7"})
    configure(response)
    class Vault:
        def get(self, ref):
            assert ref == "synthetic-password-ref"
            return "synthetic-password"
    settings = {"ongrid": {"url": "https://synthetic.invalid", "auth_type": "none", "email": "synthetic@example.invalid", "password_secret_id": "synthetic-password-ref"}}
    result = await connectors.alert_detail(settings, Vault(), "7", request_observer=guard(events))
    assert attempts == [("GET", "/v1/alerts/incidents/7"), ("POST", "/v1/auth/login"), ("GET", "/v1/alerts/incidents/7"), ("GET", "/v1/alerts/incidents/7/events")]
    assert result["source_request_count"] == 4 and len(events) == 8


@pytest.mark.asyncio
async def test_ongrid_every_page_is_guarded_and_denial_prevents_login_retry(wire):
    events, configure = wire
    pages = []
    def response(request):
        pages.append(request.url.params.get("page"))
        return httpx.Response(200, json={"items": [{"id": i} for i in range(100 if len(pages) == 1 else 1)], "total": 101})
    configure(response)
    settings = {"ongrid": {"url": "https://synthetic.invalid", "auth_type": "none"}}
    result = await connectors.alert_list(settings, None, request_observer=guard(events))
    assert pages == ["1", "2"] and result["source_request_count"] == 2 and len(events) == 4
    events.clear(); pages.clear()
    with pytest.raises(Denied):
        await connectors.alert_list(settings, None, request_observer=guard(events, 1))
    assert pages == [] and events == [("authorize", 1)]


@pytest.mark.asyncio
async def test_revocation_before_ongrid_login_sends_no_login_or_retry(wire):
    events, configure = wire
    methods = []
    def response(request):
        methods.append(request.method)
        return httpx.Response(401)
    configure(response)
    class Vault:
        def get(self, ref):
            return "synthetic-password"
    settings = {"ongrid": {"url": "https://synthetic.invalid", "auth_type": "none", "email": "synthetic@example.invalid", "password_secret_id": "synthetic-password-ref"}}
    with pytest.raises(Denied):
        await connectors.alert_detail(settings, Vault(), "7", request_observer=guard(events, 2))
    assert methods == ["GET"] and [e[0] for e in events] == ["authorize", "network", "authorize"]


@pytest.mark.asyncio
async def test_ongrid_timeline_denial_preserves_detail_and_does_not_send_timeline(wire):
    events, configure = wire
    paths = []
    def response(request):
        paths.append(request.url.path)
        return httpx.Response(200, json={"id": "7"})
    configure(response)
    result = await connectors.alert_detail({"ongrid": {"url": "https://synthetic.invalid", "auth_type": "none"}}, None, "7", request_observer=guard(events, 2))
    assert paths == ["/v1/alerts/incidents/7"] and result["source_request_count"] == 1
    assert result["timeline"] == [] and result["limitations"]
    assert [e[0] for e in events] == ["authorize", "network", "authorize"]


@pytest.mark.asyncio
async def test_ongrid_composed_count_also_rejects_async_observer(wire):
    events, configure = wire
    configure(lambda request: httpx.Response(200, json={"items": []}))
    async def invalid():
        raise AssertionError("not sync")
    with pytest.raises(connectors.ConnectorError) as exc:
        await connectors.alert_list({"ongrid": {"url": "https://synthetic.invalid", "auth_type": "none"}}, None, request_observer=invalid)
    assert exc.value.code == "request_observer_invalid" and events == []


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["https://synthetic.invalid/other", "https://synthetic.invalid/mcp?new=1", "https://other.invalid/mcp", "http://synthetic.invalid/mcp", "https://synthetic.invalid:444/mcp"])
async def test_transport_rejects_actual_target_deviation_before_network(wire, changed):
    events, configure = wire
    configure(lambda request: httpx.Response(200))
    async with httpx.AsyncClient(transport=connectors._BoundedTransport(ssl.create_default_context(), 1024, guard(events), allowed_urls=["https://synthetic.invalid/mcp"])) as client:
        with pytest.raises(connectors.ConnectorError) as exc:
            await client.get(changed)
    assert exc.value.code == "request_target_changed" and events == []


@pytest.mark.asyncio
async def test_real_mcp_sdk_same_origin_redirect_never_forwards_credential(wire):
    events, configure = wire
    sent = []
    def response(request):
        sent.append((str(request.url), request.headers.get("authorization")))
        return httpx.Response(307, headers={"Location": "https://synthetic.invalid/unapproved"})
    configure(response)
    class Vault:
        def get(self, reference):
            assert reference == "synthetic-source-reference"
            return "synthetic-source-credential"
    config = {**CONFIG, "auth_type": "bearer", "secret_id": "synthetic-source-reference"}
    with pytest.raises(BaseException):
        await connectors.mcp_discover(config, Vault(), request_observer=guard(events))
    assert sent == [("https://synthetic.invalid/mcp", "Bearer synthetic-source-credential")]
    assert len(events) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_ongrid_redirect_never_leaves_the_frozen_get_target(wire, status):
    events, configure = wire
    sent = []
    def response(request):
        sent.append(str(request.url))
        return httpx.Response(status, headers={"Location": "https://synthetic.invalid/unapproved"})
    configure(response)
    with pytest.raises(connectors.ConnectorError) as exc:
        await connectors.alert_list({"ongrid": {"url": "https://synthetic.invalid", "auth_type": "none"}}, None, request_observer=guard(events))
    assert exc.value.code == "redirect_forbidden"
    assert sent == ["https://synthetic.invalid/v1/alerts/incidents?page=1&page_size=100"]
