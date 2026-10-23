"""Upgrade archive reads retain original bytes, but never become runtime inputs."""
import asyncio
import base64
import copy
import hashlib

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import pytest

from aiopsstudio.legacy_archive import plan_archive, register_archive, digest, KEY, KIND
from aiopsstudio.main import create_app
from aiopsstudio.store import Store


@pytest.fixture
def archive_app(tmp_path):
    store = Store("sqlite:///:memory:")
    app = create_app(store=store, master_key=Fernet.generate_key(), password="synthetic-archive-admin-password",
        public_origin="http://testserver", artifact_dir=tmp_path / "artifacts", testing=True)
    admin = TestClient(app, headers={"Origin": "http://testserver"})
    assert admin.post("/api/login", json={"password": "synthetic-archive-admin-password"}).status_code == 200
    user = admin.post("/api/users", json={"username": "archive-owner", "password": "synthetic-archive-user-password"}).json()
    owner = TestClient(app, headers={"Origin": "http://testserver"})
    assert owner.post("/api/login", json={"username": "archive-owner", "password": "synthetic-archive-user-password"}).status_code == 200
    conv = app.state.conversations.create(user["id"], {"client_request_id": "legacy-conversation"})
    upload = app.state.attachments.upload(user["id"], conv["id"], {"name": "original.txt", "media_type": "text/plain", "content_base64": base64.b64encode(b"original upload").decode()})
    # These are pre-upgrade stored rows, intentionally without the new trust metadata.
    run = store.save("run", "legacy-run", {"owner_id": user["id"], "conversation_id": conv["id"], "thread_id": conv["id"], "attempt_id": "old-attempt", "status": "partial", "purpose": "investigation", "messages": [{"role": "assistant", "content": "original report"}], "report": {"analysis": "original report"}, "evidence": [], "events": [], "usage": {}})
    path = app.state.attachments._owner_dir(user["id"]) / "legacy-report.blob"
    path.write_bytes(b"original report bytes")
    report = store.save("attachment", "legacy-report", {"owner_id": user["id"], "conversation_id": conv["id"], "run_id": run["id"], "kind": "generated_report", "name": "report.md", "media_type": "text/markdown", "size": len(path.read_bytes()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    conv["messages"] = [{"id": "legacy-message", "role": "assistant", "run_id": run["id"], "content": "original report", "attachment_ids": [report["id"]]}]
    store.save("conversation", conv["id"], conv)
    yield app, store, owner, admin, user["id"], conv["id"], run["id"], report["id"], upload["id"], path
    owner.close(); admin.close(); store.close()


def register(state):
    _, store, *rest = state
    plan = plan_archive(store)
    register_archive(store, plan)
    return plan


def snapshot(store):
    return {kind: store.list(kind) for kind in ("run", "conversation", "attachment", "settings", "user")}


def test_registered_history_and_original_attachment_visible_only_to_owner_and_no_writes(archive_app):
    app, store, owner, admin, uid, conv, run, report, upload, path = archive_app
    before, raw = snapshot(store), path.read_bytes()
    assert owner.get(f"/api/conversations/{conv}").status_code == 404
    plan = register(archive_app)
    assert register_archive(store, plan)["revision"] == 1
    detail = owner.get(f"/api/conversations/{conv}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["legacy_archive"]["status"] == "legacy_unverified"
    assert detail.json()["messages"][0]["content"] == "original report"
    assert len(detail.json()["attachments"]) == 2
    assert owner.get("/api/conversations").json()["items"][0]["id"] == conv
    for url in (f"/api/runs/{run}", f"/api/attachments/{report}"):
        result = owner.get(url)
        assert result.status_code == 200, result.text
        assert result.json()["legacy_archive"]["read_only"]
        assert admin.get(url).status_code == 404
    assert owner.get("/api/runs").json()["items"][0]["legacy_archive"]["read_only"]
    assert owner.get("/api/overview").status_code == 200
    assert owner.get(f"/api/attachments/{report}/download").content == raw
    assert owner.get(f"/api/attachments/{upload}/download").content == b"original upload"
    assert admin.get(f"/api/conversations/{conv}").status_code == 404
    asyncio.run(app.state.runtime.cleanup_expired())
    assert snapshot(store) == before
    assert path.read_bytes() == raw


def test_archive_cannot_execute_inherit_read_or_send_attachments_to_model(archive_app):
    app, store, owner, admin, uid, conv, run, report, upload, path = archive_app
    register(archive_app)
    before = snapshot(store)
    requests = [
        (f"/api/runs/{run}/retry", {}),
        (f"/api/runs/{run}/messages", {"client_message_id": "new", "content": "continue"}),
        (f"/api/runs/{run}/material-reads", {"resource_type": "skill_reference", "skill_id": "x", "version_id": "v1", "path": "ref"}),
        (f"/api/conversations/{conv}/messages", {"client_message_id": "new", "content": "continue"}),
        ("/api/runs", {"client_request_id": "direct", "conversation_id": conv}),
        ("/api/runs", {"client_request_id": "direct-parent", "parent_run_id": run}),
        (f"/api/conversations/{conv}/attachments", {"name": "new.txt", "content_base64": "eA=="}),
    ]
    for url, body in requests:
        response = owner.post(url, json=body)
        assert response.status_code == 409, (url, response.status_code, response.text)
    for attachment in (report, upload):
        with pytest.raises(Exception) as error:
            app.state.attachments.reading_context(uid, conv, [attachment])
        assert error.value.status_code == 409
    with pytest.raises(Exception) as error:
        app.state.runtime._history_context({"owner_id": uid, "parent_run_id": run, "thread_id": conv})
    assert error.value.status_code == 409
    assert snapshot(store) == before


def test_plan_changes_forged_legacy_and_tampering_fail_closed(archive_app):
    app, store, owner, admin, uid, conv, run, report, upload, path = archive_app
    plan = plan_archive(store)
    altered = store.get("run", run)
    altered["title"] = "changed after plan"
    store.save("run", run, altered)
    with pytest.raises(ValueError, match="变化"):
        register_archive(store, plan)
    assert store.get(KIND, KEY) is None
    register(archive_app)
    fake = copy.deepcopy(store.get("run", run))
    store.save("run", "forged-old-run", fake)
    assert owner.get("/api/runs/forged-old-run").status_code == 404
    with pytest.raises(ValueError, match="不能扩容"):
        register_archive(store, plan_archive(store))
    altered = store.get("run", run)
    altered["report"]["analysis"] = "tampered report"
    store.save("run", run, altered)
    assert owner.get(f"/api/runs/{run}").status_code != 200
    assert owner.get(f"/api/attachments/{report}/download").status_code != 200


def test_foreign_archive_link_returns_not_found_before_archive_notice(archive_app):
    app, store, owner, admin, uid, conv, run, report, upload, path = archive_app
    register(archive_app)
    for field, identifier in (("conversation_id", conv), ("thread_id", conv), ("parent_run_id", run)):
        response = admin.post("/api/runs", json={"client_request_id": "foreign-" + field, field: identifier})
        assert response.status_code == 404
        assert "只读档案" not in response.text
