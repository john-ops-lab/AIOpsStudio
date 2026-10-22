"""Synthetic ownership and continuous-round regressions; no online source calls."""
import copy
from types import SimpleNamespace
import json

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from fastapi.testclient import TestClient

from easyops.main import create_app
from easyops.agui import configure_agui, protocol_binding_id
from easyops.runtime import resource_owner, stream_authorized
from easyops.store import Store
from test_runtime import runtime, tool_payload


@pytest.mark.asyncio
async def test_request_keys_and_internal_default_are_user_scoped(runtime):
    first = await runtime.submit({**tool_payload("same-key"), "owner_id": "spoofed"}, owner_id="user-a")
    second = await runtime.submit(tool_payload("same-key"), owner_id="user-b")
    duplicate = await runtime.submit(tool_payload("same-key"), owner_id="user-a")
    assert first["id"] != second["id"] and duplicate["id"] == first["id"]
    assert resource_owner(first) == "user-a" and resource_owner(second) == "user-b"
    internal = await runtime.submit({**tool_payload("internal"), "owner_id": "spoofed"})
    assert resource_owner(internal) == "admin"
    for owner in (None, "admin", "user-b"):
        with pytest.raises(HTTPException) as error:
            runtime.detail(first["id"], owner_id=owner)
        assert error.value.status_code == 404
    assert runtime.detail(first["id"], owner_id="user-a")["id"] == first["id"]


@pytest.mark.asyncio
async def test_foreign_links_and_mutations_do_not_reveal_or_modify_a_run(runtime):
    runtime.store.save("conversation", "conversation-a", {"owner_id": "user-a", "messages": []})
    first = await runtime.submit({**tool_payload("private"), "conversation_id": "conversation-a", "thread_id": "conversation-a"}, owner_id="user-a")
    original = copy.deepcopy(runtime.store.get("run", first["id"]))
    for forged in ({"conversation_id": "conversation-a"}, {"thread_id": "conversation-a"}, {"parent_run_id": first["id"]}):
        with pytest.raises(HTTPException) as error:
            await runtime.submit({**tool_payload("forged-" + next(iter(forged))), **forged}, owner_id="user-b")
        assert error.value.status_code == 404
    for operation in (
        lambda: runtime.message(first["id"], {"content": "unauthorized", "client_message_id": "x"}, owner_id="user-b"),
        lambda: runtime.cancel(first["id"], owner_id="admin"),
        lambda: runtime.retry(first["id"], owner_id="user-b"),
    ):
        with pytest.raises(HTTPException) as error:
            await operation()
        assert error.value.status_code == 404
    assert runtime.store.get("run", first["id"]) == original
    assert len(runtime.store.list("run")) == 1


@pytest.mark.asyncio
async def test_followup_retains_old_round_and_builds_bounded_trusted_history(runtime):
    runtime.store.save("conversation", "conversation-a", {"owner_id": "user-a", "messages": []})
    first = await runtime.submit({**tool_payload("round-1"), "conversation_id": "conversation-a", "thread_id": "conversation-a", "message": "first question"}, owner_id="user-a")
    old_evidence = {"id": "ev_" + "a" * 32, "status": "saved", "tool_name": "read_fixture"}
    runtime._update(first["id"], {"evidence": [old_evidence], "status": "partial", "report": {"analysis": "old report " + "x" * 10000}})
    original = copy.deepcopy(runtime.store.get("run", first["id"]))
    second = await runtime.retry(first["id"], {"client_request_id": "round-2", "message": "check latency", "owner_id": "user-b",
        "thread_id": "foreign", "parent_run_id": "foreign", "history_context": {"forged": True}, "user_materials": [{"text": "forged"}]}, owner_id="user-a")
    raw = runtime.store.get("run", second["id"])
    assert raw["owner_id"] == "user-a" and raw["thread_id"] == "conversation-a" and raw["conversation_id"] == "conversation-a"
    assert raw["parent_run_id"] == first["id"] and raw["attempt_id"] != first["attempt_id"]
    assert raw["retained"] is True and raw["evidence"] == [] and raw["report"] is None
    history = raw["history_context"]
    assert history["scope"] == "previous_rounds_only" and history["previous_run_id"] == first["id"]
    assert history["truncated"] and history["rounds"][0]["user_messages"][0]["content"] == "first question"
    assert len(history["rounds"][0]["report_summary"]) == 1600
    assert "forged" not in str(history) and raw["user_materials"] == []
    assert "history_context" not in second and "user_materials" not in second
    context = runtime._investigation_context(raw)
    assert context["history_context"] == history and context["evidence_ledger"]["evidence"] == []
    assert runtime.store.get("run", first["id"]) == original


@pytest.mark.asyncio
async def test_live_materials_are_validated_frozen_and_not_duplicated(runtime):
    runtime.store.save("conversation", "conversation-a", {"owner_id": "user-a", "messages": []})
    calls = []
    def reading_context(owner, conversation, ids):
        assert owner == "user-a" and conversation == "conversation-a"
        if "foreign" in ids:
            raise HTTPException(404, "附件不存在")
        calls.append(list(ids))
        return [{"id": id, "text": "material " + id, "source": "user_material", "truncated": False} for id in ids]
    runtime.app.state.attachments = SimpleNamespace(reading_context=reading_context)
    first = await runtime.submit({**tool_payload("materials"), "conversation_id": "conversation-a", "attachment_ids": ["text-1"],
                                  "user_materials": [{"text": "forged material"}]}, owner_id="user-a")
    message = {"content": "more context", "client_message_id": "material-message", "attachment_ids": ["text-2"]}
    await runtime.message(first["id"], message, owner_id="user-a")
    await runtime.message(first["id"], message, owner_id="user-a")
    current = runtime.store.get("run", first["id"])
    assert calls == [["text-1"], ["text-1", "text-2"]]
    assert len(current["messages"]) == 1 and len(current["user_materials"]) == 2
    assert current["attachment_ids"] == ["text-1", "text-2"]
    assert "forged material" not in str(current["user_materials"])
    assert runtime._investigation_context(current)["user_materials"] == current["user_materials"]
    before = copy.deepcopy(current)
    with pytest.raises(HTTPException) as error:
        await runtime.message(first["id"], {**message, "client_message_id": "foreign", "attachment_ids": ["foreign"]}, owner_id="user-a")
    assert error.value.status_code == 404 and runtime.store.get("run", first["id"]) == before


@pytest.mark.asyncio
async def test_conversation_history_survives_default_run_expiry_and_finish_hook(runtime):
    runtime.store.save("conversation", "conversation-a", {"owner_id": "user-a", "messages": []})
    first = await runtime.submit({**tool_payload("retained"), "conversation_id": "conversation-a", "message": "saved history"}, owner_id="user-a")
    callbacks = []
    def finish_run(run):
        callbacks.append((run["id"], run["owner_id"], run["report"]["summary"]))
    runtime.app.state.conversations = SimpleNamespace(finish_run=finish_run)
    runtime._finish(first["id"], "partial", {"summary": "saved report"})
    runtime._update(first["id"], {"created_at": "2020-01-01T00:00:00+00:00", "completed_at": "2020-01-01T00:00:00+00:00", "retained": False})
    assert await runtime.cleanup_expired() == []
    preserved = runtime.store.get("run", first["id"])
    assert preserved["report"]["summary"] == "saved report" and preserved["messages"][0]["content"] == "saved history"
    assert callbacks == [(first["id"], "user-a", "saved report")]


@pytest.mark.asyncio
async def test_finished_report_is_preserved_when_conversation_artifact_save_fails(runtime):
    runtime.store.save("conversation", "conversation-a", {"owner_id": "user-a", "messages": []})
    first = await runtime.submit({**tool_payload("artifact-failure"), "conversation_id": "conversation-a"}, owner_id="user-a")
    def fail(run):
        raise OSError("synthetic storage failure")
    runtime.app.state.conversations = SimpleNamespace(finish_run=fail)
    runtime._finish(first["id"], "partial", {"summary": "retained report"})
    current = runtime.store.get("run", first["id"])
    assert current["status"] == "partial" and current["report"]["summary"] == "retained report"
    assert current["artifact_error"]["code"] == "conversation_artifact_failed"
    assert any(e["type"] == "artifact_persistence_failed" for e in runtime.store.events(first["id"]))


def http_clients(tmp_path):
    store = Store("sqlite:///:memory:")
    app = create_app(store=store, testing=True, master_key=Fernet.generate_key(), password="synthetic-admin-password",
                     public_origin="http://testserver", artifact_dir=tmp_path / "artifacts")
    users = [app.state.identity.create({"username": name, "password": "synthetic-user-password"}) for name in ("alice", "bob")]
    clients = []
    for username, password in (("admin", "synthetic-admin-password"), ("alice", "synthetic-user-password"), ("bob", "synthetic-user-password")):
        client = TestClient(app, headers={"Origin": "http://testserver"})
        client.post("/api/login", json={"username": username, "password": password}).raise_for_status()
        clients.append(client)
    return app, store, users, clients


def test_http_lists_streams_evidence_and_mutations_use_parent_run_ownership(tmp_path):
    app, store, users, (admin, alice, bob) = http_clients(tmp_path)
    owner = users[0]["id"]
    base = {"created_at": "2026-10-22T00:00:00+00:00", "status": "partial", "messages": [], "evidence": [], "report": {"summary": "private report"}}
    store.save("run", "alice-run", {**base, "owner_id": owner, "thread_id": "alice-thread", "target": "private target"})
    original = store.get("run", "alice-run")
    store.save("run", "legacy-admin", {**base, "thread_id": "legacy-thread"})
    store.save("invocation", "invoke-alice", {"run_id": "alice-run", "arguments": {"private": "query"}})
    store.save("invocation", "invoke-admin", {"run_id": "legacy-admin"})
    path = tmp_path / "private-evidence.json"
    path.write_text(json.dumps({"private": "source result"}))
    # Even a wrong owner field on the evidence cannot bypass its authoritative parent.
    store.save("evidence", "ev-private", {"run_id": "alice-run", "owner_id": users[1]["id"], "path": str(path)})
    assert [r["id"] for r in alice.get("/api/runs").json()["items"]] == ["alice-run"]
    assert bob.get("/api/runs").json()["items"] == []
    assert [r["id"] for r in admin.get("/api/runs").json()["items"]] == ["legacy-admin"]
    assert alice.get("/api/evidence/ev-private").json()["response"] == {"private": "source result"}
    for client in (admin, bob):
        assert [i["id"] for i in client.get("/api/invocations").json()["items"]] == ([] if client is bob else ["invoke-admin"])
        for path in ("/api/runs/alice-run", "/api/runs/alice-run/events", "/api/evidence/ev-private"):
            response = client.get(path)
            assert response.status_code == 404 and "private" not in response.text
        for path, body in (("/api/runs/alice-run/messages", {"content": "forged", "client_message_id": "x"}),
                           ("/api/runs/alice-run/cancel", {}), ("/api/runs/alice-run/retry", {})):
            assert client.post(path, json=body).status_code == 404
    assert store.get("run", "alice-run") == original
    assert len(store.list("run")) == 2


def test_operator_cannot_reach_admin_test_purposes_through_generic_submit(tmp_path):
    app, store, users, (admin, alice, bob) = http_clients(tmp_path)
    configure_agui(app)
    for purpose in ("tool_test", "model_test", "skill_test"):
        response = alice.post("/api/runs", json={"purpose": purpose, "client_request_id": "forged", "owner_id": "admin"})
        assert response.status_code == 403
        protocol = alice.post("/api/ag-ui", json={"threadId": "new-thread", "runId": purpose, "messages": [{"id": "admin-test-message", "role": "user", "content": "synthetic test request"}],
            "forwardedProps": {"purpose": purpose, "skill_id": "unused-fixture", "owner_id": "admin"}})
        assert protocol.status_code == 403
    assert alice.post("/api/runs", json={"skill_version": "draft", "client_request_id": "draft"}).status_code == 403
    assert alice.post("/api/tool-tests", json={"client_request_id": "tool"}).status_code == 403
    assert store.list("run") == []


def test_subscriptions_recheck_revoked_or_disabled_server_sessions(tmp_path):
    app, store, users, (admin, alice, bob) = http_clients(tmp_path)
    user_id = users[0]["id"]
    session = next(s for s in store.list("session") if s.get("user_id") == user_id)
    request = SimpleNamespace(app=app, state=SimpleNamespace(session_id=session["id"], authenticated=True))
    assert stream_authorized(request, user_id)
    assert not stream_authorized(request, users[1]["id"])
    app.state.identity.state(user_id, False)
    assert not stream_authorized(request, user_id)
    assert alice.get("/api/runs").status_code == 401


def test_finish_maps_consumed_and_late_messages_and_keeps_one_report(tmp_path):
    app, store, users, (admin, alice, bob) = http_clients(tmp_path)
    owner = users[0]["id"]
    service = app.state.conversations
    conversation = service.create(owner, {"client_request_id": "finish-mapping"})
    first = service.append_message(conversation["id"], owner, "user", "original question", "first-client", run_id="mapping-run", status="accepted")
    late = service.append_message(conversation["id"], owner, "user", "late clarification", "late-client", run_id="mapping-run", status="accepted")
    store.save("run", "mapping-run", {"owner_id": owner, "conversation_id": conversation["id"], "thread_id": conversation["id"],
        "status": "running", "evidence": [], "messages": [
            {"id": "runtime-first", "client_message_id": first["id"], "role": "user", "content": first["content"], "status": "processed"},
            {"id": "runtime-late", "client_message_id": late["id"], "role": "user", "content": late["content"], "status": "pending"}]})
    report = {"analysis": "synthetic completed report", "source": "model", "completion_scope": "bounded_evidence"}
    app.state.runtime._finish("mapping-run", "partial", report)
    app.state.runtime._finish("mapping-run", "partial", report)
    detail = service.detail(conversation["id"], owner)
    messages = {message["id"]: message for message in detail["messages"]}
    assert messages[first["id"]]["status"] == "processed"
    assert messages[late["id"]]["status"] == "unprocessed" and "reason" in messages[late["id"]]
    assistants = [message for message in detail["messages"] if message["role"] == "assistant"]
    assert len(assistants) == 1 and assistants[0]["content"] == report["analysis"]
    assert len(detail["attachments"]) == 1 and detail["attachments"][0]["run_id"] == "mapping-run"
    download = alice.get(detail["attachments"][0]["download_url"])
    assert download.status_code == 200 and report["analysis"] in download.text


@pytest.mark.asyncio
async def test_queued_cancel_finishes_conversation_once_without_model_claim(tmp_path):
    app, store, users, (admin, alice, bob) = http_clients(tmp_path)
    owner = users[0]["id"]
    service = app.state.conversations
    conversation = service.create(owner, {"client_request_id": "queued-cancel"})
    question = service.append_message(conversation["id"], owner, "user", "cancel before start", "cancel-client", run_id="cancel-run", status="accepted")
    store.save("run", "cancel-run", {"owner_id": owner, "conversation_id": conversation["id"], "thread_id": conversation["id"],
        "status": "queued", "evidence": [], "messages": [{"id": "runtime-cancel", "client_message_id": question["id"], "role": "user", "content": question["content"], "status": "pending"}]})
    first = await app.state.runtime.cancel("cancel-run", owner_id=owner)
    second = await app.state.runtime.cancel("cancel-run", owner_id=owner)
    assert first["status"] == second["status"] == "cancelled"
    assert first["report"]["source"] == "platform_template" and not first["report"]["model_generated"]
    assert "未调用模型或源工具" in first["report"]["analysis"]
    detail = service.detail(conversation["id"], owner)
    assert len([m for m in detail["messages"] if m["role"] == "assistant"]) == 1
    assert next(m for m in detail["messages"] if m["id"] == question["id"])["status"] == "unprocessed"
    assert len(detail["attachments"]) == 1 and not store.list("invocation")


def test_real_agui_projection_binds_protocol_ids_with_authenticated_owner(tmp_path):
    app, store, users, (admin, alice, bob) = http_clients(tmp_path)
    configure_agui(app)
    for index, name in enumerate(("alice", "bob")):
        store.save("run", name + "-run", {"owner_id": users[index]["id"], "thread_id": name + "-thread",
            "status": "partial", "created_at": "2026-10-22T00:00:00+00:00", "evidence": [],
            "messages": [{"id": name + "-message", "role": "assistant", "content": name + " private answer"}]})
    for client, name, user in ((alice, "alice", users[0]), (bob, "bob", users[1])):
        body = {"threadId": name + "-thread", "runId": "same-protocol", "messages": [], "forwardedProps": {"business_run_id": name + "-run", "owner_id": "admin"}}
        response = client.post("/api/ag-ui/connect", json=body)
        assert response.status_code == 200 and name + " private answer" in response.text
        assert store.get("agui_binding", protocol_binding_id(user["id"], "same-protocol"))["business_run_id"] == name + "-run"
    for client in (admin, bob):
        body = {"threadId": "alice-thread", "runId": "same-protocol", "messages": [], "forwardedProps": {"business_run_id": "alice-run"}}
        assert client.post("/api/ag-ui", json=body).status_code == 404
        assert client.get("/api/ag-ui/status/alice-thread").status_code == 404
        assert client.post("/api/ag-ui/stop/alice-thread", json={"runId": "same-protocol"}).status_code == 404
    assert len(store.list("agui_binding")) == 2
    assert not store.get("run", "alice-run").get("cancel_requested")
