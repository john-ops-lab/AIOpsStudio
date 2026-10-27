"""Controlled, bounded R01 replay proof for publication; no uploaded score import."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback

from .config import ConfigError
from .store import utcnow

_LOCK = threading.Lock()
BASELINE_REF = "bd9a949cff85e2f5b00b5dfd43d323909bfd0a1f"
# The unchanged replay adapter cannot exercise these independent integrations.
# They remain unknown in the full denominator, not passed or discarded.
OUT_OF_SCOPE = {("sharing-revocation", "share_revoke_reader_denied"),
               ("human-verdict", "verdict_is_separate"),
               ("consistent-recovery", "restored_owner_and_evidence")}


def failure_diagnostic(error, stage, *, external=None, exit_code=None):
    """Project fixed names/codes only; subprocess text and source data never leave temp."""
    safe_name = lambda value: value if isinstance(value, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", value) else None
    result = {"stage": stage, "error_type": type(error).__name__, "runner_exit_code": exit_code,
              "frames": [{"module": Path(frame.filename).name, "function": frame.name, "line": frame.lineno}
                         for frame in traceback.extract_tb(error.__traceback__)[-5:]],
              "result_files": {}, "execution_errors": [], "runner_error_types": []}
    if external is None:
        return result
    external = Path(external)
    output = external / "result"
    suite = json.loads((Path(__file__).resolve().parents[2] / "evaluation/m1/suite.json").read_text())
    expected = {case["id"] for case in suite["cases"]}
    for phase in ("baseline", "candidate"):
        result["result_files"][phase] = (output / phase / "summary.json").is_file()
        for case_id in sorted(expected):
            path = output / phase / (case_id + "-attempt-1") / "observation.json"
            if not path.is_file() or path.stat().st_size > 2 * 1024 ** 2:
                continue
            try:
                observation = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if observation.get("harness_error") or observation.get("frozen_validation", {}).get("matched") is False:
                result["execution_errors"].append({"phase": phase, "case_id": case_id,
                    "harness_error": safe_name(observation.get("harness_error")),
                    "error_type": safe_name(observation.get("error_type")),
                    "subprocess_exit_code": observation.get("subprocess_exit_code") if type(observation.get("subprocess_exit_code")) is int else None,
                    "frozen_matched": observation.get("frozen_validation", {}).get("matched") is True})
    result["result_files"]["comparison"] = (output / "comparison.json").is_file()
    log = external / "private-runner.log"
    if log.is_file():
        # Logs themselves may hold arbitrary exception text. Retain only a hash
        # and whitelisted machine-readable error types from the final 64 KiB.
        with log.open("rb") as handle:
            handle.seek(max(0, log.stat().st_size - 65536))
            tail = handle.read(65536)
        result.update(log_bytes=log.stat().st_size, log_tail_sha256=hashlib.sha256(tail).hexdigest())
        for line in tail.decode("utf-8", errors="replace").splitlines():
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict) and value.get("error") == "evaluation_failed" and safe_name(value.get("type")):
                result["runner_error_types"].append(value["type"])
        result["runner_error_types"] = sorted(set(result["runner_error_types"]))
    return result


def fingerprint():
    from .management_quality import implementation_digest
    root = Path(__file__).resolve().parents[2]
    suite_path = root / "evaluation/m1/suite.json"
    suite = json.loads(suite_path.read_text())
    if suite.get("baseline_ref") != BASELINE_REF:
        raise ConfigError("固定原始评估基线不匹配", "pipeline_baseline_invalid")
    frozen_path = suite_path.parent / suite["frozen"]["path"]
    return {"schema": "controlled-r01/1", "implementation_digest": implementation_digest(),
        "suite_digest": hashlib.sha256(suite_path.read_bytes()).hexdigest(),
        "frozen_digest": hashlib.sha256(frozen_path.read_bytes()).hexdigest(),
        "scorer_digest": hashlib.sha256((root / "backend/aiopsstudio/evaluation/scoring.py").read_bytes()).hexdigest(),
        "runner_digest": hashlib.sha256((root / "scripts/evaluate_analysis.py").read_bytes()).hexdigest(),
        "baseline_ref": BASELINE_REF}


def grade(candidate, baseline, comparison):
    reasons, unknown = [], []
    root = Path(__file__).resolve().parents[2]
    suite = json.loads((root / "evaluation/m1/suite.json").read_text())
    expected = {case["id"] for case in suite["cases"]}
    results = candidate.get("results", [])
    if len(results) != len(expected) or {r.get("case_id") for r in results} != expected:
        reasons.append("case_denominator_mismatch")
    if candidate.get("incomparable_cases") or baseline.get("incomparable_cases") or comparison.get("comparable") is not True:
        reasons.append("comparison_not_verified")
    if baseline.get("code_ref") != BASELINE_REF or candidate.get("comparability") != baseline.get("comparability"):
        reasons.append("frozen_visibility_or_baseline_mismatch")
    for row in results:
        if row.get("observation_status") != "executed" or row.get("score", {}).get("harness_error"):
            reasons.append("case_not_executed")
        for check in row.get("score", {}).get("checks", []):
            status = check.get("status")
            if status == "failed":
                reasons.append("independent_assertion_failed")
            elif status == "unknown":
                entry = {"case_id": row["case_id"], "check_id": check["id"], "status": "unknown"}
                unknown.append(entry)
                if (row["case_id"], check["id"]) not in OUT_OF_SCOPE:
                    reasons.append("applicable_assertion_unknown")
            elif status != "passed":
                reasons.append("invalid_assertion_status")
    if any(row.get("critical_regressions") for row in comparison.get("results", [])):
        reasons.append("critical_regression")
    totals = candidate.get("totals", {}).get("assertions", {})
    # Bind all frozen assertions, not only whatever the result happens to list.
    total = 0
    for case in suite["cases"]:
        oracle = json.loads((root / "evaluation/m1" / case["oracle"]).read_text())
        total += len(oracle["assertions"])
    checks_count = sum(len(row.get("score", {}).get("checks", [])) for row in results)
    if totals.get("total") != total or checks_count != total:
        reasons.append("assertion_denominator_mismatch")
    return {"eligible": not reasons, "reasons": sorted(set(reasons)),
        "totals": candidate.get("totals"), "baseline_totals": baseline.get("totals"),
        "out_of_scope_unknown": unknown,
        "scope": "R01 fixed synthetic pipeline comparison; configuration runs are checked separately; no business/RCA acceptance"}


class PipelineQuality:
    def __init__(self, config):
        self.config, self.store = config, config.store

    def view(self):
        current = fingerprint()
        rows = sorted(self.store.list("quality_pipeline"), key=lambda x: (x.get("created_at", ""), x["id"]), reverse=True)
        for row in rows:
            row["current"] = row.get("fingerprint") == current
        valid = next((row for row in rows if row["current"] and row.get("status") == "completed" and row.get("eligible") is True
            and row.get("provenance") == "controlled-r01-runner/1" and row.get("candidate_code_digest") == current["implementation_digest"]), None)
        return {"required": True, "fingerprint": current, "eligible": valid is not None,
                "proof_id": valid["id"] if valid else None, "latest": rows[0] if rows else None,
                "out_of_scope_checks": [{"case_id": a, "check_id": b} for a, b in sorted(OUT_OF_SCOPE)]}

    @staticmethod
    def _alive(pid, *, group=False):
        if type(pid) is not int or pid < 2:
            return False
        try:
            (os.killpg if group else os.kill)(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def _claim(self, token):
        def claim():
            previous = self.store.get("quality_pipeline_lock", "main") or {}
            if previous.get("active") and (previous.get("expires_at", 0) > time.time()
                    or self._alive(previous.get("owner_pid")) or self._alive(previous.get("child_pid"), group=True)):
                raise ConfigError("已有固定质量回放正在运行", "pipeline_busy")
            self.store.save("quality_pipeline_lock", "main", {"active": True, "token": token,
                "owner_pid": os.getpid(), "expires_at": time.time() + 320})
        self.store.atomic(claim)

    def _release(self, token):
        def release():
            item = self.store.get("quality_pipeline_lock", "main")
            if item and item.get("token") == token:
                self.store.save("quality_pipeline_lock", "main", {"active": False, "token": token, "expires_at": 0})
        self.store.atomic(release)

    def _retain_failure(self, rid, diagnostic):
        """Keep eight small owned diagnostic projections, never raw runner logs."""
        directory = self.config.artifact_dir / "quality-diagnostics"
        if directory.is_symlink():
            raise OSError("Unsafe diagnostic directory")
        directory.mkdir(mode=0o700, exist_ok=True)
        directory.chmod(0o700)
        data = json.dumps(diagnostic, ensure_ascii=False, sort_keys=True).encode()
        if len(data) > 65536:
            raise ValueError("Diagnostic projection exceeded bound")
        path = directory / (rid + ".json")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        owned = sorted((p for p in directory.glob("pipeline_*.json")
                        if not p.is_symlink() and p.is_file()), key=lambda p: p.stat().st_mtime_ns, reverse=True)
        for old in owned[8:]:
            old.unlink()

    def run(self, *, actor="admin"):
        if not _LOCK.acquire(blocking=False):
            raise ConfigError("已有固定质量回放正在运行", "pipeline_busy")
        rid, started, claimed = "pipeline_" + secrets.token_hex(12), time.monotonic(), False
        record = {"status": "running", "owner_id": actor, "eligible": False, "created_at": utcnow(), "provenance": "controlled-r01-runner/1"}
        proc, code, stage, external, temporary = None, None, "claim", None, None
        try:
            self._claim(rid)
            claimed = True
            stage = "fingerprint"
            record["fingerprint"] = fingerprint()
            self.store.save("quality_pipeline", rid, record)
            root = Path(__file__).resolve().parents[2]
            stage = "resource_check"
            if shutil.disk_usage(tempfile.gettempdir()).free < 15 * 1024 ** 3:
                raise ConfigError("磁盘可用空间不足，未启动质量回放", "pipeline_resource_limit")
            temporary = tempfile.TemporaryDirectory(prefix="aiopsstudio-quality-")
            directory = temporary.name
            external = Path(directory)
            external.chmod(0o700)
            output = external / "result"
            environment = {key: value for key, value in os.environ.items() if key in {"PATH", "LANG", "LC_ALL", "TMPDIR", "SSL_CERT_FILE", "SSL_CERT_DIR"}}
            environment.update(HOME=directory, PYTHONDONTWRITEBYTECODE="1")
            command = [sys.executable, str(root / "scripts/evaluate_analysis.py"),
                "--suite", str(root / "evaluation/m1/suite.json"), "--candidate-ref", "working-tree", "--output", str(output)]
            stage = "runner"
            with (external / "private-runner.log").open("wb") as log:
                proc = subprocess.Popen(command, cwd=root, env=environment, stdout=log, stderr=log, start_new_session=True)
                try:
                    def bind_child():
                        lock = self.store.get("quality_pipeline_lock", "main")
                        if not lock or lock.get("token") != rid:
                            raise ConfigError("质量回放锁已失效", "pipeline_lock_lost")
                        self.store.save("quality_pipeline_lock", "main", {**lock, "child_pid": proc.pid}, lock["revision"])
                    self.store.atomic(bind_child)
                    code = proc.wait(timeout=300)
                except subprocess.TimeoutExpired:
                    raise ConfigError("固定质量回放达到时间上限，已停止本次任务", "pipeline_timeout") from None
                finally:
                    if proc.poll() is None:
                        os.killpg(proc.pid, signal.SIGTERM)
                        try:
                            proc.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            os.killpg(proc.pid, signal.SIGKILL)
                            proc.wait(timeout=5)
            if code:
                raise ConfigError("固定质量回放未完成，未批准发布", "pipeline_runner_failed")
            stage = "load_results"
            candidate = json.loads((output / "candidate/summary.json").read_text())
            baseline = json.loads((output / "baseline/summary.json").read_text())
            comparison = json.loads((output / "comparison.json").read_text())
            stage = "grade"
            result = grade(candidate, baseline, comparison)
            # The runner freezes its own app copy; do not attach old results to new code.
            stage = "verify_fingerprint"
            if record["fingerprint"] != fingerprint() or candidate.get("code_sha256") != record["fingerprint"]["implementation_digest"]:
                result.update(eligible=False, reasons=[*result["reasons"], "implementation_changed_during_replay"])
            record.update(result, status="completed", candidate_code_digest=candidate.get("code_sha256"))
        except Exception as error:
            record.update(status="failed", eligible=False, reasons=[getattr(error, "code", "pipeline_runner_failed")])
            if "diagnostic" not in record:
                try:
                    record["diagnostic"] = failure_diagnostic(error, stage, external=external, exit_code=code)
                except (OSError, ValueError, TypeError):
                    record["diagnostic"] = failure_diagnostic(error, stage, exit_code=code)
            try:
                self._retain_failure(rid, record["diagnostic"])
                record["diagnostic_retained"] = True
            except (OSError, ValueError):
                record["diagnostic_retained"] = False
        finally:
            if temporary is not None:
                try:
                    temporary.cleanup()
                except OSError:
                    record["temporary_cleanup_pending"] = True
            record.update(completed_at=utcnow(), elapsed_seconds=round(time.monotonic() - started, 3))
            try:
                saved = self.store.save("quality_pipeline", rid, record)
                self.config.audit("quality_pipeline", "publication", rid,
                    {"status": record["status"], "eligible": record["eligible"], "reasons": record.get("reasons", [])}, actor=actor)
            finally:
                if claimed:
                    self._release(rid)
                _LOCK.release()
        return saved
