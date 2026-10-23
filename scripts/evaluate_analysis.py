#!/usr/bin/env python3
"""Replay the frozen baseline before comparing a candidate. All output is external.

Example: backend/.venv/bin/python scripts/evaluate_analysis.py --baseline-only
         --output /absolute/external/evaluation-run
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
# Load evaluation tooling independently of the selected application package.
# A frozen baseline therefore imports its original aiopsstudio implementation.
SPEC = importlib.util.spec_from_file_location("aiopsstudio_evaluation_tools", ROOT / "backend/aiopsstudio/evaluation/__init__.py",
                                            submodule_search_locations=[str(ROOT / "backend/aiopsstudio/evaluation")])
PACKAGE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = PACKAGE
SPEC.loader.exec_module(PACKAGE)
from aiopsstudio_evaluation_tools.contracts import BASELINE_REF, SCORER_VERSION, canonical, digest, execution_material, load_suite, read_json, safe_relative


def write_new(path, value):
    with Path(path).open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def git(*args):
    return subprocess.run(["git", "-C", str(ROOT), *args], check=True, capture_output=True).stdout


def external_output(path):
    path = Path(path).expanduser().resolve()
    if path.is_relative_to(ROOT) or not path.is_absolute():
        raise ValueError("raw evaluation output must be outside the repository")
    path.mkdir(parents=True, exist_ok=False, mode=0o700)
    os.chmod(path, 0o700)
    return path


def code_root(ref, temp):
    if ref == "working-tree":
        target = Path(temp) / "working-tree"
        package = target / "backend/aiopsstudio"
        package.parent.mkdir(parents=True)
        shutil.copytree(ROOT / "backend/aiopsstudio", package, ignore=shutil.ignore_patterns("__pycache__", "evaluation"))
        material = {str(path.relative_to(target)): hashlib.sha256(path.read_bytes()).hexdigest() for path in package.rglob("*") if path.is_file()}
        # Every attempt runs this one copy; parallel development cannot change
        # the implementation halfway through a candidate measurement.
        return target, "working-tree@" + git("rev-parse", "HEAD").decode().strip(), digest(material)
    resolved = git("rev-parse", ref + "^{commit}").decode().strip()
    payload = git("archive", resolved, "backend")
    target = Path(temp) / resolved
    target.mkdir()
    with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
        archive.extractall(target, filter="data")
    return target, resolved, hashlib.sha256(payload).hexdigest()


def run_worker(material, app_root, directory, live_config=None):
    directory.mkdir(mode=0o700)
    input_path, result_path = directory / "execution-input.json", directory / "observation.json"
    write_new(input_path, material)
    command = [sys.executable, str(Path(__file__).resolve()), "--worker", "--input-file", str(input_path),
               "--result-file", str(result_path), "--app-root", str(app_root)]
    if live_config:
        command.extend(["--live-model-config", str(live_config)])
    # Do not inherit provider credentials, tracing exporters or application state.
    environment = {key: value for key, value in os.environ.items() if key in {"PATH", "LANG", "LC_ALL", "TMPDIR", "SSL_CERT_FILE", "SSL_CERT_DIR"}}
    environment.update(HOME=str(directory), PYTHONDONTWRITEBYTECODE="1")
    completed = subprocess.run(command, capture_output=True, env=environment, timeout=120)
    if completed.returncode or not result_path.exists():
        # Logs may contain a provider error. Never project arbitrary subprocess stderr.
        return {"schema": "aiopsstudio-evaluation-observation/1", "harness_error": "application_replay_subprocess_failed",
                "subprocess_exit_code": completed.returncode}
    return read_json(result_path)


def measure(root, suite, frozen, ref, destination, *, attempts=1, cases=None, live_config=None):
    from aiopsstudio_evaluation_tools.scoring import aggregate, score
    destination.mkdir(mode=0o700)
    selected = set(cases or (case["id"] for case in suite["cases"]))
    if selected - {case["id"] for case in suite["cases"]}:
        raise ValueError("unknown selected case")
    with tempfile.TemporaryDirectory(prefix="aiopsstudio-evaluation-code-") as temporary:
        app_root, resolved, code_digest = code_root(ref, temporary)
        results, retries, incomparable = [], [], []
        for item in suite["cases"]:
            case = read_json(safe_relative(root, item["input"]))
            if digest(case) != item["input_sha256"]:
                raise ValueError("frozen case input changed during measurement")
            observations = []
            if item["id"] in selected:
                for index in range(attempts):
                    attempt = destination / (item["id"] + "-attempt-" + str(index + 1))
                    observation = run_worker(execution_material(case, frozen), app_root, attempt, live_config)
                    if not observation.get("frozen_validation", {}).get("matched") or observation.get("source_snapshot_sha256") != case["source"]["sha256"]:
                        incomparable.append({"case_id": item["id"], "attempt": index + 1})
                    observations.append(observation)
            else:
                observations.append({"harness_error": "not_run", "observation_status": "not_run"})
            # Oracle is first loaded after all execution subprocesses for this case finish.
            oracle = read_json(safe_relative(root, item["oracle"]))
            if digest(oracle) != item["oracle_sha256"]:
                raise ValueError("oracle digest differs: " + item["id"])
            first = None
            for index, observation in enumerate(observations):
                result = {"case_id": item["id"], "category": case["category"], "work_packages": case["work_packages"],
                          "attempt": index + 1, "retry_of": None if index == 0 else item["id"] + ":1",
                          "observation_status": observation.get("observation_status", "executed"),
                          "measurement_kind": observation.get("measurement_kind"), "measurement": observation.get("measurement"),
                          "score": score(observation, oracle)}
                if index == 0:
                    results.append(result)
                    first = result
                else:
                    retries.append(result)
            print(json.dumps({"code_ref": resolved, "case_id": item["id"], "strict_pass": first["score"]["strict_pass"],
                              "assertions": first["score"]["assertions"]}, ensure_ascii=False), flush=True)
    model_identity = {"mode": "synthetic", "version": frozen["versions"]["model"]}
    if suite.get("measurement_adapter"):
        model_identity["adapter_sha256"] = hashlib.sha256((ROOT / "backend/aiopsstudio/evaluation/replay.py").read_bytes()).hexdigest()
    if live_config:
        from aiopsstudio_evaluation_tools.replay import private_model_config
        config = private_model_config(live_config, ROOT)
        model_identity = {"mode": "live", "provider": config["provider"], "model": config["model"],
                          "endpoint_sha256": hashlib.sha256(config["base_url"].encode()).hexdigest(),
                          "max_tokens": config.get("max_tokens", 4096), "temperature": config.get("temperature", frozen["components"]["model"].get("temperature")),
                          "extra_body": config.get("extra_body", {})}
    summary = {"schema": "aiopsstudio-evaluation-summary/1", "created_at": datetime.now(timezone.utc).isoformat(),
               "code_ref": resolved, "code_sha256": code_digest, "scorer_version": SCORER_VERSION,
               "comparability": {"suite_sha256": digest(suite), "frozen_sha256": digest(frozen), "scorer_sha256": suite["scorer_sha256"], "model_identity": model_identity,
                                 "visibility": "only_fixed_request_and_actual_tool_reading_views"},
               "results": results, "retries": retries, "incomparable_cases": incomparable, "totals": aggregate(results),
               "limitations": ["synthetic sources are not business acceptance", "synthetic model measures pipeline fidelity, not real LLM reasoning",
                               "SQLite replay is not PostgreSQL restore verification", "unknown cost/usage never treated as zero",
                               "first attempts determine totals; retries never replace first failures"]}
    write_new(destination / "summary.json", summary)
    return summary


def worker(args):
    from aiopsstudio_evaluation_tools.replay import guard_execution, private_model_config, replay
    material = read_json(args.input_file)
    live = private_model_config(args.live_model_config, ROOT) if args.live_model_config else None
    if live:
        os.environ["AIOPSSTUDIO_EVALUATION_ALLOW_NETWORK"] = "1"
    guard_execution()
    sys.path.insert(0, str(Path(args.app_root) / "backend"))
    with tempfile.TemporaryDirectory(prefix="aiopsstudio-evaluation-state-") as state:
        try:
            result = replay(material, state, live=live)
        except Exception as error:
            result = {"schema": "aiopsstudio-evaluation-observation/1", "harness_error": "application_replay_failed",
                      "error_type": type(error).__name__}
        write_new(args.result_file, result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", default=str(ROOT / "evaluation/suite.json"))
    parser.add_argument("--output")
    parser.add_argument("--baseline-ref", default=BASELINE_REF)
    parser.add_argument("--candidate-ref", default="working-tree")
    parser.add_argument("--baseline-only", action="store_true")
    parser.add_argument("--compare-with", help="external existing baseline summary; preserve original first attempts")
    parser.add_argument("--attempts", type=int, default=1)
    parser.add_argument("--case", action="append")
    parser.add_argument("--live-model-config", help="external owned 0600 JSON; never stored in summaries")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--input-file", help=argparse.SUPPRESS)
    parser.add_argument("--result-file", help=argparse.SUPPRESS)
    parser.add_argument("--app-root", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        worker(args)
        return
    if args.baseline_ref not in {"bd9a949", BASELINE_REF}:
        parser.error("baseline-ref must remain the original bd9a949 commit")
    if not args.output or not 1 <= args.attempts <= 5:
        parser.error("--output is required and --attempts must be between 1 and 5")
    root, suite, frozen = load_suite(args.suite)
    if args.live_model_config:
        from aiopsstudio_evaluation_tools.replay import private_model_config
        private_model_config(args.live_model_config, ROOT)
    output = external_output(args.output)
    if args.compare_with:
        baseline = read_json(args.compare_with)
        if baseline.get("code_ref") != BASELINE_REF:
            raise ValueError("comparison baseline is not the frozen original commit")
    else:
        baseline = measure(root, suite, frozen, BASELINE_REF, output / "baseline", attempts=args.attempts, cases=args.case, live_config=args.live_model_config)
    if not args.baseline_only:
        candidate = measure(root, suite, frozen, args.candidate_ref, output / "candidate", attempts=args.attempts, cases=args.case, live_config=args.live_model_config)
        from aiopsstudio_evaluation_tools.scoring import compare
        write_new(output / "comparison.json", compare(baseline, candidate))
    print(json.dumps({"output": str(output), "baseline": baseline["totals"], "strict_failures_are_preserved": True}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        # Avoid reflecting arbitrary provider/source/config exception text.
        print(json.dumps({"error": "evaluation_failed", "type": type(error).__name__}), file=sys.stderr)
        raise SystemExit(2)
