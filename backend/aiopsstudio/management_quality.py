"""Server-scored publication evidence; clients submit run references, never scores."""
from __future__ import annotations

import copy
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import secrets

from fastapi import Request

from .config import ConfigError, digest, redact
from .identity import resource_owner
from .store import StoreConflict, utcnow

SUITE_VERSION = "management-release/1"
SCORER_VERSION = "management-run-scorer/1"
THRESHOLDS = {"critical_failed": 0, "critical_unknown": 0, "critical_regressions": 0,
              "max_token_ratio": 3.0, "max_duration_ratio": 5.0}
FIELDS = {
    "skill": ("id", "name", "display_name", "description", "content", "references", "input_schema", "dependencies"),
    "model": ("id", "name", "provider", "model", "base_url", "secret_id", "temperature", "max_tokens", "extra_body", "default_headers", "pricing"),
    "mcp": ("id", "name", "url", "auth_type", "secret_id", "auth_header", "timeout_seconds", "allowed_tools", "tools", "transport", "ca_file", "tls_ca_file", "headers", "verify_tls", "max_response_bytes"),
}
PURPOSES = {"skill": "skill_test", "model": "model_test", "mcp": "tool_test"}
TERMINAL = {"succeeded", "partial", "failed", "needs_input", "cancelled"}


def candidate_projection(kind, item):
    result = {key: copy.deepcopy(item.get(key)) for key in FIELDS[kind]}
    if kind == "mcp":
        result["ca_content_digests"] = {}
        for key in ("ca_file", "tls_ca_file"):
            if item.get(key):
                try:
                    path = Path(item[key])
                    result["ca_content_digests"][key] = hashlib.sha256(path.read_bytes()).hexdigest() if path.stat().st_size <= 4 * 1048576 else "invalid"
                except (OSError, TypeError):
                    result["ca_content_digests"][key] = "unavailable"
    return result


def settings_projection(settings):
    return {key: copy.deepcopy(value) for key, value in settings.items() if key not in {"revision", "updated_at", "created_at", "artifact_dir"}}


def implementation_digest():
    root = Path(__file__).parent
    return digest({"backend/aiopsstudio/" + path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*") if path.is_file() and "__pycache__" not in path.parts and "evaluation" not in path.relative_to(root).parts})


def policy():
    return {"suite_version": SUITE_VERSION, "scorer_version": SCORER_VERSION,
            "scorer_digest": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "thresholds": copy.deepcopy(THRESHOLDS),
            "required_cases": {"model": ["actual_chinese_tool_protocol"],
                               "skill": ["actual_grounded_report"], "mcp": ["actual_readonly_tool_contract"]},
            "scope": "fixed_protocol_and_structured_claims; not business_or_root_cause_acceptance"}


class QualityService:
    def __init__(self, config):
        self.config, self.store = config, config.store

    def candidate_binding(self, kind, id, *, version_id=None):
        if kind not in FIELDS:
            raise ConfigError("不支持的配置类型", "quality_kind_invalid")
        item = self.config.require(kind, id)
        snapshot = self.config.published(item, version_id) if version_id else item
        settings = settings_projection(self.store.get("settings", "main") or {})
        related = {}
        for category in ("model", "mcp", "skill"):
            entries = []
            for entity in self.store.list(category):
                if category == kind and entity["id"] == id:
                    continue
                # Unpublished unrelated drafts cannot change an executable combination.
                if entity.get("current_version"):
                    entries.append({"id": entity["id"], "current_version": entity["current_version"],
                        "enabled": entity.get("enabled"), "blocked": entity.get("blocked"),
                        "digest": digest(candidate_projection(category, self.config.published(entity)))})
            related[category] = sorted(entries, key=lambda x: x["id"])
        p = policy()
        return {"schema": "management-candidate/1", "kind": kind, "entity_id": id,
                "candidate_digest": digest(candidate_projection(kind, snapshot)),
                "context_digest": digest({"settings": settings, "related": related,
                    "source_policy": self.store.get("access_policy", "main"),
                    "egress_policy": self.store.get("egress_policy", "main")}),
                "implementation_digest": implementation_digest(), "suite_version": p["suite_version"],
                "scorer_digest": p["scorer_digest"]}

    def _actual_context(self, kind, run, candidate):
        from .access_policy import PolicyService
        snapshot = run.get("config_snapshot") or {}
        if settings_projection(snapshot.get("settings") or {}) != settings_projection(self.store.get("settings", "main") or {}):
            return False
        if run.get("policy_snapshot") != PolicyService(self.store).snapshot():
            return False
        if kind != "skill":
            return True
        settings = self.store.get("settings", "main") or {}
        model = self.store.get("model", settings.get("default_model_id") or "")
        if not model or not model.get("current_version") or not model.get("enabled") or model.get("blocked"):
            return False
        actual_model = snapshot.get("model") or {}
        if (actual_model.get("id") != model["id"] or actual_model.get("version_id") != model["current_version"]
                or candidate_projection("model", actual_model) != candidate_projection("model", self.config.published(model))):
            return False
        dependencies = copy.deepcopy(candidate.get("dependencies") or [])
        cmdb = next((s for s in self.store.list("skill") if s.get("name") == "cmdb-query" and s.get("current_version") and s["id"] != candidate.get("id")), None)
        if cmdb:
            if not cmdb.get("enabled") or cmdb.get("blocked"):
                return False
            common = self.config.published(cmdb)
            actual_common = snapshot.get("cmdb_skill") or {}
            if actual_common.get("version_id") != cmdb["current_version"] or candidate_projection("skill", actual_common) != candidate_projection("skill", common):
                return False
            dependencies += common.get("dependencies") or []
        expected = {}
        for server in self.store.list("mcp"):
            if not server.get("current_version") or not server.get("enabled") or server.get("blocked"):
                continue
            deps = [d for d in dependencies if d.get("server_id") == server["id"]]
            if not deps:
                continue
            wanted = set()
            fixed = self.config.published(server)
            for dep in deps:
                name = dep.get("tool_name") or dep.get("original_tool_name")
                wanted.update([name] if name else fixed.get("allowed_tools", []))
            fixed["allowed_tools"] = [name for name in fixed.get("allowed_tools", []) if name in wanted]
            if fixed["allowed_tools"]:
                expected[server["id"]] = fixed
        actual = {s.get("id"): s for s in snapshot.get("mcps", [])}
        return set(actual) == set(expected) and all(actual[id].get("version_id") == self.store.get("mcp", id)["current_version"]
            and candidate_projection("mcp", actual[id]) == candidate_projection("mcp", fixed) for id, fixed in expected.items())

    def view(self, kind, id, *, actor="admin"):
        binding = self.candidate_binding(kind, id)
        records = [self.public(x) for x in self.store.list("quality_evaluation")
                   if x.get("kind") == kind and x.get("entity_id") == id and x.get("owner_id") == actor]
        for record in records:
            record["current"] = record.get("binding") == binding
        from .release_baseline import PipelineQuality
        return {"candidate": binding, "policy": policy(), "pipeline": PipelineQuality(self.config).view(),
            "evaluations": sorted(records, key=lambda x: x.get("created_at", ""), reverse=True)[:30]}

    def _snapshot(self, kind, run, id):
        snapshot = run.get("config_snapshot") or {}
        if kind == "mcp":
            return next((item for item in snapshot.get("mcps", []) if item.get("id") == id), None)
        item = snapshot.get(kind)
        return item if isinstance(item, dict) and item.get("id") == id else None

    def _input_key(self, run):
        request = run.get("_request") or {}
        excluded = {"client_request_id", "owner_id", "model_id", "skill_id", "server_id", "skill_version", "config_version", "quality_binding"}
        # These are server-frozen execution inputs, not a user-submitted score.
        request = {key: value for key, value in request.items() if key not in excluded}
        return digest({"purpose": run.get("purpose"), "request": request,
            "target": run.get("target"), "analysis_start": run.get("analysis_start"),
            "analysis_end": run.get("analysis_end"), "inputs": run.get("inputs", {})})

    def _evidence(self, run):
        hashes, valid = [], True
        root = self.config.artifact_dir.resolve()
        for record in run.get("evidence", []):
            try:
                path = Path(record["path"])
                if path.is_symlink() or not path.resolve().is_relative_to(root) or not path.is_file() or path.stat().st_size > 10 * 1048576:
                    raise ValueError()
                data = path.read_bytes()
                if hashlib.sha256(data).hexdigest() != record["sha256"] or len(data) != record["bytes"]:
                    raise ValueError()
                native = json.loads(data)
                from .material_reading import logical_document
                logical, _ = logical_document(native)
                hashes.append(digest({"tool": record.get("tool_name"), "logical": logical}))
                valid = valid and record.get("status") == "saved" and record.get("truncated") is False
            except (KeyError, ValueError, OSError, TypeError):
                valid = False
        return digest(sorted(hashes)), valid and bool(hashes)

    def _score(self, kind, id, run, expected, actor):
        checks = []
        def check(name, value):
            status = "unknown" if value is None else "passed" if value is True else "failed"
            checks.append({"id": name, "status": status, "critical": True})
        check("terminal_actual_run", run.get("purpose") == PURPOSES[kind] and run.get("status") in {"succeeded", "partial"} and not run.get("error"))
        check("candidate_binding", run.get("quality_binding") == expected)
        snap = self._snapshot(kind, run, id)
        check("actual_config_snapshot", bool(snap) and digest(candidate_projection(kind, snap)) == expected["candidate_digest"])
        check("actual_frozen_context", bool(snap) and self._actual_context(kind, run, snap))
        same = [r for r in self.store.list("run") if resource_owner(r) == actor and r.get("purpose") == PURPOSES[kind]
                and r.get("quality_binding") == expected and self._input_key(r) == self._input_key(run)]
        first = min(same, key=lambda x: (x.get("created_at", ""), x["id"])) if same else None
        check("original_first_attempt", bool(first) and first["id"] == run["id"])
        invocations = [x for x in self.store.list("invocation") if x.get("run_id") == run["id"]]
        model = [x for x in invocations if x.get("source") == "model"]
        source = [x for x in invocations if x.get("source") not in {None, "model"}]
        usage, report = run.get("usage") or {}, run.get("report") or {}
        events = self.store.events(run["id"])
        model_guards = sum(e.get("type") == "model_egress_checked" for e in events)
        check("no_policy_or_credential_failure", not any(str((x.get("error") or {}).get("code", "")).startswith(("policy_", "credential_")) for x in invocations))
        if kind in {"model", "skill"}:
            count = usage.get("model_http_request_count", 0)
            check("actual_model_dispatch", count >= (2 if kind == "model" else 1) and len(model) == count and all(x.get("status") == "succeeded" for x in model))
            check("actual_model_guard", count > 0 and model_guards == count)
            check("provider_usage_known", usage.get("token_usage_known") is True)
        source_key, evidence_valid = self._evidence(run)
        if kind == "model":
            check("chinese_tool_protocol", report.get("sample_only") is True and report.get("tool_result") == "AIOpsStudio中文工具协议样本已返回"
                and bool(re.search(r"[\u4e00-\u9fff]", str(report.get("analysis", "")))) and usage.get("tool_call_count") == 1)
            calls = [call for e in events if e.get("type") == "model_response" for call in e.get("payload", {}).get("tool_calls", [])]
            check("fixed_protocol_arguments", len(calls) == 1 and calls[0].get("name") == "aiopsstudio_protocol_echo" and calls[0].get("args") == {"text": "中文样本"})
            source_key = digest("fixed-chinese-tool-protocol/1")
        else:
            check("actual_saved_source", bool(source) and usage.get("source_request_count", 0) > 0 and evidence_valid and all(x.get("status") == "succeeded" for x in source))
            if kind == "skill":
                refs = report.get("reference_validation") or {}
                check("generated_cited_report", report.get("model_generated") is True and bool(report.get("analysis")) and bool(refs.get("cited_ids")) and not refs.get("invalid_ids"))
                validation = (report.get("grounding") or {}).get("validation") or {}
                check("structured_facts_verified", validation.get("structured_claim_count", 0) > 0
                    and validation.get("verified_claim_count") == validation.get("structured_claim_count") and validation.get("unverified_claim_count") == 0)
                reads = [r for r in self.store.list("material_read") if r.get("run_id") == run["id"] and r.get("consumer") == "model" and r.get("result")]
                if snap and snap.get("references"):
                    check("autonomous_reference_read", any((r["result"].get("resource_type") == "skill_reference"
                        and r["result"].get("material", {}).get("skill_id") == snap.get("id")
                        and r["result"].get("material", {}).get("version_id") == snap.get("version_id", "draft")
                        and r["result"].get("material", {}).get("path") in snap["references"]
                        and r["result"].get("anchor_id")) for r in reads))
            else:
                check("complete_reviewed_catalog", bool(snap) and (snap.get("discovery") or {}).get("complete") is True and self.config.validate("mcp", snap)["valid"])
                check("source_candidate_identity", bool(source) and all(x.get("source") == id for x in source))
        try:
            elapsed = max(0.0, (datetime.fromisoformat(run["completed_at"].replace("Z", "+00:00")) - datetime.fromisoformat(run["started_at"].replace("Z", "+00:00"))).total_seconds())
        except (KeyError, ValueError, TypeError):
            elapsed = None
        return {"run_id": run["id"], "input_digest": self._input_key(run), "source_digest": source_key,
                "checks": checks, "passed": sum(c["status"] == "passed" for c in checks),
                "failed": sum(c["status"] == "failed" for c in checks), "unknown": sum(c["status"] == "unknown" for c in checks),
                "metrics": {"elapsed_seconds": elapsed, "input_tokens": usage.get("input_tokens") if usage.get("token_usage_known") else None,
                    "output_tokens": usage.get("output_tokens") if usage.get("token_usage_known") else None,
                    "source_requests": usage.get("source_request_count"), "model_requests": usage.get("model_http_request_count"),
                    "price_comparison": "unknown_without_complete_versioned_pricing"}}

    def evaluate(self, kind, id, payload, *, actor="admin", authorize_run=None):
        if not isinstance(payload, dict) or set(payload) - {"expected_revision", "candidate_run_ids", "baseline_run_ids"}:
            raise ConfigError("质量评价只接受修订号和服务端试运行标识", "quality_payload_invalid")
        def operation():
            item = self.config.require(kind, id)
            if payload.get("expected_revision") != item["revision"]:
                raise StoreConflict("评价时草稿已变化，请重新读取")
            groups = {}
            for name in ("candidate_run_ids", "baseline_run_ids"):
                ids = payload.get(name, [])
                if not isinstance(ids, list) or len(ids) > 20 or any(not isinstance(v, str) or not v or len(v) > 200 for v in ids) or len(set(ids)) != len(ids):
                    raise ConfigError("试运行标识必须为不重复的有界列表", "quality_runs_invalid")
                groups[name] = []
                for rid in ids:
                    run = self.store.get("run", rid)
                    if not run or resource_owner(run) != actor:
                        raise ConfigError("试运行不存在或当前不可访问", "not_found")
                    if run.get("status") not in TERMINAL:
                        raise ConfigError("试运行尚未结束", "quality_run_pending")
                    if authorize_run:
                        authorize_run(run, actor)
                    groups[name].append(run)
            binding = self.candidate_binding(kind, id)
            candidates = [self._score(kind, id, run, binding, actor) for run in groups["candidate_run_ids"]]
            baseline_binding = self.candidate_binding(kind, id, version_id=item["current_version"]) if item.get("current_version") else None
            baselines = [self._score(kind, id, run, baseline_binding, actor) for run in groups["baseline_run_ids"]] if baseline_binding else []
            reasons, comparisons = [], []
            from .release_baseline import PipelineQuality
            pipeline = PipelineQuality(self.config).view()
            if not pipeline["eligible"]:
                reasons.append("pipeline_not_verified")
            if not candidates:
                reasons.append("candidate_not_executed")
            if any(x["failed"] or x["unknown"] for x in candidates):
                reasons.append("candidate_critical_check_failed_or_unknown")
            if baseline_binding:
                if not baselines or any(x["failed"] or x["unknown"] for x in baselines):
                    reasons.append("baseline_not_verified")
                old = {x["input_digest"]: x for x in baselines}
                new = {x["input_digest"]: x for x in candidates}
                if len(old) != len(baselines) or len(new) != len(candidates) or set(old) != set(new):
                    reasons.append("case_denominator_or_input_mismatch")
                for key in old.keys() & new.keys():
                    before, after = old[key], new[key]
                    comparable = before["source_digest"] == after["source_digest"]
                    regressions = [c["id"] for c in after["checks"] if c["status"] != "passed" and any(b["id"] == c["id"] and b["status"] == "passed" for b in before["checks"])]
                    resource_regression = False
                    for metric, ceiling in (("elapsed_seconds", THRESHOLDS["max_duration_ratio"]),):
                        a, b = after["metrics"][metric], before["metrics"][metric]
                        if a is not None and b is not None and b >= 1 and a > b * ceiling:
                            resource_regression = True
                    a = sum(after["metrics"][k] or 0 for k in ("input_tokens", "output_tokens"))
                    b = sum(before["metrics"][k] or 0 for k in ("input_tokens", "output_tokens"))
                    if b and a > b * THRESHOLDS["max_token_ratio"]:
                        resource_regression = True
                    comparisons.append({"input_digest": key, "same_source": comparable, "critical_regressions": regressions,
                                        "resource_regression": resource_regression, "before": before["metrics"], "after": after["metrics"]})
                    if not comparable:
                        reasons.append("source_snapshot_mismatch")
                    if regressions or resource_regression:
                        reasons.append("major_regression")
            elif groups["baseline_run_ids"]:
                reasons.append("bootstrap_baseline_must_be_fixed_contract")
            record = {"schema": "management-quality/1", "kind": kind, "entity_id": id, "owner_id": actor,
                "binding": binding, "policy": policy(), "candidate_cases": candidates, "baseline_cases": baselines,
                "pipeline_proof_id": pipeline.get("proof_id"),
                "baseline_kind": "actual_published_runs" if baseline_binding else "fixed_contract_first_publication",
                "baseline_binding": baseline_binding, "comparison": comparisons,
                "eligible": not reasons, "reasons": sorted(set(reasons)), "created_at": utcnow()}
            rid = "quality_" + secrets.token_hex(12)
            saved = self.store.save("quality_evaluation", rid, record)
            self.config.audit("quality_evaluate", kind, id, {"evaluation_id": rid, "eligible": record["eligible"], "reasons": record["reasons"]}, actor=actor)
            return self.public(saved)
        return self.store.atomic(operation)

    def require_eligible(self, kind, id, evaluation_id):
        record = self.store.get("quality_evaluation", evaluation_id or "")
        if not record or record.get("kind") != kind or record.get("entity_id") != id or record.get("eligible") is not True:
            raise ConfigError("正式发布需要通过的服务端质量评价", "quality_evaluation_required")
        if record.get("binding") != self.candidate_binding(kind, id) or record.get("policy") != policy():
            raise ConfigError("候选内容、依赖、策略或评价版本已变化，请重新评价", "quality_evaluation_stale")
        from .release_baseline import PipelineQuality
        pipeline = PipelineQuality(self.config).view()
        proof = self.store.get("quality_pipeline", record.get("pipeline_proof_id") or "")
        if not pipeline["eligible"] or not proof or proof.get("eligible") is not True or proof.get("fingerprint") != pipeline["fingerprint"]:
            raise ConfigError("固定案例质量回放尚未通过或已失效", "pipeline_evaluation_required")
        # A receipt cannot remain eligible after its underlying records/files disappear.
        for case in record.get("candidate_cases", []) + record.get("baseline_cases", []):
            run = self.store.get("run", case["run_id"])
            if not run or run.get("status") not in {"succeeded", "partial"}:
                raise ConfigError("评价所引用的试运行已不可用", "quality_evidence_missing")
            expected = record["binding"] if case in record.get("candidate_cases", []) else record["baseline_binding"]
            current = self._score(kind, id, run, expected, record["owner_id"])
            if current != case or current["failed"] or current["unknown"]:
                raise ConfigError("评价所引用的试运行或证据已变化", "quality_evidence_changed")
        return self.public(record)

    def public(self, record):
        return redact(copy.deepcopy(record), self.config.vault)


def configure_quality(app):
    import asyncio
    from .identity import actor_id, require_admin
    service = QualityService(app.state.config)
    from .release_baseline import PipelineQuality
    pipeline = PipelineQuality(app.state.config)

    @app.get("/api/quality/pipeline")
    async def pipeline_view(request: Request):
        require_admin(request)
        return pipeline.view()

    @app.post("/api/quality/pipeline")
    async def pipeline_run(request: Request):
        require_admin(request)
        payload = await request.json()
        if payload != {}:
            raise ConfigError("固定质量回放不接受路径、分数或自定义参数", "pipeline_payload_invalid")
        return await asyncio.to_thread(pipeline.run, actor=actor_id(request))
    for kind, path in (("skill", "skills"), ("mcp", "mcp-servers"), ("model", "model-configs")):
        def register(kind=kind, path=path):
            @app.get(f"/api/{path}/{{id}}/quality", name=f"quality_{kind}")
            async def view(id: str, request: Request):
                require_admin(request)
                return service.view(kind, id, actor=actor_id(request))

            @app.post(f"/api/{path}/{{id}}/quality", name=f"evaluate_quality_{kind}")
            async def evaluate(id: str, request: Request):
                require_admin(request)
                runtime = getattr(app.state, "runtime", None)
                if runtime is None:
                    raise ConfigError("试运行执行器尚未装配", "quality_runtime_unavailable")
                return service.evaluate(kind, id, await request.json(), actor=actor_id(request),
                    authorize_run=lambda run, owner: runtime.require_owned(run["id"], owner))
        register()
