from concurrent.futures import ThreadPoolExecutor
import copy
import json
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import pytest
from aiopsstudio.config import ConfigService, Vault, ConfigError, redact
from aiopsstudio.main import create_app
from aiopsstudio.store import Store, StoreConflict

SKILL = {"name": "cmdb-query", "display_name": "CMDB 查询", "description": "查真实 CMDB", "content": "---\nname: cmdb-query\ndescription: 只读查询 CMDB\n---\n先确认目标，再引用证据。", "references": {}, "input_schema": {"type": "object"}, "dependencies": []}

@pytest.fixture
def store(tmp_path):
    instance = Store("sqlite:///" + str(tmp_path / "test.sqlite"))
    yield instance
    instance.close()

@pytest.fixture
def service(store, tmp_path):
    return ConfigService(store, Vault(store, Fernet.generate_key()), tmp_path / "artifacts")

def test_revision_conflict_and_immutable_publication(service):
    draft = service.save("skill", SKILL)
    first = service.publish("skill", draft["id"], draft["revision"])
    first_version = copy.deepcopy(first["versions"][0])
    revised = service.save("skill", {"expected_revision": first["revision"], "content": SKILL["content"] + "\n新方法。"}, first["id"])
    assert service.published(revised)["content"] == SKILL["content"]
    with pytest.raises(StoreConflict):
        service.save("skill", {"expected_revision": first["revision"], "content": "过期内容"}, first["id"])
    second = service.publish("skill", revised["id"], revised["revision"])
    assert second["versions"][0] == first_version
    assert second["current_version"] != first_version["id"]
    reverted = service.rollback("skill", second["id"], first_version["id"])
    assert reverted["current_version"] == first_version["id"]
    assert service.published(reverted)["content"] == SKILL["content"]
    assert len(service.store.list("audit")) == 5

def test_atomic_rolls_back_changes(service):
    def failing():
        service.save("skill", SKILL)
        raise RuntimeError("模拟持久化失败")
    with pytest.raises(RuntimeError):
        service.store.atomic(failing)
    assert service.store.list("skill") == []
    assert service.store.list("audit") == []

def test_secret_encrypted_references_and_revocation(service):
    credential = "unique-private-key-never-return"
    draft = service.save("model", {"name": "模型", "model": "model-1", "secret_value": credential})
    assert credential not in json.dumps(service.store.get("secret", draft["secret_id"]))
    assert "secret_value" not in draft
    published = service.publish("model", draft["id"], draft["revision"])
    rotated = service.save("model", {"expected_revision": published["revision"], "secret_value": "rotated-private-value"}, draft["id"])
    assert service.published(rotated)["secret_id"] == draft["secret_id"]
    assert redact({"error": "Bearer " + credential, "secret_value": credential}, service.vault) == {"error": "Bearer [已脱敏]"}
    service.vault.revoke(draft["secret_id"])
    with pytest.raises(ConfigError, match="撤销"):
        service.rollback("model", draft["id"], published["current_version"])
    with pytest.raises(ConfigError, match="撤销"):
        service.vault.get(draft["secret_id"])

def test_skill_validation_bounds_and_dependencies(service):
    with pytest.raises(ConfigError, match="路径"):
        service.save("skill", {**SKILL, "references": {"references/../../escape": "bad"}})
    draft = service.save("skill", {**SKILL, "dependencies": [{"server_id": "missing", "tool_name": "read", "required": False}]})
    check = service.validate("skill", draft)
    assert check["valid"] and check["warnings"]
    draft["dependencies"][0]["required"] = True
    assert not service.validate("skill", draft)["valid"]
    draft["dependencies"] = []
    draft["content"] += "\n运行 scripts/a.py"
    assert not service.validate("skill", draft)["valid"]

def test_singleton_claim_across_independent_connections_and_durable_events(tmp_path):
    url = "sqlite:///" + str(tmp_path / "queue.sqlite")
    stores = [Store(url), Store(url)]
    for i in range(2):
        stores[0].save("run", f"run_{i}", {"status": "queued"})
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda pair: pair[1].claim_job(str(pair[0])), enumerate(stores)))
    assert sum(item is not None for item in results) == 1
    picked = next(item for item in results if item)
    assert stores[0].renew_job(picked["id"], "wrong-owner") is False
    assert stores[0].event(picked["id"], "progress", {"stage": "started"})["seq"] == 1
    stores[0].close()
    reopened = Store(url)
    assert reopened.events(picked["id"], 0)[0]["payload"]["stage"] == "started"
    assert reopened.events(picked["id"], 1) == []
    reopened.close()
    stores[1].close()

def test_expired_job_is_interrupted_and_not_replayed(store):
    store.lease_seconds = -1
    store.save("run", "old", {"status": "queued"})
    store.claim_job("old-owner")
    store.save("run", "new", {"status": "queued"})
    assert store.claim_job("new-owner")["id"] == "new"
    old = store.get("run", "old")
    assert old["status"] == "failed" and old["error"]["code"] == "execution_interrupted"

def test_auth_origin_full_api_and_secret_never_echoed(store, tmp_path):
    app = create_app(store=store, master_key=Fernet.generate_key(), password="test-password-long", public_origin="http://testserver", artifact_dir=tmp_path / "artifacts", testing=True, enable_runtime=False)
    with TestClient(app) as client:
        assert client.get("/api/session").json()["authenticated"] is False
        assert client.get("/api/settings").status_code == 401
        assert client.post("/api/login", json={"password": "test-password-long"}).status_code == 403
        headers = {"Origin": "http://testserver"}
        assert client.post("/api/login", json={"password": "bad"}, headers=headers).status_code == 401
        result = client.post("/api/login", json={"password": "test-password-long"}, headers=headers)
        assert result.status_code == 200 and "HttpOnly" in result.headers["set-cookie"]
        result = client.post("/api/model-configs", json={"name": "模型", "model": "test", "secret_value": "private-test-value"}, headers=headers)
        assert result.status_code == 200
        assert "private-test-value" not in result.text and "secret_value" not in result.text
        assert "private-test-value" not in client.get("/api/config-audit").text
        assert client.post("/api/logout", json={}, headers=headers).status_code == 200
        assert client.get("/api/model-configs").status_code == 401

def test_mcp_requires_complete_discovery_and_explicit_readonly(service):
    draft = service.save("mcp", {"name": "服务", "url": "http://127.0.0.1:9000/mcp"})
    assert not service.validate("mcp", draft)["valid"]
    draft["discovery"] = {"complete": True}
    draft["tools"] = [{"name": "read", "inputSchema": {"type": "object"}, "alias": "m_read", "readonly": False}]
    draft["allowed_tools"] = ["read"]
    assert not service.validate("mcp", draft)["valid"]
    draft["tools"][0]["readonly"] = True
    assert service.validate("mcp", draft)["valid"]

def test_settings_no_plaintext_and_optimistic_lock(service):
    old = service.store.get("settings", "main")
    changed = service.settings({"expected_revision": old["revision"], "ongrid": {"url": "http://127.0.0.1:5000", "secret_value": "ongrid-private-value"}})
    assert changed["ongrid"]["secret_id"] and "secret_value" not in changed["ongrid"]
    with pytest.raises(StoreConflict):
        service.settings({"expected_revision": old["revision"]})
    with pytest.raises(ConfigError, match="凭据"):
        service.settings({"expected_revision": changed["revision"], "identity_mapping": {"api_key": "bad"}})

def test_mcp_connection_edit_invalidates_old_catalog(service):
    draft = service.save("mcp", {"name": "服务", "url": "http://127.0.0.1:9000/mcp"})
    draft["discovery"] = {"complete": True}
    draft = service.store.save("mcp", draft["id"], draft)
    updated = service.save("mcp", {"expected_revision": draft["revision"], "url": "http://127.0.0.1:9001/mcp"}, draft["id"])
    assert not service.validate("mcp", updated)["valid"]
    assert updated["discovery"]["reason"] == "connection_changed"


def test_model_headers_cannot_embed_authentication(service):
    with pytest.raises(ConfigError, match="认证"):
        service.save("model", {"name": "模型", "model": "m", "default_headers": {"X-Api-Key": "forbidden"}})

def test_ongrid_password_is_write_only_and_reference_survives_redaction(service):
    before = service.store.get("settings", "main")
    credential = "ongrid-password-only-encrypted"
    changed = service.settings({"expected_revision": before["revision"], "ongrid": {"url": "http://127.0.0.1:5000", "email": "operator@example.invalid", "password_secret_value": credential}})
    reference = changed["ongrid"]["password_secret_id"]
    assert service.vault.get(reference) == credential
    assert "password_secret_value" not in changed["ongrid"]
    assert credential not in json.dumps(service.store.get("secret", reference))
    output = redact(changed, service.vault)
    assert output["ongrid"]["password_secret_id"] == reference
    assert credential not in json.dumps(output)
    assert credential not in json.dumps(service.store.list("audit"))
    rotated = service.settings({"expected_revision": changed["revision"], "ongrid": {**changed["ongrid"], "password_secret_value": "replacement-ongrid-password"}})
    assert rotated["ongrid"]["password_secret_id"] != reference
    assert service.vault.get(reference) == credential
    service.vault.revoke(reference)
    with pytest.raises(ConfigError, match="撤销"):
        service.settings({"expected_revision": rotated["revision"], "ongrid": {**rotated["ongrid"], "password_secret_id": reference}})


def test_skill_declared_ongrid_api_dependency(service):
    draft = service.save("skill", {**SKILL, "dependencies": [{"provider_id": "ongrid", "tool_id": "alert_detail", "required": True}]})
    assert not service.validate("skill", draft)["valid"]
    settings = service.store.get("settings", "main")
    service.settings({"expected_revision": settings["revision"], "ongrid": {"url": "http://127.0.0.1:5000", "email": "operator@example.invalid", "password_secret_value": "example-password-only"}})
    assert service.validate("skill", draft)["valid"]

def test_purge_only_expired_run_events_and_preserve_sse_cursor(store):
    store.event("expired", "progress", {"content": "old body"})
    second = store.event("expired", "progress", {"content": "old body 2"})
    store.event("retained", "progress", {"content": "retained body"})
    store.purge_events("expired")
    assert store.events("expired") == []
    assert store.events("retained")[0]["payload"]["content"] == "retained body"
    metadata = store.event("expired", "artifacts_expired", {"expired": True})
    assert metadata["seq"] == second["seq"] + 1
    assert store.events("expired", second["seq"]) == [metadata]


def test_write_only_secret_fields_rejected_outside_supported_ongrid_shape(service):
    settings = service.store.get("settings", "main")
    with pytest.raises(ConfigError, match="凭据"):
        service.settings({"expected_revision": settings["revision"], "identity_mapping": {"password_secret_value": "never-persist-this"}})


def test_publish_and_enable_freezes_usable_mcp_state(service):
    draft = service.save("mcp", {"name": "服务", "url": "http://127.0.0.1:9000/mcp"})
    draft.update(discovery={"complete": True}, blocked=True, tools=[{"name": "read", "inputSchema": {"type": "object"}, "alias": "m_read", "readonly": True}], allowed_tools=["read"])
    draft = service.store.save("mcp", draft["id"], draft)
    published = service.publish("mcp", draft["id"], draft["revision"])
    fixed = service.published(published)
    assert published["enabled"] and not published["blocked"]
    assert fixed["enabled"] and not fixed["blocked"]


def test_initial_budget_is_100000_without_rewriting_saved_settings(service):
    original = service.store.get("settings", "main")
    assert original["budget"]["max_tokens"] == 100000
    original["budget"]["max_tokens"] = 24000
    saved = service.store.save("settings", "main", original, original["revision"])
    ConfigService(service.store, service.vault, service.artifact_dir)
    assert service.store.get("settings", "main")["budget"]["max_tokens"] == 24000
    assert service.store.get("settings", "main")["revision"] == saved["revision"]


def load_local_deploy(tmp_path):
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[2] / "deploy/local.py"
    spec = importlib.util.spec_from_file_location("aiopsstudio_test_local_deploy", path)
    local = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(local)
    local.STATE = tmp_path
    return local


def test_deploy_stale_pid_cannot_stop_unrelated_process(tmp_path):
    import subprocess
    local = load_local_deploy(tmp_path)
    child = subprocess.Popen(["/bin/sleep", "30"], start_new_session=True)
    try:
        (tmp_path / "api.pid").write_text(str(child.pid))
        assert local.alive("api") is False
        local.stop()
        assert child.poll() is None
    finally:
        child.terminate()
        child.wait(timeout=5)


def test_deploy_start_identity_prevents_pid_reuse_and_stops_owned_process(tmp_path):
    import subprocess
    local = load_local_deploy(tmp_path)
    child = subprocess.Popen(["/bin/sleep", "30"], start_new_session=True)
    try:
        identity = local.process_identity(child.pid)
        assert identity and identity["pgid"] == child.pid
        (tmp_path / "api.pid").write_text(str(child.pid))
        (tmp_path / "api.identity.json").write_text(json.dumps({**identity, "started_at": "Thu Jan 01 00:00:00 1970"}))
        assert local.alive("api") is False
        local.stop()
        assert child.poll() is None
        (tmp_path / "api.pid").write_text(str(child.pid))
        (tmp_path / "api.identity.json").write_text(json.dumps(identity))
        assert local.alive("api") is True
        local.stop()
        child.wait(timeout=5)
        assert child.returncode is not None
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=5)


def test_deploy_stop_waits_for_graceful_exit(tmp_path):
    import subprocess
    import sys
    import time
    local = load_local_deploy(tmp_path)
    code = "import signal,sys,time; signal.signal(signal.SIGTERM,lambda *_: (time.sleep(.2),sys.exit(0))); print('ready',flush=True); time.sleep(30)"
    child = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True, start_new_session=True)
    try:
        assert child.stdout.readline().strip() == "ready"
        identity = local.process_identity(child.pid)
        (tmp_path / "api.pid").write_text(str(child.pid))
        (tmp_path / "api.identity.json").write_text(json.dumps(identity))
        started = time.monotonic()
        local.stop_process("api", grace_seconds=1, kill_seconds=.5)
        assert time.monotonic() - started >= .2
        child.wait(timeout=1)
        assert child.returncode == 0 and not (tmp_path / "api.pid").exists()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


def test_deploy_stop_bounds_wait_for_matching_hung_process(tmp_path):
    import subprocess
    import sys
    import time
    local = load_local_deploy(tmp_path)
    code = "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print('ready',flush=True); time.sleep(30)"
    child = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True, start_new_session=True)
    try:
        assert child.stdout.readline().strip() == "ready"
        identity = local.process_identity(child.pid)
        (tmp_path / "api.pid").write_text(str(child.pid))
        (tmp_path / "api.identity.json").write_text(json.dumps(identity))
        started = time.monotonic()
        local.stop_process("api", grace_seconds=.1, kill_seconds=.5)
        elapsed = time.monotonic() - started
        child.wait(timeout=1)
        assert .1 <= elapsed < 2
        assert child.returncode == -9 and not (tmp_path / "api.identity.json").exists()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


def test_deploy_stop_timeout_does_not_kill_after_identity_changes(tmp_path, monkeypatch):
    import subprocess
    import sys
    local = load_local_deploy(tmp_path)
    code = "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print('ready',flush=True); time.sleep(30)"
    child = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True, start_new_session=True)
    try:
        assert child.stdout.readline().strip() == "ready"
        identity = local.process_identity(child.pid)
        (tmp_path / "api.pid").write_text(str(child.pid))
        (tmp_path / "api.identity.json").write_text(json.dumps(identity))
        answers = iter([child.pid, child.pid, None])
        monkeypatch.setattr(local, "owned_pid", lambda name: next(answers))
        with pytest.raises(RuntimeError, match="身份已变化"):
            local.stop_process("api", grace_seconds=.05, kill_seconds=.1)
        assert child.poll() is None and (tmp_path / "api.pid").exists()
    finally:
        child.kill()
        child.wait(timeout=5)
