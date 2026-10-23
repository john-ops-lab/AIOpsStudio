"""Independent M1 review reproducers; synthetic wire/material only, no live calls."""
import json
from pathlib import Path

from langchain_core.messages import HumanMessage
import pytest

from aiopsstudio.access_policy import PolicyError
from aiopsstudio.analysis_context import ContextError, coverage_record
from aiopsstudio.grounded_reporting import ground_report, _anchor_valid
from aiopsstudio import connectors
from aiopsstudio.material_reading import ReadingError
from test_analysis_context import bound, catalog_entry, frozen, settings
from test_grounded_reporting import report, record as report_record, native as report_native, run as report_run
from test_runtime_trust import attempt, close, trusted, wire
from test_material_reading import material


@pytest.mark.asyncio
async def test_prepared_human_context_cannot_cross_a_later_egress_type_revocation(trusted):
    rt, case = trusted
    run, sent = await attempt(rt, case), []
    model = wire(rt, run, sent)
    projected = rt._investigation_context(run)
    prepared = rt._approved_text(run, json.dumps(projected, ensure_ascii=False))
    current = rt.store.get("egress_policy", "main")
    current.update(version="review-context-revoked")
    current["denied_types"] = list(set(current.get("denied_types", []) + ["context"]))
    rt.store.save("egress_policy", "main", current)
    try:
        with pytest.raises(PolicyError):
            await rt._invoke_model(run["id"], lambda: model.ainvoke([HumanMessage(content=prepared)]))
        assert sent == []
    finally:
        await close(rt, run)


@pytest.mark.asyncio
async def test_unregistered_extra_body_material_never_reaches_real_sdk_transport(trusted):
    rt, case = trusted
    run, sent = await attempt(rt, case), []
    rt._update(run["id"], callback=lambda value: value["config_snapshot"]["model"].update(extra_body={"user": "SYNTHETIC_UNTYPED_EXTRA_MATERIAL"}))
    run = rt.store.get("run", run["id"])
    model = wire(rt, run, sent)
    prepared = rt._approved_text(run, "已登记的固定合成输入")
    try:
        with pytest.raises(PolicyError):
            await rt._invoke_model(run["id"], lambda: model.ainvoke([HumanMessage(content=prepared)]))
        assert sent == []
    finally:
        await close(rt, run)


@pytest.mark.asyncio
async def test_locally_revoked_secret_remains_forbidden_in_model_material(trusted):
    rt, case = trusted
    run, sent = await attempt(rt, case), []
    secret = "SYNTHETIC_REVOKED_PRIVATE_CREDENTIAL"
    reference = rt.vault.put(secret)
    rt.vault.revoke(reference)
    model = wire(rt, run, sent)
    try:
        with pytest.raises(PolicyError):
            material = rt._typed(run, "user_message", {"id": "synthetic-review-message", "content": "用户材料中误粘贴凭据：" + secret})
            prepared = rt._approved_text(run, material["content"])
            await rt._invoke_model(run["id"], lambda: model.ainvoke([HumanMessage(content=prepared)]))
        assert sent == []
    finally:
        await close(rt, run)


def test_matching_number_does_not_certify_unchecked_model_wording():
    sentence = "已确认另一个对象发生数据丢失，根因是内存泄漏"
    result = report(claims=[{"kind": "observation", "evidence_id": "ev-fixture", "pointer": "/data/result/0/values", "rule": "max", "value": 90, "unit": "ms", "text": sentence}])
    claim = result["claims"][0]
    assert claim.get("model_text") == sentence
    assert claim.get("wording_status", "").startswith("unverified")
    assert sentence not in claim.get("text", "")


def test_topology_without_temporal_binding_does_not_assert_a_fault_time_relation():
    config = settings(entries=[catalog_entry(kind="topology", historical=True, time_parameters={})])
    context = frozen(config)
    try:
        selected = bound(context)
    except ContextError:
        return  # A pre-dispatch refusal is an acceptable conservative boundary.
    binding = selected["binding"]
    assert "start" not in selected["arguments"] and "end" not in selected["arguments"]
    relation = {"source": "service-1", "target": "database-1", "direction": "depends_on"}
    meta = {"id": "ev-review-topology", "source": "metrics", "status": "saved", "truncated": False, "sha256": "a" * 64,
            "coverage": coverage_record(binding, status="complete", item_count=1, historical=True)}
    result = ground_report({"id": "run-review", "attempt_id": "attempt-review", "analysis_context": context},
        {"analysis": "", "claims": [{"kind": "potential_impact", "evidence_id": meta["id"], "pointer": "/relations/0", "rule": "relation", "value": relation}]},
        [(meta, {"relations": [relation]})])
    assert not any(item["status"] == "potential_only" for item in result["potential_impacts"])


@pytest.mark.parametrize("shape", ["prometheus_label", "source_subject"])
def test_query_target_is_not_automatically_the_subject_of_every_returned_fact(shape):
    context = report_run()
    context["analysis_context"]["identity"].update(canonical={"environment": "prod", "cluster": "cluster-a", "namespace": "payments", "type": "service", "id": "expected-service"},
        mappings=[{"source_id": "source-a", "object_type": "service", "native_id": "expected-native-service", "canonical": {"environment": "prod", "cluster": "cluster-a", "namespace": "payments", "type": "service", "id": "expected-service"}}])
    if shape == "prometheus_label":
        native = report_native()
        native["data"]["result"][0]["metric"]["service"] = "unrelated-service"
        claim = {"kind": "observation", "evidence_id": "ev-fixture", "pointer": "/data/result/0/values", "rule": "max", "value": 90, "unit": "ms"}
    else:
        native = {"data": {"identity": {"environment": "other-environment", "native_id": "unrelated-service"}, "facts": [{"subject": "unrelated-service", "value": 90}]}}
        claim = {"kind": "observation", "evidence_id": "ev-fixture", "pointer": "/data/facts/0/value", "rule": "scalar", "value": 90, "unit": "ms"}
    meta = report_record(document=native)
    result = ground_report(context, {"analysis": "", "claims": [claim]}, [(meta, native)])
    assert result["claims"][0]["status"] != "verified"
    affected = [fact for fact in result["facts"] if fact["pointer"] == claim["pointer"] and fact["rule"] == claim["rule"]]
    assert affected and all(fact.get("object") != "object-fixture" or fact.get("verification", {}).get("object") != "verified" for fact in affected)


@pytest.mark.asyncio
async def test_regular_model_context_never_reads_tampered_evidence_bytes(trusted):
    rt, case = trusted
    run = await attempt(rt, case)
    server = run["config_snapshot"]["mcps"][0]
    definition = server["tools"][0]
    args, binding = rt._bind_source(run, server["id"], definition["name"], {"page": 0}, {**definition, "definition_digest": connectors.tool_digest(definition)})
    scope = rt._source_scope(run, server["id"], definition["name"], args)
    record, _ = rt._persist_evidence(run["id"], {"id": "invoke-review", "tool_name": definition["name"], "source": server["id"], "arguments": args, "query_binding": binding, "access_scopes": [scope]}, {"original": "synthetic-original"})
    changed = {"value": "SYNTHETIC_TAMPERED_AFTER_SAVE"}
    Path(record["path"]).write_text(json.dumps(changed))
    try:
        ledger = rt._evidence_ledger(rt.store.get("run", run["id"]))
    except ReadingError:
        return
    assert "SYNTHETIC_TAMPERED_AFTER_SAVE" not in json.dumps(ledger)


@pytest.mark.parametrize("selection,pointer,expected_data", [
    ({"pointer": "/rows", "fields": ["public"]}, "/rows/0/secret", [{"public": 1}, {"public": 2}]),
    ({"pointer": "/rows", "time_start": "1970-01-01T00:00:02Z", "time_end": "1970-01-01T00:00:02Z", "time_field": "/at"}, "/rows/0/secret", [{"at": 2, "public": 2, "secret": 99}]),
])
def test_fragment_anchor_does_not_cover_unread_fields_or_original_rows(material, selection, pointer, expected_data):
    store, reader, save = material
    record = save({"rows": [{"at": 1, "public": 1, "secret": 10}, {"at": 2, "public": 2, "secret": 99}]})
    result = reader.read("run_fixture", "alice", {"resource_type": "evidence", "evidence_id": record["id"], "selector": selection})
    assert result["data"] == expected_data
    assert _anchor_valid(result["anchor_id"], record, pointer, store.list("material_read")) is False


@pytest.mark.asyncio
async def test_inflight_source_revoke_rejects_before_any_evidence_is_saved(trusted):
    rt, case = trusted
    run = await attempt(rt, case)
    definition = run["analysis_context"]["query_catalog"]["entries"][0]
    source, tool = definition["source_id"], definition["tool_name"]
    arguments, binding = rt._bind_source(run, source, tool, {}, {"definition_digest": definition["definition_digest"]})
    seen = []
    async def response_after_revoke():
        seen.append("source-started")
        policy = rt.store.get("access_policy", "main")
        policy.update(version="source-revoked-in-flight", denies=[{"subjects": [run["owner_id"]]}])
        rt.store.save("access_policy", "main", policy)
        return {"isError": False, "data": "SYNTHETIC_REVOKED_RESPONSE_MUST_NOT_BE_SAVED"}
    with pytest.raises(PolicyError):
        await rt._invoke_source(run["id"], source, tool, arguments, response_after_revoke, query_binding=binding)
    assert seen == ["source-started"]
    assert rt.store.list("evidence") == []
    assert rt.store.get("run", run["id"])["evidence"] == []
    assert not list(rt.artifact_dir.glob("evidence/**/*.json"))
    invocation = rt.store.list("invocation")[0]
    assert invocation["status"] == "failed" and not invocation.get("evidence_id")
    assert "SYNTHETIC_REVOKED_RESPONSE" not in json.dumps(invocation)
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as error:
        rt.authorize_history(rt.store.get("run", run["id"]), run["owner_id"])
    assert error.value.status_code == 404
