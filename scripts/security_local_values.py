#!/usr/bin/env python3
"""Private collector: JSON through pipes only; all database transactions read-only."""
import ipaddress
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]


def external_file(value):
    path = Path(value).expanduser().resolve()
    if path.is_relative_to(ROOT) or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError()
    return path


def json_value(source):
    value = json.loads(external_file(source['path']).read_text())
    for part in source['pointer'].strip('/').split('/'):
        value = value[part.replace('~1', '/').replace('~0', '~')]
    if not isinstance(value, str):
        raise ValueError()
    return value


def document_table(connection):
    """Use one complete existing namespace; never initialize or guess a schema."""
    namespaces = []
    for prefix in ('aiopsstudio', 'easyops'):
        present = [connection.execute('SELECT to_regclass(%s)', (prefix + '_' + suffix,)).fetchone()[0] is not None
            for suffix in ('documents', 'events', 'worker_slot')]
        if any(present):
            if not all(present):
                raise ValueError()
            namespaces.append(prefix)
    if len(namespaces) != 1:
        raise ValueError()
    return namespaces[0] + '_documents'


def collect(profile, include_identifiers=True):
    values = set()
    def add(value):
        if isinstance(value, str) and len(value) >= 8:
            values.add(value)
            try:
                parsed = json.loads(value)
            except ValueError:
                parsed = None
            if isinstance(parsed, dict):
                for item in parsed.values():
                    add(item)
            if value.lower().startswith('bearer '):
                add(value[7:])
    for source in profile.get('text_sources', []):
        add(external_file(source).read_text().strip())
    for source in profile.get('json_sources', []):
        add(json_value(source))
    config = profile.get('postgres')
    if config:
        host = config['host']
        if not ipaddress.ip_address(host).is_loopback:
            raise ValueError()
        import psycopg
        from cryptography.fernet import Fernet
        key = external_file(config['master_key_file']).read_bytes().strip()
        cipher = Fernet(key)
        with psycopg.connect(host=host, port=config['port'], dbname=config['database'], user=config['user'],
            password=json_value(config['password_source']), connect_timeout=5,
            options='-c default_transaction_read_only=on') as connection:
            if connection.execute('SHOW transaction_read_only').fetchone()[0] != 'on':
                raise ValueError()
            table = document_table(connection)
            rows = connection.execute(f"SELECT kind,id,data FROM {table} WHERE kind IN ('secret','user','session','model','mcp','skill')").fetchall()
            for kind, identifier, data in rows:
                if include_identifiers and re.fullmatch(r'[a-z]+_[0-9a-f]{16,}', identifier):
                    add(identifier)
                if kind == 'secret':
                    add(data['ciphertext']); add(cipher.decrypt(data['ciphertext'].encode()).decode())
                elif kind == 'user':
                    add(data.get('password_hash'))
                elif kind == 'session':
                    add(identifier)
    return sorted(values)


if __name__ == '__main__':
    try:
        print(json.dumps(collect(json.load(sys.stdin), include_identifiers='--credentials-only' not in sys.argv[1:]), ensure_ascii=True))
    except Exception:
        print('Private source collection failed', file=sys.stderr)
        sys.exit(1)
