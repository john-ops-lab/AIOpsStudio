"""Server-scored release admission, synthetic fixtures, no network or real state."""
import copy
import json
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from aiopsstudio.config import ConfigService, ConfigError, Vault
from aiopsstudio.management_quality import QualityService, candidate_projection
from aiopsstudio.store import Store, StoreConflict
from m2_management_fixtures import evaluate_for_publication, legacy_publication, publish_with_quality, synthetic_quality_run, synthetic_pipeline_proof
from test_config import SKILL


@pytest.fixture
def config(tmp_path):
    store = Store("sqlite:///" + str(tmp_path / "management.sqlite"))
    service = ConfigService(store, Vault(store, Fernet.generate_key()), tmp_path / "artifacts")
    yield service
    store.close()


def model(config):
    return config.save("model", {"name": "合成候选模型", "model": "synthetic-model", "secret_value": "synthetic-model-key-never-send"})


def mcp(config):
    draft = config.save("mcp", {"name": "合成只读源", "url": "https://source.invalid/mcp"})
    draft.update(discovery={"complete": True}, tools=[{"name": "read", "alias": "synthetic_read", "readonly": True, "inputSchema": {"type": "object"}}], allowed_tools=["read"])
    return config.store.save("mcp", draft["id"], draft, draft["revision"])


def test_formal_default_requires_service_evaluation_and_experiment_never_activates(config):
    draft = model(config)
    with pytest.raises(ConfigError) as missing:
        config.publish("model", draft["id"], draft["revision"])
    assert missing.value.code == "quality_evaluation_required"
    experiment = config.publish("model", draft["id"], draft["revision"], channel="experimental")
    assert experiment["current_version"] is None and experiment["enabled"] is False
    version = experiment["versions"][-1]
    assert version["channel"] == "experimental" and version["snapshot"]["enabled"] is False
    with pytest.raises(ConfigError) as rollback:
        config.rollback("model", draft["id"], version["id"])
    assert rollback.value.code == "experimental_version"
    with pytest.raises(ConfigError):
        config.state("model", draft["id"], {"enabled": True})
    with pytest.raises(ConfigError):
        QualityService(config).evaluate("model", draft["id"], {"expected_revision": experiment["revision"], "passed": True, "score": 100})


def test_actual_record_checks_cannot_be_replaced_by_succeeded_status(config):
    draft = model(config)
    run = synthetic_quality_run(config, "model", draft)
    run["usage"]["model_http_request_count"] = 0
    config.store.save("run", run["id"], run)
    receipt = QualityService(config).evaluate("model", draft["id"], {"expected_revision": draft["revision"], "candidate_run_ids": [run["id"]]})
    assert receipt["eligible"] is False
    checks = receipt["candidate_cases"][0]["checks"]
    assert next(c for c in checks if c["id"] == "actual_model_dispatch")["status"] == "failed"


@pytest.mark.parametrize("change", ["content", "settings", "policy", "credential"])
def test_candidate_evaluation_is_invalidated_by_frozen_combination_change(config, change):
    draft = model(config)
    receipt = evaluate_for_publication(config, "model", draft)
    assert receipt["eligible"]
    if change == "content":
        draft = config.save("model", {"expected_revision": draft["revision"], "max_tokens": 4096}, draft["id"])
    elif change == "settings":
        old = config.store.get("settings", "main")
        config.store.save("settings", "main", {**old, "queue_limit": 3}, old["revision"])
    elif change == "policy":
        config.store.save("access_policy", "main", {"schema": "synthetic-policy", "version": "changed"})
    else:
        config.vault.revoke(draft["secret_id"])
    with pytest.raises(ConfigError):
        config.publish("model", draft["id"], draft["revision"], evaluation_id=receipt["id"])


def test_new_version_requires_comparable_baseline_not_only_candidate(config):
    first = publish_with_quality(config, "model", model(config))
    changed = config.save("model", {"expected_revision": first["revision"], "max_tokens": 3000}, first["id"])
    run = synthetic_quality_run(config, "model", changed)
    quality = QualityService(config)
    receipt = quality.evaluate("model", first["id"], {"expected_revision": changed["revision"], "candidate_run_ids": [run["id"]]})
    assert not receipt["eligible"] and "baseline_not_verified" in receipt["reasons"]
    baseline = synthetic_quality_run(config, "model", changed, version_id=first["current_version"], input_marker="different")
    receipt = quality.evaluate("model", first["id"], {"expected_revision": changed["revision"], "candidate_run_ids": [run["id"]], "baseline_run_ids": [baseline["id"]]})
    assert "case_denominator_or_input_mismatch" in receipt["reasons"]


def test_retry_does_not_replace_original_first_failed_attempt(config):
    draft = model(config)
    failed = synthetic_quality_run(config, "model", draft, failure=True)
    retry = copy.deepcopy(failed)
    retry.update(id=failed["id"] + "-retry", report={"sample_only": True, "tool_result": "AIOpsStudio中文工具协议样本已返回", "analysis": "合成说明"}, created_at="2099-01-01T00:00:00Z")
    config.store.save("run", retry["id"], retry)
    receipt = QualityService(config).evaluate("model", draft["id"], {"expected_revision": draft["revision"], "candidate_run_ids": [retry["id"]]})
    assert not receipt["eligible"]
    assert next(c for c in receipt["candidate_cases"][0]["checks"] if c["id"] == "original_first_attempt")["status"] == "failed"
    assert config.store.get("run", failed["id"])["report"] == {}


def test_saved_evidence_tamper_revokes_previously_eligible_receipt(config):
    draft = mcp(config)
    receipt = evaluate_for_publication(config, "mcp", draft)
    assert receipt["eligible"]
    run = config.store.get("run", receipt["candidate_cases"][0]["run_id"])
    path = Path(run["evidence"][0]["path"])
    path.write_text('{"tampered":true}')
    with pytest.raises(ConfigError) as error:
        config.publish("mcp", draft["id"], draft["revision"], evaluation_id=receipt["id"])
    assert error.value.code == "quality_evidence_changed"


def test_foreign_private_runs_and_receipts_are_not_readable_by_config_admin(config):
    draft = model(config)
    run = synthetic_quality_run(config, "model", draft, owner="someone-else")
    quality = QualityService(config)
    with pytest.raises(ConfigError) as denied:
        quality.evaluate("model", draft["id"], {"expected_revision": draft["revision"], "candidate_run_ids": [run["id"]]}, actor="admin")
    assert denied.value.code == "not_found"
    record = quality.evaluate("model", draft["id"], {"expected_revision": draft["revision"], "candidate_run_ids": [run["id"]]}, actor="someone-else")
    assert quality.view("model", draft["id"], actor="admin")["evaluations"] == []
    assert quality.view("model", draft["id"], actor="someone-else")["evaluations"][0]["id"] == record["id"]


def test_publish_compare_and_swap_precedes_quality_consumption(config):
    draft = model(config)
    receipt = evaluate_for_publication(config, "model", draft)
    current = config.save("model", {"expected_revision": draft["revision"], "name": "另一个页面"}, draft["id"])
    with pytest.raises(StoreConflict):
        config.publish("model", draft["id"], draft["revision"], evaluation_id=receipt["id"])
    assert config.store.get("model", draft["id"])["current_version"] is None
    assert current["name"] == "另一个页面"


def test_release_failure_cannot_be_canceled_by_resource_savings(config):
    first = publish_with_quality(config, "model", model(config))
    draft = config.save("model", {"expected_revision": first["revision"], "model": "second"}, first["id"])
    failed = synthetic_quality_run(config, "model", draft, failure=True)
    failed["usage"].update(input_tokens=1, output_tokens=1)
    config.store.save("run", failed["id"], failed)
    baseline = synthetic_quality_run(config, "model", draft, version_id=first["current_version"])
    receipt = QualityService(config).evaluate("model", first["id"], {"expected_revision": draft["revision"], "candidate_run_ids": [failed["id"]], "baseline_run_ids": [baseline["id"]]})
    assert not receipt["eligible"] and "candidate_critical_check_failed_or_unknown" in receipt["reasons"]


@pytest.mark.parametrize("field,value", [("input_per_million", -1), ("output_per_million", float("inf")), ("cached_input_per_million", True), ("as_of", "yesterday"), ("currency", "usd"), ("version", "")])
def test_price_metadata_rejects_invalid_or_nonfinite_values(config, field, value):
    pricing = {"input_per_million": 1, "output_per_million": 2, "currency": "USD", "as_of": "2026-10-27", "version": "synthetic/1"}
    pricing[field] = value
    with pytest.raises(ConfigError):
        config.save("model", {"name": "模型", "model": "synthetic", "pricing": pricing})


def test_price_is_optional_and_cache_rate_is_versioned(config):
    draft = model(config)
    assert draft["pricing"] is None
    pricing = {"input_per_million": 1, "output_per_million": 2, "cached_input_per_million": 0.1, "currency": "USD", "as_of": "2026-10-27", "version": "synthetic/1"}
    saved = config.save("model", {"expected_revision": draft["revision"], "pricing": pricing}, draft["id"])
    assert saved["pricing"] == pricing


@pytest.mark.parametrize("path", ["references//a.md", "references/a/../b.md", "references/a.md/", "references/a\x00.md", "references\\a.md"])
def test_skill_reference_paths_have_one_canonical_location(config, path):
    with pytest.raises(ConfigError):
        config.save("skill", {**SKILL, "references": {path: "synthetic"}})


def test_skill_artifact_refuses_symlink_and_preserves_existing_file(config, tmp_path):
    skill = config.save("skill", SKILL)
    snapshot = {"artifact_digest": "synthetic-fixed-digest"}
    outside = tmp_path / "unrelated"
    outside.mkdir()
    marker = outside / "SKILL.md"
    marker.write_text("keep unchanged")
    (config.artifact_dir / snapshot["artifact_digest"]).symlink_to(outside, target_is_directory=True)
    with pytest.raises(ConfigError):
        config._skill_artifact(snapshot, skill)
    assert marker.read_text() == "keep unchanged"
    assert not list(config.artifact_dir.glob(".skill-*"))


def test_mcp_duplicate_names_and_remote_schema_refs_are_rejected(config):
    with pytest.raises(ConfigError):
        config.save("mcp", {"name": "源", "url": "https://source.invalid/mcp", "tools": [{"name": "read"}, {"name": "read"}]})
    with pytest.raises(ConfigError):
        config.save("mcp", {"name": "源", "url": "https://source.invalid/mcp", "tools": [{"name": "read", "inputSchema": {"$ref": "https://unexpected.invalid/schema"}}]})


def test_required_dependency_schema_change_is_blocking_optional_is_warning(config):
    source = legacy_publication(config, "mcp", mcp(config))
    dependency = {"server_id": source["id"], "tool_name": "read", "definition_digest": "old-definition", "required": True}
    skill = config.save("skill", {**SKILL, "dependencies": [dependency]})
    assert config.validate("skill", skill)["valid"] is False
    skill["dependencies"][0]["required"] = False
    check = config.validate("skill", skill)
    assert check["valid"] and check["warnings"]


def test_actual_skill_model_cannot_differ_from_approved_default_combination(config):
    draft = config.save("skill", SKILL)
    run = synthetic_quality_run(config, "skill", draft)
    run["config_snapshot"]["model"]["model"] = "unapproved-other-model"
    config.store.save("run", run["id"], run)
    receipt = QualityService(config).evaluate("skill", draft["id"], {"expected_revision": draft["revision"], "candidate_run_ids": [run["id"]]})
    assert not receipt["eligible"]
    assert next(c for c in receipt["candidate_cases"][0]["checks"] if c["id"] == "actual_frozen_context")["status"] == "failed"


def test_source_snapshot_difference_refuses_new_old_comparison(config):
    first = publish_with_quality(config, "mcp", mcp(config))
    draft = config.save("mcp", {"expected_revision": first["revision"], "timeout_seconds": 31}, first["id"])
    candidate = synthetic_quality_run(config, "mcp", draft)
    baseline = synthetic_quality_run(config, "mcp", draft, version_id=first["current_version"])
    record = candidate["evidence"][0]
    changed = json.dumps({"structuredContent": {"value": 0.2}}).encode()
    Path(record["path"]).write_bytes(changed)
    import hashlib
    record.update(bytes=len(changed), sha256=hashlib.sha256(changed).hexdigest())
    config.store.save("run", candidate["id"], candidate)
    receipt = QualityService(config).evaluate("mcp", draft["id"], {"expected_revision": draft["revision"], "candidate_run_ids": [candidate["id"]], "baseline_run_ids": [baseline["id"]]})
    assert not receipt["eligible"] and "source_snapshot_mismatch" in receipt["reasons"]


@pytest.mark.parametrize("ids", [[{}], [None], ["x", "x"], [""], "wrong-type"])
def test_malformed_run_references_reject_without_internal_type_errors(config, ids):
    draft = model(config)
    with pytest.raises(ConfigError) as error:
        QualityService(config).evaluate("model", draft["id"], {"expected_revision": draft["revision"], "candidate_run_ids": ids})
    assert error.value.code == "quality_runs_invalid"


def test_pipeline_proof_is_required_in_addition_to_configuration_run(config):
    draft = model(config)
    run = synthetic_quality_run(config, "model", draft)
    receipt = QualityService(config).evaluate("model", draft["id"], {"expected_revision": draft["revision"], "candidate_run_ids": [run["id"]]})
    assert not receipt["eligible"] and "pipeline_not_verified" in receipt["reasons"]
    synthetic_pipeline_proof(config)
    receipt = QualityService(config).evaluate("model", draft["id"], {"expected_revision": draft["revision"], "candidate_run_ids": [run["id"]]})
    assert receipt["eligible"]
    proof = config.store.get("quality_pipeline", receipt["pipeline_proof_id"])
    proof["fingerprint"]["implementation_digest"] = "different-implementation"
    config.store.save("quality_pipeline", proof["id"], proof)
    with pytest.raises(ConfigError):
        config.publish("model", draft["id"], draft["revision"], evaluation_id=receipt["id"])


def _fixed_pipeline_result():
    from aiopsstudio.release_baseline import BASELINE_REF, OUT_OF_SCOPE
    root = Path(__file__).resolve().parents[2]
    suite = json.loads((root / "evaluation/m1/suite.json").read_text())
    results, total, passed, unknown = [], 0, 0, 0
    for case in suite["cases"]:
        oracle = json.loads((root / "evaluation/m1" / case["oracle"]).read_text())
        checks = []
        for assertion in oracle["assertions"]:
            status = "unknown" if (case["id"], assertion["id"]) in OUT_OF_SCOPE else "passed"
            checks.append({"id": assertion["id"], "critical": assertion.get("critical", False), "status": status})
            total += 1
            passed += status == "passed"
            unknown += status == "unknown"
        results.append({"case_id": case["id"], "observation_status": "executed", "score": {"checks": checks, "harness_error": False}})
    candidate = {"results": results, "incomparable_cases": [], "comparability": {"synthetic-frozen": True},
                 "totals": {"assertions": {"total": total, "passed": passed, "failed": 0, "unknown": unknown}}}
    baseline = {**copy.deepcopy(candidate), "code_ref": BASELINE_REF}
    comparison = {"comparable": True, "results": [{"case_id": row["case_id"], "critical_regressions": []} for row in results]}
    return candidate, baseline, comparison


def test_independent_pipeline_denominator_unknown_scope_and_regressions_are_preserved():
    from aiopsstudio.release_baseline import grade
    candidate, baseline, comparison = _fixed_pipeline_result()
    result = grade(candidate, baseline, comparison)
    assert result["eligible"] and result["totals"]["assertions"] == {"total": 105, "passed": 102, "failed": 0, "unknown": 3}
    assert len(result["out_of_scope_unknown"]) == 3
    candidate["results"][0]["score"]["checks"][0]["status"] = "failed"
    assert "independent_assertion_failed" in grade(candidate, baseline, comparison)["reasons"]
    candidate["results"][0]["score"]["checks"][0]["status"] = "unknown"
    assert "applicable_assertion_unknown" in grade(candidate, baseline, comparison)["reasons"]
    candidate["results"].pop()
    assert "case_denominator_mismatch" in grade(candidate, baseline, comparison)["reasons"]


def test_pipeline_lock_is_database_owned_across_independent_services(config):
    from aiopsstudio.release_baseline import PipelineQuality
    first, second = PipelineQuality(config), PipelineQuality(config)
    first._claim("first-owner")
    with pytest.raises(ConfigError) as busy:
        second._claim("second-owner")
    assert busy.value.code == "pipeline_busy"
    second._release("wrong-owner")
    assert config.store.get("quality_pipeline_lock", "main")["active"] is True
    first._release("first-owner")
    second._claim("second-owner")
    second._release("second-owner")


def test_pipeline_resource_failure_does_not_start_subprocess_and_releases_lock(config, monkeypatch):
    from aiopsstudio import release_baseline as module
    from types import SimpleNamespace
    launched = []
    monkeypatch.setattr(module.shutil, "disk_usage", lambda _: SimpleNamespace(free=1))
    monkeypatch.setattr(module.subprocess, "Popen", lambda *args, **kwargs: launched.append(True))
    result = module.PipelineQuality(config).run()
    assert result["status"] == "failed" and result["reasons"] == ["pipeline_resource_limit"]
    assert launched == [] and config.store.get("quality_pipeline_lock", "main")["active"] is False


def test_pipeline_controlled_runner_rejects_implementation_drift(config, monkeypatch):
    from aiopsstudio import release_baseline as module
    original = module.fingerprint
    started = original()
    calls = []
    class FakeProcess:
        pid = 100000001
        def __init__(self, command, **kwargs):
            calls.append((command, kwargs))
            output = Path(command[command.index("--output") + 1])
            (output / "candidate").mkdir(parents=True)
            (output / "baseline").mkdir()
            candidate, baseline, comparison = _fixed_pipeline_result()
            candidate["code_sha256"] = started["implementation_digest"]
            (output / "candidate/summary.json").write_text(json.dumps(candidate))
            (output / "baseline/summary.json").write_text(json.dumps(baseline))
            (output / "comparison.json").write_text(json.dumps(comparison))
        def wait(self, timeout):
            return 0
        def poll(self):
            return 0
    monkeypatch.setattr(module.subprocess, "Popen", FakeProcess)
    reads = []
    def changed():
        reads.append(True)
        return started if len(reads) == 1 else {**started, "implementation_digest": "new-code"}
    monkeypatch.setattr(module, "fingerprint", changed)
    record = module.PipelineQuality(config).run()
    assert record["status"] == "completed" and not record["eligible"]
    assert "implementation_changed_during_replay" in record["reasons"]
    command, options = calls[0]
    assert "--live-model-config" not in command and "--case" not in command
    assert options["start_new_session"] is True
    assert "AIOPSSTUDIO_DATABASE_URL" not in options["env"]
    assert not Path(command[command.index("--output") + 1]).exists(), "Bounded private artifacts are cleaned"


def test_pipeline_failure_retains_only_bounded_safe_diagnostic(config, monkeypatch):
    from aiopsstudio import release_baseline as module
    class FailedProcess:
        pid = 100000001
        def __init__(self, command, **kwargs):
            output = Path(command[command.index("--output") + 1])
            case = output / "candidate/normal-alert-attempt-1"
            case.mkdir(parents=True)
            (case / "observation.json").write_text(json.dumps({"harness_error": "application_replay_failed",
                "error_type": "ValueError", "source_secret": "never-retain-this-marker"}))
            kwargs["stdout"].write(b'never-retain-this-marker\n{"error":"evaluation_failed","type":"ValueError"}\n')
        def wait(self, timeout):
            return 2
        def poll(self):
            return 2
    monkeypatch.setattr(module.subprocess, "Popen", FailedProcess)
    service = module.PipelineQuality(config)
    result = service.run()
    diagnostic = result["diagnostic"]
    assert result["status"] == "failed" and result["reasons"] == ["pipeline_runner_failed"]
    assert diagnostic["stage"] == "runner" and diagnostic["runner_exit_code"] == 2
    assert diagnostic["runner_error_types"] == ["ValueError"]
    assert diagnostic["execution_errors"][0]["case_id"] == "normal-alert"
    directory = config.artifact_dir / "quality-diagnostics"
    retained = directory / (result["id"] + ".json")
    assert result["diagnostic_retained"] is True and retained.stat().st_mode & 0o077 == 0
    assert directory.stat().st_mode & 0o077 == 0
    assert "never-retain-this-marker" not in retained.read_text() + json.dumps(result)
    for index in range(10):
        service._retain_failure("pipeline_unit_" + str(index), {"error_type": "ValueError"})
    assert len(list(directory.glob("pipeline_*.json"))) == 8
    assert all(path.stat().st_size <= 65536 for path in directory.iterdir())
    assert config.store.get("quality_pipeline_lock", "main")["active"] is False


def test_pipeline_timeout_retains_diagnostic_and_only_stops_own_group(config, monkeypatch):
    from aiopsstudio import release_baseline as module
    stopped = []
    class TimedOutProcess:
        pid = 100000001
        calls = 0
        def __init__(self, command, **kwargs):
            kwargs["stdout"].write(b'private arbitrary exception text\n')
        def wait(self, timeout):
            self.calls += 1
            if self.calls == 1:
                raise module.subprocess.TimeoutExpired("controlled", timeout)
            return 0
        def poll(self):
            return None
    monkeypatch.setattr(module.subprocess, "Popen", TimedOutProcess)
    monkeypatch.setattr(module.os, "killpg", lambda pid, sig: stopped.append((pid, sig)))
    result = module.PipelineQuality(config).run()
    assert result["reasons"] == ["pipeline_timeout"] and result["diagnostic_retained"] is True
    assert result["diagnostic"]["stage"] == "runner" and result["diagnostic"]["log_bytes"] > 0
    assert stopped == [(TimedOutProcess.pid, module.signal.SIGTERM)]
    assert config.store.get("quality_pipeline_lock", "main")["active"] is False


def test_http_quality_only_accepts_run_ids_and_controlled_pipeline_trigger(config, monkeypatch):
    from fastapi.testclient import TestClient
    from aiopsstudio.main import create_app
    from aiopsstudio.runtime import configure_runtime
    from aiopsstudio.management_quality import configure_quality
    from aiopsstudio import release_baseline
    app = create_app(store=config.store, master_key=Fernet.generate_key(),
        password="synthetic-admin-password", public_origin="http://testserver", artifact_dir=config.artifact_dir, testing=True, enable_runtime=False)
    configure_runtime(app, config.store)  # Registers methods; no worker starts.
    if not any(route.path == "/api/quality/pipeline" for route in app.routes):
        configure_quality(app)
    calls = []
    monkeypatch.setattr(release_baseline.PipelineQuality, "run", lambda *args, **kwargs: calls.append(True))
    with TestClient(app, headers={"Origin": "http://testserver"}) as client:
        assert client.post("/api/login", json={"password": "synthetic-admin-password"}).status_code == 200
        created = client.post("/api/model-configs", json={"name": "synthetic", "model": "synthetic-model"}).json()
        view = client.get(f'/api/model-configs/{created["id"]}/quality')
        assert view.status_code == 200 and {"candidate", "policy", "pipeline", "evaluations"} <= set(view.json())
        forged = client.post(f'/api/model-configs/{created["id"]}/quality', json={"expected_revision": created["revision"], "passed": True})
        assert forged.status_code == 422 and forged.json()["code"] == "quality_payload_invalid"
        upload = client.post("/api/quality/pipeline", json={"eligible": True, "report": "fake"})
        assert upload.status_code == 422 and upload.json()["code"] == "pipeline_payload_invalid"
        assert calls == []
