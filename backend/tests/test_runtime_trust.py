"""Actual ChatOpenAI wire and runtime gates in an isolated Mac test database."""
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from aiopsstudio.access_policy import PolicyError
from aiopsstudio.evaluation.contracts import execution_material
from aiopsstudio.evaluation.replay import seed_frozen
from aiopsstudio.main import create_app
from aiopsstudio.store import Store
from aiopsstudio.runtime import parse_time


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def trusted(tmp_path):
    frozen = json.loads((ROOT / "evaluation/m1/frozen.json").read_text())
    case = json.loads((ROOT / "evaluation/m1/inputs/normal-alert.json").read_text())
    store = Store("sqlite:///:memory:")
    app = create_app(store=store, testing=True, master_key=Fernet.generate_key(),
        password="synthetic-trust-password", artifact_dir=tmp_path, public_origin="http://testserver")
    seed_frozen(app, execution_material(case, frozen))
    yield app.state.runtime, case
    store.close()


def test_operator_queued_context_hides_private_canonical_until_explicit_query_grant(tmp_path, monkeypatch):
    """Submission may resolve an alias internally without disclosing its mapping."""
    from aiopsstudio import connectors
    from aiopsstudio.analysis_context import bind_query

    frozen = json.loads((ROOT / "evaluation/m1/frozen.json").read_text())
    case = json.loads((ROOT / "evaluation/m1/inputs/normal-alert.json").read_text())
    store = Store("sqlite:///:memory:")
    app = create_app(store=store, testing=True, master_key=Fernet.generate_key(),
        password="synthetic-queued-admin-password", artifact_dir=tmp_path, public_origin="http://testserver")
    seed_frozen(app, execution_material(case, frozen))
    runtime = app.state.runtime
    calls = []

    def forbidden(*args, **kwargs):
        calls.append("dispatch")
        pytest.fail("queued context disclosure checks must not dispatch a source or model")

    monkeypatch.setattr(connectors, "mcp_discover", forbidden)
    monkeypatch.setattr(connectors, "mcp_call", forbidden)
    monkeypatch.setattr(runtime, "_model", forbidden)
    alias = "synthetic-user-visible-alias"
    canonical_id = "SYNTHETIC_PRIVATE_CANONICAL_ID"
    native_id = "SYNTHETIC_PRIVATE_NATIVE_ID"
    settings = runtime.settings()
    mapping = settings["identity_mapping"]["records"][0]
    mapping.update(native_id=native_id, aliases=[alias])
    mapping["canonical"]["id"] = canonical_id
    store.save("settings", "main", settings)
    admin = TestClient(app, headers={"Origin": "http://testserver"})
    operator = TestClient(app, headers={"Origin": "http://testserver"})
    try:
        assert admin.post("/api/login", json={"password": "synthetic-queued-admin-password"}).status_code == 200
        created_user = admin.post("/api/users", json={"username": "queued-operator", "password": "synthetic-queued-operator-password"})
        assert created_user.status_code == 200, created_user.text
        owner = created_user.json()["id"]
        assert operator.post("/api/login", json={"username": "queued-operator", "password": "synthetic-queued-operator-password"}).status_code == 200
        request = {**case["request"], "target": alias, "message": "分析用户提供的合成别名。", "inputs": {},
            "skill_id": "skill_evaluation", "client_request_id": "queued-private-canonical-no-grant"}
        submitted = operator.post("/api/runs", json=request)
        assert submitted.status_code == 200, submitted.text
        run_id = submitted.json()["id"]
        internal = store.get("run", run_id)
        canonical_key = internal["analysis_context"]["identity"]["canonical_key"]
        assert internal["analysis_context"]["identity"]["canonical"]["id"] == canonical_id
        assert internal["status"] == "queued" and internal["target"] == alias

        def hidden(value):
            serialized = json.dumps(value, ensure_ascii=False)
            for private in (canonical_id, native_id, canonical_key):
                assert private not in serialized

        hidden(submitted.json())
        detail = operator.get("/api/runs/" + run_id)
        assert detail.status_code == 200, detail.text
        hidden(detail.json())
        hidden(runtime.public(internal))
        listed = operator.get("/api/runs")
        assert listed.status_code == 200 and any(item["id"] == run_id for item in listed.json()["items"])
        hidden(listed.json())

        # A directory-only grant still does not authorize any registered query.
        source_policy = store.get("access_policy", "main")
        catalog_grant = copy.deepcopy(next(grant for grant in source_policy["grants"] if grant["tools"] == ["$catalog"]))
        catalog_grant["subjects"] = [owner]
        source_policy.update(version="queued-catalog-only", grants=source_policy["grants"] + [catalog_grant])
        store.save("access_policy", "main", source_policy)
        hidden(operator.get("/api/runs/" + run_id).json())

        # Authorize one exact registered query; create a new frozen run so the
        # newer permission cannot expand the first run's missing frozen grant.
        context = internal["analysis_context"]
        entry = next(entry for entry in context["query_catalog"]["entries"] if entry["tool_name"] == "read_snapshot")
        bound = bind_query(context, entry["source_id"], entry["tool_name"], {}, definition_digest=entry["definition_digest"])
        assert set(bound["arguments"]) == {"target", "startTime", "endTime"}
        query_grant = copy.deepcopy(source_policy["grants"][0])
        query_grant.update(subjects=[owner], tools=[entry["tool_name"]], targets=[alias], purposes=["investigation"],
            argument_scope={"mode": "exact", "fields": {"/target": {"equals": native_id},
                "/startTime": {"equals": bound["arguments"]["startTime"]}, "/endTime": {"equals": bound["arguments"]["endTime"]}}})
        source_policy.update(version="queued-explicit-query", grants=source_policy["grants"] + [query_grant])
        store.save("access_policy", "main", source_policy)
        authorized = operator.post("/api/runs", json={**request, "client_request_id": "queued-private-canonical-explicit-grant"})
        assert authorized.status_code == 200, authorized.text
        allowed_run = authorized.json()
        assert allowed_run["analysis_context"]["identity"]["canonical"]["id"] == canonical_id
        assert allowed_run["analysis_context"]["identity"]["canonical_key"] == canonical_key
        assert operator.get("/api/runs/" + allowed_run["id"]).json()["analysis_context"]["identity"]["canonical"]["id"] == canonical_id
        hidden(operator.get("/api/runs/" + run_id).json())

        # Idempotent POST uses Runtime.public as well; it must not replay a
        # previously saved report after the source receipt is revoked.
        saved_run = store.get("run", allowed_run["id"])
        config = next(server for server in saved_run["config_snapshot"]["mcps"] if server["id"] == entry["source_id"])
        scope = runtime.policies.authorize_source(subject_id=owner, source_id=entry["source_id"], tool_name=entry["tool_name"],
            config=config, arguments=bound["arguments"], target=alias, environment="synthetic", purpose="investigation", snapshot=saved_run["policy_snapshot"])
        runtime._persist_evidence(saved_run["id"], {"id": "synthetic-queued-receipt", "source": entry["source_id"], "tool_name": entry["tool_name"],
            "arguments": bound["arguments"], "query_binding": bound["binding"], "access_scopes": [scope]}, {"synthetic_saved": True})
        report_marker = "SYNTHETIC_SOURCE_DERIVED_PRIVATE_REPORT"
        runtime._update(saved_run["id"], {"report": {"analysis": report_marker}})
        assert operator.get("/api/runs/" + saved_run["id"]).json()["report"]["analysis"] == report_marker
        source_policy.update(version="queued-query-revoked", denies=[{"subjects": [owner], "sources": [entry["source_id"]]}])
        store.save("access_policy", "main", source_policy)
        replay = operator.post("/api/runs", json={**request, "client_request_id": "queued-private-canonical-explicit-grant"})
        assert replay.status_code == 404 and report_marker not in replay.text
        assert calls == []
        for saved in store.list("run"):
            assert saved["status"] == "queued"
            assert saved["usage"]["source_request_count"] == saved["usage"]["model_call_count"] == 0
        assert store.list("invocation") == []
    finally:
        operator.close()
        admin.close()
        store.close()


async def attempt(rt, case):
    run = await rt.submit({**case["request"], "skill_id": "skill_evaluation", "client_request_id": "wire-test"})
    return rt._update(run["id"], {"deadline_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(), "status": "running"})


def wire(rt, run, captured):
    model = rt._model(run["config_snapshot"]["model"], run_id=run["id"])
    original = model.root_async_client._client
    async def send(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "synthetic-completion", "object": "chat.completion", "created": 1,
            "model": "synthetic", "choices": [{"index": 0, "message": {"role": "assistant", "content": "合成回答"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}})
    client = httpx.AsyncClient(transport=httpx.MockTransport(send), event_hooks=original.event_hooks, follow_redirects=False, trust_env=False)
    model.root_async_client._client = client
    rt._model_clients[run["id"]].append(client)
    return model


async def close(rt, run):
    for client in rt._model_clients.pop(run["id"], []):
        await client.aclose()


@pytest.mark.asyncio
async def test_real_sdk_wire_is_projected_and_latest_revocation_stops_transport(trusted):
    rt, case = trusted
    run, sent = await attempt(rt, case), []
    model = wire(rt, run, sent)
    context = rt._investigation_context(run)
    text = rt._approved_text(run, json.dumps(context, ensure_ascii=False))
    assert case["request"]["target"] not in text
    try:
        answer = await rt._invoke_model(run["id"], lambda: model.ainvoke([HumanMessage(content=text)]))
        assert answer.content == "合成回答" and len(sent) == 1
        assert rt.store.get("run", run["id"])["usage"]["model_http_request_count"] == 1
        policy = rt.store.get("egress_policy", "main")
        policy.update(enabled=False, version="revoked")
        rt.store.save("egress_policy", "main", policy)
        with pytest.raises(PolicyError):
            await rt._invoke_model(run["id"], lambda: model.ainvoke([HumanMessage(content=text)]))
        assert len(sent) == 1
    finally:
        await close(rt, run)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["untyped", "credential", "unknown_attachment", "unknown_tool_schema", "auth_override"])
async def test_unregistered_or_sensitive_material_never_reaches_http_transport(trusted, mode):
    rt, case = trusted
    run, sent = await attempt(rt, case), []
    if mode == "auth_override":
        rt._update(run["id"], callback=lambda r: r["config_snapshot"]["model"].update(default_headers={"Authorization": "Bearer wrong-credential"}))
        run = rt.store.get("run", run["id"])
    model = wire(rt, run, sent)
    text = "raw unregistered user content" if mode == "untyped" else rt._approved_text(run, "固定合成输入")
    if mode == "credential":
        text = rt._approved_text(run, rt.vault.get(run["config_snapshot"]["model"]["secret_id"]))
    if mode == "unknown_attachment":
        rt._update(run["id"], {"user_materials": [{"id": "synthetic", "unknown_private_field": "PRIVATE_ATTACHMENT_MARKER"}]})
    try:
        with pytest.raises(Exception):
            if mode == "unknown_attachment":
                rt._investigation_context(rt.store.get("run", run["id"]))
            elif mode == "unknown_tool_schema":
                tools = [{"type": "function", "function": {"name": "synthetic", "description": "test", "parameters": {"type": "object", "properties": {}}, "unknown_private_field": "PRIVATE_SCHEMA_MARKER"}}]
                await rt._invoke_model(run["id"], lambda: model.bind(tools=tools).ainvoke([HumanMessage(content=text)]))
            else:
                await rt._invoke_model(run["id"], lambda: model.ainvoke([HumanMessage(content=text)]))
        assert sent == []
    finally:
        await close(rt, run)


@pytest.mark.asyncio
async def test_actual_protocol_null_tool_call_round_is_typed(trusted):
    rt, case = trusted
    run, sent = await attempt(rt, case), []
    model = wire(rt, run, sent)
    messages = [HumanMessage(content=rt._approved_text(run, "固定合成协议输入")),
        AIMessage(content="", tool_calls=[{"id": "call-synthetic", "name": "aiopsstudio_protocol_echo", "args": {"text": "中文"}, "type": "tool_call"}]),
        ToolMessage(content="合成工具结果", tool_call_id="call-synthetic")]
    try:
        await rt._invoke_model(run["id"], lambda: model.ainvoke(messages))
        assert len(sent) == 1 and sent[0]["messages"][1]["content"] is None
        assert sent[0]["messages"][2]["content"] == "合成工具结果"
    finally:
        await close(rt, run)


@pytest.mark.asyncio
async def test_ambiguous_context_ends_before_model_or_source_dispatch(trusted, monkeypatch):
    rt, case = trusted
    settings = rt.settings()
    settings["identity_mapping"] = {}
    rt.store.save("settings", "main", settings)
    run = await attempt(rt, case)
    def forbidden(*args, **kwargs):
        pytest.fail("needs_input must not construct a model or dispatch source")
    monkeypatch.setattr(rt, "_model", forbidden)
    monkeypatch.setattr(rt, "_source_scope", forbidden)
    await rt.execute(run)
    final = rt.store.get("run", run["id"])
    assert final["status"] == "needs_input"
    assert final["usage"]["model_call_count"] == final["usage"]["tool_call_count"] == 0
    assert rt.store.list("invocation") == []


@pytest.mark.asyncio
async def test_native_query_parameters_are_server_bound_and_conflicts_stop_before_source(trusted):
    rt, case = trusted
    run = await attempt(rt, case)
    server = run["config_snapshot"]["mcps"][0]
    definition = server["tools"][0]
    from aiopsstudio import connectors
    from aiopsstudio.analysis_context import ContextError
    arguments, binding = rt._bind_source(run, server["id"], definition["name"], {"page": 0},
        {**definition, "definition_digest": connectors.tool_digest(definition)})
    assert parse_time(arguments["startTime"]) == parse_time(run["analysis_start"])
    assert parse_time(arguments["endTime"]) == parse_time(run["analysis_end"])
    assert binding["canonical_key"] == run["analysis_context"]["identity"]["canonical_key"]
    with pytest.raises(ContextError):
        rt._bind_source(run, server["id"], definition["name"], {"target": "some-other-target", "page": 0},
            {**definition, "definition_digest": connectors.tool_digest(definition)})
    assert rt.store.list("invocation") == []


@pytest.mark.asyncio
async def test_disabled_owner_stops_a_prepared_model_dispatch(trusted):
    rt, case = trusted
    run, sent = await attempt(rt, case), []
    model = wire(rt, run, sent)
    user = rt.store.get("user", "admin")
    user["enabled"] = False
    rt.store.save("user", "admin", user)
    try:
        with pytest.raises(PolicyError):
            await rt._invoke_model(run["id"], lambda: model.ainvoke([HumanMessage(content="fixed synthetic sample")]))
        assert sent == []
    finally:
        await close(rt, run)


@pytest.mark.parametrize("text, expected", [
    ("<think>private reasoning</think>可见结论", "可见结论"),
    ("<think>unfinished reasoning", ""),
    ("正常中文结果", "正常中文结果"),
])
def test_compatible_provider_reasoning_tags_are_never_visible(text, expected):
    from aiopsstudio.runtime import visible_text
    assert visible_text(text) == expected


@pytest.mark.asyncio
async def test_bad_saved_read_returns_a_correctable_error_to_the_next_model_round(trusted, monkeypatch):
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.outputs import ChatGeneration, ChatResult
    from pydantic import PrivateAttr
    from aiopsstudio import connectors
    rt, case = trusted
    seen = []
    class CorrectingModel(BaseChatModel):
        _calls: int = PrivateAttr(default=0)
        @property
        def _llm_type(self):
            return "synthetic-reading-error-recovery"
        def bind_tools(self, tools, **kwargs):
            return self
        def _get_ls_params(self, **kwargs):
            return {"ls_provider": "openai", "ls_model_name": "synthetic"}
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            self._calls += 1
            seen.append(" ".join(str(message.content) for message in messages))
            result = AIMessage(content="", tool_calls=[{"id": "missing-material", "name": "read_saved_material",
                "args": {"resource_type": "evidence", "evidence_id": "ev_missing", "selector": {"pointer": ""}}}]) if self._calls == 1 else AIMessage(content=json.dumps({"analysis": "材料尚未取得，结论未知。", "claims": []}))
            return ChatResult(generations=[ChatGeneration(message=result)])
    async def discover(config, vault, request_observer=None):
        request_observer()
        return {"items": copy.deepcopy(config["tools"]), "complete": True}
    monkeypatch.setattr(connectors, "mcp_discover", discover)
    monkeypatch.setattr(rt, "_model", lambda *args, **kwargs: CorrectingModel())
    run = await attempt(rt, case)
    await rt.execute(run)
    final = rt.store.get("run", run["id"])
    assert final["report"]["model_generated"] is True and final["status"] == "partial", final.get("error")
    assert "reading_error" in seen[1] and "not_found" in seen[1]
    assert any(event["type"] == "material_read_failed" for event in rt.store.events(run["id"]))
