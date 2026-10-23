"""Real graph/SDK, explicitly synthetic HTTP, no provider or source network."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from aiopsstudio import connectors
from aiopsstudio.access_policy import PolicyError, policy_digest
from aiopsstudio.runtime import visible_text
from m1_fixtures import analysis_defaults
from test_runtime import runtime
from test_runtime_trust import attempt, close, trusted, wire


def completion(message):
    return {"id": "synthetic-completion", "object": "chat.completion", "created": 1, "model": "synthetic",
            "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if message.get("tool_calls") else "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}}


@pytest.mark.asyncio
@pytest.mark.parametrize("natural_report_timeout", [False, True])
async def test_real_graph_and_sdk_keep_completed_tool_round_and_hide_reasoning(runtime, monkeypatch, natural_report_timeout):
    import deepagents
    from langgraph.checkpoint.memory import InMemorySaver

    captured, graphs, request_timeouts, stages = [], [], [], []
    original_timeout = asyncio.timeout
    def stage_timeout(seconds):
        if seconds > 180:  # Runtime.execute's original whole-attempt deadline.
            return original_timeout(seconds)
        stages.append(seconds)
        # Exercise cancellation without spending real wall-clock minutes.
        return original_timeout(.01 if natural_report_timeout and len(stages) == 2 else seconds)
    monkeypatch.setattr(asyncio, "timeout", stage_timeout)
    policy = runtime.store.get("egress_policy", "main")
    policy["version"] = "explicit-synthetic-reasoning/1"
    policy["types"]["model_protocol"]["fields"]["/reasoning_content"] = "text"
    runtime.store.save("egress_policy", "main", policy)
    server = runtime.store.get("mcp", "server_fixture")
    definition = server["versions"][0]["snapshot"]["tools"][0]
    alias = connectors.tool_alias(server["id"], definition["name"])
    real_model, real_graph = runtime._model, deepagents.create_deep_agent

    def graph(*args, **kwargs):
        kwargs["checkpointer"] = InMemorySaver()
        instance = real_graph(*args, **kwargs)
        graphs.append(instance)
        return instance

    async def send(request):
        body = json.loads(request.content)
        captured.append(body)
        request_timeouts.append(request.extensions["timeout"]["read"])
        assert "timeout" not in body
        replies = [m for m in body["messages"] if m["role"] == "tool"]
        if "tools" not in body:
            import re
            evidence = re.search(r"ev_[a-f0-9]{32}", body["messages"][-1]["content"])[0]
            message = {"role": "assistant", "content": json.dumps({"analysis": "合成禁工具报告，根因未确认。[" + evidence + "]", "claims": []}, ensure_ascii=False)}
        elif not replies:
            # No private call counter: progression depends on received protocol.
            message = {"role": "assistant", "content": "<think>SYNTHETIC_HIDDEN_TAG</think>读取合成证据",
                "reasoning_content": "SYNTHETIC_HIDDEN_REASONING",
                "tool_calls": [{"id": "call-continuity", "type": "function", "function": {"name": alias, "arguments": "{}"}}]}
        else:
            previous = next(m for m in reversed(body["messages"]) if m["role"] == "assistant")
            assert previous["reasoning_content"] == "SYNTHETIC_HIDDEN_REASONING"
            assert previous["tool_calls"][0]["id"] == replies[-1]["tool_call_id"] == "call-continuity"
            if natural_report_timeout:
                await asyncio.Event().wait()
            evidence = json.loads(replies[-1]["content"])["evidence_id"]
            message = {"role": "assistant", "content": json.dumps({"analysis": "合成证据已读取，根因未确认。[" + evidence + "]", "claims": []}, ensure_ascii=False),
                       "reasoning_content": "SYNTHETIC_HIDDEN_FINAL_REASONING"}
        return httpx.Response(200, json=completion(message))

    def model(*args, **kwargs):
        instance = real_model(*args, **kwargs)
        original = instance.root_async_client._client
        client = httpx.AsyncClient(transport=httpx.MockTransport(send), event_hooks=original.event_hooks, follow_redirects=False, trust_env=False)
        instance.root_async_client._client = client
        runtime._model_clients[kwargs["run_id"]].append(client)
        return instance

    async def discover(*args, request_observer=None):
        if request_observer:
            request_observer()
        return {"items": [definition], "complete": True}

    async def source(*args, request_observer=None):
        if request_observer:
            request_observer()
        return {"structuredContent": {"data": {"items": [{"ci_id": 1, "model": "fixture", "attributes": {"name": "synthetic"}}]},
                "effective_query": {}, "completeness": {"status": "complete", "reasons": []}}, "isError": False}

    monkeypatch.setattr(deepagents, "create_deep_agent", graph)
    monkeypatch.setattr(runtime, "_model", model)
    monkeypatch.setattr(connectors, "mcp_discover", discover)
    monkeypatch.setattr(connectors, "mcp_call", source)
    run = await runtime.submit({**analysis_defaults(), "skill_id": "skill_fixture", "client_request_id": "protocol-continuity"})
    await runtime.execute(run)
    done = runtime.store.get("run", run["id"])
    assert len(captured) == (3 if natural_report_timeout else 2), done.get("error")
    assert request_timeouts == stages and all(value == 180 for value in stages)
    assert [m["role"] for m in captured[1]["messages"]] == ["system", "user", "assistant", "tool"]
    assert "evidence_ledger" in captured[1]["messages"][1]["content"]
    assert done["usage"]["tool_call_count"] == 1 and done["report"]["model_generated"] is True
    if natural_report_timeout:
        assert done["usage"]["token_usage_known"] is False
        assert done["usage"]["input_tokens"] == 200 and done["usage"]["output_tokens"] == 40
        model_invocations = [item for item in runtime.store.list("invocation") if item["source"] == "model"]
        assert sorted(item["status"] for item in model_invocations) == ["cancelled", "succeeded", "succeeded"]
        assert "tools" not in captured[-1]
    checkpoint = await graphs[0].aget_state({"configurable": {"thread_id": run["attempt_id"]}})
    assert checkpoint.values["messages"]
    # Inspect every intermediate checkpoint and pending write, not merely the
    # final graph view which could conceal a late cleanup of persisted secrets.
    saver = graphs[0].checkpointer
    assert "SYNTHETIC_HIDDEN" not in repr(saver.storage)
    assert "SYNTHETIC_HIDDEN" not in repr(saver.writes)
    assert "SYNTHETIC_HIDDEN" not in repr(saver.blobs)
    persisted = json.dumps({"run": done, "events": runtime.store.events(run["id"]),
        "invocations": runtime.store.list("invocation"), "checkpoint": [m.model_dump() for m in checkpoint.values["messages"]]}, ensure_ascii=False)
    assert "SYNTHETIC_HIDDEN" not in persisted
    assert "reasoning_content" not in json.dumps([m.model_dump() for m in checkpoint.values["messages"]])


def make_round(cache, number, graph, *, digest="policy-a", calls=1, text="tool result"):
    message = AIMessage(id="message-" + str(number), content="visible",
        additional_kwargs={"reasoning_content": "opaque"},
        tool_calls=[{"id": f"call-{number}-{i}", "name": "read_synthetic", "args": {}} for i in range(calls)])
    clean = cache.capture([message], ("run-a", "attempt-a"), digest, visible_text)
    graph.extend(clean)
    graph.extend(ToolMessage(content=text, tool_call_id=call["id"]) for call in message.tool_calls)
    return cache.messages(graph, ("run-a", "attempt-a"), digest)


def test_complete_multi_tool_groups_are_bounded_and_attempt_local():
    from aiopsstudio.model_protocol import AttemptToolTurns, ToolTurnError, protocol_bytes
    cache, graph = AttemptToolTurns("run-a", "attempt-a", max_bytes=2000), []
    for number in range(5):
        tail = make_round(cache, number, graph, calls=2)
        assert len(tail) <= 6 and protocol_bytes(tail) <= 2000
        assert sum(isinstance(m, AIMessage) for m in tail) <= 2
        for i in range(0, len(tail), 3):
            assert {call["id"] for call in tail[i].tool_calls} == {m.tool_call_id for m in tail[i + 1:i + 3]}
    assert [m.id for m in tail if isinstance(m, AIMessage)] == ["message-3", "message-4"]
    with pytest.raises(ToolTurnError):
        cache.messages(graph, ("run-a", "other-attempt"), "policy-a")
    # Unknown historical messages cannot seed a fresh attempt cache.
    assert AttemptToolTurns("run-b", "attempt-b").messages(graph, ("run-b", "attempt-b"), "policy-a") == []


@pytest.mark.parametrize("mode", ["missing", "duplicate", "oversized", "changed_policy"])
def test_incomplete_oversized_or_stale_group_is_not_partially_replayed(mode):
    from aiopsstudio.model_protocol import AttemptToolTurns, ToolTurnError
    cache = AttemptToolTurns("run-a", "attempt-a", max_bytes=700)
    assistant = AIMessage(content="", id="ai-a", tool_calls=[{"id": "one", "name": "read", "args": {}}, {"id": "two", "name": "read", "args": {}}])
    graph = cache.capture([assistant], ("run-a", "attempt-a"), "policy-a", visible_text)
    graph += [ToolMessage(content="x" * (800 if mode == "oversized" else 10), tool_call_id="one")]
    if mode != "missing":
        graph.append(ToolMessage(content="result", tool_call_id="one" if mode == "duplicate" else "two"))
    with pytest.raises(ToolTurnError):
        cache.messages(graph, ("run-a", "attempt-a"), "policy-b" if mode == "changed_policy" else "policy-a")


@pytest.mark.asyncio
async def test_source_error_reply_uses_source_type_and_never_backfills_dropped_fields(trusted, monkeypatch):
    rt, case = trusted
    run = await attempt(rt, case)
    rt._investigation_context(run)  # The real graph composes identity aliases before a source call.
    server = run["config_snapshot"]["mcps"][0]
    async def source_error(*args, **kwargs):
        raise connectors.ConnectorError("SYNTHETIC_PRIVATE_SOURCE_ERROR", "synthetic_source_failed")
    monkeypatch.setattr(connectors, "mcp_call", source_error)
    reply = await rt._mcp(run["id"], server, "read_snapshot", {"page": 0})
    record = rt.store.get("run", run["id"])["evidence"][0]
    native = rt._reader().native(record)
    assert reply["evidence_id"] == record["id"] and reply["saved_status"] == "error"
    assert reply["data"]["isError"] is True
    assert reply["data"]["error"] == {"code": "synthetic_source_failed"}
    assert native["error"]["message"]  # The source-error contract drops this from model output only.
    assert case["request"]["target"] not in json.dumps(reply, ensure_ascii=False)
    assert "SYNTHETIC_PRIVATE_SOURCE_ERROR" not in json.dumps(native, ensure_ascii=False)
    policy = rt.store.get("egress_policy", "main")
    policy["version"] = "synthetic-drop-evidence-reference"
    policy["types"]["source_evidence"]["fields"]["/evidence/*/evidence_id"] = "drop"
    rt.store.save("egress_policy", "main", policy)
    with pytest.raises(PolicyError) as denied:
        rt._model_evidence_result(run["id"], record)
    assert denied.value.code == "policy_egress_evidence_reference"
    assert rt._reader().native(record) == native


@pytest.mark.asyncio
async def test_stale_source_field_policy_blocks_prepared_protocol_at_actual_http(trusted):
    from aiopsstudio.model_protocol import ACTIVE_TOOL_TURN_POLICY
    rt, case = trusted
    run, captured = await attempt(rt, case), []
    model = wire(rt, run, captured)
    digest = policy_digest(rt.policies.snapshot())
    messages = [HumanMessage(content=rt._approved_text(run, "合成旧工具往返")),
        AIMessage(content="", tool_calls=[{"id": "old", "name": "read", "args": {}}]),
        ToolMessage(content="SYNTHETIC_OLD_FIELD_BODY", tool_call_id="old")]
    policy = rt.store.get("egress_policy", "main")
    policy["version"] = "explicit-field-revocation"
    policy["types"]["source_evidence"]["fields"]["/evidence/*/status"] = "drop"
    rt.store.save("egress_policy", "main", policy)
    # Renewing a human approval must not approve the old tool body.
    rt._approved_text(run, messages[0].content)
    token = ACTIVE_TOOL_TURN_POLICY.set((run["id"], run["attempt_id"], digest))
    try:
        with pytest.raises(PolicyError) as denied:
            await rt._invoke_model(run["id"], lambda: model.ainvoke(messages))
        assert denied.value.code == "policy_egress_stale_tool_protocol"
        assert captured == []
        assert rt.store.get("run", run["id"])["usage"].get("model_http_request_count", 0) == 0
    finally:
        ACTIVE_TOOL_TURN_POLICY.reset(token)
        await close(rt, run)


@pytest.mark.asyncio
async def test_unregistered_reasoning_field_is_rejected_without_dropping_it(trusted):
    rt, case = trusted
    run, captured = await attempt(rt, case), []
    model = wire(rt, run, captured)
    messages = [HumanMessage(content=rt._approved_text(run, "固定合成输入")),
        AIMessage(content="", additional_kwargs={"reasoning_content": "SYNTHETIC_UNREGISTERED_REASONING"},
            tool_calls=[{"id": "one", "name": "read", "args": {}}]), ToolMessage(content="合成结果", tool_call_id="one")]
    try:
        with pytest.raises(PolicyError):
            await rt._invoke_model(run["id"], lambda: model.ainvoke(messages))
        assert captured == []
    finally:
        await close(rt, run)


@pytest.mark.asyncio
async def test_protocol_bytes_count_toward_report_reserve(runtime):
    run = await runtime.submit({**analysis_defaults(), "skill_id": "skill_fixture", "client_request_id": "protocol-forecast"})
    runtime._update(run["id"], {"deadline_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()})
    run = runtime.store.get("run", run["id"])
    request = SimpleNamespace(system_message=SystemMessage(content="synthetic"), tools=[])
    base, _ = runtime._plan_model_round(run, request)
    with_protocol, _ = runtime._plan_model_round(run, request, retained_protocol_bytes=48000)
    assert with_protocol["estimated_next_input_tokens"] == base["estimated_next_input_tokens"] + 21600
    run["usage"]["input_tokens"] = run["budget"]["max_tokens"] - base["estimated_next_input_tokens"] - 4096 - run["execution_plan"]["report_token_reserve"] - 100
    assert runtime._plan_model_round(run, request)[1] is None
    assert runtime._plan_model_round(run, request, retained_protocol_bytes=48000)[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("remaining, expected", [(288, 180), (35, 34)])
async def test_report_sdk_and_stage_timeouts_share_remaining_attempt_budget(trusted, monkeypatch, remaining, expected):
    import aiopsstudio.runtime as module
    import asyncio
    rt, case = trusted
    run, captured, timeouts = await attempt(rt, case), [], []
    rt._update(run["id"], {"deadline_at": (datetime.now(timezone.utc) + timedelta(seconds=remaining)).isoformat()})
    model = wire(rt, run, captured)
    async def inspect(request):
        body = json.loads(request.content)
        captured.append(body)
        assert "tools" not in body and "timeout" not in body
        timeouts.append(request.extensions["timeout"]["read"])
        return httpx.Response(200, json=completion({"role": "assistant", "content": "合成报告；尚无业务证据。"}))
    model.root_async_client._client._transport = httpx.MockTransport(inspect)
    original_timeout = asyncio.timeout
    stage = []
    def bounded(seconds):
        stage.append(seconds)
        return original_timeout(seconds)
    monkeypatch.setattr(module.asyncio, "timeout", bounded)
    try:
        await rt._reserved_report(run["id"], model, "只写合成报告", "合成预算边界")
        assert len(captured) == 1 and len(stage) == 1
        assert expected - 1 <= stage[0] <= expected
        assert timeouts == stage
        assert captured[0]["max_completion_tokens"] <= 4096
    finally:
        await close(rt, run)
