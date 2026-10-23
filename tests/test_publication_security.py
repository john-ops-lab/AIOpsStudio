"""Exercise real Git hooks and real Gitleaks against disposable repositories only."""
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1]
TOKEN = 'ghp_' + 'Q8mZ3nR5' * 4 + 'Ab9X'
SITE_IP = '.'.join(map(str, [10, 23, 45, 67]))


class PublicationGate(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='easyops-gate-test-')
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
        source = SOURCE / 'design/workbench-concept.png'
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
        credential = 'synthetic-private-' + '9d3a' * 6
        secret = self.base / 'credential'; secret.write_text(credential); secret.chmod(0o600)
        profile = self.base / 'profile.json'; profile.write_text(json.dumps({'version':1,'text_sources':[str(secret)]})); profile.chmod(0o600)
        python = self.root / 'backend/.venv/bin/python'; python.parent.mkdir(parents=True); python.symlink_to(sys.executable)
        self.command('git', 'config', 'easyops.securityProfile', str(profile))
        self.write('guide.md', credential)
        self.denied(self.gate(), 'known_private_material', credential)
        result = self.command(sys.executable, '-c', "import sys;sys.path.insert(0,'tests');from local_secret_scan import known_secret_values;assert len(known_secret_values())==1;print('compatible read-only collector passed')")
        self.assertNotIn(credential, result.stdout + result.stderr)
        secret.unlink()
        self.denied(self.gate(), 'private_source_unavailable', credential)

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


if __name__ == '__main__':
    unittest.main()
