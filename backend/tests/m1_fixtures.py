"""Reviewed synthetic contracts for legacy unit tests; never a runtime bypass.

The base field list is checked in as evaluation/m1/frozen.json. Additions below
describe only the old tests' fixed schemas, never fields observed from a live
request. Real HTTP/deny/revocation acceptance uses independent integration tests.
"""
import copy
import json
from pathlib import Path


def analysis_defaults():
    return {"target": "fixture-target", "environment": "synthetic", "analysis_start": "2026-10-22T01:00:00Z", "analysis_end": "2026-10-22T02:00:00Z"}


def seed_policies(store, *, model, server, subjects=("admin", "user-a", "user-b")):
    frozen = json.loads((Path(__file__).resolve().parents[2] / "evaluation/m1/frozen.json").read_text())
    source = {"schema": "source-access/v1", "version": "unit-source/1", "enabled": True, "denies": [], "grants": [{
        "subjects": list(subjects), "sources": [server["id"]], "tools": ["read_fixture", "cmdb_list_models", "$catalog"],
        "targets": ["*"], "environments": ["*"], "purposes": ["investigation", "skill_test", "tool_test", "discovery"],
        "secret_ids": [server["secret_id"]] if server.get("secret_id") else [],
        "destinations": [{"url": server["url"], "allow_http_loopback": True}], "argument_scope": {"mode": "source"}}]}
    egress = copy.deepcopy(frozen["policy_documents"]["egress_policy"])
    egress["version"] = "unit-egress/1"
    egress["model_bindings"] = [{"subjects": list(subjects), "models": [model["id"]], "secret_ids": [model["secret_id"]],
        "destinations": [{"url": model.get("base_url", "https://api.openai.com/v1"), "allow_http_loopback": True}]}]
    egress["types"]["context"]["fields"].update({"/windows/fault/reason": "allow"})
    # Fixed CMDB envelope used by the old bounded-graph/source-scope unit tests.
    egress["types"]["source_evidence"]["fields"].update({
        # The fixed integer 1 is explicitly allowed for this loop-budget fixture;
        # this test does not claim identifier alias/privacy acceptance.
        "/evidence/*/data/items/*/ci_id": "allow", "/evidence/*/data/items/*/model": "allow",
        "/evidence/*/data/items/*/attributes/name": "allow", "/evidence/*/verification/unknowns/attribute_values": "allow",
        "/evidence/*/data/models/*/name": "allow", "/evidence/*/data/models/*/count": "allow",
        "/evidence/*/verification/check_count": "allow", "/evidence/*/verification/failed_checks/*/name": "allow",
        "/evidence/*/verification/failed_checks/*/passed": "allow", "/evidence/*/source_time/system": "allow",
        "/evidence/*/source_time/read_finished_at": "allow",
    })
    store.save("access_policy", "main", source)
    store.save("egress_policy", "main", egress)


def seed_context_settings(store, tool, *, model_id="model_fixture", server_id="server_fixture"):
    from aiopsstudio.connectors import tool_digest
    settings = store.get("settings", "main") or {}
    settings.update(default_model_id=model_id, min_free_bytes=1, environment="synthetic",
        identity_mapping={"version": "unit-identities/1", "records": [{
            "source_id": server_id, "object_type": "service", "native_id": "fixture-target", "aliases": [],
            "canonical": {"environment": "synthetic", "cluster": "", "namespace": "", "type": "service", "id": "fixture-target"},
            "mapping_source": "explicit-synthetic-fixture", "verified_at": "2026-10-01T00:00:00Z", "valid_from": "2026-10-01T00:00:00Z", "valid_to": None}]},
        query_catalog={"version": "unit-queries/1", "source_offsets": {server_id: {"seconds": 0, "verified_at": "2026-10-01T00:00:00Z"}},
            "entries": [{"version": "read-fixture/1", "source_id": server_id, "tool_name": tool["name"], "definition_digest": tool_digest(tool),
                "kind": "generic", "object_scope": "none", "window": "fault", "static_arguments": {}, "allowed_parameters": [], "historical": True}]})
    store.save("settings", "main", settings)
