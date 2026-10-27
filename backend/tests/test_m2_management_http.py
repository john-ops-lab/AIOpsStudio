"""HTTP version selection and actual SDK receipts, using a local mock transport."""
import json
import time

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from aiopsstudio.main import create_app
from aiopsstudio.runtime import Runtime
from aiopsstudio.store import Store
from m1_fixtures import seed_policies
from m2_management_fixtures import legacy_publication, synthetic_pipeline_proof


@pytest.mark.parametrize("candidate_version", [None, "draft"])
def test_http_model_draft_and_formal_sdk_runs_are_comparable(tmp_path, monkeypatch, candidate_version):
    store = Store("sqlite:///" + str(tmp_path / "http-model-quality.sqlite"))
    app = create_app(store=store, testing=True, master_key=Fernet.generate_key(),
        password="synthetic-http-model-password", public_origin="http://testserver",
        artifact_dir=tmp_path / "artifacts")
    config = app.state.config
    previous = config.save("model", {"name": "合成版本选择", "model": "published-model",
        "base_url": "https://model.invalid/v1", "secret_value": "synthetic-only-model-credential"})
    previous = legacy_publication(config, "model", previous)
    current_version = previous["current_version"]
    draft = config.save("model", {"expected_revision": previous["revision"], "model": "candidate-model"}, previous["id"])
    seed_policies(store, model=draft, server={"id": "unused-source", "url": "https://source.invalid/mcp"}, subjects=("admin",))
    # This test's fixed protocol echo has one string argument, unlike the
    # source-query schemas in the unchanged M1 fixture. Register its exact shape.
    policy = store.get("egress_policy", "main")
    policy["version"] = "http-model-protocol-test/1"
    policy["types"]["tool_definition"]["fields"].update({
        "/*/function/parameters/properties/text": "allow",
        "/*/function/parameters/properties/text/type": "allow",
        "/*/function/parameters/properties/text/title": "allow",
        "/*/function/parameters/properties/text/description": "allow"})
    store.save("egress_policy", "main", policy)
    synthetic_pipeline_proof(config)  # Only pipeline scope is a trusted unit fixture.
    sent = []
    original_model = Runtime._model

    async def response(request):
        body = json.loads(request.content)
        sent.append(body)
        after_tool = any(message.get("role") == "tool" for message in body["messages"])
        message = {"role": "assistant", "content": "中文工具返回已经核对。"} if after_tool else {
            "role": "assistant", "content": None, "tool_calls": [{"id": "protocol-call", "type": "function",
                "function": {"name": "aiopsstudio_protocol_echo", "arguments": json.dumps({"text": "中文样本"})}}]}
        return httpx.Response(200, json={"id": "synthetic-sdk-response", "object": "chat.completion", "created": 1,
            "model": body["model"], "choices": [{"index": 0, "message": message,
                "finish_reason": "stop" if after_tool else "tool_calls"}],
            "usage": {"prompt_tokens": 30, "completion_tokens": 12, "total_tokens": 42,
                "prompt_tokens_details": {"cached_tokens": 0}}})

    def model(runtime, selected, **kwargs):
        instance = original_model(runtime, selected, **kwargs)
        # Retain the application's SDK and real pre-dispatch policy hooks.
        instance.root_async_client._client._transport = httpx.MockTransport(response)
        return instance

    monkeypatch.setattr(Runtime, "_model", model)
    try:
        with TestClient(app, headers={"Origin": "http://testserver"}) as client:
            assert client.post("/api/login", json={"password": "synthetic-http-model-password"}).status_code == 200

            def execute(version, key):
                payload = {"expected_revision": draft["revision"], "client_request_id": key}
                if version is not None:
                    payload["config_version"] = version
                result = client.post(f"/api/model-configs/{draft['id']}/test", json=payload)
                assert result.status_code == 200, result.text
                rid = result.json()["id"]
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    saved = store.get("run", rid)
                    if saved["status"] in {"succeeded", "partial", "failed", "cancelled"}:
                        assert saved["status"] == "succeeded", saved.get("error")
                        return saved
                    time.sleep(.01)
                raise AssertionError("Bounded synthetic SDK run did not finish")

            candidate = execute(candidate_version, "candidate-first-attempt")
            baseline = execute(current_version, "baseline-first-attempt")
            assert candidate["config_snapshot"]["model"]["model"] == "candidate-model"
            assert candidate["config_snapshot"]["model"].get("version_id") is None
            assert baseline["config_snapshot"]["model"]["model"] == "published-model"
            assert baseline["config_snapshot"]["model"]["version_id"] == current_version
            assert candidate["quality_binding"] != baseline["quality_binding"]
            assert candidate["_request"]["config_version"] == "draft"
            assert "model_snapshot" not in candidate["_request"] and "model_snapshot" not in baseline["_request"]
            assert [body["model"] for body in sent] == ["candidate-model"] * 2 + ["published-model"] * 2
            for run in (candidate, baseline):
                assert run["usage"]["model_http_request_count"] == 2
                assert run["usage"]["token_usage_known"] is True
                assert len([i for i in store.list("invocation") if i.get("run_id") == run["id"] and i.get("status") == "succeeded"]) == 2
            evaluation = client.post(f"/api/model-configs/{draft['id']}/quality", json={
                "expected_revision": draft["revision"], "candidate_run_ids": [candidate["id"]], "baseline_run_ids": [baseline["id"]]})
            assert evaluation.status_code == 200, evaluation.text
            quality = evaluation.json()
            assert quality["eligible"] is True, quality["reasons"]
            assert quality["candidate_cases"][0]["input_digest"] == quality["baseline_cases"][0]["input_digest"]
            assert quality["comparison"][0]["critical_regressions"] == []
            missing = client.post(f"/api/model-configs/{draft['id']}/test", json={
                "config_version": "missing-version", "client_request_id": "invalid-version"})
            assert missing.status_code == 422 and len(sent) == 4
            submitted = []
            async def forbidden_submit(*args, **kwargs):
                submitted.append(True)
                raise AssertionError("Stale revision must fail before submission")
            monkeypatch.setattr(app.state.runtime, "submit", forbidden_submit)
            stale = client.post(f"/api/model-configs/{draft['id']}/test", json={
                "expected_revision": draft["revision"] - 1, "config_version": "draft", "client_request_id": "stale-revision"})
            assert stale.status_code == 409 and submitted == [] and len(sent) == 4
    finally:
        store.close()
