"""Deterministic facts and bounded claims from already verified saved JSON.

The caller authorizes and verifies saved bytes before passing documents. This
module does not fetch data, trust model-supplied assessments, or establish cause.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
from typing import Any

from .material_reading import ReadingError, logical_document, pointer_get

VERSION = "grounded-report/1"
RULES = {"scalar", "count", "min", "max", "first", "last", "relation"}
KINDS = {"observation", "potential_impact", "candidate", "root_cause", "normal", "anomaly"}
_MESSAGE = {
    "source_failed": "源调用失败，不能用于确认事实或正常范围", "storage_incomplete": "保存材料不完整，不能引用内部片段确认事实",
    "logical_response_incomplete": "逻辑响应传输未完整核验，不能引用其中片段", "coverage_unknown": "源覆盖未知，不能扩大为全局结论",
    "coverage_limited": "源查询覆盖受限，只表示已取得范围", "source_stale": "材料陈旧，不代表故障窗口现状",
    "historical_unsupported": "源不支持或未核验故障时段历史", "window_unverified": "对象或具名窗口未与固定分析上下文核对",
    "unit_unknown": "源和固定查询目录没有登记单位", "unknown_schema": "材料未登记可自动解释的结构，未猜测业务语义",
    "free_text_unverified": "自由文本为未核验模型解释，不能视作已确认事实", "malformed_claim": "结构化结论字段不完整或类型无效",
    "evidence_missing": "引用证据不存在于当前已保存材料", "pointer_invalid": "引用位置不存在或不支持该计算规则",
    "value_mismatch": "结论值与保存材料的确定性计算不一致", "unit_mismatch": "结论单位未被源或固定目录支持",
    "window_mismatch": "结论窗口与实际查询窗口不一致", "anchor_invalid": "片段锚点不存在或与证据/位置不一致",
    "object_identity_unknown": "保存返回对象尚未按登记身份字段和完整范围核对，不能用查询目标代替返回对象",
    "assessment_missing": "没有冻结的确定性阈值/评估规则，异常或正常标签待核实", "assessment_mismatch": "异常或正常标签与固定规则不一致",
    "root_cause_unconfirmed": "时间、拓扑和数值引用不能确认根因，需后续人工裁决", "candidate_unconfirmed": "候选解释尚未确认",
    "topology_potential_only": "拓扑关系仅表示潜在影响，不表示已发生业务故障", "conflicting_values": "相同范围的保存值存在冲突，未选择性采信",
}


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _id(prefix, value):
    return prefix + "_" + hashlib.sha256(_canonical(value).encode()).hexdigest()[:32]


def _number(value):
    if isinstance(value, bool) or value is None:
        raise ValueError()
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError() from None
    if not number.is_finite():
        raise ValueError()
    result = int(number) if number == number.to_integral_value() else float(number)
    if isinstance(result, float) and not math.isfinite(result):
        raise ValueError()
    return result


def _stamp(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, timezone.utc).timestamp()
    if not isinstance(value, str):
        raise ValueError()
    value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError()
    return value.timestamp()


def _same_window(first, second):
    try:
        return all(_stamp(first[key]) == _stamp(second[key]) for key in ("start", "end"))
    except (KeyError, ValueError, TypeError, OSError, OverflowError):
        return False


def _context(run):
    return run.get("analysis_context") or run.get("context") or run.get("config_snapshot", {}).get("analysis_context") or {}


def _binding(record):
    return record.get("query_binding") or (record.get("coverage") or {}).get("binding") or {}


def _metadata(run, record):
    coverage, binding, context = record.get("coverage") or {}, _binding(record), _context(run)
    name = binding.get("window_name", "fault")
    declared = coverage.get("declared_window") or binding.get("window")
    expected = context.get("windows", {}).get(name)
    canonical = context.get("identity", {}).get("canonical_key")
    source_key = coverage.get("canonical_key") or binding.get("canonical_key")
    window_verified = bool(context.get("version") and canonical and canonical == source_key and _same_window(declared, expected))
    limitations = []
    status = coverage.get("status", "unknown")
    if status in {"failed", "forbidden"}:
        limitations.append("source_failed")
    elif status == "stale":
        limitations.append("source_stale")
    elif status == "historical_unsupported" or coverage.get("historical") and coverage.get("historical_support") is not True:
        limitations.append("historical_unsupported")
    elif status in {"unknown", None}:
        limitations.append("coverage_unknown")
    elif status not in {"complete", "empty"} or coverage.get("pagination_complete") is False or coverage.get("truncated"):
        limitations.append("coverage_limited")
    if not window_verified:
        limitations.append("window_unverified")
    lineage = copy.deepcopy(record.get("source_lineage") or binding.get("source_lineage") or {})
    source_group = record.get("source_group") or binding.get("source_group")
    if not source_group and isinstance(lineage, dict):
        source_group = lineage.get("source_group") or lineage.get("root_source") or lineage.get("system_id")
    if not source_group:
        source_group = "source:" + str(record.get("source") or coverage.get("source_id") or "unknown")
    unit = record.get("unit", binding.get("unit"))
    return {"object": None, "query_scope": source_key, "window": copy.deepcopy(declared), "window_name": name,
            "window_verified": window_verified, "coverage_status": status, "limitations": limitations,
            "source_group": source_group, "source_lineage": lineage, "unit": unit,
            "unit_status": "verified" if isinstance(unit, str) and unit else "unknown"}


def _result_object(run, record, logical, pointer):
    """Bind the selected returned row, never smear query scope across a list.

    The administrator freezes result_identity in the query binding. A result
    can have valid numbers without enough information to identify its object.
    """
    binding, context = _binding(record), _context(run)
    contract, mapping = binding.get("result_identity"), binding.get("mapping")
    if not isinstance(contract, dict) or not isinstance(contract.get("version"), str) or not contract["version"] or not isinstance(mapping, dict):
        return None
    canonical = mapping.get("canonical")
    if (not isinstance(canonical, dict) or any(not isinstance(canonical.get(key), str) for key in ("environment", "cluster", "namespace", "type", "id"))
            or context.get("identity", {}).get("canonical") != canonical):
        return None
    source = binding.get("source_id") or (record.get("coverage") or {}).get("source_id") or record.get("source")
    if mapping.get("source_id") != source or record.get("source") != source:
        return None
    selected = logical
    collection_path = contract.get("collection")
    try:
        if collection_path is not None:
            if not isinstance(collection_path, str):
                return None
            collection = pointer_get(logical, collection_path)
            prefix = collection_path + "/"
            if not isinstance(collection, list) or not pointer.startswith(prefix):
                return None
            index = pointer[len(prefix):].split("/", 1)[0]
            if not index.isdigit() or len(index) > 1 and index.startswith("0") or int(index) >= len(collection):
                return None
            selected = collection[int(index)]
        else:
            # A singleton document rule cannot identify arbitrary returned rows.
            current = logical
            for segment in pointer.split("/")[1:] if pointer else []:
                if isinstance(current, list):
                    return None
                segment = segment.replace("~1", "/").replace("~0", "~")
                if not isinstance(current, dict) or segment not in current:
                    return None
                current = current[segment]
        if not isinstance(selected, dict) or not isinstance(contract.get("pointer"), str):
            return None
        identity = pointer_get(selected, contract["pointer"])
        selector = contract.get("mapping")
        expected = mapping.get("native_id") if selector == "native_id" else canonical["id"] if selector == "canonical.id" else None
        if not isinstance(identity, str) or not isinstance(expected, str) or identity != expected:
            return None
        fields, fixed = contract.get("scope_fields", {}), contract.get("fixed_scope", {})
        if not isinstance(fields, dict) or not isinstance(fixed, dict):
            return None
        dimensions = {"environment", "cluster", "namespace", "type"}
        if set(fields) - dimensions or set(fixed) - dimensions:
            return None
        for dimension in dimensions:
            values = []
            if dimension in fields:
                values.append(pointer_get(selected, fields[dimension]))
            if dimension in fixed:
                values.append(fixed[dimension])
            if not values or any(not isinstance(value, str) or value != canonical[dimension] for value in values):
                return None
        key = (record.get("coverage") or {}).get("canonical_key") or binding.get("canonical_key")
        return key if key and key == context.get("identity", {}).get("canonical_key") else None
    except (ReadingError, KeyError, TypeError, ValueError):
        return None


def _usable(record, native, logical):
    if (record.get("status") == "error" or isinstance(native, dict) and native.get("isError")
            or (record.get("coverage") or {}).get("status") in {"failed", "forbidden"}):
        return "source_failed"
    if record.get("truncated") or record.get("status") not in {"saved", "partial"}:
        return "storage_incomplete"
    if record.get("integrity_verified") is False or record.get("logical_response_complete") is False:
        return "logical_response_incomplete"
    if isinstance(logical, dict):
        delivery = logical.get("delivery")
        if isinstance(delivery, dict) and (delivery.get("complete") is False or delivery.get("has_more") is True
                or delivery.get("integrity_verified") is False or delivery.get("continuation_required") is True
                or delivery.get("status") in {"incomplete", "partial", "pending", "failed", "rejected"}):
            return "logical_response_incomplete"
        if (isinstance(delivery, dict) and (delivery.get("mode") in {"chunked", "segmented", "multipart"}
                or any(key in delivery for key in ("part_index", "total_parts", "next_cursor", "continuation_token")))
                and not (record.get("logical_response_complete") is True and record.get("integrity_verified") is True)):
            return "logical_response_incomplete"
        verification = logical.get("verification")
        if isinstance(verification, dict) and verification.get("status") in {"failed", "rejected", "invalid"}:
            return "logical_response_incomplete"
        if isinstance(verification, dict) and isinstance(verification.get("checks"), list) and any(
                isinstance(check, dict) and check.get("passed") is False for check in verification["checks"]):
            return "logical_response_incomplete"
    return None


def _series_values(selected):
    if not isinstance(selected, list) or not selected:
        raise ValueError()
    if all(isinstance(row, (list, tuple)) and len(row) == 2 for row in selected):
        stamps = [_number(row[0]) for row in selected]
        if stamps != sorted(stamps):
            raise ValueError()
        return [_number(row[1]) for row in selected], stamps
    return [_number(value) for value in selected], None


def _calculate(logical, pointer, rule):
    selected = pointer_get(logical, pointer)
    if rule == "scalar":
        if isinstance(selected, (dict, list)) or isinstance(selected, float) and not math.isfinite(selected):
            raise ValueError()
        return copy.deepcopy(selected), None
    if rule == "relation":
        if not isinstance(selected, dict) or not isinstance(selected.get("direction"), str):
            raise ValueError()
        keys = next((pair for pair in (("source", "target"), ("source_ci_id", "target_ci_id"), ("source_model", "target_model")) if all(k in selected for k in pair)), None)
        if not keys:
            raise ValueError()
        return {**{key: copy.deepcopy(selected[key]) for key in keys}, "direction": selected["direction"]}, None
    if not isinstance(selected, list):
        raise ValueError()
    if (isinstance(logical, dict) and isinstance(logical.get("data"), dict) and logical["data"].get("resultType") == "vector"
            and pointer.startswith("/data/result/") and pointer.endswith("/value")):
        selected = [selected]
    if rule == "count":
        return len(selected), None
    values, stamps = _series_values(selected)
    value = {"min": min, "max": max, "first": lambda data: data[0], "last": lambda data: data[-1]}[rule](values)
    return value, {"start": stamps[0], "end": stamps[-1]} if stamps else None


def _fact(run, record, logical, root, pointer, rule, *, labels=None, anchor_id=None):
    value, sample_window = _calculate(logical, pointer, rule)
    metadata = _metadata(run, record)
    reasons = list(metadata["limitations"])
    metadata["object"] = _result_object(run, record, logical, pointer)
    if metadata["object"] is None:
        reasons.append("object_identity_unknown")
    if rule == "count":
        metadata.update(unit="items", unit_status="verified", unit_basis="deterministic_count")
    elif rule == "relation" or not isinstance(value, (int, float)) or isinstance(value, bool):
        metadata.update(unit=None, unit_status="not_applicable", unit_basis="not_numeric")
    elif metadata["unit_status"] != "verified":
        reasons.append("unit_unknown")
    if sample_window:
        try:
            offset = (record.get("coverage") or {}).get("source_offset", {}).get("seconds")
            if offset is None or not metadata["window"] or sample_window["start"] - offset < _stamp(metadata["window"]["start"]) or sample_window["end"] - offset > _stamp(metadata["window"]["end"]):
                reasons.append("window_unverified")
                metadata["window_verified"] = False
        except (ValueError, TypeError, KeyError):
            reasons.append("window_unverified")
            metadata["window_verified"] = False
    coordinate = {"evidence_id": record["id"], "saved_sha256": record.get("sha256"), "saved_root": root, "pointer": pointer, "rule": rule}
    return {"id": _id("fact", coordinate), **coordinate, **metadata, "source": record.get("source"), "value": value,
            "labels": copy.deepcopy(labels), "sample_window": sample_window, "anchor_id": anchor_id,
            "limitations": sorted(set(reasons)), "verification": {"value": "verified", "unit": metadata["unit_status"],
                "object": "verified" if metadata["object"] else "unknown",
                "window": "verified" if metadata["window_verified"] else "unknown", "source_truth": "unverified"},
            "scope": "saved_material_only" if reasons else "declared_query_only"}


def _prometheus_series(logical):
    if not isinstance(logical, dict) or logical.get("status") != "success" or not isinstance(logical.get("data"), dict):
        return None
    data = logical["data"]
    if data.get("resultType") not in {"matrix", "vector"} or not isinstance(data.get("result"), list):
        return None
    result = []
    for index, series in enumerate(data["result"]):
        if not isinstance(series, dict) or not isinstance(series.get("metric"), dict):
            return None
        field = "values" if data["resultType"] == "matrix" else "value"
        samples = series.get(field)
        if field == "value":
            samples = [samples]
        try:
            _series_values(samples)
        except (ValueError, TypeError):
            return None
        result.append((index, field, copy.deepcopy(series["metric"])))
    return result


def _strict_equal(first, second):
    # bool must not self-certify numeric 0/1; ordinary JSON int/float are equal.
    if isinstance(first, bool) != isinstance(second, bool):
        return False
    return first == second


def _assessment(record, fact, kind):
    rule = record.get("assessment_rule") or _binding(record).get("assessment_rule")
    if not isinstance(rule, dict) or not isinstance(rule.get("version"), str) or not rule["version"]:
        return "assessment_missing"
    if rule.get("rule") not in (None, fact["rule"]) or rule.get("pointer") not in (None, fact["pointer"]):
        return "assessment_missing"
    if not rule.get("unit") or fact["unit_status"] != "verified" or rule.get("unit") != fact["unit"]:
        return "unit_mismatch"
    try:
        value, threshold = _number(fact["value"]), _number(rule.get("threshold"))
        operator = rule["operator"]
        outcome = {"gt": lambda: value > threshold, "gte": lambda: value >= threshold,
            "lt": lambda: value < threshold, "lte": lambda: value <= threshold,
            "eq": lambda: value == threshold, "ne": lambda: value != threshold}[operator]()
    except (KeyError, ValueError, TypeError):
        return "assessment_missing"
    return None if outcome == (kind == "anomaly") else "assessment_mismatch"


def _anchor_valid(anchor_id, record, pointer, material_reads):
    if anchor_id is None:
        return True
    for receipt in material_reads:
        anchor = receipt.get("anchor") or {}
        if receipt.get("anchor_id") != anchor_id or anchor.get("resource_type") != "evidence" or anchor.get("evidence_id") != record["id"] or anchor.get("sha256") != record.get("sha256"):
            continue
        base = (anchor.get("position") or {}).get("pointer", (anchor.get("selector") or {}).get("pointer", ""))
        if pointer == base or pointer.startswith(base + "/"):
            selector, position = anchor.get("selector") or {}, anchor.get("position") or {}
            filtered = any(key in selector for key in ("time_start", "time_end", "fields"))
            if filtered and not {"source_indices", "source_total_items", "projection_fields"}.issubset(position):
                return False  # Old filtered view offsets are not source coordinates.
            suffix = pointer[len(base):].lstrip("/")
            parts = suffix.split("/") if suffix else []
            parts = [part.replace("~1", "/").replace("~0", "~") for part in parts]
            indices, projected = position.get("source_indices"), position.get("projection_fields")
            if projected is not None and (not isinstance(projected, list) or any(not isinstance(field, str) for field in projected)):
                return False
            if isinstance(indices, list):
                if (any(type(index) is not int or index < 0 for index in indices) or len(set(indices)) != len(indices)):
                    return False
                if not parts:
                    total = position.get("source_total_items")
                    if (type(total) is not int or total < 0 or indices != list(range(total))
                            or projected is not None or any(key in selector for key in ("time_start", "time_end"))):
                        return False
                else:
                    first = parts.pop(0)
                    if not first.isdigit() or len(first) > 1 and first.startswith("0") or int(first) not in indices:
                        return False
                    if projected is not None and (not parts or parts[0] not in projected):
                        return False
            else:
                if any(key in selector for key in ("time_start", "time_end")):
                    return False
                if projected is not None and (not parts or parts[0] not in projected):
                    return False
                # Legacy unfiltered anchors preserve the original offset range.
                if pointer == base and position and (position.get("offset", 0) != 0 or position.get("end") != position.get("total_saved_items")):
                    return False
                if parts and parts[0].isdigit() and "offset" in position and "end" in position:
                    if not position["offset"] <= int(parts[0]) < position["end"]:
                        return False
            return True
    return False


def _parse(analysis_text):
    if isinstance(analysis_text, dict):
        data = copy.deepcopy(analysis_text)
    elif isinstance(analysis_text, str):
        try:
            data = json.loads(analysis_text, parse_constant=lambda value: (_ for _ in ()).throw(ValueError()))
        except ValueError:
            return {"analysis": analysis_text, "claims": []}, True
        if not isinstance(data, dict):
            return {"analysis": analysis_text, "claims": []}, True
    else:
        return {"analysis": "", "claims": []}, True
    if not isinstance(data.get("analysis", ""), str) or not isinstance(data.get("claims", []), list):
        return {"analysis": str(data.get("analysis", ""))[:64000], "claims": []}, True
    try:
        _canonical(data)
    except (ValueError, TypeError):
        return {"analysis": data.get("analysis", ""), "claims": []}, True
    return data, False


def ground_report(run, analysis_text, evidence_documents, material_reads=None):
    """Build a report without treating model wording as independently verified.

    ``evidence_documents`` are (record, native) pairs whose authorization and
    stored-byte hashes were already checked by the caller. Claims use pointers
    relative to material_reading.logical_document(native), not arbitrary files.
    """
    data, free_text = _parse(analysis_text)
    material_reads = material_reads or []
    version = _id("report", {"version": VERSION, "run_id": run.get("id"), "attempt_id": run.get("attempt_id"),
        "model_report": data, "evidence": [(record.get("id"), record.get("sha256")) for record, _ in evidence_documents]})
    report = {"schema": VERSION, "report_version": version, "analysis": data.get("analysis", ""),
        "analysis_status": "unverified_model_interpretation", "facts": [], "claims": [], "potential_impacts": [],
        "observed_anomalies": [], "counterevidence": [], "unknowns": [], "candidates": [],
        "root_cause_confirmation": "undetermined", "business_acceptance": "unverified"}
    unknown_keys = set()
    def unknown(code, evidence_id=None, claim_id=None):
        key = (code, evidence_id, claim_id)
        if key not in unknown_keys:
            unknown_keys.add(key)
            report["unknowns"].append({"code": code, "detail": _MESSAGE.get(code, "该结论尚未通过确定性核对"),
                                       "evidence_id": evidence_id, "claim_id": claim_id})
    if free_text:
        unknown("free_text_unverified")
    elif data.get("analysis") and not data.get("claims"):
        unknown("free_text_unverified")
    documents, facts = {}, {}
    def add_fact(fact):
        facts.setdefault(fact["id"], fact)
        for reason in fact["limitations"]:
            unknown(reason, fact["evidence_id"])
        return facts[fact["id"]]
    for record, native in evidence_documents:
        if not isinstance(record, dict) or not isinstance(record.get("id"), str):
            continue
        logical, root = logical_document(native)
        issue = _usable(record, native, logical)
        documents[record["id"]] = (record, logical, root, issue)
        if issue:
            unknown(issue, record["id"])
            continue
        series = _prometheus_series(logical)
        if series is None:
            unknown("unknown_schema", record["id"])
            continue
        for index, field, labels in series:
            pointer = "/data/result/" + str(index) + "/" + field
            generated = {}
            for rule in ("first", "last", "min", "max", "count"):
                try:
                    generated[rule] = add_fact(_fact(run, record, logical, root, pointer, rule, labels=labels))
                except (ReadingError, ValueError, TypeError):
                    unknown("pointer_invalid", record["id"])
            if generated.get("last") and generated.get("max") and generated["last"]["value"] < generated["max"]["value"]:
                report["counterevidence"].append({"kind": "tail_below_peak", "status": "observed_pattern_only",
                    "detail": "保存序列末端值低于该序列峰值；未据此确认恢复正常", "labels": labels,
                    "fact_ids": [generated["last"]["id"], generated["max"]["id"]], "evidence_id": record["id"],
                    "scope": "saved_material_only", "limitations": generated["last"]["limitations"]})
    for index, raw in enumerate(data.get("claims", [])[:256]):
        claim_id = _id("claim", {"report_version": version, "index": index, "claim": raw})
        claim = {"id": claim_id, "report_version": version, "status": "unverified", "fact_ids": [], "reasons": [],
                 "independent_source_groups": [], "independent_support_count": 0}
        report["claims"].append(claim)
        if not isinstance(raw, dict) or raw.get("kind") not in KINDS or raw.get("rule") not in RULES or not isinstance(raw.get("evidence_id"), str) or not isinstance(raw.get("pointer"), str) or "value" not in raw:
            claim["reasons"].append("malformed_claim")
            unknown("malformed_claim", claim_id=claim_id)
            continue
        claim.update({key: copy.deepcopy(raw[key]) for key in ("kind", "evidence_id", "pointer", "rule", "value", "unit", "window", "anchor_id", "next_check") if key in raw})
        claim["model_text"] = raw.get("text", "") if isinstance(raw.get("text", ""), str) else ""
        claim["wording_status"] = "unverified_model_interpretation"
        claim["verification_scope"] = "structured_fields_only"
        claim["text"] = raw["rule"] + "(" + raw["pointer"] + ") = " + _canonical(raw["value"])
        if isinstance(raw.get("unit"), str):
            claim["text"] += " " + raw["unit"]
        evidence_id = raw["evidence_id"]
        document = documents.get(evidence_id)
        if not document:
            claim["reasons"].append("evidence_missing")
        else:
            record, logical, root, issue = document
            if issue:
                claim["reasons"].append(issue)
            elif not _anchor_valid(raw.get("anchor_id"), record, raw["pointer"], material_reads):
                claim["reasons"].append("anchor_invalid")
            else:
                try:
                    fact = add_fact(_fact(run, record, logical, root, raw["pointer"], raw["rule"], anchor_id=raw.get("anchor_id")))
                    claim["fact_ids"] = [fact["id"]]
                    claim["independent_source_groups"] = [fact["source_group"]]
                    claim["independent_support_count"] = 1
                    if not _strict_equal(raw["value"], fact["value"]):
                        claim["reasons"].append("value_mismatch")
                    if "unit" in raw and (raw["unit"] != fact["unit"] or raw["unit"] is not None and fact["unit_status"] != "verified"):
                        claim["reasons"].append("unit_mismatch")
                    if "window" in raw and not _same_window(raw["window"], fact["window"]):
                        claim["reasons"].append("window_mismatch")
                    claim["reasons"].extend(fact["limitations"])
                    if raw["kind"] in {"normal", "anomaly"}:
                        assessment_issue = _assessment(record, fact, raw["kind"])
                        if assessment_issue:
                            claim["reasons"].append(assessment_issue)
                    if raw["kind"] == "root_cause":
                        claim["reasons"].append("root_cause_unconfirmed")
                    if raw["kind"] == "candidate":
                        claim["reasons"].append("candidate_unconfirmed")
                    if raw["kind"] == "potential_impact":
                        if raw["rule"] != "relation":
                            claim["reasons"].append("pointer_invalid")
                        else:
                            claim["status"] = "potential_only" if not claim["reasons"] else "unverified"
                            report["potential_impacts"].append({"claim_id": claim_id, "fact_ids": claim["fact_ids"], "status": claim["status"], "detail": _MESSAGE["topology_potential_only"]})
                    elif not claim["reasons"]:
                        claim["status"] = "verified"
                    if raw["kind"] == "anomaly" and claim["status"] == "verified":
                        report["observed_anomalies"].append({"claim_id": claim_id, "fact_ids": claim["fact_ids"], "status": "observed_in_declared_scope"})
                    if raw["kind"] == "normal" and claim["status"] == "verified":
                        report["counterevidence"].append({"kind": "normal_control", "claim_id": claim_id, "fact_ids": claim["fact_ids"], "status": "normal_under_frozen_rule_only"})
                except (ReadingError, ValueError, TypeError, KeyError):
                    claim["reasons"].append("pointer_invalid")
        claim["reasons"] = sorted(set(claim["reasons"]))
        for reason in claim["reasons"]:
            unknown(reason, evidence_id, claim_id)
        if claim.get("kind") in {"candidate", "root_cause"}:
            report["candidates"].append({"claim_id": claim_id, "text": claim.get("text", ""), "fact_ids": claim["fact_ids"],
                "status": "unresolved", "next_check": copy.deepcopy(claim.get("next_check"))})
    report["facts"] = list(facts.values())
    conflict_groups = {}
    for fact in report["facts"]:
        key = _canonical({"object": fact["object"], "window": fact["window"], "labels": fact["labels"],
                          "rule": fact["rule"], "pointer": fact["pointer"], "source_group": fact["source_group"]})
        conflict_groups.setdefault(key, []).append(fact)
    conflicting_fact_ids = set()
    for group in conflict_groups.values():
        if len({_canonical(fact["value"]) for fact in group}) > 1:
            ids = [fact["id"] for fact in group]
            conflicting_fact_ids.update(ids)
            report["counterevidence"].append({"kind": "conflicting_saved_values", "status": "unresolved", "fact_ids": ids,
                                              "detail": _MESSAGE["conflicting_values"]})
            unknown("conflicting_values")
    for claim in report["claims"]:
        if conflicting_fact_ids.intersection(claim["fact_ids"]):
            claim["status"] = "unverified"
            claim["reasons"] = sorted(set(claim["reasons"] + ["conflicting_values"]))
            unknown("conflicting_values", claim.get("evidence_id"), claim["id"])
    invalidated = {claim["id"] for claim in report["claims"] if claim["status"] == "unverified"}
    report["observed_anomalies"] = [item for item in report["observed_anomalies"] if item["claim_id"] not in invalidated]
    report["counterevidence"] = [item for item in report["counterevidence"] if item.get("kind") != "normal_control" or item["claim_id"] not in invalidated]
    # Conservative lineage accounting: wrappers/tools never multiply support.
    groups = sorted({fact["source_group"] for fact in report["facts"]})
    verified = sum(claim["status"] == "verified" for claim in report["claims"])
    unverified = sum(claim["status"] == "unverified" for claim in report["claims"])
    report["validation"] = {"status": "partial" if free_text or unverified or report["unknowns"] else "verified",
        "scope": "structured_claims_only",
        "structured_claim_count": len(report["claims"]), "verified_claim_count": verified, "unverified_claim_count": unverified,
        "deterministic_fact_count": len(facts), "independent_source_groups": groups, "independent_source_count": len(groups),
        "analysis_verified": False, "causal_confirmation": "not_performed", "global_normality": "not_claimed"}
    return report
