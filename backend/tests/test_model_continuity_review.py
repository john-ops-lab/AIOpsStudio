"""Independent privacy checks for retained provider protocol; no external model."""
from aiopsstudio.runtime import visible_text


def test_text_content_blocks_do_not_publish_reasoning_tags():
    value = [{"type": "reasoning", "text": "SYNTHETIC_PRIVATE_REASONING_BLOCK"},
             {"type": "text", "text": "<think>SYNTHETIC_PRIVATE_REASONING_TEXT</think>可见结论"}]
    assert visible_text(value) == "可见结论"


import asyncio
import copy
import json
from pathlib import Path
import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from aiopsstudio.access_policy import PolicyError, policy_digest
from aiopsstudio.model_protocol import ACTIVE_TOOL_TURN_POLICY, AttemptToolTurns, ToolTurnError
from test_runtime_trust import attempt, close, trusted, wire


@pytest.mark.asyncio
async def test_cancel_after_actual_sdk_dispatch_marks_provider_usage_unknown(trusted):
    rt, case = trusted
    run = await attempt(rt, case)
    sent = []
    model = wire(rt, run, sent)
    dispatched = asyncio.Event()
    async def interrupted(request):
        sent.append(request.url.path)
        dispatched.set()
        await asyncio.Event().wait()
    model.root_async_client._client._transport = httpx.MockTransport(interrupted)
    try:
        task = asyncio.create_task(rt._invoke_model(run["id"], lambda: model.ainvoke([
            HumanMessage(content=rt._approved_text(run, "合成取消校验"))])))
        await asyncio.wait_for(dispatched.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        latest = rt.store.get("run", run["id"])
        assert len(sent) == latest["usage"].get("model_http_request_count") == 1
        assert latest["usage"]["token_usage_known"] is False
        assert rt.store.list("invocation")[0]["status"] == "cancelled"
    finally:
        await close(rt, run)


@pytest.mark.asyncio
async def test_cancel_before_dispatch_keeps_known_zero_usage(trusted):
    rt, case = trusted
    run = await attempt(rt, case)
    async def before_wire():
        raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await rt._invoke_model(run["id"], before_wire)
    usage = rt.store.get("run", run["id"])["usage"]
    assert usage.get("model_http_request_count", 0) == 0
    assert usage["token_usage_known"] is True


def allow_reasoning(rt):
    policy = rt.store.get("egress_policy", "main")
    policy["types"]["model_protocol"]["fields"]["/reasoning_content"] = "text"
    policy["version"] = "synthetic-explicit-reasoning"
    rt.store.save("egress_policy", "main", policy)


@pytest.mark.asyncio
async def test_sdk_reasoning_round_trip_has_no_durable_visible_copy(trusted):
    rt, case = trusted
    allow_reasoning(rt)
    run, sent = await attempt(rt, case), []
    model = wire(rt, run, sent)
    private = "SYNTHETIC_PRIVATE_PROVIDER_REASONING"
    think = "SYNTHETIC_PRIVATE_INLINE_THOUGHT"

    async def provider(request):
        body = json.loads(request.content)
        sent.append(body)
        # Respond based on the actual wire protocol, not internal call counts.
        completed = any(m["role"] == "tool" for m in body["messages"])
        message = {"role": "assistant", "content": "合成可见结论"} if completed else {
            "role": "assistant", "content": "<think>" + think + "</think>合成进展",
            "reasoning_content": private, "unknown_private_metadata": "SYNTHETIC_PRIVATE_METADATA",
            "tool_calls": [{"id": "synthetic-call", "type": "function", "function": {
                "name": "aiopsstudio_protocol_echo", "arguments": '{"text":"中文"}'}}]}
        return httpx.Response(200, json={"id": "synthetic-response", "object": "chat.completion", "created": 1,
            "model": "synthetic", "choices": [{"index": 0, "message": message,
            "finish_reason": "stop" if completed else "tool_calls"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}})

    model.root_async_client._client._transport = httpx.MockTransport(provider)
    digest = policy_digest(rt.policies.snapshot())
    scope = (run["id"], run["attempt_id"])
    turns = AttemptToolTurns(*scope)
    try:
        first = await rt._invoke_model(run["id"], lambda: model.ainvoke([
            HumanMessage(content=rt._approved_text(run, "合成第一轮"))]))
        assert first.additional_kwargs["reasoning_content"] == private
        clean = turns.capture([first], scope, digest, visible_text)
        assert clean[0].content == "合成进展"
        persisted = json.dumps({"graph_response": [m.model_dump() for m in clean],
            "invocations": rt.store.list("invocation"), "events": rt.store.events(run["id"])}, ensure_ascii=False)
        assert all(marker not in persisted for marker in (private, think, "SYNTHETIC_PRIVATE_METADATA"))
        tail = turns.messages([*clean, ToolMessage(content="合成工具结果", tool_call_id="synthetic-call")], scope, digest)
        token = ACTIVE_TOOL_TURN_POLICY.set((*scope, digest))
        try:
            final = await rt._invoke_model(run["id"], lambda: model.ainvoke([
                HumanMessage(content=rt._approved_text(run, "合成下一轮")), *tail]))
        finally:
            ACTIVE_TOOL_TURN_POLICY.reset(token)
        assert final.content == "合成可见结论"
        assert len(sent) == 2
        assistant = next(m for m in sent[1]["messages"] if m["role"] == "assistant")
        assert assistant["reasoning_content"] == private
        assert think in assistant["content"]
        assert "unknown_private_metadata" not in assistant
        durable = json.dumps({"invocations": rt.store.list("invocation"), "events": rt.store.events(run["id"])}, ensure_ascii=False)
        assert all(marker not in durable for marker in (private, think, "SYNTHETIC_PRIVATE_METADATA"))
    finally:
        await close(rt, run)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["unregistered", "credential"])
async def test_retained_reasoning_requires_explicit_field_and_cannot_contain_credentials(trusted, mode):
    rt, case = trusted
    if mode == "credential":
        allow_reasoning(rt)
    run, sent = await attempt(rt, case), []
    model = wire(rt, run, sent)
    reasoning = (rt.vault.get(run["config_snapshot"]["model"]["secret_id"])
        if mode == "credential" else "SYNTHETIC_UNAPPROVED_REASONING")
    messages = [HumanMessage(content=rt._approved_text(run, "合成工具回复")),
        AIMessage(content="", additional_kwargs={"reasoning_content": reasoning},
            tool_calls=[{"id": "synthetic-call", "name": "aiopsstudio_protocol_echo", "args": {"text": "中文"}}]),
        ToolMessage(content="合成工具结果", tool_call_id="synthetic-call")]
    try:
        with pytest.raises(PolicyError):
            await rt._invoke_model(run["id"], lambda: model.ainvoke(messages))
        assert sent == []
        assert rt.store.get("run", run["id"])["usage"].get("model_http_request_count", 0) == 0
        assert reasoning not in json.dumps(rt.store.list("invocation"), ensure_ascii=False)
    finally:
        await close(rt, run)


@pytest.mark.asyncio
async def test_latest_field_drop_blocks_old_tool_text_even_with_newly_approved_human(trusted):
    rt, case = trusted
    run, sent = await attempt(rt, case), []
    model = wire(rt, run, sent)
    scope = (run["id"], run["attempt_id"])
    original = policy_digest(rt.policies.snapshot())
    turns = AttemptToolTurns(*scope)
    assistant = AIMessage(content="", id="current-attempt-response",
        tool_calls=[{"id": "synthetic-call", "name": "read_snapshot", "args": {}}])
    clean = turns.capture([assistant], scope, original, visible_text)
    tail = turns.messages([*clean, ToolMessage(content='{"notice":"SYNTHETIC_OLD_SOURCE_BODY"}',
        tool_call_id="synthetic-call")], scope, original)

    # Simulate a policy edit after reconstruction and before the actual SDK
    # HTTP hook. Reapprove human text so that only the tool epoch guards it.
    policy = rt.store.get("egress_policy", "main")
    policy["types"]["source_evidence"]["fields"]["/notice"] = "drop"
    policy["version"] = "synthetic-field-tightened-after-rebuild"
    rt.store.save("egress_policy", "main", policy)
    assert rt._typed(run, "source_evidence", {"notice": "SYNTHETIC_OLD_SOURCE_BODY"}) == {}
    with pytest.raises(ToolTurnError):
        turns.messages([], scope, policy_digest(rt.policies.snapshot()))
    token = ACTIVE_TOOL_TURN_POLICY.set((*scope, original))
    try:
        with pytest.raises(PolicyError) as error:
            await rt._invoke_model(run["id"], lambda: model.ainvoke([
                HumanMessage(content=rt._approved_text(run, "新许可的合成输入")), *tail]))
        assert error.value.code == "policy_egress_stale_tool_protocol"
        assert sent == []
        assert rt.store.get("run", run["id"])["usage"].get("model_http_request_count", 0) == 0
    finally:
        ACTIVE_TOOL_TURN_POLICY.reset(token)
        await close(rt, run)


@pytest.mark.asyncio
@pytest.mark.parametrize("unknown_field", [False, True])
async def test_source_tool_body_uses_source_policy_before_generic_protocol(trusted, monkeypatch, unknown_field):
    from aiopsstudio import connectors

    rt, case = trusted
    raw = copy.deepcopy(case["source"]["responses"]["read_snapshot:0"])
    private = "SYNTHETIC_SOURCE_FIELD_MUST_STAY_LOCAL"
    raw["structuredContent"]["data"]["operator_email"] = private
    if unknown_field:
        raw["structuredContent"]["data"]["unregistered_private_field"] = "SYNTHETIC_UNKNOWN_SOURCE_FIELD"
    captured, source_calls = [], []
    original_model = rt._model

    async def provider(request):
        payload = json.loads(request.content)
        captured.append(payload)
        replies = [m for m in payload["messages"] if m["role"] == "tool"]
        if not replies:
            message = {"role": "assistant", "content": "读取合成证据", "tool_calls": [{
                "id": "source-projection-call", "type": "function", "function": {"name": "read_snapshot", "arguments": "{}"}}]}
        else:
            body = json.loads(replies[-1]["content"])
            message = {"role": "assistant", "content": json.dumps({"analysis": "已读取合成证据，根因未知。[" + body["evidence_id"] + "]", "claims": []}, ensure_ascii=False)}
        return httpx.Response(200, json={"id": "source-projection-response", "object": "chat.completion", "created": 1,
            "model": "synthetic", "choices": [{"index": 0, "message": message,
                "finish_reason": "tool_calls" if message.get("tool_calls") else "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}})

    def model(*args, **kwargs):
        instance = original_model(*args, **kwargs)
        original_client = instance.root_async_client._client
        client = httpx.AsyncClient(transport=httpx.MockTransport(provider), event_hooks=original_client.event_hooks,
            follow_redirects=False, trust_env=False)
        instance.root_async_client._client = client
        rt._model_clients[kwargs["run_id"]].append(client)
        return instance

    async def discover(config, vault, request_observer=None):
        request_observer()
        return {"items": copy.deepcopy(config["tools"]), "complete": True}

    async def source(*args, request_observer=None, **kwargs):
        request_observer()
        source_calls.append("read_snapshot")
        return copy.deepcopy(raw)

    monkeypatch.setattr(rt, "_model", model)
    monkeypatch.setattr(connectors, "mcp_discover", discover)
    monkeypatch.setattr(connectors, "mcp_call", source)
    run = await attempt(rt, case)
    await rt.execute(run)
    final = rt.store.get("run", run["id"])
    assert source_calls == ["read_snapshot"]
    assert len(final["evidence"]) == 1
    saved = json.loads(Path(final["evidence"][0]["path"]).read_text())
    assert saved == raw, "Model projection must leave native saved evidence unchanged"
    outbound = json.dumps(captured, ensure_ascii=False)
    assert private not in outbound and "SYNTHETIC_UNKNOWN_SOURCE_FIELD" not in outbound
    assert "demo-checkout" not in outbound and "svc-checkout" not in outbound
    if unknown_field:
        assert len(captured) == 1, "An unregistered source field must not enter generic text"
    else:
        assert len(captured) == 2, final.get("error")
        tool_body = json.loads(next(m["content"] for m in captured[1]["messages"] if m["role"] == "tool"))
        assert tool_body["evidence_id"] == final["evidence"][0]["id"]
        assert "operator_email" not in json.dumps(tool_body)
        assert "0.18" in json.dumps(tool_body), "Allowed source facts still reach the model"
        assert final["report"]["model_generated"] is True
