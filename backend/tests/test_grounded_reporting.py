"""Independent values and scope oracles; saved synthetic JSON only."""
import copy
import hashlib
import json

import pytest

from aiopsstudio.grounded_reporting import ground_report


WINDOW = {"start": "2026-10-23T00:00:00Z", "end": "2026-10-23T00:02:00Z"}
START = 1792713600
CANONICAL = {"environment": "synthetic", "cluster": "", "namespace": "", "type": "service", "id": "service-a"}
FIXED_SCOPE = {key: CANONICAL[key] for key in ("environment", "cluster", "namespace", "type")}


def run():
    return {"id": "run-fixture", "attempt_id": "attempt-fixture", "analysis_context": {
        "version": "analysis-context/1", "identity": {"canonical_key": "object-fixture", "canonical": CANONICAL}, "windows": {"fault": WINDOW}}}


def native():
    return {"status": "success", "data": {"resultType": "matrix", "result": [
        {"metric": {"__name__": "latency", "service": "service-a"}, "values": [[START, "10"], [START + 60, "90"], [START + 120, "12"]]},
        {"metric": {"__name__": "latency", "service": "service-b"}, "values": [[START, "2"], [START + 60, "4"], [START + 120, "3"]]},
    ]}}


def record(id="ev-fixture", document=None, **changes):
    document = native() if document is None else document
    return {"id": id, "source": "source-a", "source_group": "underlying-source", "status": "saved", "truncated": False,
            "sha256": hashlib.sha256(json.dumps(document).encode()).hexdigest(), "unit": "ms", "coverage": {
                "status": "complete", "canonical_key": "object-fixture", "declared_window": WINDOW,
                "source_offset": {"seconds": 0, "status": "verified"}, "pagination_complete": True,
                "binding": {"context_version": "analysis-context/1", "source_id": changes.get("source", "source-a"),
                    "canonical_key": "object-fixture", "window_name": "fault", "window": WINDOW,
                    "mapping": {"source_id": changes.get("source", "source-a"), "native_id": "service-a", "canonical": CANONICAL},
                    "result_identity": {"version": "returned-service/1", "collection": "/data/result", "pointer": "/metric/service",
                        "mapping": "native_id", "fixed_scope": FIXED_SCOPE}}}, **changes}


def claim(**changes):
    return {"kind": "observation", "evidence_id": "ev-fixture", "pointer": "/data/result/0/values", "rule": "max", "value": 90, "unit": "ms", **changes}


def report(claims=None, document=None, meta=None, **changes):
    document = native() if document is None else document
    meta = record(document=document) if meta is None else meta
    return ground_report(run(), {"analysis": "有界分析", "claims": [claim()] if claims is None else claims}, [(meta, document)], **changes)


def codes(result):
    return {item["code"] for item in result["unknowns"]}


def test_all_series_and_tail_independent_oracle():
    result = report()
    assert len(result["facts"]) == 10
    first = {(fact["rule"], fact["labels"]["service"]): fact["value"] for fact in result["facts"]}
    assert first == {("first", "service-a"): 10, ("last", "service-a"): 12, ("min", "service-a"): 10,
        ("max", "service-a"): 90, ("count", "service-a"): 3, ("first", "service-b"): 2,
        ("last", "service-b"): 3, ("min", "service-b"): 2, ("max", "service-b"): 4, ("count", "service-b"): 3}
    assert len([item for item in result["counterevidence"] if item["kind"] == "tail_below_peak"]) == 2
    assert result["claims"][0]["status"] == "verified"
    assert result["validation"]["status"] == "partial"  # The second service is not the query target.
    assert all(fact["object"] == "object-fixture" and fact["verification"]["object"] == "verified"
               for fact in result["facts"] if fact["labels"]["service"] == "service-a")
    assert all(fact["object"] is None and fact["query_scope"] == "object-fixture" and fact["verification"]["object"] == "unknown"
               for fact in result["facts"] if fact["labels"]["service"] == "service-b")
    assert result["root_cause_confirmation"] == "undetermined" and result["business_acceptance"] == "unverified"


def test_vector_is_one_sample_not_timestamp_as_metric():
    doc = {"status": "success", "data": {"resultType": "vector", "result": [
        {"metric": {"service": "service-a"}, "value": [START + 60, "8"]},
        {"metric": {"service": "service-b"}, "value": [START + 60, "11"]},
    ]}}
    result = report(claims=[], document=doc)
    assert len(result["facts"]) == 10
    for fact in result["facts"]:
        assert fact["value"] == (1 if fact["rule"] == "count" else 8 if fact["labels"]["service"] == "service-a" else 11)


def test_fact_stability_claim_version_and_immutable_inputs():
    r, meta, doc = run(), record(), native()
    before = copy.deepcopy((r, meta, doc))
    first = ground_report(r, {"analysis": "version one", "claims": [claim()]}, [(meta, doc)])
    second = ground_report(r, {"analysis": "version two", "claims": [claim()]}, [(meta, doc)])
    assert [fact["id"] for fact in first["facts"]] == [fact["id"] for fact in second["facts"]]
    assert first["report_version"] != second["report_version"] and first["claims"][0]["id"] != second["claims"][0]["id"]
    assert (r, meta, doc) == before


@pytest.mark.parametrize("changes,expected", [
    ({"value": 900}, "value_mismatch"), ({"unit": "seconds"}, "unit_mismatch"),
    ({"evidence_id": "ev-does-not-exist"}, "evidence_missing"), ({"pointer": "/data/result/9/values"}, "pointer_invalid"),
    ({"window": {"start": "2026-10-22T00:00:00Z", "end": "2026-10-22T01:00:00Z"}}, "window_mismatch"),
    ({"anchor_id": "frag-forged"}, "anchor_invalid"),
])
def test_exact_value_unit_reference_window_and_anchor_required(changes, expected):
    result = report([claim(**changes)])
    assert result["claims"][0]["status"] == "unverified" and expected in codes(result)


def test_frozen_unit_only_no_metric_name_guess():
    meta = record()
    meta.pop("unit")
    doc = native()
    doc["data"]["result"][0]["metric"]["__name__"] = "latency_milliseconds"
    result = report(meta=meta, document=doc)
    assert all((fact["unit_status"] == "verified" and fact["unit"] == "items") if fact["rule"] == "count"
               else fact["unit_status"] == "unknown" and fact["unit"] is None for fact in result["facts"])
    assert "unit_mismatch" in codes(result)
    meta["query_binding"] = {**meta["coverage"]["binding"], "unit": "ms"}
    assert report(meta=meta, document=doc)["claims"][0]["status"] == "verified"


@pytest.mark.parametrize("state,reason", [("partial", "coverage_limited"), ("unknown", "coverage_unknown"),
    ("stale", "source_stale"), ("failed", "source_failed"), ("forbidden", "source_failed"), ("historical_unsupported", "historical_unsupported")])
def test_coverage_failure_staleness_and_unknown_limit_claims(state, reason):
    meta = record()
    meta["coverage"]["status"] = state
    result = report(meta=meta)
    assert result["claims"][0]["status"] == "unverified" and reason in codes(result)


@pytest.mark.parametrize("meta_changes,doc_changes,reason", [
    ({"truncated": True}, {}, "storage_incomplete"),
    ({"integrity_verified": False}, {}, "logical_response_incomplete"),
    ({"logical_response_complete": False}, {}, "logical_response_incomplete"),
    ({}, {"delivery": {"complete": False, "has_more": True}}, "logical_response_incomplete"),
    ({}, {"verification": {"status": "rejected"}}, "logical_response_incomplete"),
    ({}, {"isError": True}, "source_failed"),
])
def test_incomplete_transport_storage_or_failed_validation_has_no_internal_facts(meta_changes, doc_changes, reason):
    result = report(meta=record(**meta_changes), document={**native(), **doc_changes})
    assert not result["facts"] and reason in codes(result) and result["claims"][0]["status"] == "unverified"


def test_window_canonical_object_offset_and_actual_samples_verified():
    for change in ("object", "offset", "window", "outside"):
        doc, meta = native(), record()
        if change == "object":
            meta["coverage"]["canonical_key"] = "different-object"
        elif change == "offset":
            meta["coverage"]["source_offset"] = {"seconds": None, "status": "unknown"}
        elif change == "window":
            meta["coverage"]["declared_window"] = {**WINDOW, "end": "2026-10-23T00:03:00Z"}
        else:
            doc["data"]["result"][0]["values"][-1][0] += 600
        result = report(document=doc, meta=meta)
        assert "window_unverified" in codes(result) and result["claims"][0]["status"] == "unverified"


def test_normal_and_anomaly_require_source_frozen_rule_not_model_threshold():
    result = report([claim(kind="anomaly", threshold=1)])
    assert "assessment_missing" in codes(result) and not result["observed_anomalies"]
    meta = record(assessment_rule={"version": "assessment-1", "operator": "gt", "threshold": 50, "unit": "ms", "rule": "max", "pointer": "/data/result/0/values"})
    result = report([claim(kind="anomaly")], meta=meta)
    assert result["claims"][0]["status"] == "verified" and len(result["observed_anomalies"]) == 1
    result = report([claim(kind="normal")], meta=meta)
    assert result["claims"][0]["status"] == "unverified" and "assessment_mismatch" in codes(result)
    meta["assessment_rule"]["threshold"] = 100
    result = report([claim(kind="normal")], meta=meta)
    assert result["claims"][0]["status"] == "verified" and any(item["kind"] == "normal_control" for item in result["counterevidence"])


def test_root_cause_and_candidate_remain_unconfirmed():
    result = report([claim(kind="root_cause", text="certain root cause"), claim(kind="candidate", text="candidate explanation", next_check="need independent validation")])
    assert all(item["status"] == "unverified" for item in result["claims"])
    assert len(result["candidates"]) == 2 and result["candidates"][1]["next_check"] == "need independent validation"
    assert result["root_cause_confirmation"] == "undetermined"


def test_relation_direction_exact_and_potential_never_observed_impact():
    doc = {"relations": [{"source": "service-a", "target": "database-b", "direction": "depends_on"}]}
    meta = record(document=doc)
    meta["coverage"]["binding"]["result_identity"].update(collection="/relations", pointer="/source")
    value = doc["relations"][0]
    result = report([claim(kind="potential_impact", rule="relation", pointer="/relations/0", value=value, unit=None)], document=doc, meta=meta)
    assert result["claims"][0]["status"] == "potential_only" and len(result["potential_impacts"]) == 1
    assert not result["observed_anomalies"]
    result = report([claim(kind="potential_impact", rule="relation", pointer="/relations/0", value={"source": "database-b", "target": "service-a", "direction": "depends_on"}, unit=None)], document=doc, meta=meta)
    assert "value_mismatch" in codes(result) and result["claims"][0]["status"] == "unverified"


def test_logical_json_root_supports_structured_content_and_mcp_text():
    for wrapper, expected in (({"structuredContent": native()}, "/structuredContent"),
                              ({"content": [{"type": "text", "text": json.dumps(native())}]}, "/content/0/text#json")):
        result = report(document=wrapper)
        assert result["claims"][0]["status"] == "verified"
        assert all(fact["saved_root"] == expected for fact in result["facts"])


def test_free_text_malformed_json_and_unknown_schema_do_not_self_certify():
    for text in ("service is healthy: 999ms", "```json\n{\"claims\": []}\n```", '{"analysis":"x","claims":[{"value":NaN}]}'):
        result = ground_report(run(), text, [(record(), native())])
        assert result["validation"]["status"] == "partial" and "free_text_unverified" in codes(result)
    result = report(document={"unknown_schema": ["business", "values"]}, claims=[])
    assert not result["facts"] and "unknown_schema" in codes(result)


def test_same_underlying_source_not_independent_and_conflict_not_hidden():
    docs = [(record("ev-one"), native()), (record("ev-two", source="wrapper-tool"), native())]
    result = ground_report(run(), {"analysis": "bounded", "claims": [claim(evidence_id="ev-one"), claim(evidence_id="ev-two")]}, docs)
    assert result["validation"]["independent_source_count"] == 1
    changed = native()
    changed["data"]["result"][0]["values"][1][1] = "190"
    docs[1] = (record("ev-two", document=changed, source="wrapper-tool"), changed)
    result = ground_report(run(), {"analysis": "bounded", "claims": [claim(evidence_id="ev-one"), claim(evidence_id="ev-two", value=190)]}, docs)
    assert "conflicting_values" in codes(result)
    assert all(item["status"] == "unverified" for item in result["claims"])
    assert any(item["kind"] == "conflicting_saved_values" for item in result["counterevidence"])


def test_anchor_requires_correct_evidence_digest_and_actually_read_range():
    meta = record()
    receipt = {"anchor_id": "frag-known", "anchor": {"resource_type": "evidence", "evidence_id": meta["id"], "sha256": meta["sha256"],
        "position": {"pointer": "/data/result/0/values", "offset": 0, "end": 1, "total_saved_items": 3}}}
    result = report([claim(anchor_id="frag-known")], meta=meta, material_reads=[receipt])
    assert "anchor_invalid" in codes(result)
    receipt["anchor"]["position"]["end"] = 3
    result = report([claim(anchor_id="frag-known")], meta=meta, material_reads=[receipt])
    assert result["claims"][0]["status"] == "verified"


@pytest.mark.parametrize("change", ["missing_contract", "missing_version", "wrong_subject", "missing_scope_dimension", "mismatched_partition", "wrong_source"])
def test_returned_object_requires_versioned_identity_and_all_registered_dimensions(change):
    doc, meta = native(), record()
    contract = meta["coverage"]["binding"]["result_identity"]
    if change == "missing_contract":
        meta["coverage"]["binding"].pop("result_identity")
    elif change == "missing_version":
        contract.pop("version")
    elif change == "wrong_subject":
        doc["data"]["result"][0]["metric"]["service"] = "service-not-the-target"
    elif change == "missing_scope_dimension":
        contract["fixed_scope"] = {key: value for key, value in FIXED_SCOPE.items() if key != "namespace"}
    elif change == "mismatched_partition":
        contract["fixed_scope"] = {**FIXED_SCOPE, "environment": "different-environment"}
    else:
        meta["coverage"]["binding"]["mapping"]["source_id"] = "different-source"
    result = report(document=doc, meta=meta)
    fact = next(item for item in result["facts"] if item["pointer"] == "/data/result/0/values" and item["rule"] == "max")
    assert fact["value"] == 90 and fact["verification"]["value"] == "verified"
    assert fact["object"] is None and fact["query_scope"] == "object-fixture"
    assert fact["verification"]["object"] == "unknown" and fact["scope"] == "saved_material_only"
    assert result["claims"][0]["status"] == "unverified" and "object_identity_unknown" in codes(result)


def test_each_collection_row_uses_its_own_identity_and_scope_fields():
    doc, meta = native(), record()
    for series in doc["data"]["result"]:
        series["metric"].update(FIXED_SCOPE)
    contract = meta["coverage"]["binding"]["result_identity"]
    contract.pop("fixed_scope")
    contract["scope_fields"] = {key: "/metric/" + key for key in FIXED_SCOPE}
    assert report(document=doc, meta=meta)["claims"][0]["status"] == "verified"
    doc["data"]["result"][0]["metric"]["namespace"] = "other-namespace"
    result = report(document=doc, meta=meta)
    assert result["claims"][0]["status"] == "unverified"
    assert all(item["object"] is None for item in result["facts"])


def test_single_document_identity_cannot_identify_arbitrary_returned_rows():
    doc = {"identity": {"id": "service-a", **FIXED_SCOPE}, "rows": [{"subject": "foreign-service", "value": 90}]}
    meta = record(document=doc)
    meta["coverage"]["binding"]["result_identity"] = {"version": "singleton-1", "pointer": "/identity/id", "mapping": "native_id", "fixed_scope": FIXED_SCOPE}
    result = report([claim(pointer="/rows/0/value", rule="scalar")], document=doc, meta=meta)
    assert result["claims"][0]["status"] == "unverified"
    # A genuine singleton scalar can use a registered document identity.
    doc["value"] = 90
    result = report([claim(pointer="/value", rule="scalar")], document=doc, meta=meta)
    assert result["claims"][0]["status"] == "verified"


def test_canonical_selector_and_latest_frozen_assessment_never_upgrade_foreign_series():
    doc, meta = native(), record()
    meta["coverage"]["binding"]["mapping"]["native_id"] = "different-native-id"
    meta["coverage"]["binding"]["result_identity"]["mapping"] = "canonical.id"
    meta["assessment_rule"] = {"version": "fixed-assessment-1", "operator": "gt", "threshold": 3, "unit": "ms"}
    result = report([claim(kind="anomaly")], document=doc, meta=meta)
    assert result["claims"][0]["status"] == "verified"
    result = report([claim(kind="anomaly", pointer="/data/result/1/values", value=4)], document=doc, meta=meta)
    assert result["claims"][0]["status"] == "unverified" and result["observed_anomalies"] == []
