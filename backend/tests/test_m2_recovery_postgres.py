"""Opt-in real PostgreSQL recovery checks in two fresh, explicitly named test DBs.

The main integration owns the container and both database lifetimes. These tests
never create/stop containers or touch the configured running application.
"""
import os
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
import pytest

from aiopsstudio.backup import BackupError, create_backup, restore_backup
from aiopsstudio.config import Vault
from aiopsstudio.fenced_checkpoints import FencedAsyncPostgresSaver
from aiopsstudio.store import LeaseLost, Store
from langgraph.checkpoint.base import empty_checkpoint


def isolated_urls():
    source = os.environ.get('AIOPSSTUDIO_M2_RECOVERY_SOURCE_URL')
    target = os.environ.get('AIOPSSTUDIO_M2_RECOVERY_TARGET_URL')
    if not source or not target:
        pytest.skip('Explicit isolated recovery PostgreSQL databases were not supplied')
    for value in (source,target):
        parsed=urlsplit(value)
        assert parsed.hostname in {'localhost','127.0.0.1','::1'}
        assert parsed.path.removeprefix('/').startswith('m2_recovery_')
    assert source != target
    return source,target


def fresh(store):
    assert all(kind == 'schema_migration' for kind,_ in store._execute(f'SELECT kind,id FROM {store._documents}'))


@pytest.mark.asyncio
async def test_postgres_fenced_checkpoint_and_complete_restore(tmp_path, monkeypatch):
    source_url,target_url=isolated_urls()
    source,target=Store(source_url),Store(target_url)
    try:
        fresh(source);fresh(target)
        assets=tmp_path/'assets';assets.mkdir()
        (assets/'evidence.json').write_bytes(b'{"synthetic_fact":0.18}')
        key=Fernet.generate_key();secret_id=Vault(source,key).put('synthetic-postgres-backup-secret')
        source.save('settings','main',{'queue_limit':2,'artifact_dir':str(assets)})
        source.save('evidence','synthetic-evidence',{'path':str(assets/'evidence.json'),'owner_id':'admin'})
        source.save('run','synthetic-job',{'status':'queued','attempt_id':'synthetic-checkpoint-thread','usage':{'input_tokens':12}})
        claimed=source.claim_job('synthetic-worker')
        config={'configurable':{'thread_id':'synthetic-checkpoint-thread','checkpoint_ns':''}}
        checkpoint=empty_checkpoint();checkpoint['channel_values']={'payload':{'value':'synthetic-content'}}
        checkpoint['channel_versions']={'payload':'1'}
        with source.execution_scope(claimed):
            async with FencedAsyncPostgresSaver.from_conn_string(source_url) as saver:
                await saver.setup()
                saved=await saver.aput(config,checkpoint,{'source':'input','step':0,'parents':{}},{'payload':'1'})
                await saver.aput_writes(saved,[('pending','synthetic-write')],'synthetic-task')
                assert (await saver.aget_tuple(saved)).checkpoint['channel_values']['payload']=={'value':'synthetic-content'}
                source._execute(f'UPDATE {source._worker_slot} SET expires_at=0 WHERE id=1')
                with pytest.raises(LeaseLost):
                    await saver.aput(saved,checkpoint,{'source':'loop','step':1,'parents':{}},{})
        assert source.claim_job('successor') is None
        source_counts={t:source._execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
                       for t in ['checkpoints','checkpoint_blobs','checkpoint_writes','checkpoint_migrations']}
        assert source_counts['checkpoints']==1 and source_counts['checkpoint_blobs']>=1 and source_counts['checkpoint_writes']==1
        manifest=create_backup(source,assets,tmp_path/'backup',master_key=key)
        assert set(source_counts)<=set(manifest['tables'])
        # A target can be nonempty solely through checkpoint tables. Preserve
        # that thread, then explicitly clean this test-owned synthetic thread.
        async with FencedAsyncPostgresSaver.from_conn_string(target_url) as saver:
            await saver.setup()
            unrelated={'configurable':{'thread_id':'unrelated-target-thread','checkpoint_ns':''}}
            await saver.aput(unrelated,empty_checkpoint(),{'source':'input','step':0,'parents':{}},{})
            with pytest.raises(BackupError) as error:
                restore_backup(target,tmp_path/'restored',tmp_path/'backup',master_key=key)
            assert error.value.code=='target_checkpoints_not_empty'
            assert await saver.aget_tuple(unrelated) is not None
            await saver.adelete_thread('unrelated-target-thread')
        # Exercise a second Store writing after preflight, while files copy.
        from aiopsstudio import backup
        other=Store(target_url);real_copy=backup.shutil.copyfile
        def concurrent_copy(*args,**kwargs):
            result=real_copy(*args,**kwargs)
            other.save('settings','concurrent-test',{'must':'remain'})
            return result
        with monkeypatch.context() as patch:
            patch.setattr(backup.shutil,'copyfile',concurrent_copy)
            with pytest.raises(BackupError) as error:
                restore_backup(target,tmp_path/'restored',tmp_path/'backup',master_key=key)
            assert error.value.code=='target_database_not_empty'
            assert other.get('settings','concurrent-test')['must']=='remain'
        other.delete('settings','concurrent-test');other.close()
        result=restore_backup(target,tmp_path/'restored',tmp_path/'backup',master_key=key)
        assert result['status']=='restored_quarantined'
        assert target.get('run','synthetic-job')['usage']['input_tokens']==12
        assert Vault(target,key).get(secret_id)=='synthetic-postgres-backup-secret'
        assert (tmp_path/'restored/evidence.json').read_bytes()==(assets/'evidence.json').read_bytes()
        for table,count in source_counts.items():
            assert target._execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]==count
        async with FencedAsyncPostgresSaver.from_conn_string(target_url) as saver:
            restored=await saver.aget_tuple(saved)
            assert restored.checkpoint['channel_values']['payload']=={'value':'synthetic-content'}
            assert restored.pending_writes[0][2]=='synthetic-write'
    finally:
        source.close();target.close()
