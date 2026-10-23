"""Deterministic, side-effect-free identity, query and coverage boundaries.

The caller persists this snapshot on the attempt. No function discovers a target,
consults a live source, or upgrades legacy settings to a verified contract.
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import re

VERSION = "analysis-context/1"
COVERAGE_STATES = frozenset({"complete", "empty", "forbidden", "failed", "stale", "partial", "historical_unsupported", "unknown"})
_FIELDS = ("environment", "cluster", "namespace", "type", "id")
_QUERY_ARGUMENTS = {"query", "expression", "expr", "promql", "logql", "traceql"}


class ContextError(ValueError):
    def __init__(self, code: str, detail: str):
        self.code, self.detail = code, detail
        super().__init__(detail)


def _fail(code, detail):
    raise ContextError(code, detail)


def _time(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})", value):
        _fail("time_window_invalid", "时间必须为带时区的 RFC3339 绝对时间")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError()
        return stamp.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        _fail("time_window_invalid", "时间必须为带时区的 RFC3339 绝对时间")


def _iso(value):
    return _time(value).isoformat().replace("+00:00", "Z")


def _digest(value):
    try:
        serialized = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        _fail("context_configuration_invalid", "上下文必须为有限 JSON 数据")
    return hashlib.sha256(serialized.encode()).hexdigest()


def _canonical(value):
    if not isinstance(value, dict) or any(k not in value for k in _FIELDS):
        _fail("identity_invalid", "对象键须含 environment/cluster/namespace/type/id")
    result = {k: value[k] for k in _FIELDS}
    if any(not isinstance(v, str) or len(v) > 512 or any(ord(c) < 32 for c in v) for v in result.values()):
        _fail("identity_invalid", "对象键的每个维度必须为有界文本")
    if any(not result[k].strip() for k in ("environment", "type", "id")):
        _fail("identity_invalid", "对象环境、类型和 ID 不能为空")
    return result


def canonical_key(value):
    """Key includes every scope dimension; display names never identify an object."""
    obj = _canonical(value)
    return "obj_" + _digest([obj[k] for k in _FIELDS])


def _versioned(value, member):
    return isinstance(value, dict) and isinstance(value.get("version"), str) and bool(value["version"].strip()) and isinstance(value.get(member), list)


def _validate_configuration(identity_mapping=None, query_catalog=None):
    """Validate a new contract; old structures stay explicitly unverified."""
    for value, member, label in ((identity_mapping, "records", "身份映射"), (query_catalog, "entries", "查询目录")):
        if value is not None and not isinstance(value, dict):
            _fail("context_configuration_invalid", f"{label}必须为对象")
        if isinstance(value, dict) and ("version" in value or member in value) and not _versioned(value, member):
            _fail("context_configuration_invalid", f"{label}须同时提供非空 version 和 {member} 列表")
    if _versioned(identity_mapping, "records"):
        if len(identity_mapping["records"]) > 10000:
            _fail("context_configuration_invalid", "身份映射记录超过上限")
        for row in identity_mapping["records"]:
            if not isinstance(row, dict):
                _fail("identity_invalid", "身份映射记录必须为对象")
            _canonical(row.get("canonical"))
            for field in ("source_id", "object_type", "native_id", "mapping_source"):
                if not isinstance(row.get(field), str) or not row[field].strip() or len(row[field]) > 512:
                    _fail("identity_invalid", f"身份映射缺少有效 {field}")
            if not isinstance(row.get("aliases", []), list) or any(not isinstance(a, str) or len(a) > 512 for a in row.get("aliases", [])):
                _fail("identity_invalid", "身份别名必须为有界字符串列表")
            for field in ("verified_at", "valid_from", "valid_to"):
                if row.get(field) is not None:
                    _time(row[field])
            if row.get("valid_from") and row.get("valid_to") and _time(row["valid_from"]) > _time(row["valid_to"]):
                _fail("identity_invalid", "映射有效期开始晚于结束")
    if _versioned(query_catalog, "entries"):
        if len(query_catalog["entries"]) > 1000:
            _fail("query_catalog_invalid", "查询目录超过上限")
        seen = set()
        for entry in query_catalog["entries"]:
            if not isinstance(entry, dict):
                _fail("query_catalog_invalid", "查询目录条目必须为对象")
            key = (entry.get("source_id"), entry.get("tool_name"))
            if any(not isinstance(k, str) or not k.strip() for k in key) or key in seen:
                _fail("query_catalog_invalid", "查询目录 source_id/tool_name 缺失或重复")
            seen.add(key)
            if not isinstance(entry.get("version"), str) or not entry["version"].strip():
                _fail("query_catalog_invalid", "查询目录条目缺少 version")
            if not isinstance(entry.get("definition_digest"), str) or not re.fullmatch(r"[a-f0-9]{64}", entry["definition_digest"]):
                _fail("query_catalog_invalid", "查询目录必须绑定工具定义 SHA-256")
            for field in ("identity_parameters", "time_parameters", "static_arguments", "argument_templates"):
                if not isinstance(entry.get(field, {}), dict):
                    _fail("query_catalog_invalid", f"查询目录 {field} 必须为对象")
            selectors = {"native_id", "object_type", *("canonical." + key for key in _FIELDS)}
            if any(not isinstance(value, str) or value not in selectors for value in entry.get("identity_parameters", {}).values()):
                _fail("context_configuration_invalid", "身份参数必须引用已知来源映射字段")
            if any(not isinstance(value, str) for value in entry.get("argument_templates", {}).values()):
                _fail("query_catalog_invalid", "查询模板必须为字符串")
            result_identity = entry.get("result_identity")
            if result_identity is not None:
                if not isinstance(result_identity, dict) or set(result_identity) - {"version", "collection", "pointer", "mapping", "scope_fields", "fixed_scope"}:
                    _fail("query_catalog_invalid", "响应身份规则字段无效")
                if not isinstance(result_identity.get("version"), str) or not result_identity["version"].strip() or result_identity.get("mapping") not in {"native_id", "canonical.id"}:
                    _fail("query_catalog_invalid", "响应身份规则须版本化并明确映射身份")
                paths = [result_identity.get("pointer"), result_identity.get("collection", "")]
                scopes, fixed = result_identity.get("scope_fields", {}), result_identity.get("fixed_scope", {})
                dimensions = {"environment", "cluster", "namespace", "type"}
                if not isinstance(scopes, dict) or not isinstance(fixed, dict) or (set(scopes) | set(fixed)) - dimensions or set(scopes) & set(fixed):
                    _fail("query_catalog_invalid", "响应身份范围字段无效或冲突")
                if any(not isinstance(value, str) or len(value) > 512 for value in fixed.values()):
                    _fail("query_catalog_invalid", "响应固定来源范围必须为有界文本")
                paths.extend(scopes.values())
                if any(not isinstance(path, str) or len(path) > 1024 or path.count("/") > 24 or path and not path.startswith("/") or re.search(r"~(?![01])", path) for path in paths):
                    _fail("query_catalog_invalid", "响应身份位置必须为有界 JSON Pointer")
            if entry.get("window", "fault") not in {"fault", "baseline", "change", "validation"}:
                _fail("query_catalog_invalid", "查询目录窗口名称无效")
            if entry.get("time_format", "rfc3339") not in {"rfc3339", "unix_seconds", "unix_milliseconds"}:
                _fail("query_catalog_invalid", "查询目录时间编码无效")
            if entry.get("object_scope", "target") not in {"target", "none"}:
                _fail("query_catalog_invalid", "查询目录对象范围无效")
            if entry.get("historical") is not None and not isinstance(entry["historical"], bool):
                _fail("query_catalog_invalid", "历史能力必须为布尔值或 null")
            if set(entry.get("time_parameters", {})) - {"start", "end", "instant"}:
                _fail("query_catalog_invalid", "时间参数只能显式绑定 start/end/instant")
            names = []
            for field in ("identity_parameters", "static_arguments", "argument_templates"):
                names.extend(entry.get(field, {}).keys())
            names.extend(entry.get("time_parameters", {}).values())
            if any(not isinstance(name, str) or not name for name in names) or len(names) != len(set(names)):
                _fail("query_catalog_invalid", "查询参数绑定名称无效或冲突")
            if entry.get("kind", "generic") not in {"telemetry", "topology", "generic", "discovery"}:
                _fail("query_catalog_invalid", "查询类型无效")
            allowed = entry.get("allowed_parameters", [])
            if not isinstance(allowed, list) or any(not isinstance(v, str) or not v for v in allowed):
                _fail("query_catalog_invalid", "可变参数须明确登记为字符串列表")
        offsets = query_catalog.get("source_offsets", {})
        if not isinstance(offsets, dict):
            _fail("query_catalog_invalid", "源时钟偏差必须为对象")
        for source, offset in offsets.items():
            if not isinstance(source, str) or not isinstance(offset, dict):
                _fail("query_catalog_invalid", "源时钟偏差格式无效")
            seconds = offset.get("seconds")
            if seconds is not None and (isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or abs(seconds) > 86400):
                _fail("query_catalog_invalid", "源时钟偏差须为一天以内有限秒数或 null")
    return {"identity_verified": _versioned(identity_mapping, "records"), "catalog_verified": _versioned(query_catalog, "entries")}


def validate_configuration(identity_mapping=None, query_catalog=None):
    """Malformed administrator JSON is a validation error, not a server crash."""
    try:
        return _validate_configuration(identity_mapping, query_catalog)
    except (TypeError, KeyError) as error:
        raise ContextError("context_configuration_invalid", "身份映射或查询目录字段类型无效") from error


def _window(value, *, name, fallback=None):
    if value is None:
        value = fallback
    if not isinstance(value, dict) or not value.get("start") or not value.get("end"):
        _fail("time_window_required", f"请提供 {name} 带时区的绝对起止时间")
    start, end = _time(value["start"]), _time(value["end"])
    if start > end or end - start > timedelta(days=7):
        _fail("time_window_invalid", "窗口开始不能晚于结束，且最长七天")
    result = {"start": _iso(value["start"]), "end": _iso(value["end"]), "reason": str(value.get("reason") or "explicit_input")[:1024]}
    if name == "baseline":
        # Temporal ordering never proves comparable load or health.
        result.update(comparability="unknown", comparability_reason="未完成流量及健康可比性核验")
    return result


def freeze_context(payload, settings, *, captured_at=None):
    payload, settings = copy.deepcopy(payload), copy.deepcopy(settings)
    captured_at = _iso(captured_at or datetime.now(timezone.utc).isoformat())
    supplied = payload.get("windows") or {}
    if not isinstance(supplied, dict) or set(supplied) - {"fault", "baseline", "change", "validation"}:
        _fail("time_window_invalid", "具名窗口只支持 fault/baseline/change/validation")
    fault = _window(supplied.get("fault"), name="fault", fallback={"start": payload.get("analysis_start"), "end": payload.get("analysis_end")})
    if "fault" in supplied:
        for main, side in (("analysis_start", "start"), ("analysis_end", "end")):
            if payload.get(main) is not None and _iso(payload[main]) != fault[side]:
                _fail("time_window_conflict", "具名故障窗口与分析主窗口冲突")
    windows = {"fault": fault}
    for name in ("baseline", "change", "validation"):
        value = supplied.get(name, payload.get(name + "_window"))
        if value is not None:
            windows[name] = _window(value, name=name)
    mapping, catalog = settings.get("identity_mapping", {}), settings.get("query_catalog", {})
    verified = validate_configuration(mapping, catalog)
    issues, candidates = [], []
    if not verified["identity_verified"]:
        issues.append({"code": "identity_mapping_unverified", "detail": "请登记有版本和有效期的对象映射；旧配置未自动升级"})
    else:
        explicit = payload.get("target_identity")
        if explicit is not None:
            explicit = _canonical(explicit)
            for input_field, canonical_field in (("environment", "environment"), ("cluster", "cluster"), ("namespace", "namespace"), ("target_type", "type")):
                if payload.get(input_field) is not None and payload[input_field] != explicit[canonical_field]:
                    _fail("identity_scope_conflict", "显式对象键与分析范围冲突")
        target = payload.get("target")
        if explicit is None and (not isinstance(target, str) or not target.strip()):
            issues.append({"code": "target_required", "detail": "请明确待分析对象及范围"})
        else:
            for row in mapping["records"]:
                canonical = _canonical(row["canonical"])
                if explicit is not None:
                    matches = canonical == explicit
                else:
                    matches = target in [canonical["id"], row["native_id"], *row.get("aliases", [])]
                    scopes = {"environment": payload.get("environment") or settings.get("environment"), "cluster": payload.get("cluster"), "namespace": payload.get("namespace"), "type": payload.get("target_type")}
                    matches = matches and all(value is None or canonical[key] == value for key, value in scopes.items())
                if not matches:
                    continue
                begin, end = _time(fault["start"]), _time(fault["end"])
                lower = _time(row["valid_from"]) if row.get("valid_from") else None
                upper = _time(row["valid_to"]) if row.get("valid_to") else None
                if lower and lower > end or upper and upper < begin:
                    continue
                item = copy.deepcopy(row)
                item["validity_status"] = "verified" if lower and lower <= begin and "valid_to" in row and (upper is None or upper >= end) else "unknown"
                candidates.append(item)
    keys = {canonical_key(r["canonical"]) for r in candidates}
    if verified["identity_verified"] and not candidates and not any(i["code"] == "target_required" for i in issues):
        issues.append({"code": "identity_mapping_missing", "detail": "该对象或故障时段没有有效来源映射"})
    if len(keys) > 1:
        issues.append({"code": "identity_ambiguous", "detail": "对象在给定范围仍有多个身份，请补充环境/集群/namespace/类型/ID"})
    if any(r["validity_status"] != "verified" or not r.get("verified_at") for r in candidates):
        issues.append({"code": "historical_validity_unknown", "detail": "映射有效期或验证时间不足以覆盖故障窗口"})
    source_ids = {}
    for row in candidates:
        source_ids.setdefault((row["source_id"], row["object_type"]), set()).add(row["native_id"])
    if any(len(ids) > 1 for ids in source_ids.values()):
        issues.append({"code": "source_identity_conflict", "detail": "同一对象在同源同类型映射到多个原生 ID"})
    identity_ready = not issues and len(keys) == 1
    canonical = _canonical(candidates[0]["canonical"]) if identity_ready else None
    frozen_catalog = catalog if verified["catalog_verified"] else {"version": None, "entries": [], "legacy_unverified": True}
    offsets = {source: {"seconds": offset.get("seconds"), "status": "verified" if offset.get("seconds") is not None else "unknown", "reason": offset.get("reason"), "verified_at": offset.get("verified_at")} for source, offset in frozen_catalog.get("source_offsets", {}).items()}
    result = {"version": VERSION, "captured_at": captured_at, "status": "ready" if identity_ready else "needs_input", "issues": issues,
              "identity": {"status": "verified" if identity_ready else "needs_input", "canonical": canonical, "canonical_key": canonical_key(canonical) if canonical else None, "mappings": candidates, "candidate_keys": sorted(keys)},
              "windows": windows, "source_offsets": offsets,
              "identity_mapping_version": mapping.get("version") if verified["identity_verified"] else None,
              "identity_mapping_digest": _digest(mapping), "query_catalog_version": frozen_catalog.get("version"), "query_catalog_digest": _digest(catalog), "query_catalog": frozen_catalog}
    return result


def _select(mapping, selector):
    if selector == "native_id":
        return mapping["native_id"]
    if selector == "object_type":
        return mapping["object_type"]
    if selector in {"canonical." + k for k in _FIELDS}:
        return mapping["canonical"][selector.split(".", 1)[1]]
    _fail("query_catalog_invalid", "身份参数必须引用已知来源映射字段")


def _template(template, mapping):
    if not isinstance(template, str):
        _fail("query_catalog_invalid", "查询参数模板必须为字符串")
    tokens = re.findall(r"\{(native_id|object_type|canonical\.(?:environment|cluster|namespace|type|id))_json\}", template)
    # Query languages contain braces; only context-looking tokens are reserved.
    invalid = re.findall(r"\{(?:native_id|object_type|canonical\.)[^{}]*\}", template)
    for token in invalid:
        if not re.fullmatch(r"\{(?:native_id|object_type|canonical\.(?:environment|cluster|namespace|type|id))_json\}", token):
            _fail("query_catalog_invalid", "查询模板只允许 JSON 字符串标量身份占位符")
    for token in tokens:
        template = template.replace("{" + token + "_json}", json.dumps(_select(mapping, token), ensure_ascii=False))
    return template


def bind_query(context, source_id, tool_name, arguments, *, definition_digest=None):
    if not isinstance(context, dict) or context.get("version") != VERSION:
        _fail("analysis_context_unverified", "旧分析缺少确定性上下文，请新建尝试")
    catalog = context.get("query_catalog", {})
    entries = [e for e in catalog.get("entries", []) if e.get("source_id") == source_id and e.get("tool_name") == tool_name]
    if len(entries) != 1 or not catalog.get("version"):
        _fail("query_binding_missing", "工具没有明确版本化查询目录，不能猜测目标或时间参数")
    entry = entries[0]
    if not definition_digest or entry["definition_digest"] != definition_digest:
        _fail("query_definition_changed", "工具定义摘要与冻结查询目录不一致")
    if not isinstance(arguments, dict):
        _fail("query_arguments_invalid", "工具参数必须为对象")
    arguments = copy.deepcopy(arguments)
    scoped = entry.get("object_scope", "target") == "target"
    if scoped and (context.get("status") != "ready" or context.get("identity", {}).get("status") != "verified"):
        _fail("identity_needs_input", "对象身份未确定，禁止查询猜测目标")
    mappings = [m for m in context.get("identity", {}).get("mappings", []) if m.get("source_id") == source_id and (not entry.get("source_object_type") or m.get("object_type") == entry["source_object_type"])]
    mapping = None
    if scoped:
        identities = {(m["object_type"], m["native_id"]) for m in mappings if m.get("validity_status") == "verified"}
        if len(identities) != 1:
            _fail("source_identity_missing", "工具来源没有唯一、有效的对象映射")
        mapping = mappings[0]
    if not scoped and (entry.get("identity_parameters") or entry.get("argument_templates")):
        _fail("query_catalog_invalid", "无目标发现工具不能引用对象身份")
    fixed = copy.deepcopy(entry.get("static_arguments", {}))
    for name, selector in entry.get("identity_parameters", {}).items():
        fixed[name] = _select(mapping, selector)
    for name, template in entry.get("argument_templates", {}).items():
        fixed[name] = _template(template, mapping)
    # A free query body could silently select an unrelated object even with a
    # separately bound target ID; it must be an exact catalog argument/template.
    if scoped and _QUERY_ARGUMENTS.intersection(arguments) - fixed.keys():
        _fail("query_expression_unbound", "对象查询正文必须由目录固定或明确模板绑定")
    selectors = entry.get("identity_parameters", {}).values()
    identity_template = any(isinstance(v, str) and re.search(r"\{(?:native_id|canonical\.id)_json\}", v) for v in entry.get("argument_templates", {}).values())
    if scoped and not (any(v in {"native_id", "canonical.id"} for v in selectors) or identity_template):
        _fail("query_identity_unbound", "对象查询目录缺少显式身份参数或模板")
    name = entry.get("window", "fault")
    window = context.get("windows", {}).get(name)
    if window is None:
        _fail("time_window_required", "查询目录要求的具名窗口尚未冻结")
    if scoped and (not mapping.get("valid_from") or _time(mapping["valid_from"]) > _time(window["start"]) or "valid_to" not in mapping or (mapping.get("valid_to") and _time(mapping["valid_to"]) < _time(window["end"]))):
        _fail("identity_window_unverified", "对象来源映射有效期不能覆盖本次具名查询窗口")
    offset = context.get("source_offsets", {}).get(source_id, {"status": "unknown", "seconds": None})
    seconds = offset.get("seconds") or 0
    projected = {k: (_time(window[k]) + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z") for k in ("start", "end")}
    time_format = entry.get("time_format", "rfc3339")
    for side, parameter in entry.get("time_parameters", {}).items():
        value = projected["start" if side == "start" else "end"]
        if time_format != "rfc3339":
            value = _time(value).timestamp() * (1000 if time_format == "unix_milliseconds" else 1)
            value = int(value) if value.is_integer() else value
        fixed[parameter] = value
    if set(arguments) - fixed.keys() - set(entry.get("allowed_parameters", [])):
        _fail("query_parameter_unbound", "工具参数未在查询目录中显式登记")
    if entry.get("kind") == "telemetry" and not entry.get("time_parameters"):
        _fail("query_time_unbound", "遥测目录必须显式绑定绝对时间参数")
    for parameter, value in fixed.items():
        if parameter in arguments and arguments[parameter] != value:
            # Equivalent RFC3339 UTC spellings represent the same instant.
            equivalent = False
            if parameter in entry.get("time_parameters", {}).values() and time_format == "rfc3339":
                try:
                    equivalent = _time(arguments[parameter]) == _time(value)
                except ContextError:
                    pass
            if not equivalent:
                _fail("query_context_conflict", f"工具参数 {parameter} 与固定对象/窗口/目录冲突")
        arguments[parameter] = value
    return {"arguments": arguments, "binding": {"context_version": VERSION, "catalog_version": catalog["version"], "catalog_digest": context["query_catalog_digest"], "entry_version": entry["version"], "definition_digest": entry["definition_digest"],
            "source_id": source_id, "tool_name": tool_name, "kind": entry.get("kind", "generic"), "object_scope": entry.get("object_scope", "target"), "canonical_key": context.get("identity", {}).get("canonical_key") if scoped else None,
            "mapping": copy.deepcopy(mapping), "mapping_version": context.get("identity_mapping_version"), "window_name": name, "window": copy.deepcopy(window), "source_window": projected, "source_offset": copy.deepcopy(offset),
            "historical_support": None if entry.get("kind") == "topology" and not entry.get("time_parameters") else entry.get("historical"),
            **{key: copy.deepcopy(entry[key]) for key in ("unit", "assessment_rule", "source_group", "source_lineage", "result_identity") if key in entry}}}


def coverage_record(binding=None, *, status="unknown", item_count=None, observed_start=None, observed_end=None,
                    pagination_complete=None, truncated=False, stale=False, historical=False, error=None):
    if status not in COVERAGE_STATES:
        _fail("coverage_invalid", "覆盖状态无效")
    if item_count is not None and (isinstance(item_count, bool) or not isinstance(item_count, int) or item_count < 0):
        _fail("coverage_invalid", "结果数量必须为非负整数或未知")
    binding = copy.deepcopy(binding or {})
    reasons = []
    declared = binding.get("window")
    observed = {"start": _iso(observed_start) if observed_start is not None else None, "end": _iso(observed_end) if observed_end is not None else None}
    if observed["start"] and observed["end"] and _time(observed["start"]) > _time(observed["end"]):
        _fail("coverage_invalid", "结果覆盖开始晚于结束")
    # Observation timestamps are source timestamps; normalize only a known offset.
    offset = binding.get("source_offset", {}).get("seconds")
    normalized = {k: (_time(v) - timedelta(seconds=offset)).isoformat().replace("+00:00", "Z") if v and offset is not None else None for k, v in observed.items()}
    if any(observed.values()) and offset is None:
        reasons.append("source_clock_unknown")
    if declared and any(v and not (_time(declared["start"]) <= _time(v) <= _time(declared["end"])) for v in normalized.values()):
        reasons.append("out_of_window")
    if pagination_complete is False:
        reasons.append("pagination_incomplete")
    if truncated:
        reasons.append("truncated")
    if stale:
        reasons.append("stale")
    historical_support = binding.get("historical_support")
    if historical and historical_support is not True:
        reasons.append("historical_unsupported" if historical_support is False else "historical_unknown")
    if not binding.get("context_version") or binding.get("object_scope") == "none":
        reasons.append("scope_unverified")
    # Failures stay failures; they are never recast as successful empty responses.
    if status not in {"forbidden", "failed"}:
        if "historical_unsupported" in reasons:
            status = "historical_unsupported"
        elif "historical_unknown" in reasons or "scope_unverified" in reasons or "source_clock_unknown" in reasons:
            status = "unknown"
        elif stale:
            status = "stale"
        elif reasons:
            status = "partial"
        elif status in {"complete", "empty"} and item_count == 0:
            status = "empty"
    return {"version": VERSION, "status": status, "reasons": reasons, "item_count": item_count, "declared_window": declared, "source_window": binding.get("source_window"), "observed_window": observed, "normalized_observed_window": normalized,
            "source_offset": binding.get("source_offset", {"status": "unknown", "seconds": None}), "pagination_complete": pagination_complete, "truncated": bool(truncated), "historical": bool(historical),
            "historical_support": historical_support, "source_id": binding.get("source_id"), "tool_name": binding.get("tool_name"), "canonical_key": binding.get("canonical_key"), "binding": binding, "error": copy.deepcopy(error)}


def legacy_context():
    return {"version": None, "status": "unknown", "legacy_unverified": True, "identity": {"status": "unknown", "canonical": None, "canonical_key": None, "mappings": []}, "windows": {}, "source_offsets": {},
            "issues": [{"code": "legacy_unverified", "detail": "历史尝试未记录版本化对象、时间和覆盖契约；不能自动认作完整"}]}
