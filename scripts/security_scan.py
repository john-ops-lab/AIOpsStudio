#!/usr/bin/env python3
"""Read-only publication gate. Never print matched material or raw tool errors."""
import argparse
import base64
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import urllib.parse
import zlib

ROOT = Path(__file__).resolve().parents[1]
MAX_OBJECT = 16 * 1024 * 1024
MAX_TOTAL = 128 * 1024 * 1024
OID = re.compile(r'^[0-9a-f]{40}(?:[0-9a-f]{24})?$')


class GateError(Exception):
    pass


def run_git(*args, data=None, check=True):
    env = dict(os.environ, GIT_NO_REPLACE_OBJECTS='1', GIT_NO_LAZY_FETCH='1')
    r = subprocess.run(['git', '-C', str(ROOT), *args], input=data, capture_output=True, env=env)
    if check and r.returncode:
        raise GateError('git_read_failed')
    return r


def git(*args, data=None):
    return run_git(*args, data=data).stdout


def configured_profile_path():
    """Resolve the renamed local key without ever dropping a legacy profile."""
    paths = []
    for key in ('aiopsstudio.securityProfile', 'easyops.securityProfile'):
        configured = run_git('config', '--local', '--get-all', key, check=False)
        if configured.returncode == 1:
            continue
        if configured.returncode:
            raise GateError('private_profile_config_failed')
        try:
            values = configured.stdout.decode().splitlines()
            if len(values) != 1 or not values[0].strip():
                raise GateError('private_profile_config_failed')
            paths.append(Path(values[0].strip()).expanduser().resolve())
        except Exception:
            raise GateError('private_profile_config_failed') from None
    if len(set(paths)) > 1:
        raise GateError('private_profile_conflict')
    return paths[0] if paths else None


def configured_private_values(include_identifiers=True):
    path = configured_profile_path()
    if path is None:
        return [], []
    try:
        if path.is_relative_to(ROOT) or not path.is_file() or path.stat().st_mode & 0o077:
            raise GateError('private_profile_must_be_external_and_private')
        profile = json.loads(path.read_text())
        if type(profile.get('version')) is not int or profile['version'] != 1 or set(profile) - {'version', 'context_values', 'text_sources', 'json_sources', 'postgres'}:
            raise GateError('private_profile_invalid')
        markers = profile.get('context_values', [])
        if not isinstance(markers, list) or any(not isinstance(v, str) or len(v) < 4 for v in markers):
            raise GateError('private_profile_invalid')
        python = ROOT / 'backend/.venv/bin/python'
        if not python.is_file():
            raise GateError('private_source_python_missing')
        command = [str(python), str(ROOT / 'scripts/security_local_values.py')]
        if not include_identifiers:
            command.append('--credentials-only')
        r = subprocess.run(command,
            input=json.dumps(profile), text=True, capture_output=True, timeout=20,
            env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
        if r.returncode:
            raise GateError('private_source_unavailable')
        values = json.loads(r.stdout)
        if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
            raise GateError('private_source_invalid')
        return [v for v in values if len(v) >= 8], markers
    except GateError:
        raise
    except Exception:
        raise GateError('private_source_unavailable') from None


def objects(mode, stdin):
    """Return Git objects plus every historical blob/path pair, not a worktree diff."""
    if mode == 'staged':
        roots = [git('write-tree').decode().strip()]
    else:
        if git('rev-parse', '--is-shallow-repository').strip() != b'false':
            raise GateError('full_history_required')
        graft = Path(git('rev-parse', '--git-path', 'info/grafts').decode().strip())
        if not graft.is_absolute():
            graft = ROOT / graft
        if graft.exists() and graft.stat().st_size:
            raise GateError('grafted_history_not_supported')
        if mode == 'push':
            roots = []
            lines = stdin.splitlines()
            if not lines:
                raise GateError('push_update_input_required')
            for line in lines:
                parts = line.split()
                if len(parts) != 4 or not OID.fullmatch(parts[1]) or not OID.fullmatch(parts[3]):
                    raise GateError('push_update_input_invalid')
                if set(parts[1]) != {'0'}:
                    roots.append(parts[1])
        else:
            roots = git('for-each-ref', '--format=%(objectname)').decode().splitlines()
            head = run_git('rev-parse', '--verify', 'HEAD', check=False)
            if not head.returncode:
                roots.append(head.stdout.decode().strip())
    roots = sorted(set(roots))
    if not roots:
        return {}, set(), 0
    ids, commits, trees = set(), set(), set()
    # Peel explicit annotated tags so tag messages are checked as well.
    for root in roots:
        target = root
        seen = set()
        while True:
            if not OID.fullmatch(target) or target in seen:
                raise GateError('invalid_git_object')
            seen.add(target)
            kind = git('cat-file', '-t', target).decode().strip()
            ids.add(target)
            if kind == 'tag':
                body = git('cat-file', 'tag', target)
                target = body.splitlines()[0].decode().removeprefix('object ')
                continue
            if kind == 'commit':
                commits.add(target)
            elif kind == 'tree':
                trees.add(target)
            elif kind != 'blob':
                raise GateError('unsupported_git_object')
            break
    if commits:
        ids.update(git('rev-list', '--objects', '--no-object-names', *sorted(commits)).decode().splitlines())
    checks = git('cat-file', '--batch-check=%(objectname) %(objecttype) %(objectsize)',
                 data=('\n'.join(sorted(ids)) + '\n').encode()).decode().splitlines()
    total = 0
    for row in checks:
        fields = row.split()
        if len(fields) != 3 or not fields[2].isdigit():
            raise GateError('git_object_missing')
        size = int(fields[2]); total += size
        if size > MAX_OBJECT or total > MAX_TOTAL:
            raise GateError('scan_size_limit_requires_review')
    raw = git('cat-file', '--batch', data=('\n'.join(sorted(ids)) + '\n').encode())
    offset, bodies = 0, {}
    for _ in ids:
        end = raw.index(b'\n', offset)
        sha, kind, size = raw[offset:end].decode().split()
        size = int(size); start = end + 1
        bodies[sha] = (kind, raw[start:start + size]); offset = start + size + 1
        if kind == 'commit':
            commits.add(sha)
            trees.add(bodies[sha][1].splitlines()[0].decode().removeprefix('tree '))
    paths = set()
    for tree in trees:
        for item in git('ls-tree', '-rz', tree).split(b'\0'):
            if not item:
                continue
            meta, path = item.split(b'\t', 1)
            permission, kind, sha = meta.decode().split()
            name = path.decode('utf-8')
            if permission not in {'100644', '100755'} or kind != 'blob' or any(ord(c) < 32 for c in name):
                raise GateError('unsupported_file_mode_or_name')
            paths.add((sha, name))
            if sha not in bodies:
                size = int(git('cat-file', '-s', sha)); total += size
                if size > MAX_OBJECT or total > MAX_TOTAL:
                    raise GateError('scan_size_limit_requires_review')
                bodies[sha] = ('blob', git('cat-file', 'blob', sha))
    # Direct blob refs must not escape file checks.
    mapped = {sha for sha, _ in paths}
    for sha, (kind, _) in bodies.items():
        if kind == 'blob' and sha not in mapped:
            paths.add((sha, 'unpathed-object-' + sha))
    return bodies, paths, len(commits)


def forbidden_path(name):
    pieces = name.lower().split('/')
    base = pieces[-1]
    if any(p in {'private', 'evidence', 'artifacts', 'logs', '.ssh', '.aws', '.kube', '.local'} for p in pieces):
        return True
    if base in {'local.json', 'secrets.local.json', 'source-bootstrap.json', 'deployment.json',
                'admin-password', 'postgres.env', 'credentials', 'id_rsa', 'id_ed25519'}:
        return True
    if base.endswith('.local.json') or base.endswith('.env'):
        return True
    if base.startswith('.env') and base not in {'.env.example', '.env.sample'}:
        return True
    return bool(re.search(r'\.(?:key|pem|p12|pfx|sqlite\w*|db|log|pid|bundle|zip|gz|tar|7z)$', base))


def png_clean(body):
    if not body.startswith(b'\x89PNG\r\n\x1a\n'):
        return False
    at, kinds = 8, []
    try:
        while at < len(body):
            size = struct.unpack('>I', body[at:at + 4])[0]
            kind = body[at + 4:at + 8]; data = body[at + 8:at + 8 + size]
            crc = struct.unpack('>I', body[at + 8 + size:at + 12 + size])[0]
            if kind not in {b'IHDR', b'IDAT', b'IEND'} or crc != zlib.crc32(kind + data) & 0xffffffff:
                return False
            kinds.append(kind); at += size + 12
            if kind == b'IEND':
                break
        return at == len(body) and kinds[0] == b'IHDR' and kinds[-1] == b'IEND' and b'IDAT' in kinds
    except (ValueError, IndexError, struct.error):
        return False


def variants(values):
    result = set()
    for value in values:
        raw = value.encode()
        result.update([raw, base64.b64encode(raw), base64.urlsafe_b64encode(raw),
            base64.urlsafe_b64encode(raw).rstrip(b'='), urllib.parse.quote(value, safe='').encode(),
            json.dumps(value, ensure_ascii=True)[1:-1].encode(), value.encode('utf-16-le')])
    return result


def scan(mode, stdin=''):
    bodies, paths, commits = objects(mode, stdin)
    if not bodies:
        return {'passed': True, 'objects': 0, 'commits': 0, 'findings': []}
    binary = shutil.which('gitleaks')
    if not binary:
        raise GateError('gitleaks_missing_install_required')
    version = subprocess.run([binary, 'version'], capture_output=True, text=True)
    match = re.search(r'\b(\d+)\.(\d+)\.(\d+)\b', version.stdout)
    if version.returncode or not match or not (8, 30, 1) <= tuple(map(int, match.groups())) < (9, 0, 0):
        raise GateError('gitleaks_version_unsupported')
    values, markers = configured_private_values()
    secrets = variants(values)
    try:
        manifest_ref = ':security/reviewed-assets.json' if mode == 'staged' else 'HEAD:security/reviewed-assets.json'
        approved = json.loads(git('show', manifest_ref))
        if approved.get('version') != 1 or not isinstance(approved.get('png'), dict):
            raise ValueError()
    except Exception:
        raise GateError('asset_review_manifest_invalid') from None
    findings = []
    git_object_count = len(bodies)

    def safe_name(name):
        for value in values + markers:
            name = name.replace(value, '[redacted]')
        name = re.sub(r'(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?!\d|\.\d)', '[address]', name)
        name = re.sub(r'\b[a-z0-9][a-z0-9.-]*\.(?:[l]ocal|[l]an|[i]nternal)\b', '[host]', name, flags=re.I)
        if len(name) > 160 or re.search(r'[A-Za-z0-9_-]{24,}', name):
            return 'git-path-' + hashlib.sha256(name.encode()).hexdigest()[:12]
        return name

    def report(rule, name, line=1):
        finding = {'rule': rule, 'file': safe_name(name), 'line': line}
        if finding not in findings:
            findings.append(finding)

    for sha, name in sorted(paths):
        body = bodies[sha][1]
        if forbidden_path(name):
            report('runtime_or_private_file', name)
        try:
            body.decode('utf-8')
            is_text = b'\0' not in body
        except UnicodeError:
            is_text = False
        if not is_text or Path(name).suffix.lower() in {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.pdf', '.mp3', '.mp4', '.woff', '.woff2'}:
            if not (name.endswith('.png') and approved['png'].get(name) == hashlib.sha256(body).hexdigest() and png_clean(body)):
                report('binary_requires_explicit_review', name)
    names = {}
    for sha, name in sorted(paths):
        names.setdefault(sha, name)
        identifier = 'path-' + hashlib.sha256(name.encode()).hexdigest()
        bodies[identifier] = ('path', name.encode())
        names[identifier] = 'git-path-' + identifier[5:17]
    refs = ([part for row in stdin.splitlines() for part in (row.split()[0], row.split()[2])]
            if mode == 'push' else git('for-each-ref', '--format=%(refname)').decode().splitlines()
            if mode == 'history' else [])
    for ref in refs:
        identifier = 'ref-' + hashlib.sha256(ref.encode()).hexdigest()
        bodies[identifier] = ('path', ref.encode())
        names[identifier] = 'git-ref-' + identifier[4:16]
    home = re.compile(r'(?:/[U]sers/|/[h]ome/)[^\s/\\<>"\']+|(?i:[A-Z]:[\\/]U[s]ers[\\/][^\s/\\<>"\']+)', )
    # URL hosts and quoted hostname values; dotted Python imports and filenames are not hosts.
    internal_host = re.compile(r"(?:[a-z][a-z0-9+.-]*://|[\"'])[a-z0-9][a-z0-9.-]*\.(?:[l]ocal|[l]an|[i]nternal)(?=[:/\s\"'<>]|$)", re.I)
    ipv4 = re.compile(r'(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?!\d|\.\d)')
    for sha, (kind, body) in bodies.items():
        if kind not in {'blob', 'commit', 'tag', 'path'}:
            continue
        name = names.get(sha, kind + '-metadata-' + sha[:12])
        if any(secret in body for secret in secrets):
            report('known_private_material', name)
        for marker in markers:
            needle = marker.encode()
            hit = re.search(rb'(?<![A-Za-z0-9])' + re.escape(needle) + rb'(?![A-Za-z0-9])', body) if marker.isdigit() else needle in body
            if hit:
                report('known_site_context', name)
        try:
            text = body.decode('utf-8')
        except UnicodeError:
            continue
        normalized = urllib.parse.unquote(text).replace('\\/', '/')
        decoded = []
        for token in re.findall(r'(?<![A-Za-z0-9+/_-])[A-Za-z0-9+/_-]{32,}={0,2}(?![A-Za-z0-9+/_-])', text):
            try:
                raw = base64.b64decode(token + '=' * (-len(token) % 4), altchars=b'-_', validate=True)
                if any(v in raw for v in secrets) or any(v.encode() in raw for v in markers):
                    report('encoded_private_material', name)
                if raw.startswith((b'\x89PNG', b'\xff\xd8\xff', b'%PDF', b'PK\x03\x04', b'\x1f\x8b')):
                    report('embedded_media_requires_review', name)
                decoded.append(raw.decode('utf-8'))
            except (ValueError, UnicodeError):
                pass
        normalized += '\n' + '\n'.join(decoded)
        normalized = re.sub(r'\\u([0-9A-Fa-f]{4})', lambda m: chr(int(m.group(1), 16)), normalized)
        for expression, rule in [(home, 'personal_home_path'), (internal_host, 'internal_hostname')]:
            for hit in expression.finditer(normalized):
                if rule == 'personal_home_path' and hit.group().split('/')[-1] in {'example', 'example-user'}:
                    continue
                report(rule, name, normalized[:hit.start()].count('\n') + 1)
        for hit in ipv4.finditer(normalized):
            try:
                p = tuple(map(int, str(ipaddress.IPv4Address(hit.group())).split('.')))
            except ValueError:
                continue
            if p[0] == 10 or p[:2] == (192, 168) or (p[0] == 172 and 16 <= p[1] <= 31) or p[:2] == (169, 254):
                report('private_network_address', name, normalized[:hit.start()].count('\n') + 1)
        if re.search(r'data\s*:\s*(?:image|application|audio|video)/[^\s;,]+;base64\s*,', normalized, re.I):
            report('embedded_media_requires_review', name)
    with tempfile.TemporaryDirectory(prefix='aiopsstudio-security-') as tmp:
        temporary = Path(tmp); content = temporary / 'content'; content.mkdir()
        mapping = {}
        for sha, (kind, body) in bodies.items():
            if kind in {'blob', 'commit', 'tag', 'path'}:
                filename = sha + '.txt'; file = content / filename
                file.write_bytes(body); file.chmod(0o600)
                mapping[filename] = names.get(sha, kind + '-metadata-' + sha[:12])
        config = temporary / 'default.toml'; config.write_text('[extend]\nuseDefault = true\n')
        ignore = temporary / 'empty-ignore'; ignore.touch()
        cmd = [binary, 'dir', str(content), '--config', str(config), '--gitleaks-ignore-path', str(ignore),
            '--ignore-gitleaks-allow', '--redact=100', '--no-banner', '--no-color',
            '--max-decode-depth', '5', '--max-archive-depth', '3', '--max-target-megabytes', '16',
            '--report-format', 'json', '--report-path', '-']
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                env={k: v for k, v in os.environ.items() if not k.startswith('GITLEAKS_')})
            leaks = json.loads(r.stdout or '[]')
            if r.returncode not in {0, 1} or not isinstance(leaks, list) or (r.returncode == 1 and not leaks):
                raise GateError('gitleaks_execution_failed')
            for leak in leaks:
                filename = Path(leak['File']).name
                report('gitleaks:' + leak['RuleID'], mapping.get(filename, 'git-object'), int(leak['StartLine']))
        except GateError:
            raise
        except Exception:
            raise GateError('gitleaks_execution_failed') from None
    return {'passed': not findings, 'objects': git_object_count, 'commits': commits,
        'known_private_values': len(set(values)), 'private_profile': bool(values or markers), 'findings': findings}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode', choices=['staged', 'push', 'history'])
    args = p.parse_args()
    try:
        result = scan(args.mode, sys.stdin.read() if args.mode == 'push' else '')
        print(json.dumps(result, ensure_ascii=True))
        return 0 if result['passed'] else 1
    except GateError as error:
        print(json.dumps({'passed': False, 'error': str(error)}, ensure_ascii=True))
        return 2
    except Exception:
        print(json.dumps({'passed': False, 'error': 'scan_incomplete'}, ensure_ascii=True))
        return 2


if __name__ == '__main__':
    sys.exit(main())
