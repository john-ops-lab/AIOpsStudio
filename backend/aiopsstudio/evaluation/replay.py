"""Replay actual application routes with replaceable model/source boundaries.

This module must not load the suite or any oracle. It is executed in a separate
process with only Agent-visible material and an empty temporary database.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
import time

from .contracts import canonical, digest


def guard_execution():
    """Fail closed if a model/plugin attempts to read an oracle or open a network socket."""
    def audit(event, args):
        if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
            parts = Path(os.fsdecode(args[0])).resolve().parts
            if "oracles" in parts:
                raise PermissionError("evaluation oracles are unavailable during application replay")
        if event == "socket.connect" and os.environ.get("AIOPSSTUDIO_EVALUATION_ALLOW_NETWORK") != "1":
            raise PermissionError("network is disabled for synthetic application replay")
    sys.addaudithook(audit)


def private_model_config(path, repository):
    original = Path(path)
    if original.is_symlink():
        raise ValueError("live model configuration must be a regular file, not a symlink")
    path = original.resolve()
    info = path.stat()
    if path.is_relative_to(Path(repository).resolve()) or not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid():
        raise ValueError("live model configuration must be an owned external regular 0600 file")
    value = json.loads(path.read_text())
    if value.get("provider", "").casefold() not in {"minimax", "openai"} or not all(value.get(k) for k in ("model", "base_url", "api_key")):
        raise ValueError("live model configuration requires explicit provider/model/base_url/api_key; no provider fallback")
    if value["provider"].casefold() == "openai" and not value.get("explicit_provider_override"):
        raise ValueError("MiniMax is preferred; another provider requires explicit_provider_override")
    from urllib.parse import urlsplit
    endpoint = urlsplit(value["base_url"])
    if endpoint.scheme != "https" or not endpoint.hostname or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
        raise ValueError("live model endpoint must be explicit HTTPS without embedded credentials or query")
    if not isinstance(value.get("extra_body", {}), dict) or set(value.get("extra_body", {})) - {"reasoning_split", "reasoning_effort"}:
        raise ValueError("live model extra parameters must be explicitly registered nonsecret reasoning fields")
    return value


def _context(messages):
    for message in reversed(messages):
        content = getattr(message, "content", "")
        if not isinstance(content, str):
            continue
        start = content.find("{")
        if start >= 0:
            try:
                parsed = json.loads(content[start:])
                if isinstance(parsed, dict) and "evidence_ledger" in parsed:
                    return parsed
            except ValueError:
                pass
    return {}


def synthetic_model(observer, behavior="read_then_report", *, material_reads=False):
    """A common conservative extractor, without case IDs, source snapshots or answers.

    The model only receives actual LangChain messages. It asks a registered tool
    for evidence, follows visible pagination, and renders the visible ledger.
    This measures application/reading fidelity, not real LLM reasoning quality.
    """
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from langchain_core.utils.function_calling import convert_to_openai_tool
    from pydantic import PrivateAttr

    class ConservativeModel(BaseChatModel):
        _tools: list = PrivateAttr(default_factory=list)
        _definitions: list = PrivateAttr(default_factory=list)

        @property
        def _llm_type(self):
            return "synthetic-conservative-extractor-v1"

        def _get_ls_params(self, **kwargs):
            return {"ls_provider": "openai", "ls_model_name": "synthetic-conservative-extractor-v1"}

        def bind_tools(self, tools, **kwargs):
            self._tools = [getattr(tool, "name", None) or tool.get("name") or tool.get("function", {}).get("name") for tool in tools]
            self._definitions = [convert_to_openai_tool(tool) for tool in tools]
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            observer.append({"messages": [{"type": message.type, "content": message.content} for message in messages],
                             "tools": list(self._tools), "tool_definitions": copy.deepcopy(self._definitions)})
            if behavior == "model_failure":
                raise RuntimeError("synthetic model failure")
            context = _context(messages)
            ledger = context.get("evidence_ledger", {}).get("evidence", [])
            aliases = [name for name in self._tools if name in {"read_snapshot", "query_prometheus"}]
            preferred = context.get("inputs", {}).get("source_tool")
            if preferred in aliases:
                aliases.remove(preferred)
                aliases.insert(0, preferred)
            if not ledger and aliases and context.get("mode") != "final_report_no_tools":
                args = {"target": context.get("target", "synthetic-service"), "startTime": context.get("analysis_start"), "endTime": context.get("analysis_end"), "page": 0}
                message = AIMessage(content="", tool_calls=[{"name": aliases[0], "args": args, "id": "fixture_call_0", "type": "tool_call"}])
            else:
                queried = {entry.get("arguments", {}).get("page", 0) for entry in ledger}
                next_page = next((entry.get("data", {}).get("next_page") for entry in reversed(ledger)
                                  if isinstance(entry.get("data"), dict) and entry["data"].get("next_page") is not None), None)
                reads = context.get("material_reads", [])
                requested_reference = context.get("inputs", {}).get("reference_path")
                manifest = next((item for item in context.get("reference_manifest", []) if item.get("path") == requested_reference), None)
                reference_read = any(item.get("resource_type") == "skill_reference" and item.get("material", {}).get("path") == requested_reference for item in reads if isinstance(item, dict))
                truncated = next((entry for entry in ledger if entry.get("tool_name") == "read_snapshot" and entry.get("reading_view", {}).get("business_data_truncated")), None)
                eligible = material_reads and "read_saved_material" in self._tools and truncated and context.get("mode") != "final_report_no_tools"
                relevant = [item for item in reads if isinstance(item, dict) and item.get("material", {}).get("evidence_id") == (truncated or {}).get("evidence_id")]
                read_args = None
                if eligible and not relevant:
                    read_args = {"resource_type": "evidence", "evidence_id": truncated["evidence_id"], "selector": {"pointer": "/data/items", "offset": 0, "limit": 1}}
                elif eligible and relevant and relevant[-1].get("position", {}).get("total_saved_items", 0) > 1 and len(relevant) == 1:
                    read_args = {"resource_type": "evidence", "evidence_id": truncated["evidence_id"], "selector": {"pointer": "/data/items", "offset": relevant[-1]["position"]["total_saved_items"] - 1, "limit": 1}}
                if material_reads and manifest and not reference_read and "read_saved_material" in self._tools and context.get("mode") != "final_report_no_tools":
                    args = {"resource_type": "skill_reference", "skill_id": manifest["skill_id"], "version_id": manifest["version_id"], "path": manifest["path"],
                            "selector": {"pointer": "/text", "offset": 0, "limit": 200}}
                    message = AIMessage(content="", tool_calls=[{"name": "read_saved_material", "args": args, "id": "fixture_reference_read", "type": "tool_call"}])
                elif read_args:
                    message = AIMessage(content="", tool_calls=[{"name": "read_saved_material", "args": read_args, "id": "fixture_material_" + str(len(reads)), "type": "tool_call"}])
                elif next_page is not None and next_page not in queried and aliases and context.get("mode") != "final_report_no_tools":
                    args = {"target": context.get("target", "synthetic-service"), "startTime": context.get("analysis_start"), "endTime": context.get("analysis_end"), "page": next_page}
                    message = AIMessage(content="", tool_calls=[{"name": aliases[0], "args": args, "id": "fixture_call_" + str(next_page), "type": "tool_call"}])
                else:
                    lines = ["合成适配器报告：仅逐字段呈现实际读取的证据，不代表真实模型推理质量。", "根因未确认；关系和时间先后不能证明因果。"]
                    for entry in ledger:
                        lines.append("[" + entry["evidence_id"] + "] " + json.dumps({key: value for key, value in entry.items() if key != "read_at"}, ensure_ascii=False, sort_keys=True))
                    for reading in reads:
                        if isinstance(reading, dict) and reading.get("anchor_id"):
                            evidence_id = reading.get("material", {}).get("evidence_id")
                            citation = (evidence_id + "#" if evidence_id else "") + reading["anchor_id"]
                            lines.append("[" + citation + "] " + json.dumps(reading, ensure_ascii=False, sort_keys=True))
                    if not ledger:
                        lines.append("未取得源证据，不能形成事实结论。")
                    if any(entry.get("reading_view", {}).get("business_data_truncated") for entry in ledger):
                        lines.append("阅读范围不完整，仍需补读；不能声称已查全。")
                    analysis = "\n".join(lines)
                    structured = material_reads and any("最终只返回一个 JSON 对象" in str(getattr(item, "content", "")) for item in messages)
                    claims = []
                    if structured:
                        window = context.get("windows", {}).get("fault") or {"start": context.get("analysis_start"), "end": context.get("analysis_end")}
                        for entry in ledger:
                            data = entry.get("data", {})
                            for index, fact in enumerate(data.get("facts", []) if isinstance(data, dict) else []):
                                if isinstance(fact, dict) and "value" in fact:
                                    claims.append({"kind": "observation", "text": str(fact.get("field", "已读取值")) + "=" + str(fact["value"]),
                                                   "evidence_id": entry["evidence_id"], "pointer": "/data/facts/" + str(index) + "/value",
                                                   "rule": "scalar", "value": fact["value"], "unit": fact.get("unit"),
                                                   "window": {key: window.get(key) for key in ("start", "end")}})
                        for reading in reads:
                            if not isinstance(reading, dict) or reading.get("resource_type") != "evidence" or not isinstance(reading.get("data"), list):
                                continue
                            position = reading.get("position", {})
                            for index, row in enumerate(reading["data"]):
                                if isinstance(row, dict):
                                    for key, value in row.items():
                                        if isinstance(value, str):
                                            claims.append({"kind": "observation", "text": key + "=" + value,
                                                "evidence_id": reading["material"]["evidence_id"], "anchor_id": reading["anchor_id"],
                                                "pointer": position["pointer"] + "/" + str(position.get("offset", 0) + index) + "/" + key,
                                                "rule": "scalar", "value": value, "window": {key: window.get(key) for key in ("start", "end")}})
                    message = AIMessage(content=json.dumps({"analysis": analysis, "claims": claims}, ensure_ascii=False) if structured else analysis)
            # Obey the schema actually exposed to this model. New servers bind
            # identity/time themselves; the frozen old server still exposes them.
            for call in message.tool_calls:
                definition = next((item.get("function", {}) for item in self._definitions if item.get("function", {}).get("name") == call["name"]), {})
                properties = definition.get("parameters", {}).get("properties")
                if isinstance(properties, dict):
                    call["args"] = {key: value for key, value in call["args"].items() if key in properties}
            return ChatResult(generations=[ChatGeneration(message=message)])

    return ConservativeModel()


def seed_frozen(app, material, live=None):
    """Shared integration fixture: fixed synthetic entities, no production config reads."""
    components = material["frozen"]["components"]
    model = copy.deepcopy(components["model"])
    if live:
        model.update(provider="openai", model=live["model"], base_url=live["base_url"], max_tokens=live.get("max_tokens", 4096))
        for key in ("temperature", "extra_body"):
            if key in live:
                model[key] = copy.deepcopy(live[key])
    model["secret_id"] = app.state.vault.put(live["api_key"] if live else "synthetic-evaluation-credential")
    skill = copy.deepcopy(components["skill"])
    server = copy.deepcopy(components["tool_schema"])
    for kind, entity in (("model", model), ("skill", skill), ("mcp", server)):
        version = {"id": "evaluation-v1", "snapshot": copy.deepcopy(entity)}
        app.state.store.save(kind, entity["id"], {**entity, "current_version": version["id"], "versions": [version]})
    settings = {"default_model_id": model["id"], "environment": "synthetic",
        "min_free_bytes": 1, "query_catalog": components["query_catalog"], "privacy_policy": components["privacy_policy"],
        "budget": {"wall_seconds": 60, "model_calls": 8, "tool_calls": 8, "max_tokens": 32000}}
    settings.update(copy.deepcopy(material["frozen"].get("application_settings", {})))
    app.state.store.save("settings", "main", settings)
    substitutions = {"$model_secret_ref": model["secret_id"], "$model_destination": model.get("base_url")}
    def bind(value):
        if isinstance(value, dict):
            return {key: bind(item) for key, item in value.items()}
        if isinstance(value, list):
            return [bind(item) for item in value]
        return substitutions.get(value, value) if isinstance(value, str) else value
    for kind, document in material["frozen"].get("policy_documents", {}).items():
        if kind not in {"access_policy", "egress_policy"}:
            raise ValueError("evaluation policy kind must be explicit")
        app.state.store.save(kind, "main", bind(document))


def verify_frozen(run, material, live=None):
    components, snapshot = material["frozen"]["components"], run.get("config_snapshot", {})
    mismatches = []
    for component, selected in (("model", snapshot.get("model", {})), ("skill", snapshot.get("skill", {})), ("tool_schema", (snapshot.get("mcps") or [{}])[0])):
        wanted = copy.deepcopy(components[component])
        if component == "model" and live:
            wanted.update(provider="openai", model=live["model"], base_url=live["base_url"], max_tokens=live.get("max_tokens", 4096))
            for key in ("temperature", "extra_body"):
                if key in live:
                    wanted[key] = copy.deepcopy(live[key])
        actual = {key: selected.get(key) for key in wanted}
        if digest(actual) != digest(wanted):
            mismatches.append(component)
    for key in ("query_catalog", "privacy_policy"):
        if digest(snapshot.get("settings", {}).get(key)) != digest(components[key]):
            mismatches.append(key)
    for key, value in material["frozen"].get("application_settings", {}).items():
        if digest(snapshot.get("settings", {}).get(key)) != digest(value):
            mismatches.append("settings." + key)
    return {"matched": not mismatches, "mismatches": mismatches}


def _elapsed(start, end):
    if not start or not end:
        return None
    from datetime import datetime
    return max(0, (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds())


def measurement(run, wall_seconds, observed):
    usage = run.get("usage", {})
    # A dispatch that failed before usage recording can leave the runtime's
    # initial 0/known fields unchanged. No provider receipt means unknown.
    known = usage.get("token_usage_known") is True and (bool(usage.get("provider_usage")) or (usage.get("input_tokens", 0) + usage.get("output_tokens", 0)) > 0)
    return {"wall_seconds": wall_seconds, "queue_seconds": _elapsed(run.get("created_at"), run.get("started_at")),
            "execution_seconds": _elapsed(run.get("started_at"), run.get("completed_at")),
            "model_requests_observed": len(observed["model_requests"]), "tool_requests_observed": len(observed["source_requests"]),
            "control_requests_observed": len(observed["discovery_requests"]), "application_usage": usage,
            "input_tokens": usage.get("input_tokens") if known else None,
            "output_tokens": usage.get("output_tokens") if known else None,
            "token_unknown_reason": None if known else "provider_did_not_supply_complete_usage",
            "mcp_upstream_http_requests": None, "mcp_upstream_unknown_reason": "not_observable_across_MCP_boundary",
            "retries": None, "cache_hits": None, "retry_cache_unknown_reason": "provider_did_not_supply_attempt_or_cache_receipts",
            "estimated_cost": None, "cost_unknown_reason": "no_frozen_price_schedule", "price_schedule": None}


def replay(material, temporary, *, live=None):
    """Run actual login/submission/worker/detail routes against empty SQLite."""
    from cryptography.fernet import Fernet
    from fastapi.testclient import TestClient
    from aiopsstudio.main import create_app
    from aiopsstudio.runtime import Runtime, TERMINAL
    from aiopsstudio.store import Store
    from aiopsstudio import connectors

    temporary = Path(temporary)
    store = Store("sqlite:///" + str(temporary / "evaluation.sqlite"))
    app = create_app(store=store, testing=True, master_key=Fernet.generate_key(), password="synthetic-evaluation-password",
                     public_origin="http://testserver", artifact_dir=temporary / "artifacts")
    seed_frozen(app, material, live)
    observed = {"model_requests": [], "source_requests": [], "discovery_requests": [], "policy_diagnostics": []}
    original_egress = None
    try:
        from aiopsstudio.access_policy import PolicyService, PolicyError, _leaves, _rule_for, _path
        original_egress = PolicyService.egress_view
        def diagnose_egress(service, kind, value, **kwargs):
            try:
                return original_egress(service, kind, value, **kwargs)
            except PolicyError:
                # Names only, no values; this reports an offline fixture/schema
                # mismatch without granting a field or weakening the guard.
                policy = service.store.get("egress_policy", "main") or {}
                rules = policy.get("types", {}).get(kind, {}).get("fields", {})
                observed["policy_diagnostics"].append({"kind": kind, "unknown_paths": sorted({_path(parts) for parts, _ in _leaves(value) if _rule_for(rules, parts) is None})})
                raise
        PolicyService.egress_view = diagnose_egress
    except ImportError:
        pass  # The immutable old baseline intentionally has no policy module.
    schemas = material["frozen"]["components"]["tool_schema"]["tools"]
    responses = material["source"]["responses"]

    def reference_reads_at_send():
        return [{key: item.get("anchor", {}).get(key) for key in ("skill_id", "version_id", "path")}
                for item in store.list("material_read") if item.get("consumer") == "model" and item.get("anchor", {}).get("resource_type") == "skill_reference"]

    async def discover(config, vault, *, request_observer=None):
        if request_observer:
            request_observer()
        observed["discovery_requests"].append({"server_id": config["id"]})
        return {"items": copy.deepcopy(schemas), "complete": True}

    async def call(config, vault, name, arguments, *, request_observer=None):
        if request_observer:
            request_observer()
        observed["source_requests"].append({"tool_name": name, "arguments": copy.deepcopy(arguments)})
        key = name + ":" + str(arguments.get("page", 0))
        response = responses.get(key)
        if response is None:
            raise connectors.ConnectorError("synthetic snapshot has no such query/page", "synthetic_query_missing")
        if response.get("raise_error"):
            raise connectors.ConnectorError("synthetic source interface failure", "synthetic_source_failure")
        return copy.deepcopy(response)

    original_discover, original_call, original_model = connectors.mcp_discover, connectors.mcp_call, Runtime._model
    connectors.mcp_discover, connectors.mcp_call = discover, call
    wire = material["frozen"]["components"]["model"].get("model") == "synthetic-wire-extractor-v2"
    if wire and not live:
        import httpx
        from types import SimpleNamespace
        async def transport(request):
            body = json.loads(request.content)
            observed["model_requests"].append({"body": body, "reference_reads_at_send": reference_reads_at_send()})
            if material.get("model_behavior") == "model_failure":
                return httpx.Response(500, json={"error": {"type": "synthetic_failure", "message": "synthetic model interface failure"}})
            model = synthetic_model([], material.get("model_behavior"), material_reads=True)
            model.bind_tools(body.get("tools", []))
            messages = [SimpleNamespace(content=message.get("content", ""), type=message.get("role", "unknown")) for message in body.get("messages", [])]
            result = model._generate(messages).generations[0].message
            answer = {"role": "assistant", "content": result.content or None}
            if result.tool_calls:
                answer["tool_calls"] = [{"id": call["id"], "type": "function", "function": {"name": call["name"], "arguments": json.dumps(call["args"], ensure_ascii=False)}} for call in result.tool_calls]
            return httpx.Response(200, json={"id": "chatcmpl-synthetic-evaluation", "object": "chat.completion", "created": 1792458000,
                "model": body["model"], "choices": [{"index": 0, "message": answer, "finish_reason": "tool_calls" if result.tool_calls else "stop"}]})
        def model(runtime, config, **kwargs):
            instance = original_model(runtime, config, **kwargs)
            # Replace only the HTTP transport and retain the application's actual
            # SDK request hooks. No HTTP guard or Deep Agents stage is bypassed.
            previous = instance.root_async_client._client
            client = httpx.AsyncClient(transport=httpx.MockTransport(transport), event_hooks={key: list(value) for key, value in previous.event_hooks.items()},
                                      follow_redirects=False, trust_env=False)
            instance.root_async_client._client = client
            if kwargs.get("run_id") and hasattr(runtime, "_model_clients"):
                runtime._model_clients.setdefault(kwargs["run_id"], []).append(client)
            return instance
        Runtime._model = model
    elif live:
        async def capture_actual(request):
            # Added after the application's request guard; no auth headers,
            # provider error text or config values are copied into the body.
            observed["model_requests"].append({"body": json.loads(request.content), "reference_reads_at_send": reference_reads_at_send()})
        def model(runtime, config, **kwargs):
            instance = original_model(runtime, config, **kwargs)
            instance.root_async_client._client.event_hooks.setdefault("request", []).append(capture_actual)
            return instance
        Runtime._model = model
    else:
        Runtime._model = lambda *args, **kwargs: synthetic_model(observed["model_requests"], material.get("model_behavior"))
    started = time.monotonic()
    try:
        with TestClient(app) as client:
            client.headers["Origin"] = "http://testserver"
            login = client.post("/api/login", json={"password": "synthetic-evaluation-password"})
            login.raise_for_status()
            payload = {**material["request"], "skill_id": "skill_evaluation", "skill_version": "evaluation-v1", "client_request_id": "evaluation-first-attempt"}
            submitted = client.post("/api/runs", json=payload)
            submitted.raise_for_status()
            run_id = submitted.json()["id"]
            while time.monotonic() - started < 75:
                result = client.get("/api/runs/" + run_id)
                result.raise_for_status()
                run = result.json()
                if run["status"] in TERMINAL:
                    break
                time.sleep(.02)
            else:
                raise TimeoutError("evaluation run did not terminate")
            internal = store.get("run", run_id)
            evidence = {}
            for item in run.get("evidence", []):
                response = client.get("/api/evidence/" + item["id"])
                response.raise_for_status()
                evidence[item["id"]] = response.json()
            frozen = verify_frozen(internal, material, live)
            observation = {"schema": "aiopsstudio-evaluation-observation/1", "execution": "actual_FastAPI_routes_DeepAgents_worker",
                           "measurement_kind": "live_model_synthetic_source" if live else "synthetic_HTTP_model_and_source" if wire else "synthetic_model_and_source",
                           "run": run, "evidence": evidence, "invocations": store.list("invocation"), **observed,
                           "frozen_validation": frozen, "source_snapshot_sha256": material["source"]["sha256"],
                           "receipts": {}, "unverified_operations": material.get("checks", [])}
            if wire:
                reads = [item for item in store.list("material_read") if item.get("run_id") == run_id and item.get("consumer") == "model"]
                checked = [event for event in run.get("events", []) if event.get("type") == "model_egress_checked"]
                references = internal.get("config_snapshot", {}).get("skill", {}).get("references", {})
                sentinel_paths = [path for path, content in references.items() if "REFERENCE_SENTINEL_SYNTHETIC" in content]
                references_allowed = bool(observed["model_requests"]) and bool(sentinel_paths)
                for request in observed["model_requests"]:
                    if "REFERENCE_SENTINEL_SYNTHETIC" in json.dumps(request.get("body", {}), ensure_ascii=False):
                        sent_paths = {item.get("path") for item in request.get("reference_reads_at_send", [])}
                        references_allowed = references_allowed and all(path in sent_paths for path in sentinel_paths)
                observation["receipts"] = {
                    "evidence_fragment": {"saved": any(item.get("anchor", {}).get("resource_type") == "evidence" for item in reads), "count": len(reads)},
                    "egress_policy": {"enforced": bool(observed["model_requests"]) and len(checked) == len(observed["model_requests"]),
                                      "checked_requests": len(checked), "actual_model_requests": len(observed["model_requests"])},
                    "references": {"requested_only": references_allowed,
                                   "model_reads": sum(item.get("anchor", {}).get("resource_type") == "skill_reference" for item in reads)}}
            observation["measurement"] = measurement(internal, time.monotonic() - started, observed)
            if not frozen["matched"]:
                observation["harness_error"] = "frozen_snapshot_mismatch"
            return observation
    finally:
        connectors.mcp_discover, connectors.mcp_call, Runtime._model = original_discover, original_call, original_model
        if original_egress:
            PolicyService.egress_view = original_egress
        store.close()
