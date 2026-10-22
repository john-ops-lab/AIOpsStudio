"""Local-only identity, conversation receipts and attachment isolation contracts."""
import base64
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from cryptography.fernet import Fernet
from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest

from easyops.main import create_app
from easyops.runtime import configure_runtime
from easyops.store import Store
from easyops.attachments import FILE_LIMIT, USER_LIMIT

ADMIN_PASSWORD = "admin-test-password-long"
USER_PASSWORD = "operator-test-password-long"
ORIGIN = {"Origin": "http://testserver"}
CONTEXT = {"skill_id": None, "analysis_start": "2026-10-22T01:00:00Z", "analysis_end": "2026-10-22T02:00:00Z", "target": "fixture"}


@pytest.fixture
def chat(tmp_path):
    store = Store("sqlite:///" + str(tmp_path / "chat.sqlite"))
    app = create_app(store=store, master_key=Fernet.generate_key(), password=ADMIN_PASSWORD, public_origin="http://testserver", artifact_dir=tmp_path / "artifacts", testing=True, enable_runtime=False)
    configure_runtime(app, store)  # Register real methods without starting a worker or external calls.
    admin = TestClient(app, headers=ORIGIN)
    assert admin.post("/api/login", json={"password": ADMIN_PASSWORD}).status_code == 200
    created = admin.post("/api/users", json={"username": "operator", "name": "普通用户", "password": USER_PASSWORD, "role": "operator"})
    assert created.status_code == 200, created.text
    user = created.json()
    operator = TestClient(app, headers=ORIGIN)
    assert operator.post("/api/login", json={"username": "operator", "password": USER_PASSWORD}).status_code == 200
    config = app.state.config
    model = config.save("model", {"name": "合成模型配置", "model": "fixture", "secret_value": "synthetic-fixture-key-never-call"})
    model = config.publish("model", model["id"], model["revision"])
    settings = store.get("settings", "main")
    config.settings({"expected_revision": settings["revision"], "default_model_id": model["id"]})
    skill = config.save("skill", {"name": "chat-fixture", "content": "---\nname: chat-fixture\ndescription: 合成测试方法，不执行真实调用\n---\n仅用于验证持久回执。"})
    skill = config.publish("skill", skill["id"], skill["revision"])
    yield SimpleNamespace(app=app, store=store, admin=admin, operator=operator, user=user, context={**CONTEXT, "skill_id": skill["id"]}, path=tmp_path)
    admin.close()
    operator.close()
    store.close()


def conversation(client, key="same-conversation-key", context=None):
    response = client.post("/api/conversations", json={"client_request_id": key, "title": "分析测试", "context": context or {}})
    assert response.status_code == 200, response.text
    return response.json()


def upload(client, id, name="test.txt", data=b"fixture text", media_type="text/plain"):
    return client.post(f"/api/conversations/{id}/attachments", json={"name": name, "media_type": media_type, "content_base64": base64.b64encode(data).decode()})


def test_identity_roles_compatibility_and_disable(chat):
    assert chat.admin.get("/api/session").json()["user"]["id"] == "admin"
    assert chat.operator.get("/api/session").json()["user"]["id"] == chat.user["id"]
    account = chat.store.get("user", chat.user["id"])
    assert USER_PASSWORD not in json.dumps(account) and account["password_hash"].startswith("scrypt$")
    assert "password_hash" not in chat.admin.get("/api/users").text
    for method, path, body in [("put", "/api/settings", {}), ("post", "/api/model-configs/x/test", {}), ("post", "/api/mcp-servers/x/probe", {}), ("post", "/api/tool-tests", {}), ("post", "/api/users", {})]:
        assert getattr(chat.operator, method)(path, json=body).status_code == 403
    assert chat.operator.get("/api/config-audit").status_code == 403
    assert chat.operator.get("/api/settings").status_code == 200
    assert chat.admin.post(f"/api/users/{chat.user['id']}/state", json={"enabled": False}).status_code == 200
    assert chat.operator.get("/api/conversations").status_code == 401
    assert chat.operator.post("/api/login", json={"username": "operator", "password": USER_PASSWORD}).status_code == 401
    assert chat.admin.post("/api/users/admin/state", json={"enabled": False}).status_code == 422


def test_private_history_idempotency_missing_context_and_reload(chat):
    a, b = conversation(chat.admin), conversation(chat.operator)
    assert a["id"] != b["id"]
    assert conversation(chat.operator)["id"] == b["id"]
    assert chat.admin.get(f"/api/conversations/{b['id']}").status_code == 404
    assert chat.operator.put(f"/api/conversations/{a['id']}", json={"title": "越权"}).status_code == 404
    body = {"content": "先保存这个问题", "client_message_id": "same-id"}
    first = chat.operator.post(f"/api/conversations/{b['id']}/messages", json=body)
    assert first.status_code == 200 and first.json()["run"] is None
    assert first.json()["message"]["status"] == "not_executed" and first.json()["message"]["hint"]
    again = chat.operator.post(f"/api/conversations/{b['id']}/messages", json=body).json()
    assert again["message"]["id"] == first.json()["message"]["id"]
    assert chat.store.list("run") == []
    chat.operator.post("/api/logout", json={})
    chat.operator.post("/api/login", json={"username": "operator", "password": USER_PASSWORD})
    restored = chat.operator.get(f"/api/conversations/{b['id']}").json()
    assert len(restored["messages"]) == 1 and restored["messages"][0]["content"] == body["content"]
    assert len(chat.operator.get("/api/conversations").json()["items"]) == 1


def test_chat_submissions_are_user_scoped_and_context_change_creates_new_run(chat):
    a, b = conversation(chat.admin, context=chat.context), conversation(chat.operator, context=chat.context)
    body = {"content": "按上下文分析", "client_message_id": "shared-message", "owner_id": "forged"}
    one = chat.admin.post(f"/api/conversations/{a['id']}/messages", json=body)
    two = chat.operator.post(f"/api/conversations/{b['id']}/messages", json=body)
    assert one.status_code == two.status_code == 200
    ra, rb = one.json()["run"], two.json()["run"]
    assert ra and rb and ra["id"] != rb["id"]
    assert chat.store.get("run", rb["id"])["owner_id"] == chat.user["id"]
    duplicate = chat.operator.post(f"/api/conversations/{b['id']}/messages", json=body).json()
    assert duplicate["run"]["id"] == rb["id"] and len(chat.store.list("run")) == 2
    supplement = chat.operator.post(f"/api/conversations/{b['id']}/messages", json={"content": "补充同一上下文", "client_message_id": "supplement"}).json()
    assert supplement["run"]["id"] == rb["id"]
    changed = chat.operator.post(f"/api/conversations/{b['id']}/messages", json={"content": "换目标", "client_message_id": "changed", "run_input": {"target": "new-target"}}).json()
    assert changed["run"]["id"] != rb["id"] and changed["run"]["parent_run_id"] == rb["id"]


def test_attachments_owner_paths_extraction_and_cross_conversation(chat):
    a, b = conversation(chat.admin), conversation(chat.operator)
    own2 = conversation(chat.operator, "second")
    text = ("中文材料\n" * 5000).encode()
    first = upload(chat.operator, b["id"], "../../material.txt", text)
    assert first.status_code == 200, first.text
    meta = first.json()
    assert meta["name"] == "material.txt" and "path" not in meta and "storage_key" not in meta
    assert meta["sha256"] == hashlib.sha256(text).hexdigest()
    assert chat.operator.get(meta["download_url"]).content == text
    download = chat.operator.get(meta["download_url"])
    assert "attachment" in download.headers["content-disposition"] and download.headers["x-content-type-options"] == "nosniff"
    assert chat.admin.get(meta["download_url"]).status_code == 404
    assert chat.admin.get(f"/api/attachments/{meta['id']}").status_code == 404
    assert upload(chat.admin, b["id"]).status_code == 404
    with pytest.raises(HTTPException) as error:
        chat.app.state.attachments.reading_context(chat.user["id"], own2["id"], [meta["id"]])
    assert error.value.status_code == 404
    second = upload(chat.operator, b["id"], "other.txt", text).json()
    binary = upload(chat.operator, b["id"], "image.bin", b"\x00\xff\x80", "application/octet-stream").json()
    views = chat.app.state.attachments.reading_context(chat.user["id"], b["id"], [meta["id"], second["id"], binary["id"]])
    assert len(views[0]["text"]) == 12000 and len(views[1]["text"]) == 12000
    assert sum(len(v["text"] or "") for v in views) == 24000
    assert all(v.get("truncated") for v in views[:2]) and not views[2]["extracted"]
    assert views[2]["extraction_status"] == "not_extracted"
    raw = chat.app.state.attachments._path(chat.store.get("attachment", meta["id"]))
    assert raw.is_relative_to(chat.path / "artifacts" / "attachments") and raw.name != "material.txt"


def test_attachment_limits_request_stream_and_failure_cleanup(chat, monkeypatch):
    conv = conversation(chat.operator)
    invalid = chat.operator.post(f"/api/conversations/{conv['id']}/attachments", json={"name": "bad.txt", "content_base64": "not base64", "media_type": "text/plain"})
    assert invalid.status_code == 422
    assert upload(chat.operator, conv["id"], data=b"x" * (FILE_LIMIT + 1)).status_code == 413
    assert upload(chat.operator, conv["id"], data=b"x" * FILE_LIMIT).status_code == 200
    too_large = chat.operator.post(f"/api/conversations/{conv['id']}/attachments", content=(chunk for chunk in [b" " * (4 * 1024 * 1024), b" " * (4 * 1024 * 1024 + 1)]), headers={"Content-Type": "application/json"})
    assert too_large.status_code == 413
    count = len(chat.store.list("attachment"))
    original = chat.store.save
    def fail_metadata(kind, *args, **kwargs):
        if kind == "attachment":
            raise RuntimeError("synthetic metadata failure")
        return original(kind, *args, **kwargs)
    files_before = set((chat.path / "artifacts" / "attachments").rglob("*.blob"))
    monkeypatch.setattr(chat.store, "save", fail_metadata)
    with pytest.raises(RuntimeError, match="synthetic"):
        chat.app.state.attachments.upload(chat.user["id"], conv["id"], {"name": "failure.txt", "media_type": "text/plain", "content_base64": "eA=="})
    assert set((chat.path / "artifacts" / "attachments").rglob("*.blob")) == files_before
    assert len(chat.store.list("attachment")) == count


def test_user_quota_and_symlink_cannot_read_other_file(chat):
    conv = conversation(chat.operator)
    meta = upload(chat.operator, conv["id"]).json()
    record = chat.store.get("attachment", meta["id"])
    path = chat.app.state.attachments._path(record)
    outside = chat.path / "outside"
    outside.write_text("must not expose")
    path.unlink()
    path.symlink_to(outside)
    assert chat.operator.get(meta["download_url"]).status_code == 404
    chat.store.save("attachment", "quota-fixture", {"owner_id": chat.user["id"], "conversation_id": conv["id"], "size": USER_LIMIT})
    assert upload(chat.operator, conv["id"]).status_code == 413


def test_finish_reconciliation_reports_idempotent_and_unconsumed_message_visible(chat):
    conv = conversation(chat.operator, context=chat.context)
    receipt = chat.operator.post(f"/api/conversations/{conv['id']}/messages", json={"content": "首条", "client_message_id": "first"}).json()
    run = chat.store.get("run", receipt["run"]["id"])
    first_id = receipt["message"]["id"]
    last = chat.operator.post(f"/api/conversations/{conv['id']}/messages", json={"content": "最后未消费补充", "client_message_id": "last"}).json()["message"]
    run = chat.store.get("run", run["id"])
    for message in run["messages"]:
        if message.get("client_message_id") == first_id:
            message["status"] = "processed"
        elif message.get("client_message_id") == last["id"]:
            message.update(status="unprocessed", reason="结束前未消费")
    run.update(status="partial", report={"analysis": "合成分析正文", "completion_scope": "bounded_evidence", "limitations": ["合成缺口"], "source": "platform_template", "model_generated": False})
    chat.store.save("run", run["id"], run)
    detail = chat.operator.get(f"/api/conversations/{conv['id']}").json()
    assert len([m for m in detail["messages"] if m["role"] == "assistant"]) == 1
    messages = {m["id"]: m for m in detail["messages"]}
    assert messages[first_id]["status"] == "processed"
    assert messages[last["id"]]["status"] == "unprocessed"
    attachments = detail["attachments"]
    assert len(attachments) == 1 and attachments[0]["kind"] == "generated_report"
    assert "合成分析正文" in chat.operator.get(attachments[0]["download_url"]).text
    chat.app.state.conversations.finish_run(run)
    again = chat.operator.get(f"/api/conversations/{conv['id']}").json()
    assert len(again["attachments"]) == 1 and len([m for m in again["messages"] if m["role"] == "assistant"]) == 1


def test_overview_filters_owner_and_never_contains_private_snapshot_paths(chat):
    for owner in ("admin", chat.user["id"]):
        chat.store.save("run", "overview-" + owner, {"owner_id": owner, "status": "running", "title": owner + " private", "created_at": "2026-10-22", "config_snapshot": {"secret_id": "hidden-ref"}, "user_materials": [{"text": "private-reading"}], "history_context": {"text": "private-history"}, "evidence": [{"path": "/private/path"}]})
    response = chat.operator.get("/api/overview")
    assert response.status_code == 200
    assert response.json()["counts"]["runs"] == 1
    assert "admin private" not in response.text
    for hidden in ("config_snapshot", "private-reading", "private-history", "/private/path", "hidden-ref"):
        assert hidden not in response.text


def test_context_type_errors_and_no_unscoped_tool_permission(chat):
    for context in ({"skill_id": []}, {"model_id": {}}, {"target": 7}, {"tool_scope": ["arbitrary_write"]}):
        assert chat.operator.post("/api/conversations", json={"client_request_id": "bad", "context": context}).status_code == 422
    assert chat.operator.post("/api/conversations", json={"client_request_id": "blank", "title": "   "}).status_code == 422


def test_report_artifact_failure_keeps_assistant_body_and_explicit_error(chat, monkeypatch):
    conv = conversation(chat.operator, context=chat.context)
    receipt = chat.operator.post(f"/api/conversations/{conv['id']}/messages", json={"content": "保留失败报告", "client_message_id": "artifact-failure"}).json()
    run = chat.store.get("run", receipt["run"]["id"])
    run.update(status="partial", report={"analysis": "正文必须保留", "limitations": ["合成失败"], "source": "platform_template"})
    chat.store.save("run", run["id"], run)
    monkeypatch.setattr(chat.app.state.attachments, "save_report", lambda value: (_ for _ in ()).throw(RuntimeError("synthetic disk error")))
    chat.app.state.conversations.finish_run(run)
    saved = chat.store.get("run", run["id"])
    assert saved["artifact_error"]["code"] == "report_attachment_failed"
    detail = chat.operator.get(f"/api/conversations/{conv['id']}").json()
    assistant = next(m for m in detail["messages"] if m["role"] == "assistant")
    assert assistant["content"] == "正文必须保留" and assistant["error"] and not assistant["attachment_ids"]


def test_client_message_id_cannot_shadow_server_assistant_key(chat):
    conv = conversation(chat.operator)
    service = chat.app.state.conversations
    user = service.append_message(conv["id"], chat.user["id"], "user", "用户正文", "run:fixture")
    assistant = service.append_message(conv["id"], chat.user["id"], "assistant", "助手正文", "run:fixture")
    assert user["id"] != assistant["id"]
    assert service.append_message(conv["id"], chat.user["id"], "assistant", "不覆盖原正文", "run:fixture")["id"] == assistant["id"]


def test_old_session_admin_history_and_actual_request_limit(chat):
    import secrets
    import time
    token = secrets.token_urlsafe(32)
    key = hashlib.sha256(token.encode()).hexdigest()
    chat.store.save("session", key, {"user": "admin", "expires_at": time.time() + 120})
    legacy = TestClient(chat.app, headers=ORIGIN)
    legacy.cookies.set("easyops_session", token)
    assert legacy.get("/api/session").json()["user"]["id"] == "admin"
    chat.store.save("run", "legacy-ownerless", {"thread_id": "legacy", "status": "failed", "created_at": "2026-10-22", "messages": []})
    assert legacy.get("/api/runs/legacy-ownerless").status_code == 200
    assert chat.operator.get("/api/runs/legacy-ownerless").status_code == 404
    oversized = legacy.post("/api/conversations", content=(p for p in [b" " * (1024 * 1024), b" " * (1024 * 1024 + 1)]), headers={"Content-Type": "application/json"})
    assert oversized.status_code == 413
    legacy.close()


def test_pending_supplement_recovers_original_terminal_run_receipt(chat, monkeypatch):
    conv = conversation(chat.operator, context=chat.context)
    first = chat.operator.post(f"/api/conversations/{conv['id']}/messages", json={"content": "启动分析", "client_message_id": "start-recovery"}).json()
    run_id = first["run"]["id"]
    service = chat.app.state.conversations
    original_update = service._update_message
    def interrupt_receipt(*args, **kwargs):
        if kwargs.get("status") == "accepted":
            raise RuntimeError("synthetic interruption after runtime.message commit")
        return original_update(*args, **kwargs)
    monkeypatch.setattr(service, "_update_message", interrupt_receipt)
    body = {"content": "已持久化的运行中补充", "client_message_id": "recover-supplement"}
    with pytest.raises(RuntimeError, match="synthetic interruption"):
        chat.operator.post(f"/api/conversations/{conv['id']}/messages", json=body)
    pending = next(m for m in service.require_owned(conv["id"], chat.user["id"])["messages"] if m.get("client_message_id") == body["client_message_id"])
    assert pending["status"] == "pending" and pending["run_id"] is None
    run = chat.store.get("run", run_id)
    accepted = next(m for m in run["messages"] if m.get("client_message_id") == pending["id"])
    accepted.update(status="unprocessed", reason="终止前未消费，保留真实状态")
    run.update(status="partial", report={"analysis": "原轮次报告", "limitations": []})
    chat.store.save("run", run_id, run)
    monkeypatch.setattr(service, "_update_message", original_update)
    retried = chat.operator.post(f"/api/conversations/{conv['id']}/messages", json=body)
    assert retried.status_code == 200, retried.text
    receipt = retried.json()
    assert receipt["run"]["id"] == run_id
    assert receipt["message"]["id"] == pending["id"]
    assert receipt["message"]["status"] == "unprocessed"
    assert receipt["message"]["reason"] == accepted["reason"]
    assert len(chat.store.list("run")) == 1
    assert receipt["conversation"]["active_run_id"] is None


def test_detail_retries_report_artifact_and_refreshes_same_assistant(chat, monkeypatch):
    conv = conversation(chat.operator, context=chat.context)
    first = chat.operator.post(f"/api/conversations/{conv['id']}/messages", json={"content": "生成可恢复报告", "client_message_id": "artifact-recovery"}).json()
    run = chat.store.get("run", first["run"]["id"])
    for message in run["messages"]:
        message["status"] = "processed"
    run.update(status="partial", report={"analysis": "保持不变的原始报告正文", "limitations": ["合成范围限制"], "source": "platform_template"})
    chat.store.save("run", run["id"], run)
    attachments = chat.app.state.attachments
    save_report = attachments.save_report
    monkeypatch.setattr(attachments, "save_report", lambda value: (_ for _ in ()).throw(HTTPException(507, "synthetic storage quota")))
    chat.app.state.conversations.finish_run(run)
    failed = chat.app.state.conversations.require_owned(conv["id"], chat.user["id"])
    assistant = next(m for m in failed["messages"] if m["role"] == "assistant")
    assert assistant["error"] and not assistant["attachment_ids"]
    assert chat.store.get("run", run["id"])["artifact_error"]
    monkeypatch.setattr(attachments, "save_report", save_report)
    repaired = chat.operator.get(f"/api/conversations/{conv['id']}")
    assert repaired.status_code == 200, repaired.text
    detail = repaired.json()
    restored = next(m for m in detail["messages"] if m["role"] == "assistant")
    assert restored["id"] == assistant["id"] and restored["content"] == assistant["content"]
    assert restored["error"] is None and restored["report_attachment"]
    assert len(restored["attachment_ids"]) == 1 and len(detail["attachments"]) == 1
    saved_run = chat.store.get("run", run["id"])
    assert not saved_run.get("artifact_error") and not saved_run.get("report_attachment_error")
    assert saved_run["report_attachment"]["id"] == restored["report_attachment"]["id"]
    again = chat.operator.get(f"/api/conversations/{conv['id']}").json()
    assert len([m for m in again["messages"] if m["role"] == "assistant"]) == 1
    assert len(again["attachments"]) == 1
