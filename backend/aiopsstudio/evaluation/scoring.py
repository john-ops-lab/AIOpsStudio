"""Deterministic oracle scoring. No model calls or application execution here."""
from __future__ import annotations

import re
from collections import Counter

from .contracts import DIMENSIONS, SCORER_VERSION, digest

MISSING = object()


def select(value, path):
    for part in path.split(".") if path else []:
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            return MISSING
    return value


def _assertion(observation, assertion):
    value = select(observation, assertion.get("path", ""))
    if value is MISSING:
        return "unknown", "required observation is absent"
    operator, wanted = assertion["operator"], assertion.get("value")
    if operator == "exists":
        passed = value is not None
    elif operator == "equals":
        passed = value == wanted
    elif operator == "one_of":
        passed = value in wanted
    elif operator == "contains":
        passed = wanted in value
    elif operator == "not_contains":
        passed = wanted not in value
    elif operator in {"matches", "not_matches"}:
        passed = bool(re.search(wanted, str(value), re.S)) == (operator == "matches")
    elif operator == "at_least":
        passed = isinstance(value, (int, float)) and value >= wanted
    elif operator == "any_match":
        passed = isinstance(value, list) and any(isinstance(item, dict) and all(item.get(key, MISSING) == expected for key, expected in wanted.items()) for item in value)
    elif operator == "citation_source":
        # A citation must resolve to actual saved evidence of the requested source/tool.
        cited = set(observation.get("run", {}).get("report", {}).get("reference_validation", {}).get("cited_ids", []))
        matching = {e["id"] for e in observation.get("run", {}).get("evidence", []) if e.get("tool_name") == wanted}
        passed = bool(cited & matching)
    else:
        raise ValueError("unsupported assertion operator")
    return ("passed", None) if passed else ("failed", "observation does not satisfy frozen assertion")


def score(observation, oracle):
    """Every assertion is counted. Unknown, harness errors and critical failures cannot pass."""
    if oracle.get("schema") != "aiopsstudio-evaluation-oracle/1" or not oracle.get("assertions"):
        raise ValueError("oracle schema or assertions are missing")
    counts = {name: Counter(passed=0, failed=0, unknown=0, total=0) for name in DIMENSIONS}
    checks, classes = [], Counter()
    for assertion in oracle["assertions"]:
        dimension = assertion["dimension"]
        if dimension not in counts:
            raise ValueError("unknown scoring dimension")
        try:
            status, reason = _assertion(observation, assertion)
            classification = assertion.get("failure_class", "unverified_capability")
        except (TypeError, ValueError, KeyError, re.error):
            status, reason, classification = "failed", "invalid assertion or observation type", "harness_error"
        counts[dimension][status] += 1
        counts[dimension]["total"] += 1
        if status != "passed":
            classes[classification] += 1
        checks.append({"id": assertion["id"], "dimension": dimension, "status": status,
                       "critical": assertion.get("critical", False), "failure_class": classification if status != "passed" else None,
                       "reason": reason})
    totals = {key: sum(group[key] for group in counts.values()) for key in ("passed", "failed", "unknown", "total")}
    harness = observation.get("harness_error")
    if harness:
        classes["harness_error"] += 1
    return {"scorer_version": SCORER_VERSION, "oracle_sha256": digest(oracle),
            "strict_pass": not harness and totals["passed"] == totals["total"],
            "assertions": totals, "dimensions": {name: dict(value) for name, value in counts.items()},
            "failure_classes": dict(classes), "checks": checks, "harness_error": bool(harness)}


def aggregate(results):
    """The case and assertion denominators are separate and retain future/failed cases."""
    assertions = Counter(passed=0, failed=0, unknown=0, total=0)
    classes = Counter()
    dimensions = {name: Counter(passed=0, failed=0, unknown=0, total=0) for name in DIMENSIONS}
    for result in results:
        assertions.update(result["score"]["assertions"])
        classes.update(result["score"]["failure_classes"])
        for name, values in result["score"]["dimensions"].items():
            dimensions[name].update(values)
    return {"cases": {"total": len(results), "strict_passed": sum(r["score"]["strict_pass"] for r in results),
                      "strict_failed": sum(not r["score"]["strict_pass"] for r in results),
                      "not_run": sum(r.get("observation_status") == "not_run" for r in results)},
            "assertions": dict(assertions), "dimensions": {k: dict(v) for k, v in dimensions.items()}, "failure_classes": dict(classes)}


def compare(baseline, candidate):
    if baseline.get("incomparable_cases") or candidate.get("incomparable_cases"):
        raise ValueError("actual source snapshot or frozen versions are unverified; comparison refused")
    if baseline["comparability"] != candidate["comparability"]:
        raise ValueError("inputs, sources, visibility, versions or scorer differ; comparison refused")
    old = {item["case_id"]: item for item in baseline["results"]}
    new = {item["case_id"]: item for item in candidate["results"]}
    if set(old) != set(new):
        raise ValueError("comparison case denominator differs")
    differences = []
    for case_id in old:
        before, after = old[case_id]["score"], new[case_id]["score"]
        differences.append({"case_id": case_id, "before_strict_pass": before["strict_pass"], "after_strict_pass": after["strict_pass"],
                            "assertion_delta": {key: after["assertions"][key] - before["assertions"][key] for key in ("passed", "failed", "unknown")},
                            "critical_regressions": [check["id"] for check in after["checks"] if check["critical"] and check["status"] != "passed"
                                                     and any(c["id"] == check["id"] and c["status"] == "passed" for c in before["checks"])]})
    return {"schema": "aiopsstudio-evaluation-comparison/1", "baseline_ref": baseline["code_ref"], "candidate_ref": candidate["code_ref"],
            "comparable": True, "results": differences, "publication_gate": "not_implemented_R05"}
