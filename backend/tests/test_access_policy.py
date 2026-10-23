"""Synthetic policy matrix. No source, provider, private configuration or live DB."""
import copy
import json

import pytest

from aiopsstudio.access_policy import PolicyError, PolicyService, policy_digest
from aiopsstudio.store import Store


@pytest.fixture
def store(tmp_path):
    instance = Store("sqlite:///" + str(tmp_path / "policies.sqlite"))
    yield instance
    instance.close()


def source_document():
    return {"schema": "source-access/v1", "version": "source-1", "enabled": True,
        "grants": [{"subjects": ["alice"], "sources": ["inventory"], "tools": ["read"],
            "targets": ["svc-a"], "environments": ["prod"], "purposes": ["investigation", "tool_test"],
            "secret_ids": ["source-ref"], "destinations": [{"url": "https://source.invalid/mcp"}],
            "argument_scope": {"mode": "exact", "fields": {"/target": "$target", "/limit": {"enum": [1, 10]}}}}], "denies": []}


def egress_document():
    return {"schema": "model-egress/v1", "version": "egress-1", "enabled": True,
        "model_bindings": [{"subjects": ["alice", "bob"], "models": ["model-1"], "secret_ids": ["provider-ref"],
                            "destinations": [{"url": "https://provider.invalid/v1"}]}],
        "types": {"context": {"fields": {"/description": "text", "/target": "alias", "/latency_ms": "allow", "/drop_me": "drop"}, "max_bytes": 4096},
                  "user_message": {"fields": {"/content": "text"}, "max_bytes": 4096},
                  "tool_definition": {"fields": {"/name": "allow", "/description": "text", "/params/*": "allow"}, "max_bytes": 4096}},
        "denied_types": []}


@pytest.fixture
def policy(store):
    store.save("access_policy", "main", source_document())
    store.save("egress_policy", "main", egress_document())
    return PolicyService(store)


def source_request(**changes):
    return {"subject_id": "alice", "source_id": "inventory", "tool_name": "read", "target": "svc-a", "environment": "prod",
            "purpose": "investigation", "arguments": {"target": "svc-a", "limit": 10},
            "config": {"url": "https://source.invalid/mcp/", "secret_id": "source-ref"}, **changes}


def model_config(**changes):
    return {"id": "model-1", "base_url": "https://provider.invalid/v1", "secret_id": "provider-ref", **changes}


def egress_kwargs(policy, **changes):
    return {"subject_id": "alice", "scope_id": "conversation-1", "model_config": model_config(),
            "snapshot": policy.snapshot(), "credentials": ("synthetic-provider-credential",), **changes}


def test_missing_read_failure_and_invalid_policy_never_grant(store, monkeypatch):
    service = PolicyService(store)
    assert service.snapshot() == {"schema": "policy-snapshot/v1", "source": None, "egress": None}
    with pytest.raises(PolicyError, match="未配置"):
        service.authorize_source(**source_request())
    store.save("access_policy", "main", {**source_document(), "version": None})
    with pytest.raises(PolicyError) as error:
        service.snapshot()
    assert error.value.code == "policy_invalid_version"
    def broken(*args):
        raise RuntimeError("private source endpoint must never be returned")
    monkeypatch.setattr(store, "get", broken)
    with pytest.raises(PolicyError) as error:
        service.authorize_source(**source_request())
    assert error.value.code == "policy_unavailable" and "private" not in str(error.value)


def test_two_subjects_and_role_have_no_implicit_permission(policy):
    receipt = policy.authorize_source(**source_request(), snapshot=policy.snapshot())
    assert receipt["target"] == "svc-a" and receipt["policy_versions"] == ["source-1", "source-1"]
    for changes in ({"subject_id": "bob"}, {"subject_id": "admin"}, {"target": "svc-b"}, {"environment": "test"},
                    {"source_id": "other"}, {"tool_name": "write"}, {"purpose": "export"}):
        with pytest.raises(PolicyError):
            policy.authorize_source(**source_request(**changes))


def test_target_authorization_checks_every_argument_and_never_rewrites_queries(policy):
    for arguments in ({"target": "svc-b", "limit": 10}, {"target": "svc-a", "limit": 100},
                      {"target": "svc-a"}, {"target": "svc-a", "limit": 10, "query": "select all"},
                      {"target": "svc-a", "limit": 10, "destination": "https://unapproved.invalid"}):
        before = copy.deepcopy(arguments)
        with pytest.raises(PolicyError):
            policy.authorize_source(**source_request(arguments=arguments))
        assert arguments == before


def test_unconstrained_tool_requires_explicit_full_source_grant(policy, store):
    doc = source_document()
    doc["grants"][0]["argument_scope"] = {"mode": "source"}
    store.save("access_policy", "main", doc)
    with pytest.raises(PolicyError):
        policy.authorize_source(**source_request(arguments={"query": "anything"}))
    doc["grants"][0]["targets"] = ["*"]
    store.save("access_policy", "main", doc)
    assert policy.authorize_source(**source_request(arguments={"query": "anything"}))["source_id"] == "inventory"


def test_latest_revoke_denies_queued_running_results_and_history(policy, store):
    frozen = policy.snapshot()
    scope = policy.authorize_source(**source_request(), snapshot=frozen)
    record = {"owner_id": "alice", "access_scopes": [scope]}
    assert policy.authorize_material(record, subject_id="alice")
    doc = source_document()
    doc.update(version="source-2", denies=[{"subjects": ["alice"], "sources": ["inventory"]}])
    store.save("access_policy", "main", doc)
    for check in (lambda: policy.authorize_source(**source_request(), snapshot=frozen),
                  lambda: policy.authorize_material(record, subject_id="alice")):
        with pytest.raises(PolicyError) as error:
            check()
        assert error.value.code == "policy_source_denied"


def test_new_grant_cannot_expand_frozen_attempt(policy, store):
    frozen = policy.snapshot()
    doc = source_document()
    doc["grants"][0]["targets"].append("svc-b")
    doc["version"] = "source-2"
    store.save("access_policy", "main", doc)
    request = source_request(target="svc-b", arguments={"target": "svc-b", "limit": 1})
    assert policy.authorize_source(**request)
    with pytest.raises(PolicyError):
        policy.authorize_source(**request, snapshot=frozen)


@pytest.mark.parametrize("config", [
    {"url": "https://source.invalid/else", "secret_id": "source-ref"},
    {"url": "https://other.invalid/mcp", "secret_id": "source-ref"},
    {"url": "https://source.invalid/mcp?auth=private", "secret_id": "source-ref"},
    {"url": "https://source.invalid:bad/mcp", "secret_id": "source-ref"},
    {"url": "https://source.invalid/mcp", "secret_id": "another-ref"},
    {"url": "https://source.invalid/mcp", "secret_id": "source-ref", "password_secret_id": "extra-ref"},
    {"url": "https://source.invalid/mcp", "secret_id": "source-ref", "verify_tls": False},
    {"url": "https://source.invalid/mcp", "secret_id": "source-ref", "follow_redirects": True},
    {"url": "https://source.invalid/mcp", "secret_id": "source-ref", "headers": {"Host": "unapproved.invalid"}},
])
def test_destination_tls_redirect_and_exact_credential_binding(policy, config):
    with pytest.raises(PolicyError):
        policy.authorize_source(**source_request(config=config))


def test_http_requires_explicit_loopback_binding_and_no_insecure_remote(policy, store):
    doc = source_document()
    grant = doc["grants"][0]
    grant["destinations"] = [{"url": "http://127.0.0.1:12345/mcp"}]
    store.save("access_policy", "main", doc)
    request = source_request(config={"url": "http://127.0.0.1:12345/mcp", "secret_id": "source-ref"})
    with pytest.raises(PolicyError):
        policy.authorize_source(**request)
    grant["destinations"][0]["allow_http_loopback"] = True
    store.save("access_policy", "main", doc)
    assert policy.authorize_source(**request)
    grant["destinations"][0]["url"] = "http://source.invalid/mcp"
    store.save("access_policy", "main", doc)
    with pytest.raises(PolicyError):
        policy.authorize_source(**source_request(config={"url": "http://source.invalid/mcp", "secret_id": "source-ref"}))


def test_history_admin_private_and_unknown_origin_rejected(policy):
    scope = policy.authorize_source(**source_request())
    for record, reader in (({"owner_id": "alice", "access_scopes": [scope]}, "admin"),
                           ({"owner_id": "alice"}, "alice"), ({"owner_id": "alice", "access_scopes": []}, "alice"),
                           ({"owner_id": "alice", "access_scopes": [{}]}, "alice")):
        with pytest.raises(PolicyError):
            policy.authorize_material(record, subject_id=reader)


def test_aliases_consistent_across_rounds_isolated_across_users_and_conversations(policy):
    value = {"description": "svc-a latency is 23, svc-a10 is a different target", "target": "svc-a", "latency_ms": 23, "drop_me": "omitted"}
    kwargs = egress_kwargs(policy)
    first = policy.egress_view("context", value, **kwargs)
    same = policy.egress_view("context", value, **kwargs)
    assert same == first and first["target"].startswith("resource_")
    assert first["target"] in first["description"] and "svc-a10" in first["description"]
    assert first["latency_ms"] == 23 and "drop_me" not in first
    assert "svc-a\"" not in json.dumps(first) and "entries" not in json.dumps(first)
    for changes in ({"subject_id": "bob"}, {"scope_id": "conversation-2"}):
        other = policy.egress_view("context", value, **{**kwargs, **changes})
        assert other["target"] != first["target"]
    assert len(policy.store.list("egress_alias")) == 3
    assert "entries" not in json.dumps(kwargs["snapshot"])


def test_alias_resolution_never_guesses_other_scopes_or_rewrites_source_queries(policy):
    kwargs = egress_kwargs(policy)
    view = policy.egress_view("context", {"target": "svc-a", "description": "svc-a", "latency_ms": 23}, **kwargs)
    alias = view["target"]
    assert policy.resolve_aliases({"target": alias}, subject_id="alice", scope_id="conversation-1") == {"target": "svc-a"}
    assert policy.resolve_aliases("Report " + alias, subject_id="alice", scope_id="conversation-1", text=True) == "Report svc-a"
    for args in (({"target": alias}, "bob", "conversation-1"), ({"target": alias}, "alice", "conversation-2"),
                 ({"query": "select " + alias}, "alice", "conversation-1")):
        with pytest.raises(PolicyError):
            policy.resolve_aliases(args[0], subject_id=args[1], scope_id=args[2])


@pytest.mark.parametrize("kind,value", [("log", {"content": "raw log"}), ("attachment", {"content": "raw material"}),
    ("source_error", {"content": "internal"}), ("user_message", {"content": "safe", "unregistered": "secret-marker"}),
    ("user_message", {"content": "synthetic-provider-credential"}), ("user_message", {"content": "Bearer fake-marker"}),
    ("user_message", {"content": "safe", "authorization": "anything"})])
def test_unknown_type_field_credentials_and_source_errors_fail_closed(policy, kind, value):
    with pytest.raises(PolicyError):
        policy.egress_view(kind, value, **egress_kwargs(policy))


def test_credential_fields_cannot_be_allowed_by_mistaken_policy(policy, store):
    doc = egress_document()
    doc["types"]["context"]["fields"]["/api_key"] = "allow"
    store.save("egress_policy", "main", doc)
    with pytest.raises(PolicyError) as error:
        policy.egress_view("context", {"api_key": "secret-marker"}, **egress_kwargs(policy))
    assert error.value.code == "policy_egress_credential_field"


def test_egress_latest_deny_and_model_binding_apply_to_test_and_retry(policy, store):
    frozen = policy.snapshot()
    kwargs = egress_kwargs(policy, snapshot=frozen)
    assert policy.egress_view("user_message", {"content": "allowed sample"}, **kwargs)
    for config in (model_config(id="another-model"), model_config(secret_id="rotated-ref"), model_config(base_url="https://other.invalid/v1")):
        with pytest.raises(PolicyError):
            policy.egress_view("user_message", {"content": "safe"}, **{**kwargs, "model_config": config})
    doc = egress_document()
    doc.update(version="egress-2", denied_types=["user_message"])
    store.save("egress_policy", "main", doc)
    with pytest.raises(PolicyError):
        policy.egress_view("user_message", {"content": "previously allowed"}, **kwargs)
    doc["denied_types"] = []
    doc["model_bindings"] = []
    store.save("egress_policy", "main", doc)
    with pytest.raises(PolicyError):
        policy.authorize_model(model_config(), subject_id="alice", snapshot=frozen)


def test_array_wildcard_cannot_whitelist_unknown_object_keys(policy):
    kwargs = egress_kwargs(policy)
    assert policy.egress_view("tool_definition", {"name": "read", "description": "safe", "params": ["target"]}, **kwargs)
    with pytest.raises(PolicyError):
        policy.egress_view("tool_definition", {"name": "read", "description": "safe", "params": {"hidden": "raw"}}, **kwargs)


def test_snapshot_is_independent_and_digest_stable(policy):
    snapshot = policy.snapshot()
    digest = policy_digest(snapshot)
    assert digest == policy_digest(copy.deepcopy(snapshot))
    snapshot["source"]["grants"] = []
    assert policy.snapshot()["source"]["grants"]
    assert digest != policy_digest(snapshot)


def test_admin_validation_rejects_bad_rules_but_can_store_disabled_policy(policy):
    assert policy.validate_document("access_policy", source_document())["schema"] == "source-access/v1"
    assert policy.validate_document("egress_policy", egress_document())["schema"] == "model-egress/v1"
    doc = {**source_document(), "enabled": False}
    assert policy.validate_document("access_policy", doc)["enabled"] is False
    invalid = [
        ("access_policy", {**source_document(), "denies": [{"secret_ids": ["unrecognized-selector"]}]}),
        ("access_policy", {**source_document(), "grants": [{**source_document()["grants"][0], "argument_scope": {"mode": "source"}}]}),
        ("egress_policy", {**egress_document(), "types": {"raw": {"fields": {"/x": {"unknown": "action"}}, "max_bytes": 10}}}),
        ("egress_policy", {**egress_document(), "types": {"raw": {"fields": {"/x~2": "allow"}, "max_bytes": 10}}}),
    ]
    for kind, candidate in invalid:
        with pytest.raises(PolicyError):
            policy.validate_document(kind, candidate)


def test_latest_field_tightening_drops_and_snapshot_never_expands(policy, store):
    snapshot = policy.snapshot()
    doc = egress_document()
    doc["types"]["context"]["fields"]["/latency_ms"] = "drop"
    doc["types"]["context"]["fields"]["/new_field"] = "allow"
    store.save("egress_policy", "main", doc)
    kwargs = egress_kwargs(policy, snapshot=snapshot)
    result = policy.egress_view("context", {"target": "svc-a", "description": "svc-a", "latency_ms": 23}, **kwargs)
    assert "latency_ms" not in result
    with pytest.raises(PolicyError):
        policy.egress_view("context", {"new_field": "newly allowed but not frozen"}, **kwargs)


def test_source_arguments_remain_exact_after_alias_view_and_restore(policy):
    kwargs = egress_kwargs(policy)
    view = policy.egress_view("context", {"target": "svc-a", "description": "svc-a", "latency_ms": 23}, **kwargs)
    original = {"target": view["target"], "limit": 10}
    resolved = policy.resolve_aliases(original, subject_id="alice", scope_id="conversation-1")
    assert resolved == {"target": "svc-a", "limit": 10} and original["target"] == view["target"]
    assert policy.authorize_source(**source_request(arguments=resolved))
