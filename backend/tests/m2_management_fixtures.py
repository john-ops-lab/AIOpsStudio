"""Explicit synthetic publication fixtures. No production gate bypass or network.

Legacy records model an installation created before M2. The second helper seeds
server-owned synthetic execution records and uses the real scorer for tests of
new publication; its transport evidence is synthetic, never live acceptance.
"""
import copy
import hashlib
import json
from pathlib import Path
import secrets
from aiopsstudio.config import digest
from aiopsstudio.management_quality import QualityService, PURPOSES
from aiopsstudio.access_policy import PolicyService


def synthetic_pipeline_proof(config):
    """A trusted unit fixture, not an uploaded production pipeline receipt."""
    from aiopsstudio.release_baseline import fingerprint
    fixed = fingerprint()
    return config.store.save("quality_pipeline", "synthetic-unit-pipeline", {
        "fingerprint": fixed, "candidate_code_digest": fixed["implementation_digest"], "status": "completed", "eligible": True,
        "provenance": "controlled-r01-runner/1", "created_at": "2026-10-20T00:00:00Z", "synthetic_unit_fixture": True})


def legacy_publication(config, kind, item):
    snapshot = {k: copy.deepcopy(v) for k, v in item.items() if k not in {"versions", "validation", "current_version"}}
    snapshot.update(enabled=True, blocked=False, status="published")
    version = {"id": "legacy-synthetic-" + secrets.token_hex(6), "snapshot": snapshot, "digest": digest(snapshot), "created_at": item["created_at"], "source_revision": item["revision"]}
    record = copy.deepcopy(item)
    record.setdefault("versions", []).append(version)
    record.update(current_version=version["id"], enabled=True, blocked=False, status="published")
    return config.store.save(kind, item["id"], record, item["revision"])


def synthetic_quality_run(config, kind, item, *, version_id=None, owner="admin", failure=False, input_marker="fixed"):
    if kind == "skill" and not (config.store.get("settings", "main") or {}).get("default_model_id"):
        # Synthetic server-owned fixture, not a callable production model.
        model = {"id": "synthetic-quality-model", "name": "synthetic", "provider": "openai", "model": "synthetic",
                 "base_url": "https://model.invalid/v1", "secret_id": None, "max_tokens": 2048,
                 "temperature": None, "extra_body": {}, "default_headers": {}, "pricing": None,
                 "created_at": "2026-10-20T00:00:00Z", "revision": 0}
        model = legacy_publication(config, "model", model)
        settings = config.store.get("settings", "main")
        config.store.save("settings", "main", {**settings, "default_model_id": model["id"]}, settings["revision"])
    quality = QualityService(config)
    binding = quality.candidate_binding(kind, item["id"], version_id=version_id)
    for run in sorted(config.store.list("run"), key=lambda x: (x.get("created_at", ""), x["id"])):
        if run.get("quality_binding") == binding and run.get("inputs") == {"marker": input_marker}:
            return run
    snapshot = config.published(item, version_id) if version_id else copy.deepcopy(item)
    rid = "synthetic-quality-run-" + secrets.token_hex(8)
    run = {"id": rid, "owner_id": owner, "purpose": PURPOSES[kind], "quality_binding": binding,
        "status": "succeeded", "started_at": "2026-10-20T01:00:00Z", "completed_at": "2026-10-20T01:00:01Z",
        "config_snapshot": {"mcps": [snapshot]} if kind == "mcp" else {kind: snapshot},
        "_request": {"fixed_input": input_marker}, "inputs": {"marker": input_marker}, "error": None,
        "usage": {"model_http_request_count": 0 if kind == "mcp" else 2, "tool_call_count": 1,
            "source_request_count": 0 if kind == "model" else 1, "token_usage_known": True, "input_tokens": 100, "output_tokens": 20},
        "evidence": []}
    run["config_snapshot"]["settings"] = copy.deepcopy(config.store.get("settings", "main"))
    run["policy_snapshot"] = PolicyService(config.store).snapshot()
    if kind == "skill":
        model = config.store.get("model", run["config_snapshot"]["settings"]["default_model_id"])
        run["config_snapshot"]["model"] = {**config.published(model), "version_id": model["current_version"]}
    if kind == "model":
        run["report"] = {"sample_only": True, "tool_result": "AIOpsStudio中文工具协议样本已返回", "analysis": "合成中文协议说明"}
        config.store.event(rid, "model_response", {"tool_calls": [{"name": "aiopsstudio_protocol_echo", "args": {"text": "中文样本"}}]})
    else:
        eid = "synthetic-evidence-" + secrets.token_hex(8)
        data = json.dumps({"structuredContent": {"value": 0.18}}, ensure_ascii=False).encode()
        folder = config.artifact_dir / "evidence" / rid
        folder.mkdir(parents=True)
        path = folder / (eid + ".json")
        path.write_bytes(data)
        source_id = item["id"] if kind == "mcp" else "synthetic-fixture-source"
        run["evidence"] = [{"id": eid, "path": str(path), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "status": "saved", "truncated": False, "tool_name": "read"}]
        config.store.save("invocation", rid + "-source", {"run_id": rid, "source": source_id, "status": "succeeded"})
        run["report"] = {"model_generated": True, "analysis": "合成事实报告",
            "reference_validation": {"cited_ids": [eid], "invalid_ids": []},
            "grounding": {"validation": {"structured_claim_count": 1, "verified_claim_count": 1, "unverified_claim_count": 0}}}
        if kind == "skill" and snapshot.get("references"):
            config.store.save("material_read", rid + "-read", {"run_id": rid, "consumer": "model", "result": {"data": "synthetic",
                "resource_type": "skill_reference", "anchor_id": "frag_synthetic_quality",
                "material": {"skill_id": item["id"], "version_id": version_id or "draft", "path": next(iter(snapshot["references"]))}}})
    for i in range(run["usage"]["model_http_request_count"]):
        config.store.save("invocation", rid + "-model-" + str(i), {"run_id": rid, "source": "model", "status": "succeeded"})
        config.store.event(rid, "model_egress_checked", {"typed": True})
    if failure:
        run["report"] = {}
    return config.store.save("run", rid, run)


def evaluate_for_publication(config, kind, item):
    synthetic_pipeline_proof(config)
    candidate = synthetic_quality_run(config, kind, item)
    baseline = synthetic_quality_run(config, kind, item, version_id=item["current_version"]) if item.get("current_version") else None
    return QualityService(config).evaluate(kind, item["id"], {"expected_revision": item["revision"],
        "candidate_run_ids": [candidate["id"]], "baseline_run_ids": [baseline["id"]] if baseline else []})


def publish_with_quality(config, kind, item):
    evaluation = evaluate_for_publication(config, kind, item)
    assert evaluation["eligible"], evaluation["reasons"]
    return config.publish(kind, item["id"], item["revision"], evaluation_id=evaluation["id"])
