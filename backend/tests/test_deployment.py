"""Only temporary files and mocked transports/processes; never run a deployment."""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from deploy.settings import DEFAULT_DATABASE, load_config
from deploy import seed as seed_module
from deploy.seed import apply_bootstrap, local_origin, read_external_json, validate_bootstrap


def config_file(tmp_path, body):
    path = tmp_path / 'deployment.json'
    path.write_text(json.dumps(body))
    return path


def local_module(tmp_path, monkeypatch):
    monkeypatch.setenv('AIOPSSTUDIO_STATE_DIR', str(tmp_path / 'runtime'))
    settings = config_file(tmp_path, {'database': DEFAULT_DATABASE})
    monkeypatch.setenv('AIOPSSTUDIO_DEPLOY_CONFIG', str(settings))
    spec = importlib.util.spec_from_file_location('isolated_deployment', ROOT / 'deploy/local.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generic_defaults_have_no_implicit_external_connections(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    config = load_config(environ={})
    assert config['ports'] == {'web': 3000, 'api': 8000, 'postgres': 5432}
    assert config['docker_context'] is None
    assert config['ssh_tunnel'] == {'enabled': False}
    assert config['state_dir'] == tmp_path / '.local/share/aiopsstudio'
    assert config['database']['volume'] == 'aiopsstudio-db-data'


@pytest.mark.parametrize('body', [
    {'ports': {'web': True}}, {'ports': {'api': 0}}, {'ports': {'postgres': 65536}},
    {'ports': {'web': '3000'}}, {'ports': {'web': 8000}}, {'unknown_field': 1},
    {'ssh_tunnel': {'enabled': True}},
    {'ssh_tunnel': {'enabled': True, 'host': '-o', 'local_port': 9000, 'remote_port': 9001}},
    {'ssh_tunnel': {'enabled': True, 'host': 'example-ssh', 'local_port': 3000, 'remote_port': 9001}},
])
def test_configuration_errors_fail_before_any_process_action(tmp_path, body):
    with pytest.raises(ValueError):
        load_config(config_file(tmp_path, body), environ={})
    assert not (tmp_path / 'private').exists()


def test_external_ports_context_and_optional_tunnel(tmp_path):
    cfg = load_config(config_file(tmp_path, {'database': DEFAULT_DATABASE,
        'ports': {'web': 3100, 'api': 8100, 'postgres': 5440}, 'docker_context': 'example-context',
        'ssh_tunnel': {'enabled': True, 'host': 'example-ssh', 'local_port': 9000, 'remote_port': 9001}}), environ={'AIOPSSTUDIO_STATE_DIR': str(tmp_path / 'state')})
    assert cfg['ports']['web'] == 3100 and cfg['state_dir'] == tmp_path / 'state'
    assert cfg['docker_context'] == 'example-context' and cfg['ssh_tunnel']['remote_host'] == '127.0.0.1'


def test_existing_database_url_never_controls_a_container(tmp_path, monkeypatch):
    module = local_module(tmp_path, monkeypatch)
    monkeypatch.setenv('AIOPSSTUDIO_DATABASE_URL', 'postgresql://example.invalid/example')
    monkeypatch.setattr(module.subprocess, 'run', lambda *a, **k: pytest.fail('must not invoke Docker'))
    module.db()
    assert not module.STATE.exists()


def test_init_preserves_existing_local_material_and_permissions(tmp_path, monkeypatch):
    module = local_module(tmp_path, monkeypatch)
    first = module.init()
    snapshots = {name: (module.PRIVATE / name).read_bytes() for name in ('local.json', 'admin-password', 'master.key')}
    assert module.init() == first
    assert all((module.PRIVATE / name).read_bytes() == body for name, body in snapshots.items())
    assert module.STATE.stat().st_mode & 0o777 == 0o700
    assert all((module.PRIVATE / name).stat().st_mode & 0o777 == 0o600 for name in snapshots)


def test_same_named_foreign_container_is_not_started(tmp_path, monkeypatch):
    module = local_module(tmp_path, monkeypatch)
    monkeypatch.delenv('AIOPSSTUDIO_DATABASE_URL', raising=False)
    calls = []
    def run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout='different-owner\n')
    monkeypatch.setattr(module.subprocess, 'run', run)
    with pytest.raises(RuntimeError, match='不属于此状态目录'):
        module.db()
    assert len(calls) == 1 and 'inspect' in calls[0]


def test_same_named_foreign_volume_is_never_mounted(tmp_path, monkeypatch):
    module = local_module(tmp_path, monkeypatch)
    monkeypatch.delenv('AIOPSSTUDIO_DATABASE_URL', raising=False)
    calls = []
    def run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0 if 'volume' in cmd else 1, stdout='different-owner\n')
    monkeypatch.setattr(module.subprocess, 'run', run)
    with pytest.raises(RuntimeError, match='未挂载'):
        module.db()
    assert len(calls) == 2 and all('inspect' in cmd for cmd in calls)


@pytest.mark.parametrize('label_mode', ['previous', 'current', 'same-both'])
def test_existing_owned_container_is_reused_without_recreating_data(tmp_path, monkeypatch, label_mode):
    module = local_module(tmp_path, monkeypatch)
    monkeypatch.delenv('AIOPSSTUDIO_DATABASE_URL', raising=False)
    state_id = hashlib.sha256(str(module.STATE).encode()).hexdigest()[:16]
    labels = {'previous': '<no value>|' + state_id, 'current': state_id + '|<no value>', 'same-both': state_id + '|' + state_id}
    calls = []
    def run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout=labels[label_mode] + '\n' if 'inspect' in cmd else '')
    monkeypatch.setattr(module.subprocess, 'run', run)
    module.db()
    assert any('start' in cmd for cmd in calls)
    assert not any('volume' in cmd or 'run' in cmd for cmd in calls)
    assert 'easyops.state_id' in calls[0][-2] and 'aiopsstudio.state_id' in calls[0][-2]


def test_conflicting_container_labels_are_rejected_without_start(tmp_path, monkeypatch):
    module = local_module(tmp_path, monkeypatch)
    monkeypatch.delenv('AIOPSSTUDIO_DATABASE_URL', raising=False)
    state_id = hashlib.sha256(str(module.STATE).encode()).hexdigest()[:16]
    calls = []
    def run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout=state_id + '|other-state\n')
    monkeypatch.setattr(module.subprocess, 'run', run)
    with pytest.raises(RuntimeError, match='不属于此状态目录'):
        module.db()
    assert len(calls) == 1


def test_start_has_no_tunnel_when_disabled_and_no_real_processes(tmp_path, monkeypatch):
    module = local_module(tmp_path, monkeypatch)
    launched = []
    monkeypatch.setattr(module, 'db', lambda: None)
    monkeypatch.setattr(module, 'spawn', lambda name, *a, **k: launched.append(name))
    monkeypatch.setattr(module, 'alive', lambda name: True)
    monkeypatch.setattr(module, 'status', lambda: None)
    monkeypatch.setattr(module.shutil, 'which', lambda name: '/example/node')
    monkeypatch.setattr(module.socket, 'create_connection', lambda *a, **k: contextlib.nullcontext())
    module.start()
    assert launched == ['api', 'web']


def test_external_database_first_spawn_initializes_only_its_selected_state(tmp_path, monkeypatch):
    module = local_module(tmp_path, monkeypatch)
    monkeypatch.setenv('AIOPSSTUDIO_DATABASE_URL', 'postgresql://example.invalid/example')
    monkeypatch.setattr(module, 'alive', lambda name: False)
    monkeypatch.setattr(module.subprocess, 'Popen', lambda *a, **k: SimpleNamespace(pid=12345))
    monkeypatch.setattr(module, 'process_identity', lambda pid: {'pid': pid, 'pgid': pid, 'started_at': 'synthetic', 'command': 'synthetic'})
    module.spawn('example', ['synthetic-command'])
    assert (tmp_path / 'runtime/example.log').is_file()
    assert (tmp_path / 'runtime/private/master.key').is_file()
    assert (tmp_path / 'runtime/example.identity.json').is_file()


@pytest.mark.parametrize('raw', ['{"ports":{"web":3000,"web":3100}}', '{"ports":{"web":NaN}}'])
def test_external_json_rejects_duplicates_and_non_finite_values(tmp_path, raw):
    path = tmp_path / 'deployment.json'
    path.write_text(raw)
    with pytest.raises(ValueError):
        load_config(path, environ={})
    bootstrap = tmp_path / 'bootstrap.json'
    bootstrap.write_text('{"version":1,"settings":' + raw + '}')
    bootstrap.chmod(0o600)
    with pytest.raises(ValueError):
        read_external_json(bootstrap)


def test_bootstrap_rejects_non_string_dependency_before_network(tmp_path):
    with pytest.raises(ValueError):
        validate_bootstrap({'version': 1, 'skills': [{'name': 'demo', 'content': 'synthetic', 'dependencies': [{'server_name': [], 'tool_name': 'read'}]}]}, tmp_path)


@pytest.mark.parametrize('url', ['https://example.invalid', 'http://example.invalid', 'http://user:pass@127.0.0.1:3000',
    'http://127.0.0.1:3000/api', 'http://127.0.0.1:3000/?token=example', '//127.0.0.1:3000'])
def test_bootstrap_password_target_is_loopback_origin_only(url):
    with pytest.raises(ValueError):
        local_origin(url)


@pytest.mark.parametrize('url', ['http://localhost:3000/', 'http://127.0.0.1:3000'])
def test_bootstrap_loopback_alias_keeps_url_origin_and_host_consistent(tmp_path, monkeypatch, url):
    bootstrap = tmp_path / 'bootstrap.json'
    bootstrap.write_text(json.dumps({'version': 1}))
    bootstrap.chmod(0o600)
    password = tmp_path / 'password'
    password.write_text('synthetic-bootstrap-password')
    requests = []
    def handle(request):
        requests.append(request.url.path)
        assert request.url.host == '127.0.0.1'
        assert request.headers['Origin'] == 'http://127.0.0.1:3000'
        assert request.headers['Host'] == '127.0.0.1:3000'
        return httpx.Response(200, json={'items': []})
    client_class = httpx.Client
    monkeypatch.delenv('AIOPSSTUDIO_DEPLOY_CONFIG', raising=False)
    monkeypatch.setattr(seed_module.httpx, 'Client', lambda **kwargs: client_class(**kwargs, transport=httpx.MockTransport(handle)))
    seed_module.main(['--url', url, '--bootstrap', str(bootstrap), '--password-file', str(password)])
    assert requests == ['/api/login', '/api/mcp-servers', '/api/model-configs', '/api/skills']


def test_bootstrap_is_private_external_and_content_cannot_escape(tmp_path):
    path = tmp_path / 'bootstrap.json'
    path.write_text(json.dumps({'version': 1}))
    path.chmod(0o644)
    with pytest.raises(ValueError, match='0600'):
        read_external_json(path)
    path.chmod(0o600)
    assert read_external_json(path)['mcp_servers'] == []
    content = tmp_path / 'method.md'
    content.write_text('synthetic method')
    assert validate_bootstrap({'version': 1, 'skills': [{'name': 'demo', 'content_file': 'method.md'}]}, tmp_path)['skills'][0]['content'] == 'synthetic method'
    for content_file in ['../outside.md', str(content)]:
        with pytest.raises(ValueError):
            validate_bootstrap({'version': 1, 'skills': [{'name': 'demo', 'content_file': content_file}]}, tmp_path)
    path.write_text(json.dumps({'version': True}))
    with pytest.raises(ValueError):
        read_external_json(path)
    with pytest.raises(ValueError):
        read_external_json(ROOT / 'package.json')


def test_bootstrap_discovery_never_opens_an_undiscovered_tool(tmp_path):
    requests = []
    def handle(request):
        requests.append((request.method, request.url.path))
        if request.method == 'GET':
            return httpx.Response(200, json={'items': []})
        if request.url.path.endswith('/discover'):
            return httpx.Response(200, json={'config': {'id': 'synthetic-server', 'revision': 2, 'tools': [{'name': 'other_read'}]}})
        return httpx.Response(200, json={'id': 'synthetic-server', 'revision': 1})
    body = validate_bootstrap({'version': 1, 'mcp_servers': [{'name': 'demo', 'url': 'https://mcp.example.invalid/mcp', 'allowed_tools': ['approved_read']}]}, tmp_path)
    with httpx.Client(base_url='http://127.0.0.1:3000', transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ValueError, match='未在实际发现目录'):
            apply_bootstrap(client, body)
    assert requests[-1] == ('POST', '/api/mcp-servers/synthetic-server/discover')
    assert not any(path.endswith('/publish') or '/tools/call' in path for _, path in requests)


@pytest.mark.parametrize('credential', ['synthetic-input-credential', 'synthetic-quote"slash\\newline\n中文'])
def test_bootstrap_api_errors_never_echo_input_secret(tmp_path, credential):
    body = validate_bootstrap({'version': 1, 'mcp_servers': [{'name': 'demo', 'url': 'https://mcp.example.invalid/mcp', 'secret_value': credential, 'allowed_tools': []}]}, tmp_path)
    def handle(request):
        if request.method == 'GET':
            return httpx.Response(200, json={'items': []})
        return httpx.Response(422, content=json.dumps({'detail': credential}, ensure_ascii=True))
    with httpx.Client(base_url='http://127.0.0.1:3000', transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(RuntimeError) as caught:
            apply_bootstrap(client, body)
    assert credential not in str(caught.value)
    assert str(caught.value) == '管理请求失败 HTTP 422；请在工作台检查对应配置和连接'


def test_bootstrap_publishes_only_discovered_allowlist_and_resolves_names(tmp_path):
    requests = []
    def handle(request):
        method, path = request.method, request.url.path
        body = json.loads(request.content) if request.content else None
        requests.append((method, path, body))
        if method == 'GET' and path == '/api/settings':
            return httpx.Response(200, json={'revision': 4})
        if method == 'GET':
            return httpx.Response(200, json={'items': []})
        if path.endswith('/discover'):
            return httpx.Response(200, json={'config': {'id': 'server-demo', 'revision': 2, 'tools': [{'name': 'approved_read'}, {'name': 'other_operation'}]}})
        if path.startswith('/api/mcp-servers'):
            if method == 'PUT':
                assert body['allowed_tools'] == ['approved_read']
                assert {x['name']: x['readonly'] for x in body['tools']} == {'approved_read': True, 'other_operation': False}
            return httpx.Response(200, json={'id': 'server-demo', 'revision': 3, 'current_version': 'v-demo'})
        if path.startswith('/api/model-configs'):
            return httpx.Response(200, json={'id': 'model-demo', 'revision': 1, 'current_version': 'v-demo'})
        if path == '/api/settings':
            assert body['default_model_id'] == 'model-demo' and body['expected_revision'] == 4
            return httpx.Response(200, json={'revision': 5})
        if path == '/api/skills':
            assert body['dependencies'] == [{'server_id': 'server-demo', 'tool_name': 'approved_read', 'required': True}]
        return httpx.Response(200, json={'id': 'skill-demo', 'revision': 1})
    body = validate_bootstrap({'version': 1,
        'mcp_servers': [{'name': 'demo', 'url': 'https://mcp.example.invalid/mcp', 'allowed_tools': ['approved_read']}],
        'model_configs': [{'name': 'demo-model', 'provider': 'openai_compatible', 'model': 'example-model', 'base_url': 'https://model.example.invalid/v1'}],
        'default_model_name': 'demo-model', 'skills': [{'name': 'demo-skill', 'content': 'synthetic method',
            'dependencies': [{'server_name': 'demo', 'tool_name': 'approved_read', 'required': True}]}]}, tmp_path)
    with httpx.Client(base_url='http://127.0.0.1:3000', transport=httpx.MockTransport(handle)) as client:
        result = apply_bootstrap(client, body)
    assert result == {'created': 3, 'published': 3, 'preserved_existing': 0}
    assert not any('/runs' in path or path.endswith('/test') or '/tools/call' in path for _, path, _ in requests)
