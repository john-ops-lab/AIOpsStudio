"""Explicit synthetic fixtures verify runtime semantics, not live business acceptance."""
import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException

from aiopsstudio import connectors
from aiopsstudio.config import ConfigService, Vault
from aiopsstudio.runtime import Runtime, ExecutionStop, now
from aiopsstudio.store import Store
from m1_fixtures import analysis_defaults, seed_context_settings, seed_policies


@pytest.fixture
def runtime(tmp_path):
    store = Store("sqlite:///:memory:")
    vault = Vault(store, Fernet.generate_key())
    app = SimpleNamespace(state=SimpleNamespace(vault=vault))
    rt = Runtime(app, store)
    rt.artifact_dir = tmp_path
    app.state.config = ConfigService(store, vault, tmp_path)
    store.save("settings", "main", {"default_model_id": "model_fixture", "min_free_bytes": 1})
    model = {"id": "model_fixture", "name": "synthetic", "model": "synthetic-openai-model", "enabled": True,
             "provider": "openai", "base_url": "http://127.0.0.1:1/v1", "secret_id": vault.put("synthetic-secret-do-not-leak"), "max_tokens": 4096}
    model.update(current_version="v1", versions=[{"id": "v1", "snapshot": copy.deepcopy(model)}])
    store.save("model", model["id"], model)
    skill = {"id": "skill_fixture", "name": "cmdb-query", "content": "---\nname: cmdb-query\ndescription: 合成协议样本\n---\n固定测试方法正文，必须进入模型。", "enabled": True,
             "references": {}, "dependencies": [{"server_id": "server_fixture", "tool_name": "read_fixture"}], "input_schema": {"type": "object"}}
    skill.update(current_version="v1", versions=[{"id": "v1", "snapshot": copy.deepcopy(skill)}])
    store.save("skill", skill["id"], skill)
    tool = {"name": "read_fixture", "description": "synthetic read-only fixture", "inputSchema": {"type": "object", "properties": {}}, "readonly": True}
    server = {"id": "server_fixture", "name": "synthetic MCP", "url": "http://127.0.0.1:1/mcp", "enabled": True,
              "allowed_tools": [tool["name"]], "tools": [tool]}
    server.update(current_version="v1", versions=[{"id": "v1", "snapshot": copy.deepcopy(server)}])
    store.save("mcp", server["id"], server)
    seed_context_settings(store, tool)
    seed_policies(store, model=model, server=server)
    yield rt
    store.close()


def tool_payload(key="fixture-request"):
    return {"purpose": "tool_test", "server_id": "server_fixture", "tool_name": "read_fixture", "arguments": {}, "client_request_id": key}


async def wait_terminal(runtime, id):
    for _ in range(100):
        run = runtime.store.get("run", id)
        if run["status"] in {"succeeded", "failed", "partial", "cancelled"}:
            return run
        await asyncio.sleep(.03)
    pytest.fail("synthetic runtime did not terminate")


@pytest.mark.asyncio
async def test_idempotency_snapshots_messages_and_queue_limit(runtime):
    run = await runtime.submit(tool_payload())
    assert (await runtime.submit(tool_payload()))["id"] == run["id"]
    server = runtime.store.get("mcp", "server_fixture")
    server["url"] = "http://changed.invalid"
    runtime.store.save("mcp", server["id"], server)
    assert runtime.store.get("run", run["id"])["config_snapshot"]["mcps"][0]["url"] == "http://127.0.0.1:1/mcp"
    message = {"client_message_id": "msg-1", "content": "只读补充"}
    await runtime.message(run["id"], message)
    await runtime.message(run["id"], message)
    assert len(runtime.store.get("run", run["id"])["messages"]) == 1
    for i in range(9):
        await runtime.submit(tool_payload(str(i)))
    with pytest.raises(HTTPException) as error:
        await runtime.submit(tool_payload("over-limit"))
    assert error.value.status_code == 429


@pytest.mark.asyncio
async def test_shared_slot_serial_execution_and_native_error_evidence(runtime, monkeypatch):
    active = maximum = calls = 0
    async def synthetic_mcp(*args, request_observer=None):
        if request_observer:
            request_observer()
        nonlocal active, maximum, calls
        active += 1
        maximum = max(maximum, active)
        calls += 1
        await asyncio.sleep(.03)
        active -= 1
        return {"content": [{"type": "text", "text": "synthetic failure"}], "structuredContent": {"fixture": True}, "isError": True}
    monkeypatch.setattr(connectors, "mcp_call", synthetic_mcp)
    first = await runtime.submit(tool_payload("first"))
    second = await runtime.submit(tool_payload("second"))
    await runtime.start()
    try:
        a, b = await asyncio.gather(wait_terminal(runtime, first["id"]), wait_terminal(runtime, second["id"]))
    finally:
        await runtime.stop()
    assert maximum == 1 and calls == 2
    assert a["status"] == b["status"] == "failed"
    evidence = a["evidence"][0]
    assert json.loads(Path(evidence["path"]).read_text())["isError"] is True
    assert runtime.store.list("invocation")[0]["status"] == "failed"
    events = runtime.store.events(a["id"])
    assert [x["seq"] for x in events] == list(range(1, len(events) + 1))
    assert next(x["seq"] for x in events if x["type"] == "evidence_saved") < next(x["seq"] for x in events if x["type"] == "completed")


@pytest.mark.asyncio
async def test_shutdown_interrupts_inflight_without_replaying(runtime, monkeypatch):
    entered = asyncio.Event()
    async def synthetic_inflight(*args, request_observer=None):
        if request_observer:
            request_observer()
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(connectors, "mcp_call", synthetic_inflight)
    run = await runtime.submit(tool_payload("shutdown-fixture"))
    await runtime.start()
    await asyncio.wait_for(entered.wait(), 1)
    await asyncio.wait_for(runtime.stop(), 2)
    done = runtime.store.get("run", run["id"])
    assert done["status"] == "failed" and done["error"]["code"] == "interrupted"
    assert len(runtime.store.list("invocation")) == 1


@pytest.mark.asyncio
async def test_immediate_block_queued_cancel_and_retry(runtime, monkeypatch):
    first = await runtime.submit(tool_payload("blocked"))
    entity = runtime.store.get("mcp", "server_fixture")
    entity["blocked"] = True
    runtime.store.save("mcp", entity["id"], entity)
    await runtime.execute(first)
    assert runtime.store.get("run", first["id"])["status"] == "failed"
    assert runtime.store.get("run", first["id"])["error"]["code"] == "capability_blocked"
    entity["blocked"] = False
    runtime.store.save("mcp", entity["id"], entity)
    second = await runtime.submit(tool_payload("cancelled"))
    assert (await runtime.cancel(second["id"]))["status"] == "cancelled"
    retried = await runtime.retry(second["id"])
    assert retried["parent_run_id"] == second["id"] and retried["id"] != second["id"]


@pytest.mark.asyncio
async def test_truncation_and_secret_redaction_before_storage(runtime, monkeypatch):
    async def synthetic_mcp(*args, request_observer=None):
        if request_observer:
            request_observer()
        return {"content": [{"type": "text", "text": "synthetic-secret-do-not-leak" + "x" * 1500}], "isError": False}
    monkeypatch.setattr(connectors, "mcp_call", synthetic_mcp)
    run = await runtime.submit(tool_payload())
    # Test-only lower cap proves both saved and disclosed responses are partial.
    runtime._update(run["id"], callback=lambda r: r["budget"].update(evidence_bytes=600))
    model_ref = runtime.store.get("model", "model_fixture")["secret_id"]
    runtime._update(run["id"], callback=lambda r: r["config_snapshot"].update(model={"secret_id": model_ref}))
    await runtime.execute(runtime.store.get("run", run["id"]))
    done = runtime.store.get("run", run["id"])
    assert done["status"] == "partial" and done["evidence"][0]["truncated"]
    assert "synthetic-secret-do-not-leak" not in Path(done["evidence"][0]["path"]).read_text()
    assert "path" not in runtime.detail(run["id"])["evidence"][0]


@pytest.mark.asyncio
async def test_fixed_window_draft_model_and_budget(runtime):
    model = runtime.store.get("model", "model_fixture")
    model["enabled"] = False
    runtime.store.save("model", model["id"], model)
    sample = await runtime.submit({"purpose": "model_test", "model_id": model["id"], "client_request_id": "model-draft"})
    assert sample["config_snapshot"]["model"]["model"] == "synthetic-openai-model"
    with pytest.raises(connectors.ConnectorError):
        runtime._check_window({"analysis_start": "2026-10-22T01:00:00Z", "analysis_end": "2026-10-22T02:00:00Z"}, {"startTime": "now-1h"})
    with pytest.raises(connectors.ConnectorError):
        runtime._check_window({"analysis_start": "2026-10-22T01:00:00Z", "analysis_end": "2026-10-22T02:00:00Z"}, {"startTime": "2026-10-22T00:00:00Z"})
    runtime._update(sample["id"], {"deadline_at": "2020-01-01T00:00:00Z"})
    with pytest.raises(ExecutionStop):
        runtime._count(sample["id"], "model")


@pytest.mark.asyncio
async def test_real_deepagents_fixed_body_and_no_builtin_execution_tools(runtime, monkeypatch):
    """Real Deep Agents SDK, explicit fake model/source; no business acceptance."""
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    observed = {}
    server = runtime.store.get("mcp", "server_fixture")
    server["versions"][0]["snapshot"]["tools"][0]["alias"] = "explicit_frozen_alias"
    runtime.store.save("mcp", server["id"], server)
    skill = runtime.store.get("skill", "skill_fixture")
    skill["versions"][0]["snapshot"]["dependencies"] = [{"server_id": server["id"], "tool_name": "read_fixture", "required": True}]
    runtime.store.save("skill", skill["id"], skill)
    class SyntheticModel(BaseChatModel):
        @property
        def _llm_type(self):
            return "synthetic-openai-fixture"
        def _get_ls_params(self, **kwargs):
            return {"ls_provider": "openai", "ls_model_name": "synthetic"}
        def bind_tools(self, tools, **kwargs):
            observed["tools"] = [t.name if hasattr(t, "name") else t.get("name") for t in tools]
            return self
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            observed["messages"] = messages
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="合成协议测试没有读取业务证据，结论待验证。"))])
    async def synthetic_discovery(*args, request_observer=None):
        if request_observer:
            request_observer()
        return {"items": server["versions"][0]["snapshot"]["tools"], "complete": True}
    monkeypatch.setattr(connectors, "mcp_discover", synthetic_discovery)
    monkeypatch.setattr(runtime, "_model", lambda *a, **k: SyntheticModel())
    run = await runtime.submit({**analysis_defaults(), "skill_id": "skill_fixture", "client_request_id": "deepagents-fixture"})
    await runtime.execute(run)
    done = runtime.store.get("run", run["id"])
    assert done["status"] == "partial", done.get("error")
    assert "固定测试方法正文" in " ".join(str(m.content) for m in observed["messages"])
    assert observed.get("tools", []) == ["explicit_frozen_alias", "read_saved_material"]
    assert not set(observed.get("tools", [])) & {"task", "execute", "write_file", "edit_file", "read_file", "ls", "glob", "grep"}
    assert done["usage"]["model_call_count"] == 1
    assert done["usage"]["token_usage_known"] is False


def test_alias_is_unique_and_readonly_never_inferred(runtime):
    assert connectors.tool_alias("server-a", "read") != connectors.tool_alias("server-b", "read")
    assert len(connectors.tool_alias("server-a", "x" * 200)) <= 64
    with pytest.raises(connectors.ConnectorError):
        connectors.permitted_tool({"allowed_tools": ["read"], "tools": [{"name": "read", "annotations": {"readOnlyHint": True}}]}, "read")
    with pytest.raises(connectors.ConnectorError):
        connectors._tls({"verify_tls": False})


@pytest.mark.asyncio
async def test_model_revocation_and_immediate_block_before_each_dispatch(runtime):
    run = await runtime.submit({"purpose": "model_test", "model_id": "model_fixture", "client_request_id": "revocation"})
    runtime._update(run["id"], {"deadline_at": "2099-01-01T00:00:00Z"})
    runtime._count(run["id"], "model")
    model = runtime.store.get("model", "model_fixture")
    runtime.vault.revoke(model["secret_id"])
    with pytest.raises(ExecutionStop) as error:
        runtime._count(run["id"], "model")
    assert error.value.code == "credential_revoked"
    # Already revoked secrets still scrub delayed native output.
    assert "synthetic-secret-do-not-leak" not in runtime.safe("synthetic-secret-do-not-leak")
    assert runtime.safe({"password_secret_id": "sec_reference"})["password_secret_id"] == "sec_reference"
    model["blocked"] = True
    runtime.store.save("model", model["id"], model)
    with pytest.raises(ExecutionStop) as error:
        runtime._count(run["id"], "model")
    assert error.value.code == "model_blocked"


@pytest.mark.asyncio
async def test_skill_static_and_input_schema_gate(runtime):
    skill = runtime.store.get("skill", "skill_fixture")
    skill["content"] = "malformed draft"
    runtime.store.save("skill", skill["id"], skill)
    with pytest.raises(HTTPException) as error:
        await runtime.submit({"skill_id": skill["id"], "skill_version": "draft", "purpose": "skill_test", "client_request_id": "bad-body"})
    assert error.value.status_code == 422
    skill["content"] = skill["versions"][0]["snapshot"]["content"]
    skill["input_schema"] = {"type": "object", "properties": {"target": {"type": "string"}}, "required": ["target"], "additionalProperties": False}
    runtime.store.save("skill", skill["id"], skill)
    valid = await runtime.submit({"skill_id": skill["id"], "skill_version": "draft", "purpose": "skill_test", "inputs": {"target": "actual-target"}, "client_request_id": "nested-real-input"})
    assert valid["target"] == "actual-target" and valid["inputs"] == {"target": "actual-target"}
    with pytest.raises(HTTPException) as error:
        await runtime.submit({"skill_id": skill["id"], "skill_version": "draft", "purpose": "skill_test", "target": None,
                              "inputs": {"target": "bypass-target"}, "client_request_id": "conflicting-input"})
    assert error.value.status_code == 422
    skill["content"] = skill["versions"][0]["snapshot"]["content"]
    skill["input_schema"] = {"type": "object", "required": ["required_fixture_input"]}
    runtime.store.save("skill", skill["id"], skill)
    with pytest.raises(HTTPException) as error:
        await runtime.submit({"skill_id": skill["id"], "skill_version": "draft", "purpose": "skill_test", "client_request_id": "bad-input"})
    assert error.value.status_code == 422


@pytest.mark.asyncio
async def test_quota_gates_all_queue_types_and_each_response(runtime, monkeypatch):
    settings = runtime.settings()
    settings["storage_quota_bytes"] = 100
    runtime.store.save("settings", "main", settings)
    (runtime.artifact_dir / "existing.bin").write_bytes(b"x" * 101)
    for payload in [tool_payload(), {"purpose": "model_test", "model_id": "model_fixture", "client_request_id": "model-quota"}]:
        with pytest.raises(HTTPException) as error:
            await runtime.submit(payload)
        assert error.value.status_code == 507
    (runtime.artifact_dir / "existing.bin").unlink()
    run = await runtime.submit(tool_payload("response-quota"))
    async def synthetic_large(*args, request_observer=None):
        if request_observer:
            request_observer()
        return {"content": [{"type": "text", "text": "x" * 2000}], "isError": False}
    monkeypatch.setattr(connectors, "mcp_call", synthetic_large)
    await runtime.execute(run)
    done = runtime.store.get("run", run["id"])
    assert done["status"] == "failed" and done["error"]["code"] == "storage_quota"
    assert not done["evidence"]


@pytest.mark.asyncio
async def test_retention_groups_protect_pinned_and_referenced_evidence(runtime, monkeypatch):
    async def synthetic_read(*args, request_observer=None):
        if request_observer:
            request_observer()
        return {"content": [{"type": "text", "text": "explicit synthetic fixture"}], "isError": False}
    monkeypatch.setattr(connectors, "mcp_call", synthetic_read)
    old = []
    for key in ("expired", "pinned", "referenced"):
        run = await runtime.submit(tool_payload(key))
        await runtime.execute(run)
        runtime._update(run["id"], {"completed_at": "2020-01-01T00:00:00Z", "pinned": key == "pinned"})
        old.append(runtime.store.get("run", run["id"]))
    active_reference = await runtime.submit(tool_payload("still-valid-reference"))
    runtime._update(active_reference["id"], {"report": {"evidence_ids": [old[2]["evidence"][0]["id"]]}})
    last_event_seq = runtime.store.events(old[0]["id"])[-1]["seq"]
    assert await runtime.cleanup_expired() == [old[0]["id"]]
    assert not Path(old[0]["evidence"][0]["path"]).exists()
    assert runtime.store.get("evidence", old[0]["evidence"][0]["id"])["expired"]
    assert Path(old[1]["evidence"][0]["path"]).exists()
    assert Path(old[2]["evidence"][0]["path"]).exists()
    assert runtime.store.events(old[0]["id"], last_event_seq)[0]["type"] == "artifacts_expired"
    with pytest.raises(HTTPException) as error:
        await runtime.retry(old[0]["id"])
    assert error.value.status_code == 410


@pytest.mark.asyncio
async def test_official_mcp_sdk_against_explicit_local_fake_server(runtime):
    """Wire-level SDK test against synthetic FastMCP; no real source data."""
    import socket
    import threading
    import uvicorn
    from mcp.server.fastmcp import FastMCP
    from mcp.types import CallToolResult, TextContent
    server = FastMCP("explicit-synthetic-fixture", stateless_http=True, json_response=True)
    @server.tool()
    def fixture_echo(text: str) -> str:
        """Synthetic fixture only; echoes non-sensitive test input."""
        return "fixture:" + text
    @server.tool()
    def fixture_failure() -> CallToolResult:
        """Synthetic native MCP business error."""
        return CallToolResult(content=[TextContent(type="text", text="explicit synthetic isError")], isError=True)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    port = sock.getsockname()[1]
    http = uvicorn.Server(uvicorn.Config(server.streamable_http_app(), log_level="critical"))
    thread = threading.Thread(target=lambda: http.run(sockets=[sock]), daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if http.started:
                break
            await asyncio.sleep(.01)
        config = {"id": "explicit-local-fake", "url": f"http://127.0.0.1:{port}/mcp", "auth_type": "none", "timeout_seconds": 5}
        catalog = await connectors.mcp_discover(config, runtime.vault)
        assert catalog["complete"] and len(catalog["items"]) == 2
        assert all(t["readonly"] is False for t in catalog["items"])
        config.update(tools=[{**t, "readonly": True} for t in catalog["items"]], allowed_tools=["fixture_echo", "fixture_failure"])
        response = await connectors.mcp_call(config, runtime.vault, "fixture_echo", {"text": "中文样本"})
        assert response["isError"] is False and "fixture:中文样本" in json.dumps(response, ensure_ascii=False)
        failure = await connectors.mcp_call(config, runtime.vault, "fixture_failure", {})
        assert failure["isError"] is True
    finally:
        http.should_exit = True
        await asyncio.to_thread(thread.join, 5)
        sock.close()


@pytest.mark.asyncio
async def test_model_reading_view_keeps_native_evidence_and_source_facts(runtime):
    run = await runtime.submit({**analysis_defaults(), "skill_id": "skill_fixture", "client_request_id": "reading-view-fixture"})
    runtime._update(run["id"], {"deadline_at": "2099-01-01T00:00:00Z"})
    envelope = {"schema_version": "fixture-mcp/1", "request_id": "explicit-fixture", "data": {"models": [{"name": "fixture-model", "count": 3}]},
                "effective_query": {"scope": "fixture"}, "completeness": {"status": "partial", "reasons": ["fixture-limit"]},
                "verification": {"status": "partial", "complete_for_query": False, "unknowns": {"attribute_values": 2},
                                 "checks": [{"name": "synthetic-check-" + str(i), "passed": True} for i in range(500)] + [{"name": "must-preserve-failure", "passed": False}]},
                "source": {"system": "veops", "read_finished_at": "2026-10-22T01:00:00Z", "long_contract": "not-business-data-" * 1000},
                "report": {"markdown": "duplicate narrative " * 2000}}
    native = {"structuredContent": envelope, "content": [{"type": "text", "text": json.dumps(envelope)}], "isError": False}
    async def synthetic_native():
        return native
    reading = await runtime._invoke_source(run["id"], "server_fixture", "cmdb_list_models", {}, synthetic_native)
    record = runtime.store.get("evidence", reading["evidence_id"])
    assert json.loads(Path(record["path"]).read_text()) == native
    assert reading["response"]["data"] == envelope["data"]
    assert reading["response"]["completeness"] == envelope["completeness"]
    assert reading["response"]["verification"]["failed_checks"] == [{"name": "must-preserve-failure", "passed": False}]
    assert reading["response"]["source"]["read_finished_at"] == envelope["source"]["read_finished_at"]
    assert reading["reading_view"]["business_data_truncated"] is False
    assert len(json.dumps(reading, ensure_ascii=False).encode()) <= 16 * 1024


def test_oversized_reading_view_counts_omissions_without_changing_native(runtime):
    native = {"structuredContent": {"data": {"items": [{"id": i, "value": "x" * 1000} for i in range(200)]},
              "effective_query": {"fixture": True}, "completeness": {"status": "complete", "reasons": []}}, "isError": False}
    untouched = copy.deepcopy(native)
    record = {"id": "ev_explicit_synthetic", "tool_name": "cmdb_search_cis", "truncated": False, "original_bytes": 300000}
    reading = runtime._model_reading_view({}, record, native)
    assert native == untouched
    assert len(json.dumps(reading, ensure_ascii=False).encode()) <= 16 * 1024
    assert reading["reading_view"]["business_data_truncated"] is True
    assert reading["response"]["completeness"]["status"] == "complete"
    assert reading["completeness"] == "reading_view_partial"
    assert reading["reading_view"]["omitted_items"][0]["original_count"] == 200


def test_ongrid_reading_view_filters_timeline_but_preserves_detail(runtime):
    window = {"analysis_start": "2026-10-22T01:00:00Z", "analysis_end": "2026-10-22T02:00:00Z"}
    native = {"detail": {"id": 7, "subject": "synthetic-only", "labels": {"device": "fixture"}}, "timeline": [
        {"id": 1, "occurred_at": "2026-10-22T01:30:00Z"}, {"id": 2, "occurred_at": "2026-10-21T01:30:00Z"}, {"id": 3, "note": "timestamp-unknown"}]}
    reading = runtime._model_reading_view(window, {"id": "ev_fixture", "tool_name": "alert_detail", "truncated": False, "original_bytes": 1000}, native)
    assert reading["response"]["detail"] == native["detail"]
    assert [x["id"] for x in reading["response"]["timeline"]] == [1]
    selected = reading["reading_view"]["timeline_selection"]
    assert (selected["source_returned_count"], selected["in_window_count"], selected["outside_window_count"], selected["unknown_time_count"]) == (3, 1, 1, 1)
    assert len(native["timeline"]) == 3


def test_grafana_time_defaults_are_frozen_and_manual_tests_are_explicit(runtime):
    definition = {"inputSchema": {"properties": {"startRfc3339": {"type": "string"}, "endRfc3339": {"type": "string"}}}}
    run = {"analysis_start": "2026-10-22T01:00:00Z", "analysis_end": "2026-10-22T02:00:00Z"}
    actual = runtime._check_window(run, {}, definition)
    assert actual == {"startRfc3339": run["analysis_start"], "endRfc3339": run["analysis_end"]}
    with pytest.raises(connectors.ConnectorError):
        runtime._check_window({}, {}, definition)
    with pytest.raises(connectors.ConnectorError):
        runtime._check_window(run, {"startRfc3339": "now-1h", "endRfc3339": run["analysis_end"]}, definition)
    definition["name"] = "query_loki_logs"
    canonical = runtime._check_window(run, {"startRfc3339": "2026-10-22T09:00:00+08:00", "endRfc3339": "2026-10-22T10:00:00+08:00"}, definition)
    assert canonical == actual
    with pytest.raises(connectors.ConnectorError) as error:
        runtime._check_window(run, {"startRfc3339": "2026-10-22T01:00:00.123456Z", "endRfc3339": run["analysis_end"]}, definition)
    assert error.value.code == "time_precision_unsupported"


@pytest.mark.asyncio
@pytest.mark.parametrize("report_fails", [False, True])
async def test_real_graph_limits_exploration_and_reserves_tool_free_report(runtime, monkeypatch, report_fails):
    """Actual SDK scheduling, explicit fake provider and source, no live calls."""
    import re
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    seen = {"explore": 0, "report": 0, "context_sizes": []}
    server = runtime.store.get("mcp", "server_fixture")
    server["versions"][0]["snapshot"]["tools"][0]["alias"] = "bounded_fixture_read"
    runtime.store.save("mcp", server["id"], server)
    skill = runtime.store.get("skill", "skill_fixture")
    skill["versions"][0]["snapshot"]["dependencies"] = [{"server_id": server["id"], "tool_name": "read_fixture"}]
    runtime.store.save("skill", skill["id"], skill)
    class LoopingSyntheticModel(BaseChatModel):
        @property
        def _llm_type(self):
            return "explicit-looping-synthetic"
        def _get_ls_params(self, **kwargs):
            return {"ls_provider": "openai", "ls_model_name": "synthetic"}
        def bind_tools(self, tools, **kwargs):
            return self
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            text = str(messages[-1].content)
            if "停止取证原因：" in text:
                seen["report"] += 1
                if report_fails:
                    raise RuntimeError("explicit synthetic report failure")
                ids = sorted(set(re.findall(r"ev_[a-f0-9]{32}", text)))
                message = AIMessage(content="合成有界报告：已取得证据；潜在影响、已观测影响仍有范围限制；根因未确认。" + " ".join("[" + key + "]" for key in ids))
            else:
                seen["explore"] += 1
                seen["context_sizes"].append(len(text.encode()))
                message = AIMessage(content="", tool_calls=[{"id": "fixture-" + str(seen["explore"]), "name": "bounded_fixture_read", "args": {}}])
            message.usage_metadata = {"input_tokens": 1000, "output_tokens": 100, "total_tokens": 1100}
            return ChatResult(generations=[ChatGeneration(message=message)])
    async def discovery(*args, request_observer=None):
        if request_observer:
            request_observer()
        return {"items": server["versions"][0]["snapshot"]["tools"], "complete": True}
    async def native(*args, request_observer=None):
        if request_observer:
            request_observer()
        return {"structuredContent": {"data": {"items": [{"ci_id": 1, "model": "fixture", "attributes": {"name": "synthetic"}}]},
                "effective_query": {}, "completeness": {"status": "complete", "reasons": []}}, "isError": False}
    monkeypatch.setattr(connectors, "mcp_discover", discovery)
    monkeypatch.setattr(connectors, "mcp_call", native)
    monkeypatch.setattr(runtime, "_model", lambda *a, **k: LoopingSyntheticModel())
    run = await runtime.submit({**analysis_defaults(), "skill_id": "skill_fixture", "client_request_id": "bounded-model-" + str(report_fails)})
    await runtime.execute(run)
    done = runtime.store.get("run", run["id"])
    assert seen["explore"] == 4 and seen["report"] == 1, done.get("error")
    assert done["usage"]["model_call_count"] == 5 and done["usage"]["tool_call_count"] == 4
    assert done["usage"]["mcp_request_count"] == 5 and done["usage"]["mcp_tool_call_count"] == 4
    # Each explicit fake dispatch invokes the platform HTTP observer once;
    # source-internal requests behind MCP remain unknown.
    assert done["usage"]["source_request_count"] == 5 and done["usage"]["mcp_upstream_http_request_count"] is None
    assert done["status"] == "partial" and done["report"]["analysis"]
    assert done["report"]["model_generated"] is not report_fails
    assert done["report"]["source"] == ("platform_template" if report_fails else "model")
    assert len(done["evidence"]) == 4 and all(Path(e["path"]).is_file() for e in done["evidence"])
    assert len(runtime.store.list("invocation")) == 9
    assert max(seen["context_sizes"]) < 30000
    phases = [i["arguments"].get("phase") for i in runtime.store.list("invocation") if i["source"] == "model"]
    assert phases.count("report") == 1
    with pytest.raises(ExecutionStop) as error:
        runtime._count(run["id"], "tool")
    assert error.value.code == "report_tools_disabled"


@pytest.mark.asyncio
async def test_budget_forecast_stops_before_consuming_report_reserve(runtime):
    from langchain_core.messages import SystemMessage
    from langchain_core.tools import StructuredTool
    run = await runtime.submit({**analysis_defaults(), "skill_id": "skill_fixture", "client_request_id": "reserve-forecast"})
    runtime._update(run["id"], {"deadline_at": "2099-01-01T00:00:00Z"})
    runtime._update(run["id"], callback=lambda r: r["usage"].update(input_tokens=83000, output_tokens=0))
    request = SimpleNamespace(system_message=SystemMessage(content="explicit fixture"), tools=[])
    current = runtime.store.get("run", run["id"])
    forecast, reason = runtime._plan_model_round(current, request)
    assert forecast["remaining_tokens"] == 17000 and reason
    assert current["usage"]["model_call_count"] == 0
    runtime._failure(run["id"], "合成预算保底", "token_budget")
    report = runtime.store.get("run", run["id"])["report"]
    assert report["source"] == "platform_template" and report["model_generated"] is False
    assert "潜在影响" in report["analysis"] and "已观测影响" in report["analysis"] and "下一步" in report["analysis"]


@pytest.mark.asyncio
@pytest.mark.parametrize("scope_status,verified,expected", [("partial", False, "partial"), ("unknown", False, "partial"), ("complete", True, "partial")])
async def test_natural_report_propagates_explicit_source_scope(runtime, monkeypatch, scope_status, verified, expected):
    """Source partial is independent of saved size and model reading truncation."""
    import re
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    server = runtime.store.get("mcp", "server_fixture")
    server["versions"][0]["snapshot"]["tools"][0]["alias"] = "scope_fixture_read"
    runtime.store.save("mcp", server["id"], server)
    skill = runtime.store.get("skill", "skill_fixture")
    skill["versions"][0]["snapshot"]["dependencies"] = [{"server_id": server["id"], "tool_name": "read_fixture"}]
    runtime.store.save("skill", skill["id"], skill)
    class ScopeSyntheticModel(BaseChatModel):
        @property
        def _llm_type(self):
            return "explicit-source-scope-fixture"
        def _get_ls_params(self, **kwargs):
            return {"ls_provider": "openai", "ls_model_name": "synthetic"}
        def bind_tools(self, tools, **kwargs):
            return self
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            ids = re.findall(r"ev_[a-f0-9]{32}", str(messages[-1].content))
            if ids:
                reference = ids[0][:7] + "..." if scope_status == "complete" else ids[0]
                message = AIMessage(content="合成自然终答，结论以已保存的源证据范围为准。[" + reference + "]")
            else:
                message = AIMessage(content="", tool_calls=[{"id": "scope-fixture", "name": "scope_fixture_read", "args": {}}])
            return ChatResult(generations=[ChatGeneration(message=message)])
    async def discovery(*args, request_observer=None):
        if request_observer:
            request_observer()
        return {"items": server["versions"][0]["snapshot"]["tools"], "complete": True}
    reasons = ["request_budget", "root_coverage_incomplete"] if scope_status != "complete" else []
    native_result = {"structuredContent": {"data": {"items": [{"value": "explicit tiny fixture"}]}, "effective_query": {},
                    "completeness": {"status": scope_status, "reasons": reasons},
                    "verification": {"status": "passed" if verified else "partial", "complete_for_query": verified, "unknowns": {"attribute_values": 1}}}, "isError": False}
    async def native(*args, request_observer=None):
        if request_observer:
            request_observer()
        return native_result
    monkeypatch.setattr(connectors, "mcp_discover", discovery)
    monkeypatch.setattr(connectors, "mcp_call", native)
    monkeypatch.setattr(runtime, "_model", lambda *a, **k: ScopeSyntheticModel())
    run = await runtime.submit({**analysis_defaults(), "skill_id": "skill_fixture", "client_request_id": "source-scope-" + scope_status})
    await runtime.execute(run)
    done = runtime.store.get("run", run["id"])
    assert done["status"] == expected, done.get("error")
    assert done["usage"]["model_call_count"] == 2
    assert done["evidence"][0]["status"] == "saved" and not done["evidence"][0]["truncated"]
    assert not done["evidence"][0]["model_view"]["business_data_truncated"]
    assert not done["context_reading"]["business_data_truncated"]
    # Source completeness cannot certify this intentionally unstructured model prose.
    assert done["report"]["completion_scope"] == "bounded_evidence"
    assert done["report"]["grounding"]["validation"]["status"] == "partial"
    assert done["report"]["grounding"]["analysis_status"] == "unverified_model_interpretation"
    assert bool(done["report"]["source_limits"]) is not verified
    if not verified:
        assert done["report"]["source_limits"][0]["reasons"] == reasons
        assert any("源查询范围受限" in gap for gap in done["report"]["limitations"])
    assert json.loads(Path(done["evidence"][0]["path"]).read_text()) == native_result
    if verified:
        actual_id = done["evidence"][0]["id"]
        assert done["report"]["reference_validation"]["expansions"][0]["evidence_id"] == actual_id
        assert "[" + actual_id + "]" in done["report"]["analysis"]
        last_model = sorted([i for i in runtime.store.list("invocation") if i["source"] == "model"], key=lambda i: i["created_at"])[-1]
        assert "[" + actual_id[:7] + "...]" in last_model["result"][0]["content"]


@pytest.mark.asyncio
async def test_report_ledger_prioritizes_arguments_alert_times_and_all_series(runtime):
    run = await runtime.submit({**analysis_defaults(), "skill_id": "skill_fixture", "client_request_id": "priority-facts", "analysis_start": "2026-10-22T01:00:00Z", "analysis_end": "2026-10-22T02:00:00Z"})
    detail = {"id": 7, "target_type": "device", "target_id": "3", "labels": {"mountpoint": "/"},
              "fired_at": "2026-10-22T01:00:00Z", "last_fired_at": "2026-10-22T01:10:00Z", "resolved_at": "2026-10-22T01:50:00Z",
              "summary": "body intentionally large " * 2000}
    alert_native = {"id": "7", "detail": detail, "timeline": [], "limitations": []}
    arguments = {"expr": "sum by (service_name)(rate(demo_http_requests_total[2m]))", "datasourceUid": "fixture-prometheus", "startTime": run["analysis_start"], "endTime": run["analysis_end"], "stepSeconds": 60}
    series = [{"metric": {"service_name": name}, "values": [[i, str(.9 + i / 10000)] for i in range(100)]} for name in ("service-beta", "service-alpha")]
    metric_native = {"structuredContent": {"data": series}, "isError": False}
    alert, _ = runtime._persist_evidence(run["id"], {"id": "fixture-alert", "tool_name": "alert_detail", "source": "ongrid", "arguments": {"incident_id": "7"}}, alert_native)
    metric, _ = runtime._persist_evidence(run["id"], {"id": "fixture-metric", "tool_name": "query_prometheus", "source": "grafana", "arguments": arguments}, metric_native)
    current = runtime.store.get("run", run["id"])
    ledger = runtime._evidence_ledger(current, 6000)
    by_id = {e["evidence_id"]: e for e in ledger["evidence"]}
    assert len(json.dumps(ledger, ensure_ascii=False, separators=(",", ":")).encode()) <= 6000
    assert by_id[metric["id"]]["arguments"] == arguments
    for field in ("target_type", "target_id", "fired_at", "last_fired_at", "resolved_at"):
        assert by_id[alert["id"]]["compact_fact"][field] == detail[field]
    summary = by_id[metric["id"]]["compact_fact"]
    assert (summary["series_returned_count"], summary["series_included_count"], summary["series_omitted_count"]) == (2, 2, 0)
    assert {s["labels"]["service_name"] for s in summary["series"]} == {"service-beta", "service-alpha"}
    assert all(s["returned_point_count"] == 100 and s["omitted_point_count"] > 0 for s in summary["series"])
    assert ledger["business_data_truncated"] is True
    assert json.loads(Path(alert["path"]).read_text()) == alert_native
    assert json.loads(Path(metric["path"]).read_text()) == metric_native


def test_prometheus_summary_preserves_tail_drop_and_non_numeric_counts():
    source = {"structuredContent": {"data": [
        {"metric": {"device": "/dev/demo-a"}, "values": [[100, "73"], [200, "75"], [300, "41"]]},
        {"metric": {"device": "/dev/demo-b"}, "values": [[100, "62"], [200, "64"], [300, "27"]]},
        {"metric": {"service_name": "non-numeric-fixture"}, "values": [[100, "NaN"], [200, "+Inf"], [300, "bad"], [400, "2"]]}
    ]}}
    original = copy.deepcopy(source)
    summary = Runtime._prometheus_compact_fact(source)
    assert summary["series_included_count"] == 3 and summary["series_omitted_count"] == 0
    a, b, invalid = summary["series"]
    assert a["first"] == [100, "73"] and a["last"] == a["min"] == [300, "41"] and a["max"] == [200, "75"]
    assert b["last"] == [300, "27"] and b["labels"]["device"] == "/dev/demo-b"
    assert invalid["returned_point_count"] == 4 and invalid["non_numeric_point_count"] == 1 and invalid["non_finite_point_count"] == 2
    assert invalid["finite_numeric_point_count"] == 1 and invalid["min"] == invalid["max"] == [400, "2"]
    assert source == original
    empty = Runtime._prometheus_compact_fact({"structuredContent": {"data": []}})
    assert empty["series_returned_count"] == 0 and empty["series"] == [] and empty["data"] == []


def test_reference_validation_expands_only_unique_current_evidence_prefixes():
    from aiopsstudio.runtime import validate_evidence_references
    first, second = "ev_" + "a1b2" + "a" * 28, "ev_" + "c3d4" + "b" * 28
    original = f"磁盘[ev_a1b2...]；业务[ev_c3d4…]；原始[{first}]。"
    normalized, result = validate_evidence_references(original, {first, second})
    assert result["valid"] and len(result["expansions"]) == 2
    assert "ev_a1b2..." not in normalized and "ev_c3d4…" not in normalized
    assert first in normalized and second in normalized
    assert result["cited_ids"] == sorted([first, second])
    ambiguous = "ev_" + "a1b2" + "c" * 28
    bad_text = "[ev_a1b2...] [ev_ffff...] [ev_NOT_HEX] [ev_123] [ev_] [ev_" + "f" * 32 + "]"
    untouched, invalid = validate_evidence_references(bad_text, {first, second, ambiguous})
    assert untouched == bad_text and invalid["valid"] is False and invalid["expansions"] == []
    assert "ev_a1b2..." in invalid["invalid_ids"] and "ev_ffff..." in invalid["invalid_ids"] and "ev_NOT_HEX" in invalid["invalid_ids"]
