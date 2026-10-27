"""Independent publication-proof and owned-process-group review regressions."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

import pytest

from aiopsstudio import management_quality, release_baseline as module
from m2_management_fixtures import synthetic_pipeline_proof
from test_m2_management import config


@pytest.fixture
def fake_replay_root(tmp_path, monkeypatch):
    actual = Path(module.__file__).resolve().parents[2]
    root = tmp_path / "repository"
    shutil.copytree(actual / "evaluation/m1", root / "evaluation/m1")
    shutil.copytree(actual / "backend/aiopsstudio/evaluation", root / "backend/aiopsstudio/evaluation",
                    ignore=shutil.ignore_patterns("__pycache__"))
    (root / "scripts").mkdir()
    (root / "scripts/evaluate_analysis.py").write_text("# fixed synthetic runner\n")
    monkeypatch.setattr(module, "__file__", str(root / "backend/aiopsstudio/release_baseline.py"))
    monkeypatch.setattr(management_quality, "implementation_digest", lambda: "synthetic-application-digest")
    return root


@pytest.mark.parametrize("relative", ["backend/aiopsstudio/evaluation/replay.py",
    "backend/aiopsstudio/evaluation/contracts.py", "backend/aiopsstudio/evaluation/__init__.py",
    "evaluation/m1/inputs/normal-alert.json", "evaluation/m1/oracles/normal-alert.json"])
def test_pipeline_proof_is_invalidated_by_actual_replay_code_and_material_bytes(config, fake_replay_root, relative):
    service = module.PipelineQuality(config)
    synthetic_pipeline_proof(config)
    before = module.fingerprint()
    assert service.view()["eligible"] is True
    path = fake_replay_root / relative
    path.write_bytes(path.read_bytes() + b"\n")
    assert module.fingerprint() != before
    assert service.view()["eligible"] is False


def test_new_replay_module_invalidates_previous_proof(config, fake_replay_root):
    service = module.PipelineQuality(config)
    synthetic_pipeline_proof(config)
    (fake_replay_root / "backend/aiopsstudio/evaluation/new_adapter_helper.py").write_text("VALUE = 'synthetic'\n")
    assert service.view()["eligible"] is False


@pytest.mark.parametrize("leader_exits", [True, False])
def test_pipeline_stops_term_ignoring_descendants_even_after_leader_exit(config, tmp_path, monkeypatch, leader_exits):
    child_pid = tmp_path / "child.pid"
    heartbeat = tmp_path / "child-heartbeat"
    child = tmp_path / "owned-child.py"
    child.write_text("""import os,signal,sys,time
from pathlib import Path
signal.signal(signal.SIGTERM,signal.SIG_IGN)
Path(sys.argv[1]).write_text(str(os.getpid()))
deadline=time.monotonic()+15
while time.monotonic()<deadline:
 with Path(sys.argv[2]).open('ab') as handle:handle.write(b'x')
 time.sleep(.02)
""")
    leader = tmp_path / "owned-leader.py"
    leader.write_text("""import subprocess,sys,time
from pathlib import Path
subprocess.Popen([sys.executable,sys.argv[1],sys.argv[2],sys.argv[3]])
deadline=time.monotonic()+3
while not Path(sys.argv[2]).exists() and time.monotonic()<deadline:time.sleep(.01)
if sys.argv[4]=='stay':time.sleep(15)
""")
    real_popen = subprocess.Popen
    started = []
    class ControlledProcess:
        def __init__(self, command, **kwargs):
            self.process = real_popen([sys.executable, str(leader), str(child), str(child_pid),
                str(heartbeat), "exit" if leader_exits else "stay"], **kwargs)
            self.pid = self.process.pid
            started.append(self.process)
        def poll(self):
            return self.process.poll()
        def wait(self, timeout):
            return self.process.wait(timeout=min(timeout, .3) if timeout == 300 else timeout)
    monkeypatch.setattr(module.subprocess, "Popen", lambda command, **kwargs:
        ControlledProcess(command, **kwargs) if "--suite" in command else real_popen(command, **kwargs))
    # Keep the real TERM/KILL behavior; only shorten the owned test grace period.
    monkeypatch.setattr(module, "GROUP_TERM_GRACE_SECONDS", .15, raising=False)
    monkeypatch.setattr(module, "GROUP_KILL_WAIT_SECONDS", 2, raising=False)
    service = module.PipelineQuality(config)
    try:
        record = service.run()
        assert started and child_pid.exists()
        size = heartbeat.stat().st_size
        time.sleep(.12)
        assert heartbeat.stat().st_size == size, "An owned replay worker is still running after cleanup"
        assert not service._alive(started[0].pid, group=True)
        assert record["status"] == "failed"
        assert config.store.get("quality_pipeline_lock", "main")["active"] is False
    finally:
        for process in started:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass


def test_cleanup_failure_keeps_database_lock_and_private_files_until_group_stops(config, tmp_path, monkeypatch):
    class UnfinishedProcess:
        pid = 100000001
        def __init__(self, command, **kwargs):
            self.output = Path(command[command.index("--output") + 1])
            self.output.mkdir()
        def poll(self):
            return 2
        def wait(self, timeout):
            return 2
    monkeypatch.setattr(module.subprocess, "Popen", UnfinishedProcess)
    monkeypatch.setattr(module.PipelineQuality, "_stop_process_group", lambda *args: False)
    monkeypatch.setattr(module.PipelineQuality, "_alive", staticmethod(lambda pid, group=False: pid == UnfinishedProcess.pid))
    service = module.PipelineQuality(config)
    record = service.run()
    try:
        lock = config.store.get("quality_pipeline_lock", "main")
        assert record["status"] == "failed" and record["reasons"] == ["pipeline_process_cleanup_pending"]
        assert record["temporary_cleanup_pending"] is True
        assert lock["active"] is True and lock["cleanup_pending"] is True
        temporary, pgid = module._PENDING_TEMPORARIES[record["id"]]
        assert Path(temporary.name).is_dir() and pgid == UnfinishedProcess.pid
        with pytest.raises(module.ConfigError) as caught:
            module.PipelineQuality(config)._claim("next-run")
        assert caught.value.code == "pipeline_busy"
        monkeypatch.setattr(module.PipelineQuality, "_alive", staticmethod(lambda *args, **kwargs: False))
        service._reap_finished_temporaries()
        assert record["id"] not in module._PENDING_TEMPORARIES and not Path(temporary.name).exists()
        service._claim("after-stop")
        service._release("after-stop")
        assert config.store.get("quality_pipeline_lock", "main")["active"] is False
    finally:
        pending = module._PENDING_TEMPORARIES.pop(record["id"], None)
        if pending:
            pending[0].cleanup()


@pytest.mark.parametrize('states,expected', [
    ('777001 Z\n777002 R\n', False),
    ('777001 Zs\n777001 X\n777002 R\n', False),
    ('-1 Z\n777001 Z\n777002 R\n', False),
    ('777001 Z\n777001 S+\n777002 Z\n', True),
])
def test_group_liveness_counts_only_executable_members_of_owned_group(monkeypatch, states, expected):
    from types import SimpleNamespace
    monkeypatch.setattr(module.os, 'killpg', lambda *args: None)
    calls=[]
    def probe(command, **kwargs):
        calls.append((command,kwargs))
        return SimpleNamespace(returncode=0,stdout=states)
    monkeypatch.setattr(module.subprocess, 'run', probe)
    assert module.PipelineQuality._alive(777001,group=True) is expected
    assert len(calls)==1


@pytest.mark.parametrize('failure',['exit','timeout','malformed'])
def test_liveness_probe_failure_is_fail_closed(monkeypatch, failure):
    from types import SimpleNamespace
    monkeypatch.setattr(module.os,'killpg',lambda *args:None)
    def probe(*args,**kwargs):
        if failure=='timeout':raise subprocess.TimeoutExpired('ps',2)
        return SimpleNamespace(returncode=1 if failure=='exit' else 0,stdout='unreadable process state')
    monkeypatch.setattr(module.subprocess,'run',probe)
    assert module.PipelineQuality._alive(777001,group=True) is True


def test_zombie_owner_does_not_hold_stale_lock(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(module.os,'kill',lambda *args:None)
    monkeypatch.setattr(module.subprocess,'run',lambda *args,**kwargs:SimpleNamespace(returncode=0,stdout='Z+\n'))
    assert module.PipelineQuality._alive(777001) is False


def test_two_rounds_reclaim_zombie_only_group_lock_and_private_outputs(config,tmp_path,monkeypatch):
    from types import SimpleNamespace
    import tempfile
    monkeypatch.setattr(module.os,'killpg',lambda *args:None)
    monkeypatch.setattr(module.os,'kill',lambda *args:None)
    def probe(command,**kwargs):
        return SimpleNamespace(returncode=0,stdout='777001 Z\n777002 R\n' if 'pgid=,stat=' in command else 'Z\n')
    monkeypatch.setattr(module.subprocess,'run',probe)
    service=module.PipelineQuality(config)
    for number in range(2):
        token='synthetic-zombie-'+str(number)
        temporary=tempfile.TemporaryDirectory(dir=tmp_path)
        module._PENDING_TEMPORARIES[token]=(temporary,777001)
        config.store.save('quality_pipeline_lock','main',{'active':True,'token':token,'owner_pid':777001,
            'child_pid':777001,'expires_at':0,'cleanup_pending':True})
        try:
            service._reap_finished_temporaries()
            assert token not in module._PENDING_TEMPORARIES and not Path(temporary.name).exists()
            service._claim('next-'+str(number))
            service._release('next-'+str(number))
            assert config.store.get('quality_pipeline_lock','main')['active'] is False
        finally:
            pending=module._PENDING_TEMPORARIES.pop(token,None)
            if pending:pending[0].cleanup()


@pytest.mark.skipif(not hasattr(os,'fork'),reason='POSIX process-state oracle')
def test_real_owned_zombie_group_is_not_an_active_worker():
    reader,writer=os.pipe()
    pid=os.fork()
    if pid==0:
        os.close(reader)
        os.setsid()
        os.write(writer,b'ready')
        os.close(writer)
        os._exit(0)
    os.close(writer)
    try:
        assert os.read(reader,5)==b'ready'
        deadline=time.monotonic()+2
        while module.PipelineQuality._alive(pid,group=True) and time.monotonic()<deadline:
            time.sleep(.02)
        # The parent intentionally has not waited: kill(0) still observes the
        # zombie, but it has no runnable members and must not hold a replay lock.
        os.kill(pid,0)
        assert module.PipelineQuality._alive(pid,group=True) is False
        assert module.PipelineQuality._alive(pid) is False
    finally:
        os.close(reader)
        os.waitpid(pid,0)


@pytest.mark.parametrize('states,expected',[('777001 Z\n',False),('777001 S\n',True),('',True)])
def test_permission_denied_group_requires_explicit_state_observation(monkeypatch,states,expected):
    from types import SimpleNamespace
    def inaccessible(*args):raise PermissionError()
    monkeypatch.setattr(module.os,'killpg',inaccessible)
    monkeypatch.setattr(module.subprocess,'run',lambda *args,**kwargs:SimpleNamespace(returncode=0,stdout=states))
    assert module.PipelineQuality._alive(777001,group=True) is expected


@pytest.mark.parametrize('after_probe,expected',[('gone',False),('live',True),('denied',True)])
def test_pid_exit_between_signal_probe_and_ps_is_rechecked(monkeypatch,after_probe,expected):
    from types import SimpleNamespace
    checks=[]
    def exists(pid,signal_number):
        checks.append((pid,signal_number))
        if len(checks)>1:
            if after_probe=='gone':raise ProcessLookupError()
            if after_probe=='denied':raise PermissionError()
    monkeypatch.setattr(module.os,'kill',exists)
    monkeypatch.setattr(module.subprocess,'run',lambda *args,**kwargs:SimpleNamespace(returncode=1,stdout=''))
    assert module.PipelineQuality._alive(777001) is expected
    assert checks==[(777001,0),(777001,0)]


def test_owner_exit_during_ps_allows_expired_pipeline_lock_recovery(config,monkeypatch):
    from types import SimpleNamespace
    checks=[]
    def exists(pid,signal_number):
        checks.append((pid,signal_number))
        if len(checks)>1:raise ProcessLookupError()
    monkeypatch.setattr(module.os,'kill',exists)
    monkeypatch.setattr(module.subprocess,'run',lambda *args,**kwargs:SimpleNamespace(returncode=1,stdout=''))
    config.store.save('quality_pipeline_lock','main',{'active':True,'token':'expired-owner',
        'owner_pid':777001,'expires_at':0})
    service=module.PipelineQuality(config)
    service._claim('replacement')
    assert config.store.get('quality_pipeline_lock','main')['token']=='replacement'
    service._release('replacement')
