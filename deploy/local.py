#!/usr/bin/env python3
"""Mac 本机运行管理；只管理本项目的数据库、进程和转发。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from deploy.settings import load_config

SETTINGS = load_config()
STATE = SETTINGS['state_dir']
PRIVATE = STATE / 'private'
DOCKER = ['docker'] + (['--context', SETTINGS['docker_context']] if SETTINGS['docker_context'] else [])
PORTS = SETTINGS['ports']


def private_write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(text)
    path.chmod(0o600)


def init():
    PRIVATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    STATE.chmod(0o700)
    PRIVATE.chmod(0o700)
    path = PRIVATE / 'local.json'
    if not path.exists():
        private_write(path, json.dumps({'db_password': secrets.token_urlsafe(30)}, indent=2))
    if not (PRIVATE / 'admin-password').exists():
        private_write(PRIVATE / 'admin-password', secrets.token_urlsafe(20))
    if not (PRIVATE / 'master.key').exists():
        import base64
        private_write(PRIVATE / 'master.key', base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())
    return json.loads(path.read_text())


def env():
    cfg = init()
    database = SETTINGS['database']
    return {**os.environ,
        'EASYOPS_DATABASE_URL': os.environ.get('EASYOPS_DATABASE_URL') or f"postgresql://{quote(database['user'], safe='')}:{quote(cfg['db_password'], safe='')}@127.0.0.1:{PORTS['postgres']}/{quote(database['name'], safe='')}",
        'EASYOPS_MASTER_KEY_FILE': str(PRIVATE / 'master.key'),
        'EASYOPS_PASSWORD_FILE': str(PRIVATE / 'admin-password'),
        'EASYOPS_PUBLIC_ORIGIN': f"http://127.0.0.1:{PORTS['web']}",
        'EASYOPS_ARTIFACT_DIR': str(STATE / 'artifacts'),
        'EASYOPS_PRIVATE_DIR': str(PRIVATE),
        'EASYOPS_API_URL': f"http://127.0.0.1:{PORTS['api']}",
        'PORT': str(PORTS['web']),
        'DO_NOT_TRACK': '1',
        'COPILOTKIT_TELEMETRY_DISABLED': 'true',
    }


def db():
    if os.environ.get('EASYOPS_DATABASE_URL'):
        print('使用外部数据库配置；未创建或启动数据库容器')
        return
    cfg = init()
    database = SETTINGS['database']
    container = database['container']
    state_id = hashlib.sha256(str(STATE).encode()).hexdigest()[:16]
    probe = subprocess.run(DOCKER + ['inspect', '--format', '{{ index .Config.Labels "easyops.state_id" }}', container], capture_output=True, text=True)
    if probe.returncode == 0:
        if probe.stdout.strip() != state_id:
            raise RuntimeError('同名数据库容器不属于此状态目录，未启动；请配置独立容器名')
        subprocess.run(DOCKER + ['start', container], check=True, stdout=subprocess.DEVNULL)
    else:
        volume = subprocess.run(DOCKER + ['volume', 'inspect', '--format', '{{ index .Labels "easyops.state_id" }}', database['volume']], capture_output=True, text=True)
        if volume.returncode == 0 and volume.stdout.strip() != state_id:
            raise RuntimeError('同名数据库卷不属于此状态目录，未挂载；请配置独立卷名')
        if volume.returncode != 0:
            subprocess.run(DOCKER + ['volume', 'create', '--label', 'easyops.state_id=' + state_id, database['volume']], check=True, stdout=subprocess.DEVNULL)
        db_env = PRIVATE / 'postgres.env'
        private_write(db_env, f"POSTGRES_DB={database['name']}\nPOSTGRES_USER={database['user']}\nPOSTGRES_PASSWORD=" + cfg['db_password'] + '\n')
        subprocess.run(DOCKER + ['run', '-d', '--name', container, '--restart', 'unless-stopped',
            '--label', 'easyops.owner=workbench', '--label', 'easyops.state_id=' + state_id, '--cpus', '1', '--memory', '512m', '--pids-limit', '128',
            '--env-file', str(db_env), '-p', f"127.0.0.1:{PORTS['postgres']}:5432",
            '-v', database['volume'] + ':/var/lib/postgresql/data', database['image']],
            check=True, stdout=subprocess.DEVNULL)
    for _ in range(60):
        r = subprocess.run(DOCKER + ['exec', container, 'pg_isready', '-U', database['user'], '-d', database['name']], capture_output=True)
        if r.returncode == 0:
            print(f"PostgreSQL ready on 127.0.0.1:{PORTS['postgres']}")
            return
        time.sleep(.5)
    raise RuntimeError('PostgreSQL启动超时')


def process_identity(pid):
    """A PID alone can be reused; compare the process birth and launch command."""
    result = subprocess.run(['ps', '-ww', '-p', str(pid), '-o', 'pgid=', '-o', 'lstart=', '-o', 'command='],
        capture_output=True, text=True, env={**os.environ, 'LC_ALL': 'C'})
    fields = result.stdout.strip().split(None, 6)
    if result.returncode or len(fields) != 7:
        return None
    return {'pid': pid, 'pgid': int(fields[0]), 'started_at': ' '.join(fields[1:6]), 'command': fields[6]}


def legacy_identity(name, pid, path, current):
    """Migrate existing task PID files only after checking this project's launch."""
    commands = {
        'api': ([str(ROOT / 'backend/.venv/bin/python'), '-m', 'uvicorn', 'easyops.application:create_app', '--factory', '--host', '127.0.0.1', '--port', str(PORTS['api'])], ROOT / 'backend'),
        'web': ([SETTINGS['node_binary'], 'server.mjs'], ROOT / 'web'),
    }
    if SETTINGS['ssh_tunnel']['enabled']:
        commands['ssh-tunnel'] = (tunnel_command(), ROOT)
    expected = commands.get(name)
    if not expected or current['command'] != ' '.join(expected[0]):
        return False
    try:
        birth = time.mktime(time.strptime(current['started_at'], '%a %b %d %H:%M:%S %Y'))
        if not 0 <= path.stat().st_mtime - birth <= 3:
            return False
        cwd = subprocess.run(['lsof', '-a', '-p', str(pid), '-d', 'cwd', '-Fn'], capture_output=True, text=True)
        directories = [line[1:] for line in cwd.stdout.splitlines() if line.startswith('n')]
        return cwd.returncode == 0 and str(expected[1].resolve()) in directories
    except (ValueError, OSError):
        return False


def owned_pid(name):
    path = STATE / f'{name}.pid'
    identity_path = STATE / f'{name}.identity.json'
    try:
        pid = int(path.read_text().strip())
        if pid <= 1:
            return None
        current = process_identity(pid)
        if not current or current['pgid'] != pid:
            return None
        if identity_path.exists():
            if json.loads(identity_path.read_text()) != current:
                return None
        elif legacy_identity(name, pid, path, current):
            private_write(identity_path, json.dumps(current))
        else:
            return None
        return pid
    except (ValueError, OSError, json.JSONDecodeError):
        return None


def alive(name):
    return owned_pid(name) is not None


def spawn(name, cmd, cwd=ROOT):
    if alive(name): return
    process_env = env()  # External databases skip db(), so initialise this state first.
    with (STATE / f'{name}.log').open('ab') as log:
        proc = subprocess.Popen(cmd, cwd=cwd, env=process_env, stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    identity = process_identity(proc.pid)
    if not identity or identity['pgid'] != proc.pid:
        raise RuntimeError(f'{name}未能建立启动身份，请查看对应日志')
    private_write(STATE / f'{name}.pid', str(proc.pid))
    private_write(STATE / f'{name}.identity.json', json.dumps(identity))


def tunnel_command():
    cfg = SETTINGS['ssh_tunnel']
    if not cfg['enabled']:
        raise RuntimeError('SSH 转发未启用，请先配置 ssh_tunnel')
    return ['ssh', '-N', '-o', 'ExitOnForwardFailure=yes', '-o', 'ServerAliveInterval=30',
        '-L', f"127.0.0.1:{cfg['local_port']}:{cfg['remote_host']}:{cfg['remote_port']}", cfg['host']]


def tunnel():
    spawn('ssh-tunnel', tunnel_command())


def start():
    python = ROOT / 'backend/.venv/bin/python'
    if not python.exists(): raise RuntimeError('先在backend执行 uv sync --frozen')
    if not shutil.which(SETTINGS['node_binary']): raise RuntimeError('未找到配置的 Node 可执行文件，请安装 Node 或设置 node_binary')
    db()
    if SETTINGS['ssh_tunnel']['enabled']:
        tunnel()
    spawn('api', [str(python), '-m', 'uvicorn', 'easyops.application:create_app', '--factory', '--host', '127.0.0.1', '--port', str(PORTS['api'])], ROOT / 'backend')
    spawn('web', [SETTINGS['node_binary'], 'server.mjs'], ROOT / 'web')
    listeners = [('api', PORTS['api']), ('web', PORTS['web'])]
    if SETTINGS['ssh_tunnel']['enabled']:
        listeners.append(('ssh-tunnel', SETTINGS['ssh_tunnel']['local_port']))
    for name, port in listeners:
        for _ in range(100):
            try:
                with socket.create_connection(('127.0.0.1', port), timeout=.15):
                    if not alive(name):
                        raise RuntimeError(f'{name}端口有监听但本项目进程未存活，未报告就绪')
                    break
            except OSError: time.sleep(.1)
        else: raise RuntimeError(f'{name}未就绪，请查看{STATE}中的对应日志')
    status()


def stop_process(name, grace_seconds=5, kill_seconds=2):
    path = STATE / f'{name}.pid'
    identity_path = STATE / f'{name}.identity.json'
    pid = owned_pid(name)
    if pid is None:
        if path.exists():
            print(f'{name}的PID未匹配本项目启动身份，未向该进程发送信号')
        path.unlink(missing_ok=True)
        identity_path.unlink(missing_ok=True)
        return
    identity = process_identity(pid)

    def original_exists():
        return identity is not None and process_identity(pid) == identity

    def wait_for_exit(seconds):
        deadline = time.monotonic() + seconds
        while original_exists():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(.05, remaining))
        return True

    if original_exists():
        if owned_pid(name) != pid:
            raise RuntimeError(f'{name}的启动身份已变化，未发送停止信号')
        try: os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError: pass
    if not wait_for_exit(grace_seconds):
        # A hanging SSE subscriber must not keep a previous listener alive.
        # Force termination is scoped to the same verified launch only.
        if original_exists():
            if owned_pid(name) != pid:
                raise RuntimeError(f'{name}停止超时且身份已变化，未强制终止其他进程')
            try: os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError: pass
        if not wait_for_exit(kill_seconds):
            raise RuntimeError(f'{name}停止超时，尚未确认端口所属进程退出；请检查本机日志')
    # Do not remove a registration replaced by a concurrent fresh launch.
    if path.exists() and path.read_text().strip() == str(pid):
        if not identity_path.exists() or json.loads(identity_path.read_text()) == identity:
            path.unlink(missing_ok=True)
            identity_path.unlink(missing_ok=True)


def stop():
    for name in ['web', 'api', 'ssh-tunnel']:
        stop_process(name)
    print('已停止本项目应用进程与转发；数据库及持久数据保留')


def status():
    listeners = list(PORTS.items())
    if SETTINGS['ssh_tunnel']['enabled']:
        listeners.append(('ssh-tunnel', SETTINGS['ssh_tunnel']['local_port']))
    for name, port in listeners:
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=.5): state = 'listening'
        except OSError: state = 'stopped'
        print(f'{name}: {state} 127.0.0.1:{port}')
    print(f'登录口令文件：{PRIVATE / "admin-password"}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['init', 'db', 'start', 'stop', 'status', 'tunnel'])
    action = parser.parse_args().action
    try: globals()[action]()
    except Exception as error:
        print(f'{type(error).__name__}: {error}', file=sys.stderr)
        sys.exit(1)
