"""Release-review regressions use synthetic server receipts, never model/network I/O."""
import copy
from concurrent.futures import ThreadPoolExecutor
import threading

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import pytest

from aiopsstudio.config import ConfigError
from aiopsstudio.management_quality import QualityService
from aiopsstudio.main import create_app
from aiopsstudio.store import StoreConflict
from m2_management_fixtures import evaluate_for_publication, publish_with_quality, synthetic_quality_run, synthetic_pipeline_proof
from test_m2_management import config, model


def test_display_and_unused_fields_cannot_hide_first_fixed_protocol_failure(config):
    draft = model(config)
    synthetic_pipeline_proof(config)
    failed = synthetic_quality_run(config, "model", draft, failure=True, input_marker="old-unused")
    passed = synthetic_quality_run(config, "model", draft, input_marker="new-unused")
    passed["_request"].update(title="a new display label", arbitrary_unconsumed_field="new")
    config.store.save("run", passed["id"], passed)
    result = QualityService(config).evaluate("model", draft["id"], {
        "expected_revision": draft["revision"], "candidate_run_ids": [passed["id"]]})
    assert result["eligible"] is False
    assert next(c for c in result["candidate_cases"][0]["checks"] if c["id"] == "original_first_attempt")["status"] == "failed"
    assert config.store.get("run", failed["id"])["report"] == {}


def test_explicit_retry_cannot_be_promoted_even_if_original_row_is_gone(config):
    draft = model(config)
    synthetic_pipeline_proof(config)
    retry = synthetic_quality_run(config, "model", draft)
    retry.update(retry_of="deleted-original", retry_usage_reference="deleted-original", retry_budget_semantics="new_attempt_independent_budget")
    config.store.save("run", retry["id"], retry)
    result = QualityService(config).evaluate("model", draft["id"], {
        "expected_revision": draft["revision"], "candidate_run_ids": [retry["id"]]})
    assert result["eligible"] is False
    assert next(c for c in result["candidate_cases"][0]["checks"] if c["id"] == "original_first_attempt")["status"] == "failed"


@pytest.mark.parametrize("same_content", [False, True])
def test_rollback_current_baseline_invalidates_old_comparison(config, same_content):
    first = publish_with_quality(config, "model", model(config))
    changed = config.save("model", {"expected_revision": first["revision"], "model": first["model"] if same_content else "second-model"}, first["id"])
    second = publish_with_quality(config, "model", changed)
    draft = config.save("model", {"expected_revision": second["revision"], "model": "candidate-third-model"}, first["id"])
    receipt = evaluate_for_publication(config, "model", draft)
    assert receipt["eligible"]
    reverted = config.rollback("model", draft["id"], first["current_version"])
    with pytest.raises(ConfigError):
        config.publish("model", draft["id"], reverted["revision"], evaluation_id=receipt["id"])
    assert config.store.get("model", draft["id"])["current_version"] == first["current_version"]
    fresh = evaluate_for_publication(config, "model", reverted)
    assert fresh["eligible"]
    assert config.publish("model", draft["id"], reverted["revision"], evaluation_id=fresh["id"])["current_version"] != first["current_version"]


def test_first_publication_receipt_cannot_be_reused_after_a_baseline_exists(config):
    draft = model(config)
    receipt = evaluate_for_publication(config, "model", draft)
    published = config.publish("model", draft["id"], draft["revision"], evaluation_id=receipt["id"])
    with pytest.raises(ConfigError):
        config.publish("model", draft["id"], published["revision"], evaluation_id=receipt["id"])


def test_case_identity_uses_execution_content_not_tracking_ids(config):
    quality = QualityService(config)
    protocol = {"purpose": "model_test", "inputs": {"ignored": 1}, "_request": {"title": "one"}}
    assert quality._input_key(protocol) == quality._input_key({**protocol, "inputs": {"ignored": 2}, "_request": {"title": "two", "extra": "ignored"}})
    tool = {"purpose": "tool_test", "_request": {"tool_name": "read", "arguments": {"value": 1}}}
    assert quality._input_key(tool) != quality._input_key({**tool, "_request": {"tool_name": "read", "arguments": {"value": 2}}})
    skill = {"purpose": "skill_test", "inputs": {"business_id": "source-one"},
        "messages": [{"id": "tracking-one", "role": "user", "content": "same question", "status": "processed"}],
        "analysis_context": {"captured_at": "one", "identity": {"canonical_key": "service-one"}, "windows": {"fault": {"start": "one"}}},
        "user_materials": [{"attachment_id": "one", "sha256": "same-content", "text": "source text"}],
        "history_context": {"previous_run_id": "one", "rounds": [{"run_id": "one", "window": {"start": "one"}, "report_summary": "same report", "user_messages": [{"id": "one", "content": "same"}]}]}}
    tracked = copy.deepcopy(skill)
    tracked["messages"][0].update(id="tracking-two", status="completed")
    tracked["analysis_context"]["captured_at"] = "two"
    tracked["user_materials"][0]["attachment_id"] = "two"
    tracked["history_context"]["previous_run_id"] = "two"
    tracked["history_context"]["rounds"][0]["run_id"] = "two"
    tracked["history_context"]["rounds"][0]["user_messages"][0]["id"] = "two"
    assert quality._input_key(skill) == quality._input_key(tracked)
    for section in ("message", "source", "window", "material", "history"):
        changed = copy.deepcopy(tracked)
        if section == "message": changed["messages"][0]["content"] = "different"
        elif section == "source": changed["inputs"]["business_id"] = "source-two"
        elif section == "window": changed["analysis_context"]["windows"]["fault"]["start"] = "two"
        elif section == "material": changed["user_materials"][0]["sha256"] = "different"
        else: changed["history_context"]["rounds"][0]["report_summary"] = "different"
        assert quality._input_key(skill) != quality._input_key(changed), section


def test_slow_quality_io_does_not_hold_store_lock_or_block_http_loop(tmp_path, monkeypatch):
    from types import SimpleNamespace
    app = create_app(database_url="sqlite:///" + str(tmp_path / "responsive.db"), master_key=Fernet.generate_key(),
        password="synthetic-review-password", public_origin="http://testserver", artifact_dir=tmp_path / "artifacts", testing=True, enable_runtime=False)
    config = app.state.config
    draft = model(config)
    synthetic_pipeline_proof(config)
    run = synthetic_quality_run(config, "model", draft)
    app.state.runtime = SimpleNamespace(require_owned=lambda rid, owner: config.store.get("run", rid))
    entered, release = threading.Event(), threading.Event()
    original = QualityService._score
    def slow(self, *args, **kwargs):
        entered.set()
        assert release.wait(3), "bounded test release"
        return original(self, *args, **kwargs)
    monkeypatch.setattr(QualityService, "_score", slow)
    try:
        with TestClient(app, headers={"Origin": "http://testserver"}) as client, ThreadPoolExecutor(max_workers=2) as pool:
            assert client.post("/api/login", json={"password": "synthetic-review-password"}).status_code == 200
            pending = pool.submit(client.post, f"/api/model-configs/{draft['id']}/quality", json={"expected_revision": draft["revision"], "candidate_run_ids": [run["id"]]})
            try:
                assert entered.wait(2)
                # This endpoint reads the same Store; merely a coroutine ticker
                # would miss a long worker-thread-held RLock.
                probe = pool.submit(client.get, "/healthz")
                assert probe.result(timeout=.6).status_code == 200
            finally:
                release.set()
            assert pending.result(timeout=3).status_code == 200
    finally:
        config.store.close()


def test_evaluation_rejects_receipt_metadata_changed_during_scoring(config, monkeypatch):
    draft = model(config)
    synthetic_pipeline_proof(config)
    run = synthetic_quality_run(config, "model", draft)
    quality = QualityService(config)
    original = quality._score
    def mutate(*args, **kwargs):
        result = original(*args, **kwargs)
        current = config.store.get("run", run["id"])
        current["report"]["analysis"] = "changed after scoring"
        config.store.save("run", run["id"], current)
        return result
    monkeypatch.setattr(quality, "_score", mutate)
    with pytest.raises(StoreConflict):
        quality.evaluate("model", draft["id"], {"expected_revision": draft["revision"], "candidate_run_ids": [run["id"]]})


@pytest.mark.parametrize("channel", ["formal", "experimental"])
def test_slow_publish_preparation_keeps_store_and_http_responsive(tmp_path, monkeypatch, channel):
    from aiopsstudio.config import ConfigService
    from test_config import SKILL
    app = create_app(database_url="sqlite:///" + str(tmp_path / "publish-responsive.db"), master_key=Fernet.generate_key(),
        password="synthetic-review-password", public_origin="http://testserver", artifact_dir=tmp_path / "artifacts", testing=True, enable_runtime=False)
    config = app.state.config
    kind = "model" if channel == "formal" else "skill"
    draft = model(config) if kind == "model" else config.save("skill", SKILL)
    receipt = evaluate_for_publication(config, kind, draft) if channel == "formal" else None
    entered, release = threading.Event(), threading.Event()
    cls, name = (QualityService, "require_eligible") if channel == "formal" else (ConfigService, "_prepare_skill_artifact")
    original = getattr(cls, name)
    def slow(self, *args, **kwargs):
        entered.set()
        assert release.wait(3), "bounded test release"
        return original(self, *args, **kwargs)
    monkeypatch.setattr(cls, name, slow)
    route = "model-configs" if kind == "model" else "skills"
    try:
        with TestClient(app, headers={"Origin": "http://testserver"}) as client, ThreadPoolExecutor(max_workers=2) as pool:
            assert client.post("/api/login", json={"password": "synthetic-review-password"}).status_code == 200
            payload = {"expected_revision": draft["revision"], "channel": channel}
            if receipt: payload["evaluation_id"] = receipt["id"]
            pending = pool.submit(client.post, f"/api/{route}/{draft['id']}/publish", json=payload)
            try:
                assert entered.wait(2)
                assert pool.submit(client.get, "/healthz").result(timeout=.6).status_code == 200
            finally:
                release.set()
            result = pending.result(timeout=3)
            assert result.status_code == 200, result.text
    finally:
        config.store.close()


@pytest.mark.parametrize("changed", ["record", "file"])
def test_publish_rejects_evidence_changed_after_preparation(config, monkeypatch, changed):
    from test_m2_management import mcp
    draft = mcp(config)
    receipt = evaluate_for_publication(config, "mcp", draft)
    run = config.store.get("run", receipt["candidate_cases"][0]["run_id"])
    original = QualityService.prepare_eligible
    def alter(self, *args, **kwargs):
        prepared = original(self, *args, **kwargs)
        if changed == "file":
            from pathlib import Path
            path = Path(run["evidence"][0]["path"])
            path.write_bytes(b"x" * path.stat().st_size)
        else:
            current = config.store.get("run", run["id"])
            current["usage"]["source_request_count"] += 1
            config.store.save("run", run["id"], current)
        return prepared
    monkeypatch.setattr(QualityService, "prepare_eligible", alter)
    with pytest.raises(ConfigError):
        config.publish("mcp", draft["id"], draft["revision"], evaluation_id=receipt["id"])
    assert config.store.get("mcp", draft["id"])["current_version"] is None


def test_publish_rechecks_current_baseline_after_expensive_preparation(config, monkeypatch):
    first = publish_with_quality(config, "model", model(config))
    changed = config.save("model", {"expected_revision": first["revision"], "model": "second-model"}, first["id"])
    second = publish_with_quality(config, "model", changed)
    draft = config.save("model", {"expected_revision": second["revision"], "model": "candidate-third-model"}, first["id"])
    receipt = evaluate_for_publication(config, "model", draft)
    original = QualityService.prepare_eligible
    def rollback(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        config.rollback("model", draft["id"], first["current_version"])
        return result
    monkeypatch.setattr(QualityService, "prepare_eligible", rollback)
    with pytest.raises(StoreConflict):
        config.publish("model", draft["id"], draft["revision"], evaluation_id=receipt["id"])
    assert config.store.get("model", draft["id"])["current_version"] == first["current_version"]


def test_untrusted_prepared_receipts_are_not_accepted(config):
    draft = model(config)
    with pytest.raises(ConfigError):
        QualityService(config).verify_prepared({"eligible": True}, "model", draft["id"], "not-server-prepared")


def test_skill_file_activation_rolls_back_when_database_save_fails(config, monkeypatch):
    from test_config import SKILL
    draft = config.save("skill", SKILL)
    original = config.store.save
    def failed(kind, key, data, *args, **kwargs):
        if kind == "skill" and data.get("versions"):
            raise StoreConflict("synthetic failure after immutable directory activation")
        return original(kind, key, data, *args, **kwargs)
    monkeypatch.setattr(config.store, "save", failed)
    with pytest.raises(StoreConflict):
        config.publish("skill", draft["id"], draft["revision"], channel="experimental")
    assert config.store.get("skill", draft["id"])["versions"] == []
    assert list(config.artifact_dir.iterdir()) == []


@pytest.mark.parametrize("target", ["quality_files", "skill_files"])
def test_final_metadata_scan_never_holds_store_lock(tmp_path, monkeypatch, target):
    """Delay the final scan, not the earlier proof or package preparation."""
    from aiopsstudio.config import ConfigService
    from test_config import SKILL
    app = create_app(database_url="sqlite:///" + str(tmp_path / "final-scan.db"), master_key=Fernet.generate_key(),
        password="synthetic-review-password", public_origin="http://testserver", artifact_dir=tmp_path / "artifacts", testing=True, enable_runtime=False)
    config = app.state.config
    formal = target == "quality_files"
    kind = "model" if formal else "skill"
    draft = model(config) if formal else config.save("skill", SKILL)
    receipt = evaluate_for_publication(config, kind, draft) if formal else None
    cls, name, pause_at = (QualityService, "_publication_files", 3) if formal else (ConfigService, "_artifact_state", 2)
    original = getattr(cls, name)
    entered, release = threading.Event(), threading.Event()
    calls = []
    def slow_final(self, *args, **kwargs):
        calls.append(True)
        if len(calls) == pause_at:
            entered.set()
            assert release.wait(3), "bounded final-scan test release"
        return original(self, *args, **kwargs)
    monkeypatch.setattr(cls, name, slow_final)
    route = "model-configs" if formal else "skills"
    try:
        with TestClient(app, headers={"Origin": "http://testserver"}) as client, ThreadPoolExecutor(max_workers=2) as pool:
            assert client.post("/api/login", json={"password": "synthetic-review-password"}).status_code == 200
            payload = {"expected_revision": draft["revision"], "channel": "formal" if formal else "experimental"}
            if receipt: payload["evaluation_id"] = receipt["id"]
            pending = pool.submit(client.post, f"/api/{route}/{draft['id']}/publish", json=payload)
            try:
                assert entered.wait(2), "the final scan was not exercised"
                assert pool.submit(client.get, "/healthz").result(timeout=.6).status_code == 200
            finally:
                release.set()
            result = pending.result(timeout=3)
            assert result.status_code == 200, result.text
            assert len(calls) == pause_at, "no later filesystem scan is allowed inside the commit"
    finally:
        config.store.close()


@pytest.mark.parametrize("channel", ["formal", "experimental"])
def test_waiting_for_store_expires_publication_ticket(config, monkeypatch, channel):
    from types import SimpleNamespace
    from aiopsstudio import config as config_module, management_quality as quality_module
    from test_config import SKILL
    kind = "model" if channel == "formal" else "skill"
    draft = model(config) if kind == "model" else config.save("skill", SKILL)
    receipt = evaluate_for_publication(config, kind, draft) if channel == "formal" else None
    clock = {"now": 100.0}
    source = SimpleNamespace(monotonic=lambda: clock["now"])
    monkeypatch.setattr(config_module, "time", source)
    monkeypatch.setattr(quality_module, "time", source)
    original = config.store.atomic
    def wait_for_lock(callback):
        if callback.__name__ == "action":
            clock["now"] += quality_module.PUBLICATION_TICKET_SECONDS + 1
        return original(callback)
    monkeypatch.setattr(config.store, "atomic", wait_for_lock)
    with pytest.raises(ConfigError) as error:
        config.publish(kind, draft["id"], draft["revision"], channel=channel,
                       evaluation_id=receipt["id"] if receipt else None)
    assert error.value.code == "quality_precheck_expired"
    assert config.store.get(kind, draft["id"])["versions"] == []
    assert not list(config.artifact_dir.glob(".skill-*"))


def test_final_scan_time_is_not_counted_as_store_waiting_time(config, monkeypatch):
    from types import SimpleNamespace
    from aiopsstudio import management_quality as module
    draft = model(config)
    receipt = evaluate_for_publication(config, "model", draft)
    clock = {"now": 100.0}
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock["now"]))
    original = QualityService._publication_files
    calls = []
    def slow(self, *args, **kwargs):
        calls.append(True)
        if len(calls) == 3:
            clock["now"] += 100
        return original(self, *args, **kwargs)
    monkeypatch.setattr(QualityService, "_publication_files", slow)
    value = config.publish("model", draft["id"], draft["revision"], evaluation_id=receipt["id"])
    assert value["current_version"] and len(calls) == 3


def test_recursive_cleanup_after_publish_rollback_runs_outside_store(tmp_path, monkeypatch):
    from aiopsstudio import config as module
    from test_config import SKILL
    app = create_app(database_url="sqlite:///" + str(tmp_path / "rollback-cleanup.db"), master_key=Fernet.generate_key(),
        password="synthetic-review-password", public_origin="http://testserver", artifact_dir=tmp_path / "artifacts", testing=True, enable_runtime=False)
    config = app.state.config
    draft = config.save("skill", SKILL)
    before = set(config.artifact_dir.iterdir())  # create_app owns its empty attachments directory.
    original_save, original_cleanup = config.store.save, module.shutil.rmtree
    entered, release = threading.Event(), threading.Event()
    def fail(kind, key, data, *args, **kwargs):
        if kind == "skill" and data.get("versions"):
            raise StoreConflict("synthetic commit failure")
        return original_save(kind, key, data, *args, **kwargs)
    def cleanup(*args, **kwargs):
        entered.set()
        assert release.wait(3), "bounded cleanup test release"
        assert config.store._depth == 0
        return original_cleanup(*args, **kwargs)
    monkeypatch.setattr(config.store, "save", fail)
    monkeypatch.setattr(module.shutil, "rmtree", cleanup)
    try:
        with TestClient(app, headers={"Origin": "http://testserver"}) as client, ThreadPoolExecutor(max_workers=2) as pool:
            assert client.post("/api/login", json={"password": "synthetic-review-password"}).status_code == 200
            pending = pool.submit(client.post, f"/api/skills/{draft['id']}/publish", json={"expected_revision": draft["revision"], "channel": "experimental"})
            try:
                assert entered.wait(2)
                assert pool.submit(client.get, "/healthz").result(timeout=.6).status_code == 200
            finally:
                release.set()
            assert pending.result(timeout=3).status_code == 409
            assert config.store.get("skill", draft["id"])["versions"] == []
            assert set(config.artifact_dir.iterdir()) == before
    finally:
        config.store.close()
