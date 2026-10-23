"""Exercise real Git hooks and real Gitleaks against disposable repositories only."""
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1]
TOKEN = 'ghp_' + 'Q8mZ3nR5' * 4 + 'Ab9X'
SITE_IP = '.'.join(map(str, [10, 23, 45, 67]))


class PublicationGate(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='aiopsstudio-gate-test-')
        self.base = Path(self.temporary.name)
        self.root = self.base / 'repo'
        self.root.mkdir()
        for folder in ['scripts', '.githooks']:
            shutil.copytree(SOURCE / folder, self.root / folder, ignore=shutil.ignore_patterns('__pycache__'))
        (self.root / 'tests').mkdir()
        shutil.copy2(SOURCE / 'tests/local_secret_scan.py', self.root / 'tests/local_secret_scan.py')
        (self.root / 'security').mkdir()
        (self.root / 'security/reviewed-assets.json').write_text('{"version":1,"png":{}}')
        self.command('git', 'init', '-b', 'main')
        self.command('git', 'config', 'user.name', 'Example Developer')
        self.command('git', 'config', 'user.email', 'developer@example.com')
        self.command('git', 'config', 'core.hooksPath', '/dev/null')
        self.write('README.md', 'Public example\n')
        self.commit('initial safe tree')
        self.remote = self.base / 'remote.git'
        self.command('git', 'init', '--bare', str(self.remote))
        self.command('git', 'remote', 'add', 'origin', str(self.remote))

    def tearDown(self):
        self.temporary.cleanup()

    def command(self, *args, data=None, ok=True, env=None):
        result = subprocess.run(args, input=data, cwd=self.root, capture_output=True, text=True,
            env={**os.environ, 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1', **(env or {})})
        if ok:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def write(self, path, text):
        p = self.root / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        self.command('git', 'add', '--', path)

    def commit(self, message='synthetic commit'):
        self.command('git', '-c', 'core.hooksPath=/dev/null', 'add', '--all')
        self.command('git', '-c', 'core.hooksPath=/dev/null', 'commit', '-m', message)
        return self.command('git', 'rev-parse', 'HEAD').stdout.strip()

    def gate(self, mode='staged', data=None, env=None):
        return self.command(sys.executable, 'scripts/security_scan.py', mode, data=data, ok=False, env=env)

    def denied(self, result, rule=None, hidden=None):
        self.assertNotEqual(result.returncode, 0, result.stdout)
        if rule:
            self.assertIn(rule, result.stdout + result.stderr)
        if hidden:
            self.assertNotIn(hidden, result.stdout + result.stderr)

    def install(self):
        self.command('git', 'config', '--unset', 'core.hooksPath')
        self.command(sys.executable, 'scripts/install_security_hooks.py')

    def private_profile(self):
        credential = 'synthetic-private-' + '9d3a' * 6
        secret = self.base / 'credential'; secret.write_text(credential); secret.chmod(0o600)
        profile = self.base / 'profile.json'; profile.write_text(json.dumps({'version':1,'text_sources':[str(secret)]})); profile.chmod(0o600)
        python = self.root / 'backend/.venv/bin/python'; python.parent.mkdir(parents=True); python.symlink_to(sys.executable)
        return credential, secret, profile

    def test_clean_snapshot_and_examples(self):
        self.write('.env.example', 'API_KEY=\nAPI_URL=https://api.example.invalid/v1\n')
        self.write('guide.md', 'http://127.0.0.1:3000 and https://example.com\n')
        self.assertEqual(self.gate().returncode, 0)

    def test_installed_hooks_allow_clean_commit_and_push(self):
        self.install()
        self.write('guide.md', 'Public documentation\n')
        self.command('git', 'commit', '-m', 'safe documentation')
        self.command('git', 'push', 'origin', 'main')
        head = self.command('git', 'rev-parse', 'HEAD').stdout.strip()
        self.assertIn(head, self.command('git', 'ls-remote', 'origin', 'refs/heads/main').stdout)

    def test_routes_modules_and_filenames_are_not_site_context(self):
        self.write('guide.md', '/api/users/example\nfrom deploy.local import read_config\nsecrets.local.json\n')
        self.assertEqual(self.gate().returncode, 0)

    def test_staged_secret_is_not_hidden_by_fixed_worktree(self):
        self.write('settings.py', "API_TOKEN = '" + TOKEN + "'\n")
        (self.root / 'settings.py').write_text('API_TOKEN = None\n')
        index_before = self.command('git', 'write-tree').stdout
        self.denied(self.gate(), 'gitleaks:', TOKEN)
        self.assertEqual(self.command('git', 'write-tree').stdout, index_before)
        self.assertEqual((self.root / 'settings.py').read_text(), 'API_TOKEN = None\n')

    def test_unstaged_secret_is_not_part_of_snapshot(self):
        self.write('settings.py', 'PUBLIC = True\n')
        (self.root / 'settings.py').write_text("API_TOKEN = '" + TOKEN + "'\n")
        self.assertEqual(self.gate().returncode, 0)

    def test_actual_precommit_rejects_private_network(self):
        self.install()
        self.write('guide.md', 'source http://' + SITE_IP + '/api\n')
        self.denied(self.command('git', 'commit', '-m', 'synthetic denied', ok=False), 'private_network_address', SITE_IP)

    def test_local_paths_and_internal_hosts(self):
        path = '/' + 'Users' + '/fixture-person/private-config'
        self.write('guide.md', path + '\nhttps://source.' + 'internal/api\n')
        result = self.gate()
        self.denied(result, 'personal_home_path', path)
        self.assertIn('internal_hostname', result.stdout)

    def test_encoded_context_is_detected(self):
        body = json.dumps({'address': SITE_IP})
        self.write('guide.md', base64.b64encode(body.encode()).decode())
        self.denied(self.gate(), 'private_network_address', SITE_IP)

    def test_private_files_and_unknown_binary_are_denied(self):
        for name in ['.env', 'state.local.json', 'private/config.json', 'evidence/result.json', 'data.sqlite', 'backup.zip']:
            with self.subTest(name=name):
                self.write(name, 'synthetic')
        self.write('attachment.bin', 'before\x00after')
        result = self.gate()
        self.denied(result, 'runtime_or_private_file')
        self.assertIn('binary_requires_explicit_review', result.stdout)

    def test_png_requires_review_and_clean_metadata(self):
        source = SOURCE / 'design/aiopsstudio-workbench-concept.png'
        target = self.root / 'demo.png'
        target.write_bytes(source.read_bytes())
        self.command('git', 'add', 'demo.png')
        self.denied(self.gate(), 'binary_requires_explicit_review')
        manifest = {'version': 1, 'png': {'demo.png': hashlib.sha256(target.read_bytes()).hexdigest()}}
        (self.root / 'security/reviewed-assets.json').write_text(json.dumps(manifest))
        self.denied(self.gate(), 'binary_requires_explicit_review')
        self.command('git', 'add', 'security/reviewed-assets.json')
        self.assertEqual(self.gate().returncode, 0)
        target.write_bytes(target.read_bytes() + b'EXTRA')
        manifest['png']['demo.png'] = hashlib.sha256(target.read_bytes()).hexdigest()
        (self.root / 'security/reviewed-assets.json').write_text(json.dumps(manifest))
        self.command('git', 'add', 'demo.png', 'security/reviewed-assets.json')
        self.denied(self.gate(), 'binary_requires_explicit_review')

    def test_suppression_environment_does_not_disable_default_rules(self):
        self.write('settings.py', "API_TOKEN = '" + TOKEN + "' # gitleaks:allow\n")
        self.denied(self.gate(env={'GITLEAKS_CONFIG_TOML': '[allowlist]\npaths=[".*"]', 'SKIP': 'gitleaks'}), 'gitleaks:', TOKEN)

    def test_missing_scanner_fails_closed(self):
        tool_dir = self.base / 'tools'; tool_dir.mkdir()
        (tool_dir / 'git').symlink_to(shutil.which('git'))
        result = self.gate(env={'PATH': str(tool_dir)})
        self.denied(result, 'gitleaks_missing')

    def test_private_profile_dynamic_read_and_missing_source(self):
        credential, secret, profile = self.private_profile()
        self.command('git', 'config', 'easyops.securityProfile', str(profile))
        self.write('guide.md', credential)
        self.denied(self.gate(), 'known_private_material', credential)
        result = self.command(sys.executable, '-c', "import sys;sys.path.insert(0,'tests');from local_secret_scan import known_secret_values;assert len(known_secret_values())==1;print('compatible read-only collector passed')")
        self.assertNotIn(credential, result.stdout + result.stderr)
        secret.unlink()
        self.denied(self.gate(), 'private_source_unavailable', credential)

    def test_new_and_same_legacy_profile_keys_still_scan_private_values(self):
        credential, _, profile = self.private_profile()
        self.command('git', 'config', 'aiopsstudio.securityProfile', str(profile))
        self.write('guide.md', credential)
        self.denied(self.gate(), 'known_private_material', credential)
        self.command('git', 'config', 'easyops.securityProfile', str(profile))
        self.denied(self.gate(), 'known_private_material', credential)

    def test_conflicting_profile_keys_fail_closed_without_exposing_paths(self):
        _, _, profile = self.private_profile()
        self.command('git', 'config', 'easyops.securityProfile', str(profile))
        other = self.base / 'different-profile.json'
        self.command('git', 'config', 'aiopsstudio.securityProfile', str(other))
        self.denied(self.gate(), 'private_profile_conflict', str(profile))
        self.assertNotIn(str(other), self.gate().stdout)

    def test_invalid_new_profile_never_falls_back_to_legacy(self):
        _, _, profile = self.private_profile()
        self.command('git', 'config', 'easyops.securityProfile', str(profile))
        self.command('git', 'config', 'aiopsstudio.securityProfile', '')
        self.denied(self.gate(), 'private_profile_config_failed', str(profile))

    def test_multiple_values_of_one_profile_key_are_rejected(self):
        _, _, profile = self.private_profile()
        for _ in range(2):
            self.command('git', 'config', '--add', 'aiopsstudio.securityProfile', str(profile))
        self.denied(self.gate(), 'private_profile_config_failed', str(profile))

    def test_installer_new_profile_uses_canonical_key_only(self):
        _, _, profile = self.private_profile()
        self.command('git', 'config', '--unset', 'core.hooksPath')
        self.command(sys.executable, 'scripts/install_security_hooks.py', '--local-profile', str(profile))
        self.assertEqual(self.command('git', 'config', '--local', '--get', 'aiopsstudio.securityProfile').stdout.strip(), str(profile.resolve()))
        self.assertEqual(self.command('git', 'config', '--local', '--get', 'easyops.securityProfile', ok=False).returncode, 1)

    def test_installer_preserves_legacy_profile_and_accepts_same_pair(self):
        _, _, profile = self.private_profile()
        self.command('git', 'config', 'easyops.securityProfile', str(profile))
        self.command('git', 'config', '--unset', 'core.hooksPath')
        self.command(sys.executable, 'scripts/install_security_hooks.py')
        self.assertEqual(self.command('git', 'config', '--local', '--get', 'aiopsstudio.securityProfile').stdout.strip(), str(profile.resolve()))
        self.assertEqual(self.command('git', 'config', '--local', '--get', 'easyops.securityProfile').stdout.strip(), str(profile))
        self.command(sys.executable, 'scripts/install_security_hooks.py', '--local-profile', str(profile))

    def test_installer_conflict_does_not_mutate_git_configuration(self):
        _, _, profile = self.private_profile()
        self.command('git', 'config', 'easyops.securityProfile', str(profile))
        self.command('git', 'config', '--unset', 'core.hooksPath')
        before = (self.root / '.git/config').read_bytes()
        other = self.base / 'other-profile.json'
        result = self.command(sys.executable, 'scripts/install_security_hooks.py', '--local-profile', str(other), ok=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.root / '.git/config').read_bytes(), before)
        self.assertNotIn(str(profile), result.stdout + result.stderr)
        self.command('git', 'config', 'aiopsstudio.securityProfile', str(other))
        before = (self.root / '.git/config').read_bytes()
        result = self.command(sys.executable, 'scripts/install_security_hooks.py', ok=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.root / '.git/config').read_bytes(), before)

    def test_installer_invalid_new_profile_does_not_drop_legacy(self):
        _, _, profile = self.private_profile()
        self.command('git', 'config', 'easyops.securityProfile', str(profile))
        self.command('git', 'config', 'aiopsstudio.securityProfile', '')
        self.command('git', 'config', '--unset', 'core.hooksPath')
        before = (self.root / '.git/config').read_bytes()
        result = self.command(sys.executable, 'scripts/install_security_hooks.py', ok=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.root / '.git/config').read_bytes(), before)

    def test_installer_does_not_overwrite_existing_hooks(self):
        result = self.command(sys.executable, 'scripts/install_security_hooks.py', ok=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.command('git', 'config', 'core.hooksPath').stdout.strip(), '/dev/null')

    def test_actual_prepush_rejects_deleted_historical_secret(self):
        self.write('settings.py', "API_TOKEN = '" + TOKEN + "'\n")
        self.commit()
        self.write('settings.py', 'PUBLIC = True\n')
        self.commit()
        self.install()
        result = self.command('git', 'push', 'origin', 'main', ok=False)
        self.denied(result, 'gitleaks:', TOKEN)
        self.assertEqual(self.command('git', 'ls-remote', 'origin', 'refs/heads/main').stdout, '')

    def test_other_branch_multiple_refs_and_tag_are_scanned(self):
        safe = self.command('git', 'rev-parse', 'HEAD').stdout.strip()
        self.write('guide.md', SITE_IP)
        bad = self.commit()
        self.command('git', 'branch', 'unsafe', bad)
        self.command('git', 'reset', '--hard', safe)
        self.command('git', 'tag', '-a', 'unsafe-tag', bad, '-m', 'synthetic tag')
        self.install()
        for refs in [('main', 'unsafe'), ('unsafe-tag',)]:
            self.denied(self.command('git', 'push', '--atomic', 'origin', *refs, ok=False), 'private_network_address', SITE_IP)
        self.assertEqual(self.command('git', 'ls-remote', 'origin').stdout, '')

    def test_tag_message_and_direct_blob_are_scanned(self):
        self.command('git', 'tag', '-a', 'message-tag', '-m', TOKEN)
        result = self.gate('history')
        self.denied(result, 'gitleaks:', TOKEN)
        blob = self.command('git', 'hash-object', '-w', '--stdin', data=SITE_IP).stdout.strip()
        updates = 'fixture ' + blob + ' refs/tags/raw ' + '0' * 40 + '\n'
        self.denied(self.gate('push', updates), 'private_network_address', SITE_IP)

    def test_commit_message_is_scanned(self):
        self.write('guide.md', 'Public material\n')
        self.commit(TOKEN)
        self.denied(self.gate('history'), 'gitleaks:', TOKEN)

    def test_shallow_history_fails_closed(self):
        sha = self.command('git', 'rev-parse', 'HEAD').stdout.strip()
        (self.root / '.git/shallow').write_text(sha + '\n')
        self.denied(self.gate('history'), 'full_history_required')

    def test_reference_name_is_scanned_without_logging_value(self):
        self.command('git', 'branch', 'site-' + SITE_IP)
        self.install()
        result = self.command('git', 'push', 'origin', 'site-' + SITE_IP, ok=False)
        # Git itself prints the requested ref in stderr; scanner output must not.
        self.denied(result, 'private_network_address')
        self.assertNotIn(SITE_IP, result.stdout)
        self.assertEqual(self.command('git', 'ls-remote', 'origin').stdout, '')

    def test_sensitive_filename_in_forbidden_folder_is_not_logged(self):
        self.write('private/' + TOKEN + '.txt', 'synthetic')
        self.denied(self.gate(), 'runtime_or_private_file', TOKEN)

    def test_delete_only_update_has_no_content(self):
        sha = self.command('git', 'rev-parse', 'HEAD').stdout.strip()
        result = self.gate('push', '(delete) ' + '0' * 40 + ' refs/heads/obsolete ' + sha + '\n')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(json.loads(result.stdout)['objects'], 0)

    def test_filename_is_scanned_without_leaking_it(self):
        self.write('source-' + SITE_IP + '.txt', 'safe')
        self.denied(self.gate(), 'private_network_address', SITE_IP)


class PrivateDatabaseNamespace(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('publication_private_collector', SOURCE / 'scripts/security_local_values.py')
        self.collector = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.collector)

    class Connection:
        def __init__(self, tables, read_only='on'):
            self.tables = set(tables)
            self.queries = []
            self.row = None
            self.read_only = read_only

        def execute(self, query, params=()):
            self.queries.append((query, params))
            if query == 'SELECT to_regclass(%s)':
                self.row = (params[0] if params[0] in self.tables else None,)
            elif query == 'SHOW transaction_read_only':
                self.row = (self.read_only,)
            elif query.startswith('SELECT kind,id,data FROM '):
                self.row = []
            else:
                raise AssertionError('collector attempted an unexpected database operation')
            return self

        def fetchone(self):
            return self.row

        def fetchall(self):
            return self.row

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    @staticmethod
    def tables(prefix):
        return {prefix + '_' + suffix for suffix in ('documents', 'events', 'worker_slot')}

    def test_existing_new_or_legacy_namespace_is_selected_read_only(self):
        for prefix in ('aiopsstudio', 'easyops'):
            with self.subTest(prefix=prefix):
                connection = self.Connection(self.tables(prefix))
                self.assertEqual(self.collector.document_table(connection), prefix + '_documents')
                self.assertEqual(len(connection.queries), 6)
                self.assertTrue(all(query == 'SELECT to_regclass(%s)' for query, _ in connection.queries))

    def test_missing_partial_mixed_or_duplicate_namespace_is_rejected(self):
        for tables in (set(), {'easyops_documents'}, {'aiopsstudio_documents'},
                self.tables('easyops') | {'aiopsstudio_events'},
                self.tables('aiopsstudio') | {'easyops_worker_slot'},
                self.tables('easyops') | self.tables('aiopsstudio')):
            with self.subTest(tables=sorted(tables)):
                connection = self.Connection(tables)
                with self.assertRaises(ValueError):
                    self.collector.document_table(connection)
                self.assertTrue(all(query == 'SELECT to_regclass(%s)' for query, _ in connection.queries))

    def test_collector_checks_read_only_transaction_and_reads_selected_namespace(self):
        from types import SimpleNamespace
        # Fake dependencies and synthetic paths ensure no real database, key or
        # application initialization is involved in this collector regression.
        class File:
            def read_bytes(self):
                return b'synthetic-external-master-key'
        for prefix in ('aiopsstudio', 'easyops'):
            for read_only in ('on', 'off'):
                with self.subTest(prefix=prefix, read_only=read_only):
                    connection = self.Connection(self.tables(prefix), read_only)
                    options = {}
                    def connect(**kwargs):
                        options.update(kwargs)
                        return connection
                    modules = {'psycopg': SimpleNamespace(connect=connect),
                        'cryptography.fernet': SimpleNamespace(Fernet=lambda key: object())}
                    with patch.dict(sys.modules, modules), \
                            patch.object(self.collector, 'external_file', return_value=File()), \
                            patch.object(self.collector, 'json_value', return_value='synthetic-connection-password'):
                        profile = {'postgres': {'host': '127.0.0.1', 'port': 5432, 'database': 'fixture', 'user': 'fixture',
                            'password_source': {}, 'master_key_file': '/absolute/external/master.key'}}
                        if read_only == 'off':
                            with self.assertRaises(ValueError):
                                self.collector.collect(profile)
                            self.assertEqual(connection.queries, [('SHOW transaction_read_only', ())])
                        else:
                            self.assertEqual(self.collector.collect(profile), [])
                            self.assertIn(f'SELECT kind,id,data FROM {prefix}_documents ', connection.queries[-1][0])
                    self.assertEqual(options['options'], '-c default_transaction_read_only=on')


if __name__ == '__main__':
    unittest.main()
