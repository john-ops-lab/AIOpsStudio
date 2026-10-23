"""Versioned synthetic evaluation contracts, without importing application code."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

BASELINE_REF = "bd9a949cff85e2f5b00b5dfd43d323909bfd0a1f"
SCORER_VERSION = "analysis-evaluation/1"
REQUIRED_VERSIONS = {"model", "skill", "tool_schema", "summarizer", "query_catalog", "privacy_policy"}
DIMENSIONS = ("facts", "omissions", "causality", "coverage", "references", "boundaries")


def canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def digest(value) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def safe_relative(root: Path, relative: str) -> Path:
    target = (root / relative).resolve()
    if not target.is_relative_to(root.resolve()) or not target.is_file():
        raise ValueError("evaluation file must be a regular file inside suite directory")
    return target


def validate_input(case: dict, frozen: dict) -> None:
    """Reject answer-shaped fields before material is sent to an execution child."""
    def check(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key.casefold() in {"oracle", "oracle_path", "expected", "expected_answer", "answer", "reference_answer"}:
                    raise ValueError("Agent-visible input contains an oracle field")
                check(item)
        elif isinstance(value, list):
            for item in value:
                check(item)
    check(case)
    if case.get("schema") != "aiopsstudio-evaluation-input/1":
        raise ValueError("unsupported evaluation input schema")
    if set(case.get("versions", {})) != REQUIRED_VERSIONS:
        raise ValueError("all model/Skill/schema/summarizer/query/privacy versions must be frozen")
    if case["versions"] != frozen["versions"]:
        raise ValueError("case versions differ from frozen suite")
    source = case.get("source", {})
    if not source.get("snapshot_version") or source.get("sha256") != digest(source.get("responses")):
        raise ValueError("source snapshot version or digest is missing or changed")
    request = case.get("request", {})
    for field in ("analysis_start", "analysis_end", "target"):
        if not request.get(field):
            raise ValueError("case must include target and absolute analysis window")
    for key in ("analysis_start", "analysis_end"):
        from datetime import datetime
        stamp = datetime.fromisoformat(request[key].replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError("evaluation window must have timezone")


def load_suite(path):
    path = Path(path).resolve()
    root, suite = path.parent, read_json(path)
    if suite.get("schema") != "aiopsstudio-evaluation-suite/1" or suite.get("scorer_version") != SCORER_VERSION:
        raise ValueError("unsupported suite or scorer version")
    if suite.get("baseline_ref") != BASELINE_REF:
        raise ValueError("current baseline must remain the original bd9a949 commit")
    scorer_source = Path(__file__).with_name("scoring.py").read_bytes()
    if suite.get("scorer_sha256") != hashlib.sha256(scorer_source).hexdigest():
        raise ValueError("scorer source digest differs from frozen suite")
    frozen = read_json(safe_relative(root, suite["frozen"]["path"]))
    if digest(frozen) != suite["frozen"]["sha256"]:
        raise ValueError("frozen configuration digest differs")
    if set(frozen.get("versions", {})) != REQUIRED_VERSIONS:
        raise ValueError("frozen configuration versions are incomplete")
    for key in REQUIRED_VERSIONS:
        entry = frozen["versions"][key]
        if not entry.get("version") or entry.get("sha256") != digest(frozen["components"][key]):
            raise ValueError("frozen component version/digest differs: " + key)
    ids = set()
    for item in suite["cases"]:
        if item["id"] in ids:
            raise ValueError("duplicate evaluation case")
        ids.add(item["id"])
        case = read_json(safe_relative(root, item["input"]))
        if digest(case) != item["input_sha256"]:
            raise ValueError("case input digest differs: " + item["id"])
        validate_input(case, frozen)
        # The oracle content is intentionally loaded only after application replay.
        safe_relative(root, item["oracle"])
    return root, suite, frozen


def execution_material(case, frozen):
    """No suite, case identity, category, expected answers or oracle path in child input."""
    validate_input(case, frozen)
    return {"request": case["request"], "source": case["source"], "model_behavior": case.get("model_behavior", "read_then_report"),
            "checks": case.get("checks", []), "frozen": frozen}
