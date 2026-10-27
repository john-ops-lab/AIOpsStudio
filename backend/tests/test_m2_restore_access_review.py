"""Independent HTTP quarantine oracles over synthetic restored workspace data."""
import copy
import hashlib
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import pytest

from aiopsstudio.attachments import AttachmentsService
from aiopsstudio.backup import BackupError, create_backup, restore_backup
from aiopsstudio.config import ConfigService, Vault
from aiopsstudio.legacy_archive import archived, plan_archive, register_archive
from aiopsstudio.store import Store
from aiopsstudio.usage import configure_usage
from test_legacy_archive import archive_app, register
from test_trust_api import source_document


def _workspace(store):
    return [tuple(row) for row in store._execute(
        f"SELECT kind,id,data FROM {store._documents} ORDER BY kind,id")]


def _registered_routes(routes):
    for route in routes:
        original = getattr(route, "original_router", None)
        if original is not None:
            yield from _registered_routes(original.routes)
        else:
            yield route


def test_restore_quarantine_blocks_every_workspace_surface_before_dispatch(archive_app, monkeypatch):
    app, store, owner, admin, uid, conv, run, report, upload, path = archive_app
    configure_usage(app)
    config = app.state.config
    model = config.save("model", {"name": "隔离验证模型", "model": "synthetic", "secret_value": "synthetic-never-dispatched"})
    server = config.save("mcp", {"name": "隔离验证目录", "url": "https://source.invalid/mcp", "auth_type": "none"})
    skill = config.save("skill", {"name": "quarantine-fixture", "content": "---\nname: quarantine-fixture\ndescription: 合成隔离验证\n---\n合成方法"})
    policy = source_document()
    grant = policy["grants"][0]
    grant.update(subjects=["admin", uid], sources=[server["id"]], tools=["$catalog"], targets=["*"],
                 environments=["*"], purposes=["discovery"], secret_ids=[], argument_scope={"mode": "source"})
    browse = copy.deepcopy(grant)
    browse.update(sources=["ongrid"], tools=["alert_list", "alert_detail"], purposes=["browse"],
                  destinations=[{"url": "https://alerts.invalid"}])
    policy["grants"].append(browse)
    store.save("access_policy", "main", policy)
    settings = store.get("settings", "main")
    settings.update(ongrid={"url": "https://alerts.invalid", "auth_type": "none"})
    store.save("settings", "main", settings)

    dispatches = []

    async def catalog(config, vault, request_observer=None):
        request_observer()
        dispatches.append("catalog")
        return {"complete": True, "items": [], "protocol_version": "synthetic"}

    async def alerts(settings, vault, *args, request_observer=None):
        request_observer()
        dispatches.append("alerts")
        return {"items": [], "complete": True}

    async def unexpected(*args, **kwargs):
        dispatches.append("unexpected")
        raise AssertionError("quarantined operation reached a dispatcher")

    monkeypatch.setattr("aiopsstudio.connectors.mcp_discover", catalog)
    monkeypatch.setattr("aiopsstudio.connectors.alert_list", alerts)
    monkeypatch.setattr("aiopsstudio.connectors.alert_detail", alerts)
    monkeypatch.setattr("aiopsstudio.connectors.mcp_call", unexpected)
    # Prove these direct source surfaces are reachable before quarantine.
    assert admin.post(f"/api/mcp-servers/{server['id']}/probe", json={}).status_code == 200
    assert admin.post(f"/api/mcp-servers/{server['id']}/discover", json={}).status_code == 200
    assert owner.get("/api/alerts").status_code == 200
    assert owner.get("/api/alerts/synthetic-alert").status_code == 200
    assert dispatches == ["catalog", "catalog", "alerts", "alerts"]
    dispatches.clear()
    register(archive_app)
    assert owner.get("/api/conversations").status_code == 200
    assert owner.get(f"/api/attachments/{upload}/download").content == b"original upload"
    assert owner.get(f"/api/attachments/{report}/download").content == b"original report bytes"

    store.save("restore_guard", "main", {"verified": False, "backup_signature": "private-signature-not-for-http"})
    before, original_bytes = _workspace(store), path.read_bytes()
    admin_routes = [
        ("GET", "/api/settings"), ("PUT", "/api/settings"),
        ("GET", "/api/overview"), ("GET", "/api/config-audit"),
        ("GET", "/api/users"), ("POST", "/api/users"),
        ("GET", "/api/secrets"), ("POST", "/api/secrets/synthetic/revoke"),
        ("GET", "/api/access-policies/source"), ("PUT", "/api/access-policies/source"),
        ("GET", "/api/access-policies/egress"), ("PUT", "/api/access-policies/egress"),
        ("GET", "/api/query-bindings/builtins"), ("POST", "/api/tool-tests"),
        ("GET", "/api/quality/pipeline"), ("POST", "/api/quality/pipeline"),
        ("POST", f"/api/mcp-servers/{server['id']}/probe"),
        ("POST", f"/api/mcp-servers/{server['id']}/discover"),
        ("GET", f"/api/mcp-servers/{server['id']}/tools"),
        ("POST", f"/api/model-configs/{model['id']}/test"),
    ]
    for prefix, identifier in (("skills", skill["id"]), ("mcp-servers", server["id"]), ("model-configs", model["id"])):
        admin_routes.extend((method, f"/api/{prefix}{suffix}") for method, suffix in [
            ("GET", ""), ("POST", ""), ("GET", f"/{identifier}"), ("PUT", f"/{identifier}"),
            ("GET", f"/{identifier}/versions"), ("POST", f"/{identifier}/publish"),
            ("POST", f"/{identifier}/validate"), ("GET", f"/{identifier}/quality"), ("POST", f"/{identifier}/quality"),
        ])
    owner_routes = [
        ("GET", "/api/alerts"), ("GET", "/api/alerts/synthetic-alert"),
        ("GET", "/api/conversations"), ("POST", "/api/conversations"),
        ("GET", f"/api/conversations/{conv}"), ("PUT", f"/api/conversations/{conv}"),
        ("GET", f"/api/conversations/{conv}/export"),
        ("POST", f"/api/conversations/{conv}/messages"),
        ("POST", f"/api/conversations/{conv}/attachments"),
        ("POST", f"/api/conversations/{conv}/archive"),
        ("POST", f"/api/conversations/{conv}/deletion-preview"),
        ("POST", f"/api/conversations/{conv}/delete"),
        ("GET", "/api/runs"), ("POST", "/api/runs"), ("GET", f"/api/runs/{run}"),
        ("GET", f"/api/runs/{run}/usage"), ("GET", f"/api/runs/{run}/events"),
        ("POST", f"/api/runs/{run}/messages"), ("POST", f"/api/runs/{run}/retry"),
        ("POST", f"/api/runs/{run}/repair-report"),
        ("POST", f"/api/runs/{run}/material-reads"),
        ("GET", "/api/fragments/frag_" + "0" * 32), ("GET", "/api/evidence/synthetic"),
        ("GET", "/api/invocations"), ("GET", "/api/attachments/usage"),
        ("POST", "/api/attachments/orphans/deletion-preview"), ("POST", "/api/attachments/orphans/delete"),
    ]
    for identifier in (upload, report):
        owner_routes.extend((method, f"/api/attachments/{identifier}{suffix}") for method, suffix in [
            ("GET", ""), ("GET", "/download"), ("POST", "/deletion-preview"), ("POST", "/delete"),
        ])
    for client, routes in ((admin, admin_routes), (owner, owner_routes)):
        for method, url in routes:
            # Do not let middleware accidentally give coverage to an absent route.
            assert any(method in getattr(route, "methods", set()) and route.path_regex.fullmatch(urlsplit(url).path)
                       for route in _registered_routes(app.routes)), (method, url)
            response = client.request(method, url, **({"json": {}} if method != "GET" else {}))
            assert response.status_code == 503, (method, url, response.status_code, response.text)
            assert response.json()["code"] == "restore_unverified", (method, url, response.text)
    assert dispatches == []
    assert _workspace(store) == before and path.read_bytes() == original_bytes


def test_restore_quarantine_only_exposes_identity_and_verification_flag(archive_app):
    app, store, owner, admin, *_ = archive_app
    store.save("restore_guard", "main", {"verified": False, "backup_signature": "private-backup-signature", "reason": "private-reason"})
    assert owner.get("/api/session").json()["authenticated"] is True
    assert owner.get("/api/restore-status").json() == {"authorization_verified": False}
    fresh = TestClient(app, headers={"Origin": "http://testserver"})
    try:
        assert fresh.get("/api/restore-status").status_code == 401
        assert fresh.post("/api/login", json={"password": "synthetic-archive-admin-password"}).status_code == 200
        assert fresh.get("/api/restore-status").json() == {"authorization_verified": False}
        assert fresh.post("/api/logout", json={}).status_code == 200
        assert fresh.get("/api/session").json()["authenticated"] is False
    finally:
        fresh.close()


def _artifact_workspace(store, root, key):
    config = ConfigService(store, Vault(store, key), root)
    item = config.save("skill", {"name": "restore-artifact-fixture",
        "content": "---\nname: restore-artifact-fixture\ndescription: synthetic\n---\nSee [reference](references/a.txt).",
        "references": {"references/a.txt": "synthetic reference"}})
    # Actual package creation; no formal publication or model/quality invocation.
    published = config.publish("skill", item["id"], item["revision"], channel="experimental")
    package = Path(published["versions"][0]["snapshot"]["artifact_path"])
    store.save("conversation", "synthetic-conversation", {"owner_id": "synthetic-owner", "messages": []})
    attachments = AttachmentsService(store, root)
    attachment = attachments.upload("synthetic-owner", "synthetic-conversation",
        {"name": "synthetic.txt", "media_type": "text/plain", "content_base64": "Zml4dHVyZQ=="})
    paths = {"attachment": attachments._path(attachment), "skill": package / "references/a.txt"}
    for kind in ("evidence", "material_read"):
        path, payload = root / (kind + ".json"), ("synthetic " + kind).encode()
        path.write_bytes(payload)
        store.save(kind, kind, {"owner_id": "synthetic-owner", "path": str(path),
            "sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload)})
        paths[kind] = path
    return item["id"], attachment, package, paths


def _assert_relocated(target, target_root, source_root, skill_id, attachment, package, paths):
    source_root, target_root = source_root.resolve(), target_root.resolve()
    for kind in ("evidence", "material_read"):
        path = Path(target.get(kind, kind)["path"])
        assert path == target_root / paths[kind].resolve().relative_to(source_root)
        assert path.read_bytes() == paths[kind].read_bytes()
    restored = target.get("skill", skill_id)["versions"][0]["snapshot"]
    assert Path(restored["artifact_path"]) == target_root / package.resolve().relative_to(source_root)
    assert (Path(restored["artifact_path"]) / "references/a.txt").read_text() == "synthetic reference"
    assert AttachmentsService(target, target_root)._path(attachment).read_bytes() == b"fixture"


def test_every_declared_artifact_has_integrity_and_restored_location(tmp_path):
    source, target = Store("sqlite:///" + str(tmp_path / "source.db")), Store("sqlite:///" + str(tmp_path / "target.db"))
    try:
        root, key = (tmp_path / "assets").resolve(), Fernet.generate_key()
        skill, attachment, package, paths = _artifact_workspace(source, root, key)
        original = _workspace(source)
        for kind, path in paths.items():
            payload = path.read_bytes()
            path.unlink()
            missing = tmp_path / ("missing-" + kind)
            with pytest.raises(BackupError, match="reference_missing"):
                create_backup(source, root, missing, master_key=key)
            assert not missing.exists()
            path.write_bytes(b"changed")
            changed = tmp_path / ("changed-" + kind)
            with pytest.raises(BackupError, match="reference_integrity"):
                create_backup(source, root, changed, master_key=key)
            assert not changed.exists()
            path.write_bytes(payload)
        create_backup(source, root, tmp_path / "backup", master_key=key)
        restore_backup(target, tmp_path / "restored", tmp_path / "backup", master_key=key)
        _assert_relocated(target, tmp_path / "restored", root, skill, attachment, package, paths)
        assert _workspace(source) == original
    finally:
        source.close()
        target.close()


def test_declared_artifact_root_aliases_restore_to_target_without_rewriting_text(tmp_path):
    source, target = Store("sqlite:///" + str(tmp_path / "source.db")), Store("sqlite:///" + str(tmp_path / "target.db"))
    try:
        real_root, alias, key = tmp_path / "actual-assets", tmp_path / "alias-assets", Fernet.generate_key()
        real_root.mkdir()
        alias.symlink_to(real_root, target_is_directory=True)
        skill, attachment, package, paths = _artifact_workspace(source, alias, key)
        literal = str(alias / "evidence.json")
        source.save("run", "synthetic-report", {"status": "partial", "report": {"analysis": literal, "literal_paths": [literal]}})
        create_backup(source, alias, tmp_path / "backup", master_key=key)
        restore_backup(target, tmp_path / "restored", tmp_path / "backup", master_key=key)
        _assert_relocated(target, tmp_path / "restored", alias, skill, attachment, package, paths)
        assert target.get("run", "synthetic-report")["report"] == {"analysis": literal, "literal_paths": [literal]}
    finally:
        source.close()
        target.close()


def test_restore_relocation_preserves_registered_legacy_archive_access(tmp_path):
    source, target = Store("sqlite:///" + str(tmp_path / "source.db")), Store("sqlite:///" + str(tmp_path / "target.db"))
    try:
        root, key = (tmp_path / "assets").resolve(), Fernet.generate_key()
        root.mkdir()
        native = root / "evidence.json"
        native.write_bytes(b"synthetic original evidence")
        source.save("settings", "main", {"artifact_dir": str(root)})
        source.save("run", "old-run", {"owner_id": "synthetic-owner", "status": "partial", "attempt_id": "old-attempt",
            "config_snapshot": {"settings": {"artifact_dir": str(root)}},
            "report": {"analysis": "original report"}, "evidence": [{"id": "original-evidence", "path": str(native)}]})
        register_archive(source, plan_archive(source))
        before = _workspace(source)
        assert archived(source, "run", source.get("run", "old-run"), "synthetic-owner")
        create_backup(source, root, tmp_path / "backup", master_key=key)
        restore_backup(target, tmp_path / "restored", tmp_path / "backup", master_key=key)
        record = target.get("run", "old-run")
        assert archived(target, "run", record, "synthetic-owner")
        assert Path(record["evidence"][0]["path"]).read_bytes() == native.read_bytes()
        assert record["report"] == {"analysis": "original report"}
        assert _workspace(source) == before
    finally:
        source.close()
        target.close()
