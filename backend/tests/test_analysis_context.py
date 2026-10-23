"""Synthetic, no-network R02 boundaries; not live source/business acceptance."""
import copy

import pytest

from aiopsstudio.analysis_context import (ContextError, bind_query, canonical_key,
    coverage_record, freeze_context, legacy_context, validate_configuration)

STAMP = "2026-01-02T12:00:00Z"
DIGEST = "a" * 64


def canonical(environment="prod", id="service-1", **extra):
    return {"environment": environment, "cluster": "cluster-a", "namespace": "payments", "type": "service", "id": id, **extra}


def record(environment="prod", id="service-1", **extra):
    return {"source_id": "metrics", "object_type": "service", "native_id": "native-1", "canonical": canonical(environment, id), "aliases": ["api"],
            "mapping_source": "synthetic-approved-map", "verified_at": "2026-01-01T00:00:00Z", "valid_from": "2025-12-01T00:00:00Z", "valid_to": None, **extra}


def catalog_entry(**extra):
    return {"source_id": "metrics", "tool_name": "query", "definition_digest": DIGEST, "version": "query/1", "kind": "telemetry", "historical": True,
            "identity_parameters": {"target_id": "native_id"}, "time_parameters": {"start": "start", "end": "end"}, "allowed_parameters": ["step"], **extra}


def settings(records=None, entries=None):
    return {"identity_mapping": {"version": "mapping/1", "records": records if records is not None else [record()]},
            "query_catalog": {"version": "catalog/1", "entries": entries if entries is not None else [catalog_entry()], "source_offsets": {"metrics": {"seconds": 0, "reason": "synthetic clock fixture"}}}}


def payload(**extra):
    return {"target": "api", "environment": "prod", "analysis_start": "2026-01-02T10:00:00Z", "analysis_end": "2026-01-02T11:00:00Z", **extra}


def frozen(config=None, **extra):
    return freeze_context(payload(**extra), config if config is not None else settings(), captured_at=STAMP)


def bound(context=None, arguments=None, **extra):
    return bind_query(context or frozen(), "metrics", "query", arguments or {}, definition_digest=DIGEST, **extra)


def assert_code(expected, function, *args, **kwargs):
    with pytest.raises(ContextError) as exc:
        function(*args, **kwargs)
    assert exc.value.code == expected


def test_complete_scope_key_and_cross_environment_same_names_do_not_merge():
    config = settings([record(), record("test", native_id="native-test")])
    prod, test = frozen(config), frozen(config, environment="test")
    assert prod["status"] == test["status"] == "ready"
    assert prod["identity"]["canonical_key"] != test["identity"]["canonical_key"]
    for field in ("environment", "cluster", "namespace", "type", "id"):
        assert canonical_key(canonical()) != canonical_key({**canonical(), field: "different"})
    assert prod["identity"]["canonical"] == canonical()


@pytest.mark.parametrize("records,expected", [
    ([record(), record(id="service-2", native_id="native-2")], "identity_ambiguous"),
    ([], "identity_mapping_missing"),
    ([record(), record(native_id="conflict")], "source_identity_conflict"),
    ([record(valid_from=None)], "historical_validity_unknown"),
    ([record(valid_to="2026-01-02T10:30:00Z")], "historical_validity_unknown"),
    ([record(verified_at=None)], "historical_validity_unknown"),
])
def test_missing_ambiguous_conflicting_or_unknown_validity_never_bind(records, expected):
    context = frozen(settings(records))
    assert context["status"] == "needs_input"
    assert expected in {i["code"] for i in context["issues"]}
    assert_code("identity_needs_input", bound, context)


def test_explicit_client_key_is_resolved_only_from_server_mapping():
    assert frozen(target_identity=canonical())["status"] == "ready"
    unknown = frozen(target_identity=canonical(id="self-reported"))
    assert unknown["status"] == "needs_input" and unknown["identity"]["canonical"] is None
    assert_code("identity_scope_conflict", frozen, target_identity=canonical("test"))


def test_recreated_alias_uses_verified_historical_interval():
    old = record(id="old-uid", native_id="old", valid_to="2026-01-01T00:00:00Z")
    current = record(id="new-uid", native_id="new", valid_from="2026-01-01T00:00:00Z")
    context = frozen(settings([old, current]))
    assert context["status"] == "ready" and context["identity"]["canonical"]["id"] == "new-uid"
    crossing = frozen(settings([old, current]), analysis_start="2025-12-31T23:00:00Z", analysis_end="2026-01-01T01:00:00Z")
    assert crossing["status"] == "needs_input"


def test_absolute_utc_named_windows_baseline_and_source_offset_are_frozen():
    config = settings()
    config["query_catalog"]["source_offsets"]["metrics"] = {"seconds": 90, "reason": "synthetic measured offset"}
    args = payload(analysis_start="2026-01-02T18:00:00+08:00", analysis_end="2026-01-02T19:00:00+08:00",
                   baseline_window={"start": "2026-01-01T10:00:00Z", "end": "2026-01-01T11:00:00Z", "reason": "previous day"})
    snapshot = copy.deepcopy((args, config))
    context = freeze_context(args, config, captured_at=STAMP)
    config["query_catalog"]["version"] = "changed"
    assert context["windows"]["fault"]["start"] == "2026-01-02T10:00:00Z"
    assert context["windows"]["baseline"]["comparability"] == "unknown"
    result = bound(context)
    assert result["arguments"]["start"] == "2026-01-02T10:01:30Z"
    assert result["binding"]["window"]["start"] == "2026-01-02T10:00:00Z"
    assert result["binding"]["source_offset"]["seconds"] == 90
    assert context["query_catalog_version"] == "catalog/1"
    assert args == snapshot[0]


@pytest.mark.parametrize("changes", [
    {"analysis_start": "now"}, {"analysis_start": "2026-01-02T10:00:00"},
    {"analysis_start": "2026-01-02T12:00:00Z"}, {"analysis_start": "2025-12-01T00:00:00Z"},
    {"analysis_start": "2026-01-02 10:00:00+00:00"}, {"analysis_start": "2026-01-02T10:00:00.1234567Z"},
])
def test_relative_naive_inverted_and_excessive_windows_are_rejected(changes):
    assert_code("time_window_invalid", frozen, **changes)


def test_conflicting_named_window_is_rejected_and_no_window_defaults_to_now():
    assert_code("time_window_conflict", frozen, windows={"fault": {"start": "2026-01-02T10:01:00Z", "end": "2026-01-02T11:00:00Z"}})
    assert_code("time_window_required", freeze_context, {}, settings())


@pytest.mark.parametrize("arguments", [{"target_id": "other"}, {"start": "now"}, {"end": "2026-01-03T00:00:00Z"}])
def test_bound_parameters_cannot_be_overridden(arguments):
    assert_code("query_context_conflict", bound, arguments=arguments)


def test_binding_accepts_equivalent_instant_and_locks_versions():
    result = bound(arguments={"start": "2026-01-02T18:00:00+08:00", "step": 60})
    assert result["arguments"] == {"target_id": "native-1", "start": "2026-01-02T10:00:00Z", "end": "2026-01-02T11:00:00Z", "step": 60}
    assert result["binding"]["entry_version"] == "query/1"
    assert result["binding"]["mapping_version"] == "mapping/1"
    assert_code("query_definition_changed", bind_query, frozen(), "metrics", "query", {}, definition_digest="b" * 64)
    assert_code("query_binding_missing", bind_query, frozen(), "metrics", "not-listed", {}, definition_digest=DIGEST)
    assert_code("query_parameter_unbound", bound, arguments={"unregistered": "maybe another target"})


def test_query_body_must_be_frozen_and_template_uses_escaped_json_scalars():
    assert_code("query_expression_unbound", bound, arguments={"query": "up{service=\"other\"}"})
    row = record(native_id='odd"\\value')
    config = settings([row], [catalog_entry(argument_templates={"query": "up{service={native_id_json}}"})])
    result = bound(frozen(config))
    assert result["arguments"]["query"] == 'up{service="odd\\"\\\\value"}'
    assert_code("query_context_conflict", bound, frozen(config), {"query": "up{}"})
    bad = settings(entries=[catalog_entry(argument_templates={"query": "up{service={native_id}}"})])
    assert_code("query_catalog_invalid", bound, frozen(bad))


def test_unscoped_discovery_is_explicitly_unverified_and_requires_catalog():
    config = settings([], [catalog_entry(object_scope="none", kind="discovery", identity_parameters={}, time_parameters={})])
    result = bound(frozen(config))
    coverage = coverage_record(result["binding"], status="complete", item_count=1)
    assert coverage["status"] == "unknown" and "scope_unverified" in coverage["reasons"]


@pytest.mark.parametrize("time_format,multiplier", [("unix_seconds", 1), ("unix_milliseconds", 1000)])
def test_explicit_numeric_time_formats(time_format, multiplier):
    result = bound(frozen(settings(entries=[catalog_entry(time_format=time_format)])))
    assert result["arguments"]["start"] == 1767348000 * multiplier


@pytest.mark.parametrize("status,count,expected", [("complete", 0, "empty"), ("complete", 2, "complete"), ("forbidden", 0, "forbidden"), ("failed", 0, "failed"), ("unknown", None, "unknown")])
def test_distinct_empty_permission_failure_and_unknown_are_preserved(status, count, expected):
    coverage = coverage_record(bound()["binding"], status=status, item_count=count, error={"code": "synthetic_timeout"} if status == "failed" else None)
    assert coverage["status"] == expected


@pytest.mark.parametrize("extra,expected,reason", [
    ({"pagination_complete": False}, "partial", "pagination_incomplete"),
    ({"truncated": True}, "partial", "truncated"),
    ({"stale": True}, "stale", "stale"),
    ({"observed_start": "2026-01-02T09:59:59Z"}, "partial", "out_of_window"),
])
def test_incomplete_stale_and_out_of_window_results_are_not_complete(extra, expected, reason):
    result = coverage_record(bound()["binding"], status="complete", item_count=2, **extra)
    assert result["status"] == expected and reason in result["reasons"]


@pytest.mark.parametrize("support,expected", [(None, "unknown"), (False, "historical_unsupported"), (True, "complete")])
def test_current_topology_cannot_assert_fault_time_without_historical_support(support, expected):
    binding = bound(frozen(settings(entries=[catalog_entry(kind="topology", historical=support)])))["binding"]
    assert coverage_record(binding, status="complete", item_count=2, historical=True)["status"] == expected


def test_observed_source_times_are_checked_after_recorded_offset_projection():
    config = settings()
    config["query_catalog"]["source_offsets"]["metrics"]["seconds"] = 90
    binding = bound(frozen(config))["binding"]
    result = coverage_record(binding, status="complete", item_count=2, observed_start="2026-01-02T10:01:30Z", observed_end="2026-01-02T11:01:30Z")
    assert result["status"] == "complete"
    assert result["normalized_observed_window"]["start"] == "2026-01-02T10:00:00Z"


def test_baseline_binding_rechecks_mapping_validity_for_its_own_window():
    config = settings([record(valid_from="2026-01-02T00:00:00Z")], [catalog_entry(window="baseline")])
    context = frozen(config, baseline_window={"start": "2026-01-01T10:00:00Z", "end": "2026-01-01T11:00:00Z"})
    assert context["status"] == "ready"
    assert_code("identity_window_unverified", bound, context)


def test_unknown_source_clock_cannot_prove_observed_points_are_inside_window():
    config = settings()
    config["query_catalog"]["source_offsets"] = {}
    binding = bound(frozen(config))["binding"]
    result = coverage_record(binding, status="complete", item_count=1, observed_start="2026-01-02T10:30:00Z")
    assert result["status"] == "unknown" and "source_clock_unknown" in result["reasons"]
    assert result["normalized_observed_window"]["start"] is None


def test_legacy_configuration_and_runs_are_not_retroactively_complete():
    config = {"identity_mapping": {"api": "native-1"}, "query_catalog": {"metrics": {"historical_query": "up"}}}
    context = frozen(config)
    assert context["status"] == "needs_input"
    assert context["identity_mapping_version"] is None and context["query_catalog_version"] is None
    assert_code("query_binding_missing", bound, context)
    assert legacy_context()["status"] == "unknown"
    assert coverage_record(status="complete", item_count=0)["status"] == "unknown"
    assert_code("analysis_context_unverified", bind_query, legacy_context(), "metrics", "query", {}, definition_digest=DIGEST)


@pytest.mark.parametrize("value", [{"version": "x"}, {"records": []}, {"version": "", "records": []}])
def test_partially_versioned_configuration_is_rejected(value):
    assert_code("context_configuration_invalid", validate_configuration, value, {})


def test_duplicate_catalog_and_invalid_offset_cannot_be_frozen():
    assert_code("query_catalog_invalid", frozen, settings(entries=[catalog_entry(), catalog_entry()]))
    config = settings()
    config["query_catalog"]["source_offsets"]["metrics"]["seconds"] = float("nan")
    assert_code("query_catalog_invalid", frozen, config)


@pytest.mark.parametrize("entry", [
    catalog_entry(identity_parameters={"environment": "canonical.environment"}, argument_templates={"query": "up{}"}),
    catalog_entry(identity_parameters={}, static_arguments={"query": "up{}"}),
])
def test_scope_only_or_constant_query_cannot_pretend_to_bind_an_object(entry):
    assert_code("query_identity_unbound", bound, frozen(settings(entries=[entry])))


@pytest.mark.parametrize("field,value", [("window", {}), ("kind", []), ("time_format", []), ("object_scope", {}), ("historical", {}), ("definition_digest", 42), ("identity_parameters", {"id": []})])
def test_malformed_catalog_types_fail_closed(field, value):
    entry = catalog_entry(**{field: value})
    if field == "identity_parameters":
        # A malformed selector is rejected before any connector can dispatch.
        assert_code("context_configuration_invalid", validate_configuration, settings()["identity_mapping"], {"version": "v", "entries": [entry]})
    else:
        with pytest.raises(ContextError):
            frozen(settings(entries=[entry]))
