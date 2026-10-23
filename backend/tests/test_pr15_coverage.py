"""Source completion signals survive the runtime/report boundary."""
import copy

import pytest

from aiopsstudio.runtime_trust import TrustBoundary
from aiopsstudio.grounded_reporting import ground_report

WINDOW = {"start": "2026-10-23T00:00:00Z", "end": "2026-10-23T00:02:00Z"}
CANONICAL = {"environment": "synthetic", "cluster": "", "namespace": "", "type": "service", "id": "service-a"}
BINDING = {"context_version": "analysis-context/1", "object_scope": "target", "kind": "telemetry",
    "canonical_key": "object-a", "window": WINDOW, "source_offset": {"seconds": 0}, "source_id": "metrics", "unit": "ms",
    "mapping": {"source_id": "metrics", "native_id": "service-a", "canonical": CANONICAL},
    "result_identity": {"version": "1", "collection": "/data/result", "pointer": "/metric/service", "mapping": "native_id",
        "fixed_scope": {key: CANONICAL[key] for key in ("environment", "cluster", "namespace", "type")}}}


def response():
    return {"status": "success", "data": {"resultType": "matrix", "result": [
        {"metric": {"service": "service-a"}, "values": [[1792713600, "5"], [1792713720, "8"]]}]}}


@pytest.mark.parametrize("status", ["partial", "failed", "forbidden", "stale", "unknown", "historical_unsupported"])
def test_source_limitation_prevents_verified_claim(status):
    native = {**response(), "coverage": {"status": status}}
    coverage = TrustBoundary()._coverage({}, native, BINDING)
    assert coverage["status"] == status
    run = {"id": "run-a", "attempt_id": "attempt-a", "analysis_context": {"version": "analysis-context/1",
        "identity": {"canonical": CANONICAL, "canonical_key": "object-a"}, "windows": {"fault": WINDOW}}}
    record = {"id": "ev-a", "source": "metrics", "status": "saved", "truncated": False, "coverage": coverage}
    claim = {"kind": "observation", "evidence_id": "ev-a", "pointer": "/data/result/0/values", "rule": "max", "value": 8, "unit": "ms"}
    report = ground_report(run, {"analysis": "bounded", "claims": [claim]}, [(record, native)])
    assert report["claims"][0]["status"] == "unverified"


@pytest.mark.parametrize("changes", [
    {"coverage": {"pagination_complete": False}}, {"coverage": {"status": "partial", "pagination_complete": False}},
    {"coverage": {"truncated": True}}, {"has_more": True}, {"next_cursor": "synthetic-next"},
    {"coverage": {"status": "complete", "has_more": True}}, {"truncated": True},
])
def test_success_does_not_erase_incomplete_delivery(changes):
    coverage = TrustBoundary()._coverage({}, {**response(), **changes}, BINDING)
    assert coverage["status"] == "partial"
    assert coverage["pagination_complete"] is False or coverage["truncated"] is True


def test_success_default_empty_and_error_control():
    document = response()
    assert TrustBoundary()._coverage({}, document, BINDING)["status"] == "complete"
    document["data"]["result"] = []
    assert TrustBoundary()._coverage({}, document, BINDING)["status"] == "empty"
    assert TrustBoundary()._coverage({}, {**document, "isError": True}, BINDING)["status"] == "failed"


@pytest.mark.parametrize("row", [None, "bad-row", {"metric": {}, "values": [[10**100, "8"]]}, {"metric": {}, "values": [[True, "8"]]}])
def test_malformed_success_retains_unknown(row):
    document = copy.deepcopy(response())
    document["data"]["result"] = [row]
    assert TrustBoundary()._coverage({}, document, BINDING)["status"] == "unknown"
