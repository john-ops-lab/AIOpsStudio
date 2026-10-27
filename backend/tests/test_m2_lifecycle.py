"""M2 lifecycle acceptance uses synthetic SQLite/bytes, never business assets."""
import base64
import copy
import hashlib
import json
from pathlib import Path

import pytest
from fastapi import HTTPException

from test_chat_services import chat, conversation, upload
from aiopsstudio.lifecycle import LifecycleService


def completed(chat, conv, key="complete", body="合成报告保持原文"):
    receipt = chat.operator.post(f"/api/conversations/{conv['id']}/messages", json={"content": "分析合成材料", "client_message_id": key}).json()
    run = chat.store.get("run", receipt["run"]["id"])
    for item in run["messages"]:
        item["status"] = "processed"
    run.update(status="partial", report={"analysis": body, "limitations": ["合成边界"], "source": "platform_template", "model_generated": False})
    chat.store.save("run", run["id"], run)
    chat.app.state.conversations.finish_run(run)
    return chat.store.get("run", run["id"])


def preview(client, kind, id):
    prefix = "conversations" if kind == "conversation" else "attachments"
    response = client.post(f"/api/{prefix}/{id}/deletion-preview", json={})
    assert response.status_code == 200, response.text
    return response.json()


def remove(client, kind, id, token):
    prefix = "conversations" if kind == "conversation" else "attachments"
    return client.post(f"/api/{prefix}/{id}/delete", json={"preview_token": token})


def test_database_pagination_search_archive_time_and_owner(chat, monkeypatch):
    values = []
    for index in range(5):
        value = conversation(chat.operator, "page-" + str(index))
        value.update(title="事件 " + str(index), created_at=f"2026-10-2{index}T01:00:00+00:00")
        values.append(chat.store.save("conversation", value["id"], value))
    foreign = conversation(chat.admin, "foreign-page")
    chat.store.save("conversation", foreign["id"], {**foreign, "title": "事件 外部"})
    # A list of all conversations must never be needed by the user listing.
    original = chat.store.list
    monkeypatch.setattr(chat.store, "list", lambda kind: (_ for _ in ()).throw(AssertionError("full conversation listing")) if kind == "conversation" else original(kind))
    first = chat.operator.get("/api/conversations?search=事件&limit=2").json()
    assert len(first["items"]) == 2 and first["next_cursor"]
    # An insertion after page one is outside its stable snapshot ceiling.
    chat.store.save("conversation", "synthetic-later", {"owner_id": chat.user["id"], "title": "事件 新增", "messages": [], "context": {}, "created_at": "2099-01-01T00:00:00+00:00"})
    seen = [c["id"] for c in first["items"]]
    cursor = first["next_cursor"]
    while cursor:
        response = chat.operator.get("/api/conversations", params={"search": "事件", "limit": 2, "cursor": cursor})
        assert response.status_code == 200, response.text
        page = response.json()
        seen.extend(c["id"] for c in page["items"])
        cursor = page["next_cursor"]
    assert seen == [c["id"] for c in reversed(values)]
    assert len(seen) == len(set(seen)) and foreign["id"] not in seen
    assert chat.admin.get("/api/conversations", params={"search": "事件", "limit": 2, "cursor": first["next_cursor"]}).status_code == 422
    assert chat.operator.get("/api/conversations", params={"search": "other", "limit": 2, "cursor": first["next_cursor"]}).status_code == 422
    narrow = chat.operator.get("/api/conversations", params={"start": "2026-10-21T00:00:00Z", "end": "2026-10-22T23:00:00Z"}).json()
    assert {c["id"] for c in narrow["items"]} == {values[1]["id"], values[2]["id"]}
    assert chat.operator.get("/api/conversations?start=2026-01-01").status_code == 422
    assert chat.operator.get("/api/conversations?limit=101").status_code == 422


def test_archive_restore_and_legacy_defaults_do_not_rewrite_content(chat):
    conv = conversation(chat.operator)
    original = chat.store.get("conversation", conv["id"])
    original.pop("lifecycle_version", None)
    original.pop("archived", None)
    chat.store.save("conversation", conv["id"], original)
    before = copy.deepcopy(chat.store.get("conversation", conv["id"]))
    assert chat.operator.get(f"/api/conversations/{conv['id']}").status_code == 200
    assert chat.store.get("conversation", conv["id"]) == before
    archived = chat.operator.post(f"/api/conversations/{conv['id']}/archive", json={"archived": True, "expected_revision": before["revision"]})
    assert archived.status_code == 200, archived.text
    saved = archived.json()
    assert saved["lifecycle_version"] == 2 and saved["messages"] == before["messages"] and saved["context"] == before["context"]
    assert not chat.operator.get("/api/conversations").json()["items"]
    assert chat.operator.get("/api/conversations?archived=true").json()["items"][0]["id"] == conv["id"]
    assert chat.operator.post(f"/api/conversations/{conv['id']}/messages", json={"content": "不能运行", "client_message_id": "archived-send"}).status_code == 409
    assert upload(chat.operator, conv["id"]).status_code == 409
    assert chat.operator.put(f"/api/conversations/{conv['id']}", json={"title": "不能修改"}).status_code == 409
    restored = chat.operator.post(f"/api/conversations/{conv['id']}/archive", json={"archived": False, "expected_revision": saved["revision"]})
    assert restored.status_code == 200
    assert restored.json()["messages"] == before["messages"] and restored.json()["context"] == before["context"]
    assert chat.admin.post(f"/api/conversations/{conv['id']}/archive", json={"archived": True, "expected_revision": restored.json()["revision"]}).status_code == 404


def test_export_authorized_actual_bytes_and_no_runtime_private_fields(chat):
    conv = conversation(chat.operator, context=chat.context)
    meta = upload(chat.operator, conv["id"], "材料.txt", "实际导出字节".encode()).json()
    run = completed(chat, conv)
    result = chat.operator.get(f"/api/conversations/{conv['id']}/export")
    assert result.status_code == 200, result.text
    payload = result.json()
    exported = next(a for a in payload["attachments"] if a["id"] == meta["id"])
    decoded = base64.b64decode(exported["content_base64"])
    assert decoded == "实际导出字节".encode() and hashlib.sha256(decoded).hexdigest() == meta["sha256"]
    for forbidden in ("config_snapshot", "storage_key", "\"path\"", "secret_id", "synthetic-fixture-key-never-call"):
        assert forbidden not in result.text
    assert chat.admin.get(f"/api/conversations/{conv['id']}/export").status_code == 404
    path = chat.app.state.attachments._path(chat.store.get("attachment", meta["id"]))
    path.write_bytes(b"corrupted")
    assert chat.operator.get(f"/api/conversations/{conv['id']}/export").status_code == 410


def test_attachment_confirmed_delete_reclaims_quota_and_blocks_old_links(chat):
    conv = conversation(chat.operator)
    meta = upload(chat.operator, conv["id"], data=b"capacity bytes").json()
    assert chat.operator.get("/api/attachments/usage").json()["used_bytes"] == len(b"capacity bytes")
    plan = preview(chat.operator, "attachment", meta["id"])
    assert not plan["blocked"] and plan["counts"]["attachment_bytes"] == meta["size"]
    assert remove(chat.admin, "attachment", meta["id"], plan["preview_token"]).status_code == 404
    result = remove(chat.operator, "attachment", meta["id"], plan["preview_token"])
    assert result.status_code == 200 and result.json()["cleanup_status"] == "complete"
    assert remove(chat.operator, "attachment", meta["id"], plan["preview_token"]).json()["already_deleted"] is True
    assert chat.operator.get(meta["download_url"]).status_code == 404
    assert chat.operator.get(f"/api/attachments/{meta['id']}").status_code == 404
    assert chat.operator.get("/api/attachments/usage").json()["used_bytes"] == 0
    assert not chat.app.state.attachments._path(chat.store.get("attachment", meta["id"])).exists()


def test_preview_changed_revision_expired_and_reference_added_are_rejected(chat):
    conv = conversation(chat.operator)
    meta = upload(chat.operator, conv["id"]).json()
    plan = preview(chat.operator, "attachment", meta["id"])
    record = chat.store.get("attachment", meta["id"])
    chat.store.save("attachment", meta["id"], {**record, "name": "new-name.txt"})
    assert remove(chat.operator, "attachment", meta["id"], plan["preview_token"]).status_code == 409
    plan = preview(chat.operator, "attachment", meta["id"])
    key = hashlib.sha256(plan["preview_token"].encode()).hexdigest()
    ticket = chat.store.get("deletion_preview", key)
    chat.store.save("deletion_preview", key, {**ticket, "expires_at": "2000-01-01T00:00:00+00:00"})
    assert remove(chat.operator, "attachment", meta["id"], plan["preview_token"]).status_code == 409
    plan = preview(chat.operator, "attachment", meta["id"])
    chat.app.state.conversations.append_message(conv["id"], chat.user["id"], "user", "引用材料", "reference-added", attachment_ids=[meta["id"]])
    assert remove(chat.operator, "attachment", meta["id"], plan["preview_token"]).status_code == 409
    blocked = preview(chat.operator, "attachment", meta["id"])
    assert blocked["blocked"] and blocked["preview_token"] is None and "retained_reference" in blocked["reasons"]
    assert chat.operator.get(meta["download_url"]).status_code == 200


def test_running_conversation_and_external_reference_protect_material(chat):
    conv = conversation(chat.operator, context=chat.context)
    receipt = chat.operator.post(f"/api/conversations/{conv['id']}/messages", json={"content": "等待执行", "client_message_id": "queued-protection"}).json()
    run = chat.store.get("run", receipt["run"]["id"])
    assert preview(chat.operator, "conversation", conv["id"])["blocked"]
    assert chat.operator.post(f"/api/conversations/{conv['id']}/archive", json={"archived": True, "expected_revision": receipt["conversation"]["revision"]}).status_code == 409
    run.update(status="partial", report={"summary": "合成完成"})
    chat.store.save("run", run["id"], run)
    chat.app.state.conversations.finish_run(run)
    foreign = conversation(chat.admin, "external-protection")
    chat.store.save("run", "synthetic-external-reference", {"owner_id": "admin", "conversation_id": foreign["id"], "thread_id": foreign["id"], "status": "partial", "parent_run_id": run["id"], "trust_version": "trusted-runtime/1", "messages": [], "evidence": []})
    result = preview(chat.operator, "conversation", conv["id"])
    assert result["blocked"] and result["reasons"] == ["retained_reference"]
    assert "synthetic-external-reference" not in json.dumps(result) and foreign["id"] not in json.dumps(result)


def test_whole_conversation_delete_keeps_tombstones_and_pending_cleanup_retries(chat, monkeypatch):
    conv = conversation(chat.operator, context=chat.context)
    uploaded = upload(chat.operator, conv["id"]).json()
    run = completed(chat, conv)
    plan = preview(chat.operator, "conversation", conv["id"])
    assert not plan["blocked"] and plan["counts"]["runs"] == 1 and plan["counts"]["attachments"] == 2
    unlink = LifecycleService._safe_unlink
    monkeypatch.setattr(LifecycleService, "_safe_unlink", lambda *args: (_ for _ in ()).throw(OSError("synthetic cleanup interruption")))
    first = remove(chat.operator, "conversation", conv["id"], plan["preview_token"])
    assert first.status_code == 200 and first.json()["cleanup_status"] == "pending"
    assert chat.operator.get(f"/api/conversations/{conv['id']}").status_code == 404
    assert chat.operator.get(uploaded["download_url"]).status_code == 404
    assert not chat.operator.get("/api/conversations").json()["items"]
    monkeypatch.setattr(LifecycleService, "_safe_unlink", unlink)
    again = remove(chat.operator, "conversation", conv["id"], plan["preview_token"])
    assert again.status_code == 200 and again.json()["cleanup_status"] == "complete"
    assert chat.store.get("conversation", conv["id"])["messages"] == []
    assert chat.store.get("run", run["id"])["report"]["expired"] is True
    assert not list((chat.path / "artifacts" / "attachments").rglob("*.blob"))


def test_report_repair_reuses_original_identity_and_no_model_or_source_dispatch(chat):
    conv = conversation(chat.operator, context=chat.context)
    run = completed(chat, conv)
    attachment = run["report_attachment"]
    record = chat.store.get("attachment", attachment["id"])
    path = chat.app.state.attachments._path(record)
    expected = path.read_bytes()
    usage = copy.deepcopy(run["usage"])
    path.unlink()
    response = chat.operator.post(f"/api/runs/{run['id']}/repair-report", json={})
    assert response.status_code == 200, response.text
    assert response.json()["delivery_status"] == "ready" and response.json()["report_attachment"]["id"] == attachment["id"]
    assert path.read_bytes() == expected and chat.store.get("run", run["id"])["usage"] == usage
    assert len(chat.store.list("attachment")) == 1
    path.write_bytes(b"bad bytes")
    response = chat.operator.post(f"/api/runs/{run['id']}/repair-report", json={})
    assert response.status_code == 200 and response.json()["delivery_status"] == "failed"
    assert path.read_bytes() == b"bad bytes" and chat.store.get("run", run["id"])["usage"] == usage
    assert chat.admin.post(f"/api/runs/{run['id']}/repair-report", json={}).status_code == 404


def test_modes_create_new_parent_rounds_and_receipts_keep_original_mode(chat):
    conv = conversation(chat.operator, context=chat.context)
    parent = completed(chat, conv)
    body = {"content": "解释已有报告", "client_message_id": "mode-explain", "mode": "explain", "parent_run_id": parent["id"]}
    response = chat.operator.post(f"/api/conversations/{conv['id']}/messages", json=body)
    assert response.status_code == 200, response.text
    first = response.json()
    saved = chat.store.get("run", first["run"]["id"])
    assert saved["id"] != parent["id"] and saved["mode"] == "explain" and saved["parent_run_id"] == parent["id"]
    assert first["message"]["mode"] == "explain"
    duplicate = chat.operator.post(f"/api/conversations/{conv['id']}/messages", json={**body, "mode": "followup"}).json()
    assert duplicate["run"]["id"] == saved["id"] and duplicate["message"]["mode"] == "explain"
    follow = chat.operator.post(f"/api/conversations/{conv['id']}/messages", json={"content": "补查新证据", "client_message_id": "mode-followup", "mode": "followup", "parent_run_id": parent["id"]}).json()
    assert follow["run"]["id"] != saved["id"] and follow["message"]["mode"] == "followup"
    assert chat.store.get("run", parent["id"])["report"] == parent["report"]
    assert chat.operator.post(f"/api/conversations/{conv['id']}/messages", json={"content": "bad", "client_message_id": "invalid-mode", "mode": "bad"}).status_code == 422
    fresh = upload(chat.operator, conv["id"]).json()
    assert chat.operator.post(f"/api/conversations/{conv['id']}/messages", json={**body, "client_message_id": "explain-new-material", "attachment_ids": [fresh["id"]]}).status_code == 422
    other = conversation(chat.operator, "other-parent", context=chat.context)
    assert chat.operator.post(f"/api/conversations/{other['id']}/messages", json={**body, "client_message_id": "foreign-parent"}).status_code == 404


def test_orphan_bytes_are_charged_and_confirmation_does_not_collect_live_files(chat):
    conv = conversation(chat.operator)
    attachment = upload(chat.operator, conv["id"], data=b"protected bytes").json()
    service = chat.app.state.attachments
    directory = service._owner_dir(chat.user["id"])
    orphan = directory / ("a" * 32 + ".blob")
    orphan.write_bytes(b"synthetic crash orphan")
    other = conversation(chat.admin, "orphan-other")
    foreign = upload(chat.admin, other["id"], data=b"foreign bytes").json()
    other_orphan = service._owner_dir("admin") / ("b" * 32 + ".blob")
    other_orphan.write_bytes(b"other orphan")
    usage = chat.operator.get("/api/attachments/usage").json()
    assert usage["orphan_bytes"] == orphan.stat().st_size and usage["used_bytes"] == attachment["size"] + orphan.stat().st_size
    planned = chat.operator.post("/api/attachments/orphans/deletion-preview", json={}).json()
    assert planned["file_count"] == 1 and "a" * 32 not in json.dumps(planned)
    assert chat.admin.post("/api/attachments/orphans/delete", json={"preview_token": planned["preview_token"]}).status_code == 404
    deleted = chat.operator.post("/api/attachments/orphans/delete", json={"preview_token": planned["preview_token"]})
    assert deleted.status_code == 200 and deleted.json()["cleanup_status"] == "complete"
    assert not orphan.exists() and other_orphan.exists()
    assert chat.operator.get(attachment["download_url"]).content == b"protected bytes"
    assert chat.admin.get(foreign["download_url"]).content == b"foreign bytes"
    assert chat.operator.post("/api/attachments/orphans/delete", json={"preview_token": planned["preview_token"]}).json()["already_deleted"]
    assert chat.operator.get("/api/attachments/usage").json()["orphan_bytes"] == 0


def test_orphan_preview_change_is_rejected_without_deleting_anything(chat):
    conv = conversation(chat.operator)
    service = chat.app.state.attachments
    upload(chat.operator, conv["id"])
    path = service._owner_dir(chat.user["id"]) / ("c" * 32 + ".blob")
    path.write_bytes(b"first")
    planned = chat.operator.post("/api/attachments/orphans/deletion-preview", json={}).json()
    path.write_bytes(b"modified after preview")
    response = chat.operator.post("/api/attachments/orphans/delete", json={"preview_token": planned["preview_token"]})
    assert response.status_code == 409 and path.read_bytes() == b"modified after preview"


def test_blob_publish_is_rolled_back_when_commit_fencing_fails(chat, monkeypatch):
    from aiopsstudio.store import LeaseLost
    conv = conversation(chat.operator)
    original = chat.store._execute
    before = set((chat.path / "artifacts" / "attachments").rglob("*"))
    count = len(chat.store.list("attachment"))
    def failed_commit(sql, args=()):
        if sql == "COMMIT" and chat.store._rollback_actions:
            raise LeaseLost("synthetic stale execution at commit")
        return original(sql, args)
    monkeypatch.setattr(chat.store, "_execute", failed_commit)
    with pytest.raises(LeaseLost):
        chat.app.state.attachments.upload(chat.user["id"], conv["id"], {"name": "stale.txt", "media_type": "text/plain", "content_base64": "eA=="})
    assert len(chat.store.list("attachment")) == count
    assert not list((chat.path / "artifacts" / "attachments").rglob("*.blob"))
    assert not list((chat.path / "artifacts" / "attachments").rglob(".write-*"))


def test_export_limit_also_applies_when_no_attachment_exists(chat, monkeypatch):
    import aiopsstudio.lifecycle as module
    conv = conversation(chat.operator)
    monkeypatch.setattr(module, "EXPORT_LIMIT", 100)
    assert chat.operator.get(f"/api/conversations/{conv['id']}/export").status_code == 413


from test_legacy_archive import archive_app, register, snapshot


def test_registered_legacy_archive_readonly_boundary_and_export_preserve_originals(archive_app):
    app, store, owner, admin, uid, conv_id, run_id, report_id, upload_id, path = archive_app
    register(archive_app)
    before = snapshot(store)
    original = path.read_bytes()
    current = store.get("conversation", conv_id)
    archive = owner.post(f"/api/conversations/{conv_id}/archive", json={"archived": True, "expected_revision": current["revision"]})
    assert archive.status_code == 409 and archive.json()["code"] == "legacy_archive_readonly"
    deletion = owner.post(f"/api/conversations/{conv_id}/deletion-preview", json={})
    assert deletion.status_code == 409 and deletion.json()["code"] == "legacy_archive_readonly"
    exported = owner.get(f"/api/conversations/{conv_id}/export")
    assert exported.status_code == 200, exported.text
    report = next(a for a in exported.json()["attachments"] if a["id"] == report_id)
    assert base64.b64decode(report["content_base64"]) == original
    assert snapshot(store) == before and path.read_bytes() == original


def test_orphan_preview_cannot_observe_an_inflight_file_before_metadata_commit(chat, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    service = chat.app.state.attachments
    conv = conversation(chat.operator)
    published, release = Event(), Event()
    write = service._write_bytes
    def paused(path, data):
        write(path, data)
        published.set()
        assert release.wait(3)
    monkeypatch.setattr(service, "_write_bytes", paused)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending_upload = pool.submit(service.upload, chat.user["id"], conv["id"], {"name": "inflight.txt", "media_type": "text/plain", "content_base64": "eA=="})
        assert published.wait(3)
        pending_preview = pool.submit(service.orphan_preview, chat.user["id"])
        try:
            assert not pending_preview.done(), "The preview must wait for the writer's transaction lock"
        finally:
            release.set()
        saved, plan = pending_upload.result(timeout=3), pending_preview.result(timeout=3)
    assert plan["file_count"] == 0
    assert chat.operator.get(saved["download_url"]).content == b"x"


def test_old_thread_only_running_record_is_protected(chat):
    conv = conversation(chat.operator)
    chat.store.save("run", "synthetic-thread-only", {"owner_id": chat.user["id"], "thread_id": conv["id"],
        "status": "queued", "trust_version": "trusted-runtime/1", "messages": [], "evidence": []})
    planned = preview(chat.operator, "conversation", conv["id"])
    assert planned["blocked"] and "active_run" in planned["reasons"] and planned["counts"]["runs"] == 1


def test_deleted_run_and_derived_payloads_do_not_reappear_in_api_or_new_creation(chat):
    conv = conversation(chat.operator, key="do-not-resurrect", context=chat.context)
    run = completed(chat, conv)
    chat.store.save("invocation", "synthetic-deleted-invocation", {"owner_id": chat.user["id"], "run_id": run["id"], "source": "model", "result": "SYNTHETIC_REMOVED_RESULT"})
    chat.store.save("material_read", "synthetic-deleted-material", {"owner_id": chat.user["id"], "run_id": run["id"], "result": "SYNTHETIC_REMOVED_MATERIAL"})
    planned = preview(chat.operator, "conversation", conv["id"])
    assert remove(chat.operator, "conversation", conv["id"], planned["preview_token"]).status_code == 200
    assert chat.operator.get(f"/api/runs/{run['id']}").status_code == 404
    assert run["id"] not in {r["id"] for r in chat.operator.get("/api/runs").json()["items"]}
    assert chat.operator.get("/api/overview").json()["counts"]["runs"] == 0
    assert "result" not in chat.store.get("invocation", "synthetic-deleted-invocation")
    assert "result" not in chat.store.get("material_read", "synthetic-deleted-material")
    assert chat.store.events(run["id"]) == []
    assert chat.operator.post("/api/conversations", json={"client_request_id": "do-not-resurrect", "title": "不得复活"}).status_code == 404
