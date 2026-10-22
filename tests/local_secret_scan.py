#!/usr/bin/env python3
"""Mac-only secret regression: report paths/counts, never credential contents."""
from pathlib import Path
import json
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'backend'))


def known_secret_values():
    from deploy.local import env, PRIVATE
    from easyops.store import Store
    from easyops.config import Vault
    local_env = env()
    store = Store(local_env['EASYOPS_DATABASE_URL'])
    try:
        values = Vault(store, (PRIVATE / 'master.key').read_bytes().strip()).sensitive_values()
    finally:
        store.close()
    values.extend([(PRIVATE / 'admin-password').read_text().strip(),
                   (PRIVATE / 'master.key').read_text().strip(),
                   json.loads((PRIVATE / 'local.json').read_text())['db_password']])
    return list({value for value in values if value})


def main():
    secrets = [value.encode() for value in known_secret_values()]
    candidates = [ROOT / path for path in subprocess.check_output(
        ['git', 'ls-files', '--cached', '--others', '--exclude-standard'], cwd=ROOT, text=True).splitlines()]
    from deploy.local import STATE
    state = STATE
    candidates.extend(path for path in (ROOT / 'web/dist').rglob('*') if path.is_file())
    candidates.extend(path for path in (state / 'evidence').rglob('*') if path.is_file())
    candidates.extend(path for path in (state / 'artifacts').rglob('*') if path.is_file())
    candidates.extend(state.glob('*.log'))
    findings = []
    scanned = 0
    for path in sorted(set(candidates)):
        if not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
            continue
        body = path.read_bytes()
        scanned += 1
        if any(value in body for value in secrets):
            findings.append(str(path))
    result = {'known_secret_count': len(secrets), 'files_scanned': scanned,
              'matched_paths': findings, 'passed': not findings}
    print(json.dumps(result, ensure_ascii=False))
    assert not findings, 'Known credential material found; inspect listed paths locally'


if __name__ == '__main__':
    main()
