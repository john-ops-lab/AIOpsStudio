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
GROUP_TERM_GRACE_SECONDS = 5
GROUP_KILL_WAIT_SECONDS = 5
# Retain private outputs until an unkillable owned group actually stops. A new
# run reaps completed entries while holding _LOCK; active groups keep the DB lock.
_PENDING_TEMPORARIES = {}
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


def _replay_digests(root, suite_path, suite):
    """Bind evaluator code and actual fixed inputs, independently of app code."""
    root, suite_root = root.resolve(), suite_path.parent.resolve()
    implementation = {}
    for path in sorted((root / "backend/aiopsstudio/evaluation").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ConfigError("固定回放实现路径无效", "pipeline_material_invalid")
        implementation[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    if not implementation:
        raise ConfigError("固定回放实现缺失", "pipeline_material_invalid")
    relative_files = {suite["frozen"]["path"]}
    for case in suite["cases"]:
        relative_files.update((case["input"], case["oracle"]))
    material = {}
    for relative in sorted(relative_files):
        path = suite_root / relative
        if path.is_symlink() or not path.resolve().is_relative_to(suite_root) or not path.is_file():
            raise ConfigError("固定回放材料路径无效", "pipeline_material_invalid")
        material[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    encode = lambda value: json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return {"replay_implementation_digest": hashlib.sha256(encode(implementation)).hexdigest(),
            "fixed_material_digest": hashlib.sha256(encode(material)).hexdigest()}


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
        "baseline_ref": BASELINE_REF, **_replay_digests(root, suite_path, suite)}


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
        permission_denied = False
        try:
            (os.killpg if group else os.kill)(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            # macOS can return EPERM for an owned zombie-only process group.
            # Confirm states rather than retaining that group indefinitely.
            permission_denied = True
        # kill(0) also succeeds for zombies. In containers a non-reaping PID 1
        # can keep these entries indefinitely even though no code can run.
        # ps supports these fields on both macOS and Linux; inspect only this
        # owned PID/PGID, and keep the lock if observation is unavailable.
        command = ["ps", "-axo", "pgid=,stat="] if group else ["ps", "-p", str(pid), "-o", "stat="]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=2)
            if not group and result.returncode == 1 and isinstance(result.stdout, str) and not result.stdout.strip():
                # The PID may have exited between kill(0) and ps. Only a
                # definite disappearance permits reclaiming its expired lock.
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    return False
                except OSError:
                    return True
            if result.returncode or not isinstance(result.stdout, str):
                return True
            states = []
            for line in result.stdout.splitlines():
                fields = line.split()
                if group:
                    if len(fields) != 2:
                        return True
                    try:
                        observed_group = int(fields[0])
                    except ValueError:
                        return True
                    # macOS may print -1 for an already detached zombie.
                    if observed_group != pid:
                        continue
                    states.append(fields[1])
                else:
                    if len(fields) != 1:
                        return True
                    states.append(fields[0])
            if not states and permission_denied:
                return True  # No reliable observation of an inaccessible group.
            return any(state[0] not in {"Z", "X"} for state in states)
        except (OSError, subprocess.SubprocessError, UnicodeError):
            return True

    def _stop_process_group(self, proc):
        """Reap the leader AND every descendant in this Popen-owned group."""
        alive = lambda: self._alive(proc.pid, group=True)
        if proc.poll() is not None and not alive():
            return True
        deadline = time.monotonic() + GROUP_TERM_GRACE_SECONDS
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=max(.001, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            pass
        while alive() and time.monotonic() < deadline:
            time.sleep(.02)
        if not alive():
            return True
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + GROUP_KILL_WAIT_SECONDS
        try:
            proc.wait(timeout=max(.001, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            pass
        while alive() and time.monotonic() < deadline:
            time.sleep(.02)
        return not alive()

    def _reap_finished_temporaries(self):
        for token, (temporary, pgid) in list(_PENDING_TEMPORARIES.items()):
            if not self._alive(pgid, group=True):
                try:
                    temporary.cleanup()
                except OSError:
                    continue
                del _PENDING_TEMPORARIES[token]

    def _claim(self, token):
        def claim():
            previous = self.store.get("quality_pipeline_lock", "main") or {}
            if previous.get("active") and (previous.get("expires_at", 0) > time.time()
                    or self._alive(previous.get("owner_pid")) or self._alive(previous.get("child_pid"), group=True)):
                raise ConfigError("已有固定质量回放正在运行", "pipeline_busy")
            self.store.save("quality_pipeline_lock", "main", {"active": True, "token": token,
                "owner_pid": os.getpid(), "expires_at": time.time() + 320})
        self.store.atomic(claim)

    def _release(self, token, *, group_stopped=True):
        def release():
            item = self.store.get("quality_pipeline_lock", "main")
            if item and item.get("token") == token:
                self.store.save("quality_pipeline_lock", "main", {**item, "active": not group_stopped,
                    "token": token, "owner_pid": None, "expires_at": 0, "cleanup_pending": not group_stopped})
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
        group_stopped = True
        try:
            self._reap_finished_temporaries()
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
                    # A successfully exited leader can still leave live workers.
                    # Do not remove their files or release the cross-process lock.
                    try:
                        group_stopped = self._stop_process_group(proc)
                    except (OSError, subprocess.SubprocessError):
                        group_stopped = False
                    if not group_stopped:
                        stage = "runner_cleanup"
                        raise ConfigError("固定质量回放进程组尚未停止，继续保留隔离锁", "pipeline_process_cleanup_pending")
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
                if not group_stopped:
                    record["temporary_cleanup_pending"] = True
                    _PENDING_TEMPORARIES[rid] = (temporary, proc.pid)
                else:
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
                    self._release(rid, group_stopped=group_stopped)
                _LOCK.release()
        return saved
