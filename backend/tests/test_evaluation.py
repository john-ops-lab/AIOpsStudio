"""Independent evaluation contracts; no production sources or model credentials."""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

from aiopsstudio.evaluation.contracts import BASELINE_REF, digest, execution_material, load_suite, read_json, validate_input
from aiopsstudio.evaluation.scoring import aggregate, compare, score
from aiopsstudio.evaluation.replay import synthetic_model

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def suite():
    return load_suite(ROOT / "evaluation/suite.json")


def test_suite_freezes_categories_versions_and_separates_oracles(suite):
    root, manifest, frozen = suite
    categories = set()
    for item in manifest["cases"]:
        visible = read_json(root / item["input"])
        categories.add(visible["category"])
        material = execution_material(visible, frozen)
        assert "id" not in material and "category" not in material
        serialized = json.dumps(material)
        assert '"oracle"' not in serialized and '"oracle_path"' not in serialized and item["id"] not in serialized
        assert digest(read_json(root / item["oracle"])) == item["oracle_sha256"]
    assert set(manifest["minimum_categories"]) == categories
    assert len(manifest["cases"]) == 15


def test_unknown_and_critical_failures_are_not_passed_or_averaged():
    oracle = {"schema": "aiopsstudio-evaluation-oracle/1", "assertions": [
        {"id": "ordinary", "dimension": "facts", "path": "answer", "operator": "equals", "value": "correct"},
        {"id": "privacy", "dimension": "boundaries", "path": "payload", "operator": "not_contains", "value": "PRIVATE_MARKER", "critical": True, "failure_class": "privacy_violation"},
        {"id": "future", "dimension": "boundaries", "path": "receipts.restore", "operator": "equals", "value": True, "failure_class": "recovery_gap"}]}
    result = score({"answer": "correct", "payload": "PRIVATE_MARKER", "receipts": {}}, oracle)
    assert result["strict_pass"] is False
    assert result["assertions"] == {"passed": 1, "failed": 1, "unknown": 1, "total": 3}
    assert result["failure_classes"] == {"privacy_violation": 1, "recovery_gap": 1}
    assert aggregate([{"score": result}])["cases"] == {"total": 1, "strict_passed": 0, "strict_failed": 1, "not_run": 0}


def test_version_and_source_tampering_fail_closed(suite):
    root, manifest, frozen = suite
    case = read_json(root / manifest["cases"][0]["input"])
    changed = copy.deepcopy(case)
    changed["source"]["responses"]["read_snapshot:0"]["structuredContent"]["data"]["facts"][0]["value"] = 0.99
    with pytest.raises(ValueError, match="source snapshot"):
        validate_input(changed, frozen)
    changed = copy.deepcopy(case)
    changed["versions"]["privacy_policy"]["version"] = "unknown"
    with pytest.raises(ValueError, match="versions"):
        validate_input(changed, frozen)
    changed = copy.deepcopy(case)
    changed["request"]["expected_answer"] = "injected"
    with pytest.raises(ValueError, match="oracle"):
        validate_input(changed, frozen)


def test_reference_must_resolve_to_actual_tool_not_merely_contain_ev_text():
    oracle = {"schema": "aiopsstudio-evaluation-oracle/1", "assertions": [{"id": "cite", "dimension": "references", "path": "run.report", "operator": "citation_source", "value": "read_snapshot"}]}
    observation = {"run": {"report": {"analysis": "[ev_fabricated]", "reference_validation": {"cited_ids": ["ev_fabricated"]}},
                           "evidence": [{"id": "ev_real", "tool_name": "read_snapshot"}]}}
    assert score(observation, oracle)["assertions"]["failed"] == 1


def test_compare_refuses_changed_visibility_versions_or_denominator():
    old = {"comparability": {"suite_sha256": "old"}, "results": []}
    new = {"comparability": {"suite_sha256": "new"}, "results": []}
    with pytest.raises(ValueError, match="comparison refused"):
        compare(old, new)
    with pytest.raises(ValueError, match="unverified"):
        compare({**old, "incomparable_cases": [{"case_id": "source_unknown"}]}, old)


def test_invocation_assertion_does_not_depend_on_storage_list_order():
    oracle = {"schema": "aiopsstudio-evaluation-oracle/1", "assertions": [
        {"id": "native_failure_saved", "dimension": "facts", "path": "invocations", "operator": "any_match", "value": {"tool_name": "read_snapshot", "status": "failed"}}]}
    calls = [{"tool_name": "model:synthetic", "status": "succeeded"}, {"tool_name": "read_snapshot", "status": "failed"}]
    assert score({"invocations": calls}, oracle) == score({"invocations": list(reversed(calls))}, oracle)


def test_actual_application_result_changes_with_tool_response_and_oracle_does_not(suite, tmp_path):
    root, manifest, frozen = suite
    item = manifest["cases"][0]
    case = read_json(root / item["input"])
    material = execution_material(case, frozen)
    spec = importlib.util.spec_from_file_location("evaluation_cli_test", ROOT / "scripts/evaluate_analysis.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    # This legacy fixture intentionally measures the immutable initial version;
    # new runtime policy requirements must not silently change its test target.
    app_root, _, _ = cli.code_root(BASELINE_REF, tmp_path)
    first_dir, second_dir = tmp_path / "first", tmp_path / "second"
    first = cli.run_worker(material, app_root, first_dir)
    assert first["execution"] == "actual_FastAPI_routes_DeepAgents_worker"
    assert len(first["source_requests"]) == 1 and len(first["model_requests"]) == 2
    oracle = read_json(root / item["oracle"])
    assert score(first, oracle)["strict_pass"] is True
    changed_oracle = copy.deepcopy(oracle)
    changed_oracle["assertions"][-3]["value"] = "this-is-absent"
    assert score(first, changed_oracle)["strict_pass"] is False
    changed = copy.deepcopy(material)
    changed["source"]["responses"]["read_snapshot:0"]["structuredContent"]["data"]["facts"][0]["value"] = 0.91
    changed["source"]["sha256"] = digest(changed["source"]["responses"])
    second = cli.run_worker(changed, app_root, second_dir)
    assert "0.91" in second["run"]["report"]["analysis"]
    assert score(second, oracle)["strict_pass"] is False
    assert first["measurement"]["input_tokens"] is None
    assert first["measurement"]["estimated_cost"] is None


def test_synthetic_model_only_uses_actual_messages():
    from langchain_core.messages import HumanMessage
    observed = []
    model = synthetic_model(observed)
    model.bind_tools([{"name": "read_snapshot"}])
    context = {"target": "demo-checkout", "analysis_start": "2026-10-20T01:00:00Z", "analysis_end": "2026-10-20T01:10:00Z", "evidence_ledger": {"evidence": []}}
    assert model.invoke([HumanMessage(content=json.dumps(context))]).tool_calls[0]["name"] == "read_snapshot"
    assert not any(word in json.dumps(observed) for word in ("oracle_path", "normal-alert"))


def test_execution_guard_denies_oracle_reads_and_network(tmp_path):
    oracle = tmp_path / "oracles" / "secret.json"
    oracle.parent.mkdir()
    oracle.write_text('{"hidden":true}')
    source = "from aiopsstudio.evaluation.replay import guard_execution\nimport pathlib,socket\nguard_execution()\n"
    read = subprocess.run([sys.executable, "-c", source + "pathlib.Path(" + repr(str(oracle)) + ").read_text()"], capture_output=True)
    assert read.returncode != 0 and b"oracles are unavailable" in read.stderr
    alias = tmp_path / "visible-alias.json"
    alias.symlink_to(oracle)
    linked = subprocess.run([sys.executable, "-c", source + "pathlib.Path(" + repr(str(alias)) + ").read_text()"], capture_output=True)
    assert linked.returncode != 0 and b"oracles are unavailable" in linked.stderr
    network = subprocess.run([sys.executable, "-c", source + "socket.create_connection(('127.0.0.1',1))"], capture_output=True)
    assert network.returncode != 0 and b"network is disabled" in network.stderr


def test_live_config_requires_external_private_owned_file_and_explicit_provider(tmp_path):
    from aiopsstudio.evaluation.replay import private_model_config
    config = tmp_path / "model.json"
    config.write_text(json.dumps({"provider": "minimax", "model": "synthetic-live-name", "base_url": "https://example.invalid/v1", "api_key": "synthetic-not-a-real-key"}))
    config.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        private_model_config(config, ROOT)
    config.chmod(0o600)
    assert private_model_config(config, ROOT)["provider"] == "minimax"
    value = json.loads(config.read_text())
    value["provider"] = "openai"
    config.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="explicit_provider_override"):
        private_model_config(config, ROOT)


def test_m1_policies_are_explicit_frozen_and_unknown_fields_still_denied(tmp_path):
    from cryptography.fernet import Fernet
    from types import SimpleNamespace
    from aiopsstudio.config import Vault
    from aiopsstudio.store import Store
    from aiopsstudio.access_policy import PolicyError, PolicyService
    from aiopsstudio.evaluation.replay import seed_frozen
    root, suite, frozen = load_suite(ROOT / "evaluation/m1/suite.json")
    visible = read_json(root / suite["cases"][0]["input"])
    store = Store("sqlite:///:memory:")
    app = SimpleNamespace(state=SimpleNamespace(store=store, vault=Vault(store, Fernet.generate_key())))
    seed_frozen(app, execution_material(visible, frozen))
    service = PolicyService(store)
    for kind in ("access_policy", "egress_policy"):
        service.validate_document(kind, store.get(kind, "main"))
    model = store.get("model", "model_evaluation")
    kwargs = {"subject_id": "admin", "scope_id": "synthetic-scope", "model_config": model, "snapshot": service.snapshot()}
    assert service.egress_view("context", {"target": "demo-checkout"}, **kwargs)["target"].startswith("resource_")
    with pytest.raises(PolicyError):
        service.egress_view("context", {"target": "demo-checkout", "unregistered_synthetic_field": "must_not_leave"}, **kwargs)
    with pytest.raises(PolicyError):
        service.egress_view("unregistered_type", {"content": "must_not_leave"}, **kwargs)
    store.close()


def test_working_tree_is_copied_before_attempts_not_read_live_between_cases(tmp_path):
    spec = importlib.util.spec_from_file_location("evaluation_copy_test", ROOT / "scripts/evaluate_analysis.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    source = tmp_path / "source"
    package = source / "backend/aiopsstudio"
    package.mkdir(parents=True)
    module = package / "runtime.py"
    module.write_text("VERSION = 'before'\n")
    cli.ROOT = source
    cli.git = lambda *args: (BASELINE_REF + "\n").encode()
    snapshot, _, snapshot_digest = cli.code_root("working-tree", tmp_path / "snapshot")
    module.write_text("VERSION = 'after'\n")
    assert (snapshot / "backend/aiopsstudio/runtime.py").read_text() == "VERSION = 'before'\n"
    assert snapshot_digest
