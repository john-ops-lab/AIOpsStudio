"""Mac-local API contracts, synthetic data only and no worker/source requests."""
import copy
import base64
import hashlib
import json
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from fastapi.testclient import TestClient

from aiopsstudio.access_policy import PolicyError, PolicyService
from aiopsstudio.analysis_context import ContextError
from aiopsstudio.main import create_app
from aiopsstudio.material_reading import MaterialReader, ReadingError
from aiopsstudio.store import Store
from aiopsstudio.trust_api import configure_trust_api
from test_access_policy import source_document, egress_document, source_request


PASSWORD = "synthetic-admin-password-long"
USER_PASSWORD = "synthetic-operator-password-long"
ORIGIN = {"Origin": "http://testserver"}


@pytest.fixture
def api(tmp_path):
    store = Store("sqlite:///" + str(tmp_path / "trust.sqlite"))
    app = create_app(store=store, master_key=Fernet.generate_key(), password=PASSWORD,
        public_origin="http://testserver", artifact_dir=tmp_path / "artifacts", testing=True, enable_runtime=False)
    app.state.policy = PolicyService(store)
    reader = MaterialReader(store, tmp_path / "artifacts", authorize_material=lambda record, owner, run:
        app.state.policy.authorize_material(record, subject_id=owner))
    app.state.runtime = SimpleNamespace(material_read=lambda run_id, owner, body, consumer="user": reader.read(run_id, owner, body, consumer=consumer))
    configure_trust_api(app)
    admin = TestClient(app, headers=ORIGIN)
    assert admin.post("/api/login", json={"password": PASSWORD}).status_code == 200
    user = admin.post("/api/users", json={"username": "operator", "password": USER_PASSWORD}).json()
    operator = TestClient(app, headers=ORIGIN)
    assert operator.post("/api/login", json={"username": "operator", "password": USER_PASSWORD}).status_code == 200
    yield SimpleNamespace(app=app, store=store, admin=admin, operator=operator, user=user, root=tmp_path, reader=reader)
    admin.close()
    operator.close()
    store.close()


def save_source(api, **changes):
    doc = source_document()
    doc["grants"][0]["subjects"] = ["admin"]
    doc.update(changes)
    api.store.save("access_policy", "main", doc)
    return doc


def seed_material(api, *, owner="admin", consumer="user"):
    save_source(api)
    scope = api.app.state.policy.authorize_source(**source_request(subject_id="admin"))
    run_id, attempt_id, evidence_id = "run-fragment", "attempt-fragment", "ev-fragment"
    api.store.save("run", run_id, {"owner_id": owner, "attempt_id": attempt_id, "reading_usage": {}})
    folder = api.root / "artifacts" / "evidence" / run_id
    folder.mkdir(parents=True)
    path = folder / "evidence.json"
    data = {"rows": [{"name": "object-a", "value": 7}, {"name": "object-b", "value": 11}]}
    raw = json.dumps(data).encode()
    path.write_bytes(raw)
    api.store.save("evidence", evidence_id, {"owner_id": owner, "run_id": run_id, "attempt_id": attempt_id,
        "path": str(path), "sha256": hashlib.sha256(raw).hexdigest(), "truncated": False, "access_scopes": [scope]})
    body = {"resource_type": "evidence", "evidence_id": evidence_id, "selector": {"pointer": "/rows", "offset": 0, "limit": 1}}
    return run_id, evidence_id, path, body


def test_policy_admin_crud_cas_immutability_and_operator_denial(api):
    for kind, doc in (("source", source_document()), ("egress", egress_document())):
        url = "/api/access-policies/" + kind
        assert api.admin.get(url).json() == {"configured": False, "revision": 0}
        assert api.operator.get(url).status_code == 403
        assert api.operator.put(url, json={"expected_revision": 0, "policy": doc}).status_code == 403
        response = api.admin.put(url, json={"expected_revision": 0, "policy": doc})
        assert response.status_code == 200, response.text
        first = response.json()
        assert first["configured"] and first["policy"]["version"] == doc["version"]
        assert api.admin.put(url, json={"expected_revision": 0, "policy": doc}).status_code == 409
        # Re-saving identical version is a no-op, not a new snapshot version.
        assert api.admin.put(url, json={"expected_revision": first["revision"], "policy": first["policy"]}).json()["revision"] == first["revision"]
        modified = {**doc, "enabled": False}
        assert api.admin.put(url, json={"expected_revision": first["revision"], "policy": modified}).status_code == 409
        modified["version"] += "-2"
        second = api.admin.put(url, json={**modified, "expected_revision": first["revision"]})
        assert second.status_code == 200, second.text
        # Historic version may be selected unchanged; its contents cannot change.
        changed_old = {**doc, "enabled": False}
        assert api.admin.put(url, json={"policy": changed_old, "expected_revision": second.json()["revision"]}).status_code == 409
    assert len([item for item in api.store.list("audit") if item["action"] == "save_policy"]) == 4


def test_policy_malformed_and_real_known_credentials_do_not_persist(api):
    for body in ({}, [], {"expected_revision": False, "policy": source_document()}, {"expected_revision": 0, "policy": []},
                 {"expected_revision": 0, "policy": {**source_document(), "version": None}}):
        assert api.admin.put("/api/access-policies/source", json=body).status_code == 422
    secret = "synthetic-known-credential-marker"
    api.app.state.vault.put(secret)
    doc = {**source_document(), "description": secret}
    response = api.admin.put("/api/access-policies/source", json={"expected_revision": 0, "policy": doc})
    assert response.status_code == 422 and secret not in response.text
    assert api.store.get("access_policy", "main") is None


def test_material_reads_and_fragment_revalidate_bytes_source_and_budget(api):
    run_id, evidence_id, path, body = seed_material(api)
    url = "/api/runs/" + run_id + "/material-reads"
    first = api.admin.post(url, json=body)
    assert first.status_code == 200, first.text
    selected = first.json()
    assert selected["data"] == [{"name": "object-a", "value": 7}]
    assert str(api.root) not in first.text and str(path) not in first.text
    fragment = api.admin.get("/api/fragments/" + selected["anchor_id"])
    assert fragment.status_code == 200, fragment.text
    assert fragment.json()["anchor_id"] == selected["anchor_id"] and fragment.json()["data"] == selected["data"]
    assert api.store.get("run", run_id)["reading_usage"]["user"]["calls"] == 2
    assert api.operator.get("/api/fragments/" + selected["anchor_id"]).status_code == 404
    assert api.operator.post(url, json=body).status_code == 404
    save_source(api, version="revoke-2", denies=[{"subjects": ["admin"]}])
    response = api.admin.get("/api/fragments/" + selected["anchor_id"])
    assert response.status_code == 404 and "source.invalid" not in response.text
    save_source(api, version="allow-3")
    path.write_text('{"changed":true}')
    assert api.admin.get("/api/fragments/" + selected["anchor_id"]).status_code == 404


def test_unknown_fragment_and_legacy_origin_material_fail_closed(api):
    run_id, evidence_id, path, body = seed_material(api)
    meta = api.store.get("evidence", evidence_id)
    meta.pop("access_scopes")
    api.store.save("evidence", evidence_id, meta)
    assert api.admin.post("/api/runs/" + run_id + "/material-reads", json=body).status_code == 404
    assert api.admin.get("/api/fragments/frag_" + "a" * 32).status_code == 404
    assert api.admin.get("/api/fragments/not-an-anchor").status_code == 404


def test_selector_budget_and_fixed_error_handlers(api):
    run_id, evidence_id, path, body = seed_material(api)
    url = "/api/runs/" + run_id + "/material-reads"
    assert api.admin.post(url, json=[]).status_code == 422
    assert api.admin.post(url, json={**body, "selector": {"pointer": "relative"}}).status_code == 422
    run = api.store.get("run", run_id)
    run["reading_limits"] = {"user": {"calls": 0, "bytes": 100}}
    api.store.save("run", run_id, run)
    assert api.admin.post(url, json=body).status_code == 429
    for error, expected in ((PolicyError("policy_missing", "private endpoint"), 503),
                            (ContextError("test_context", "private target"), 422)):
        def fail(*args, _error=error, **kwargs):
            raise _error
        api.app.state.runtime.material_read = fail
        response = api.admin.post(url, json=body)
        assert response.status_code == expected and "private" not in response.text


def test_context_settings_validates_new_format_and_preserves_legacy(api):
    current = api.store.get("settings", "main")
    response = api.admin.put("/api/settings", json={"expected_revision": current["revision"],
        "identity_mapping": {"version": "identity-1"}, "query_catalog": {}})
    assert response.status_code == 422
    assert api.store.get("settings", "main")["revision"] == current["revision"]
    old_mapping = {"legacy_source": {"alias": "service-a", "arbitrary_legacy_field": "preserve"}}
    response = api.admin.put("/api/settings", json={"expected_revision": current["revision"], "identity_mapping": old_mapping,
        "query_catalog": {"legacy_queries": {"query": "old fixed text"}}, "environment": "synthetic"})
    assert response.status_code == 200, response.text
    saved = api.store.get("settings", "main")
    assert saved["identity_mapping"] == old_mapping and saved["query_catalog"]["legacy_queries"]["query"] == "old fixed text"
    assert saved["environment"] == "synthetic"


def test_catalog_probe_needs_source_wide_admin_grant_and_checks_return_revoke(api, monkeypatch):
    server = api.app.state.config.save("mcp", {"name": "synthetic catalog", "url": "https://source.invalid/mcp", "auth_type": "none"})
    url = "/api/mcp-servers/" + server["id"] + "/discover"
    calls = []
    async def discover(config, vault, request_observer=None):
        request_observer()
        calls.append(config["id"])
        return {"complete": True, "items": [], "protocol_version": "synthetic"}
    monkeypatch.setattr("aiopsstudio.connectors.mcp_discover", discover)
    assert api.admin.post(url, json={}).status_code == 503
    assert calls == []
    doc = source_document()
    grant = doc["grants"][0]
    grant.update(subjects=["admin"], sources=[server["id"]], tools=["$catalog"], targets=["*"], environments=["*"], purposes=["discovery"], secret_ids=[], argument_scope={"mode": "source"})
    api.store.save("access_policy", "main", doc)
    assert api.admin.post(url, json={}).status_code == 200
    assert calls == [server["id"]]
    assert api.operator.post(url, json={}).status_code == 403
    async def revoked(config, vault, request_observer=None):
        request_observer()
        changed = copy.deepcopy(doc)
        changed.update(version="catalog-revoked", denies=[{"subjects": ["admin"]}])
        api.store.save("access_policy", "main", changed)
        return {"complete": True, "items": [{"name": "private tool"}]}
    monkeypatch.setattr("aiopsstudio.connectors.mcp_discover", revoked)
    response = api.admin.post(url, json={})
    assert response.status_code == 403 and "private tool" not in response.text
    assert api.store.get("mcp", server["id"])["candidate_tools"] == []


def seed_history(api, owner="admin", client=None):
    client = client or api.admin
    doc = source_document()
    doc["grants"][0]["subjects"] = ["admin", api.user["id"]]
    api.store.save("access_policy", "main", doc)
    conversation = client.post("/api/conversations", json={"client_request_id": "history-" + owner, "title": "private-source-report"}).json()
    scope = api.app.state.policy.authorize_source(**source_request(subject_id=owner))
    run_id = "history-run-" + owner
    run = api.store.save("run", run_id, {"owner_id": owner, "conversation_id": conversation["id"], "thread_id": conversation["id"],
        "attempt_id": "history-attempt", "status": "partial", "access_scopes": [scope], "messages": [], "events": [],
        "report": {"analysis": "private-source-derived-analysis", "limitations": []}})
    def authorize_history(item, reader):
        if (item.get("owner_id") or "admin") != reader:
            raise HTTPException(404, "不存在")
        try:
            api.app.state.policy.authorize_material(item, subject_id=reader)
        except PolicyError:
            raise HTTPException(404, "不存在或当前不可访问") from None
    api.app.state.runtime.authorize_history = authorize_history
    api.app.state.runtime.public = lambda item: {key: item[key] for key in ("id", "status", "report") if key in item}
    attachment = api.app.state.attachments.save_report(run)
    run["report_attachment"] = attachment
    api.store.save("run", run_id, run)
    conversation["messages"] = [{"id": "assistant-fixture", "role": "assistant", "run_id": run_id,
        "content": "private-source-derived-analysis", "status": "partial", "report_attachment": attachment}]
    api.store.save("conversation", conversation["id"], conversation)
    return conversation, run, attachment


def test_source_revoke_hides_conversation_and_generated_report_all_read_paths(api):
    conversation, run, attachment = seed_history(api)
    cid, aid = conversation["id"], attachment["id"]
    own_upload = api.admin.post("/api/conversations/" + cid + "/attachments", json={"name": "own.txt", "media_type": "text/plain",
        "content_base64": base64.b64encode(b"my own notes").decode()}).json()
    assert api.admin.get("/api/conversations/" + cid).status_code == 200
    assert api.admin.get("/api/attachments/" + aid + "/download").status_code == 200
    assert api.app.state.attachments.reading_context("admin", cid, [aid])[0]["extracted"]
    save_source(api, version="source-revoked", denies=[{"subjects": ["admin"]}])
    for path in ("/api/conversations/" + cid, "/api/attachments/" + aid, "/api/attachments/" + aid + "/download"):
        response = api.admin.get(path)
        assert response.status_code == 404 and "private-source" not in response.text
    listing = api.admin.get("/api/conversations")
    assert listing.status_code == 200 and "private-source" not in listing.text and listing.json()["items"] == []
    assert all(item["id"] != aid for item in api.app.state.attachments.list("admin", cid))
    with pytest.raises(HTTPException) as error:
        api.app.state.attachments.reading_context("admin", cid, [aid])
    assert error.value.status_code == 404
    # Own uploads have no source-derived authorization; still owner/conversation scoped.
    assert api.admin.get("/api/attachments/" + own_upload["id"] + "/download").content == b"my own notes"
    retry = api.admin.post("/api/conversations", json={"client_request_id": "history-admin", "title": "duplicate"})
    assert retry.status_code == 404 and len(api.store.list("conversation")) == 1


def test_admin_role_never_reads_operator_history_or_derived_report(api):
    conversation, run, attachment = seed_history(api, owner=api.user["id"], client=api.operator)
    assert api.operator.get("/api/conversations/" + conversation["id"]).status_code == 200
    assert api.operator.get("/api/attachments/" + attachment["id"] + "/download").status_code == 200
    for path in ("/api/conversations/" + conversation["id"], "/api/attachments/" + attachment["id"], "/api/attachments/" + attachment["id"] + "/download"):
        assert api.admin.get(path).status_code == 404
    assert api.admin.get("/api/conversations").json()["items"] == []


def test_missing_history_authorizer_never_fails_open_and_empty_conversations_work(api):
    conversation, run, attachment = seed_history(api)
    delattr(api.app.state.runtime, "authorize_history")
    assert api.admin.get("/api/conversations/" + conversation["id"]).status_code == 503
    assert api.admin.get("/api/attachments/" + attachment["id"] + "/download").status_code == 503
    assert api.admin.get("/api/conversations").json()["items"] == []
    assert api.admin.post("/api/conversations", json={"client_request_id": "new-empty"}).status_code == 200


def test_download_rejects_changed_saved_report(api):
    conversation, run, attachment = seed_history(api)
    internal = api.store.get("attachment", attachment["id"])
    api.app.state.attachments._path(internal).write_bytes(b"tampered report")
    response = api.admin.get("/api/attachments/" + attachment["id"] + "/download")
    assert response.status_code == 410 and "tampered" not in response.text


def test_operator_configuration_selection_never_exposes_unrelated_connections_or_material(api):
    config = api.app.state.config
    ca_path = api.root / "synthetic-private-ca.pem"
    ca_path.write_text("synthetic certificate placeholder; no TLS request is made")
    source = config.save("mcp", {"name": "UNRELATED_PRIVATE_SOURCE_SERVICE", "url": "https://unrelated-source.invalid/mcp",
        "ca_file": str(ca_path), "auth_type": "bearer", "secret_value": "synthetic-source-secret-marker"})
    # A fixed synthetic discovery receipt exercises metadata publication only;
    # no request or real source acceptance is implied by this fixture.
    source = api.store.save("mcp", source["id"], {**source, "discovery": {"complete": True, "synthetic": True}})
    source = config.publish("mcp", source["id"], source["revision"])
    model = config.save("model", {"name": "合成可选模型", "model": "selection-model", "base_url": "https://unrelated-provider.invalid/v1",
        "secret_value": "synthetic-provider-selection-secret"})
    model = config.publish("model", model["id"], model["revision"])
    skill = config.save("skill", {"name": "operator-selection", "display_name": "合成可选方法",
        "content": "---\nname: operator-selection\ndescription: 合成方法\n---\n固定方法正文。",
        "references": {"references/private.md": "UNRELATED_PRIVATE_SKILL_REFERENCE"}})
    skill = config.publish("skill", skill["id"], skill["revision"])
    settings = api.store.get("settings", "main")
    settings.update(identity_mapping={"unrelated-source": {"native_id": "UNRELATED_NATIVE_ID_MARKER"}},
        query_catalog={"private-query": "UNRELATED_QUERY_BODY_MARKER"},
        ongrid={"url": "https://unrelated-alerts.invalid", "ca_file": "synthetic-alert-ca.pem", "secret_id": source["secret_id"]})
    api.store.save("settings", "main", settings)
    response = api.operator.get("/api/settings")
    assert response.status_code == 200
    for value in ("UNRELATED_NATIVE_ID_MARKER", "UNRELATED_QUERY_BODY_MARKER", "unrelated-alerts.invalid", "synthetic-alert-ca.pem", source["secret_id"]):
        assert value not in response.text
    source_list = api.operator.get("/api/mcp-servers")
    assert source_list.status_code == 200 and source_list.json()["items"] == []
    assert api.operator.get("/api/mcp-servers/" + source["id"] + "/tools").status_code == 403
    for path, expected_id in (("/api/model-configs", model["id"]), ("/api/skills", skill["id"])):
        response = api.operator.get(path)
        assert response.status_code == 200 and any(item["id"] == expected_id for item in response.json()["items"])
        for item in response.json()["items"]:
            assert not {"content", "references", "secret_id", "base_url", "versions", "default_headers", "extra_body"}.intersection(item)
        assert "UNRELATED_PRIVATE_SKILL_REFERENCE" not in response.text and "unrelated-provider.invalid" not in response.text
    # Direct URLs cannot recover the omitted configuration body either.
    for path in ("/api/mcp-servers/" + source["id"], "/api/model-configs/" + model["id"], "/api/skills/" + skill["id"]):
        response = api.operator.get(path)
        assert response.status_code in {200, 403, 404}
        for value in ("UNRELATED_PRIVATE_SKILL_REFERENCE", "synthetic-private-ca.pem", "unrelated-provider.invalid", source["secret_id"], model["secret_id"]):
            assert value not in response.text
    # Explicit catalog/discovery permission reveals selection metadata only.
    doc = source_document()
    doc["grants"] = [{"subjects": [api.user["id"]], "sources": [source["id"]], "tools": ["$catalog"], "targets": ["*"],
        "environments": ["*"], "purposes": ["discovery"], "secret_ids": [source["secret_id"]],
        "destinations": [{"url": source["url"]}], "argument_scope": {"mode": "source"}}]
    api.store.save("access_policy", "main", doc)
    visible = api.operator.get("/api/mcp-servers")
    assert visible.status_code == 200
    selected = next(item for item in visible.json()["items"] if item["id"] == source["id"])
    assert set(selected) <= {"id", "name", "current_version", "enabled", "blocked"}
    assert api.operator.get("/api/mcp-servers/" + source["id"] + "/tools").status_code == 403
    assert api.operator.get("/api/mcp-servers/" + source["id"] + "/versions").status_code == 403


def test_operator_overview_filters_revoked_runs_and_private_source_services(api):
    conversation, run, attachment = seed_history(api, owner=api.user["id"], client=api.operator)
    source = api.app.state.config.save("mcp", {"name": "UNRELATED_PRIVATE_SOURCE_SERVICE", "url": "https://unrelated-source.invalid/mcp"})
    source_policy = api.store.get("access_policy", "main")
    source_policy.update(version="operator-source-revoked", denies=[{"subjects": [api.user["id"]]}])
    api.store.save("access_policy", "main", source_policy)
    response = api.operator.get("/api/overview")
    assert response.status_code == 200
    body = response.json()
    assert body["recent_runs"] == [] and body["counts"]["runs"] == 0
    for value in ("private-source-derived-analysis", run["id"], "UNRELATED_PRIVATE_SOURCE_SERVICE", source["id"]):
        assert value not in response.text
