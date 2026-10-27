#!/usr/bin/env python3
"""Explicit private backup/restore command. Never runs as part of app startup.

Use an environment variable for the database URL, never a credential-bearing
command-line URL. Restores require an empty, isolated database and artifact dir.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from aiopsstudio.backup import BackupError, create_backup, restore_backup, verify_backup, verify_restored_authorization
from aiopsstudio.config import read_private_file
from aiopsstudio.store import Store


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['backup','verify','restore','authorize'])
    parser.add_argument('--database-env',default='AIOPSSTUDIO_DATABASE_URL')
    parser.add_argument('--master-key-file')
    parser.add_argument('--artifact-dir')
    parser.add_argument('--backup-dir')
    parser.add_argument('--current-authorization-file')
    args=parser.parse_args()
    os.umask(0o077)
    if args.action in {'backup','restore','verify'}:
        if not args.master_key_file or not args.backup_dir:
            parser.error('backup/restore/verify需要--master-key-file和--backup-dir')
        key=read_private_file(args.master_key_file,'独立主密钥')
    if args.action=='verify':
        manifest,_=verify_backup(args.backup_dir,master_key=key)
        print(json.dumps({'status':'verified','files':len(manifest['files']),'tables':len(manifest['tables'])}));return
    database=os.environ.get(args.database_env)
    if not database:
        parser.error('数据库环境变量未配置')
    store=Store(database)
    try:
        if args.action=='authorize':
            if not args.current_authorization_file: parser.error('authorize需要--current-authorization-file')
            facts=json.loads(read_private_file(args.current_authorization_file,'当前授权核对'))
            result=verify_restored_authorization(store,**facts)
        else:
            if not args.artifact_dir: parser.error('backup/restore需要--artifact-dir')
            if args.action=='backup':
                manifest=create_backup(store,args.artifact_dir,args.backup_dir,master_key=key)
                result={'status':'backed_up','files':len(manifest['files']),'tables':len(manifest['tables']),'bytes':manifest['total_bytes']}
            else:
                result=restore_backup(store,args.artifact_dir,args.backup_dir,master_key=key)
        print(json.dumps(result))
    finally:
        store.close()


if __name__=='__main__':
    try:
        main()
    except BackupError as error:
        print(json.dumps({'status':'failed','code':error.code}));raise SystemExit(1)
    except Exception:
        print(json.dumps({'status':'failed','code':'backup_operation_failed'}));raise SystemExit(1)
