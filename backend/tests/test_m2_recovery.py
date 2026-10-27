"""Bounded synthetic recovery oracles; never touch a running workspace."""
import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import time

from cryptography.fernet import Fernet
from fastapi import HTTPException
import pytest

from aiopsstudio.backup import BackupError, create_backup, restore_backup, verify_backup, verify_restored_authorization
from aiopsstudio.config import Vault
from aiopsstudio.store import LeaseLost, Store
from test_runtime import runtime, tool_payload
from test_runtime_trust import trusted, attempt, wire, close


def test_sql_page_scope_filters_and_stable_created_cursor(tmp_path):
    store = Store('sqlite:///' + str(tmp_path / 'page.db'))
    try:
        for i in range(7):
            store.save('conversation', str(i), {'owner_id': 'a' if i < 5 else 'b', 'title': 'needle ' + str(i),
                'created_at': f'2026-01-0{i+1}T00:00:00+00:00', 'status': 'ready', 'archived_at': 'yes' if i == 4 else None})
        first = store.query_documents('conversation', owner_id='a', search='needle', archived=False, limit=2)
        assert [i['id'] for i in first['items']] == ['3', '2']
        store.save('conversation', 'later', {'owner_id':'a','title':'needle','created_at':'2027-01-01T00:00:00+00:00'})
        second = store.query_documents('conversation', owner_id='a', search='needle', archived=False, limit=2, cursor=first['next_cursor'])
        assert [i['id'] for i in second['items']] == ['1', '0'] and second['next_cursor'] is None
        with pytest.raises(ValueError):
            store.query_documents('conversation', owner_id='b', cursor=first['next_cursor'], search='needle', archived=False)
        assert [i['id'] for i in store.query_documents('conversation', owner_id='a', archived=True)['items']] == ['4']
        assert len(store.query_documents('conversation', owner_id='a', created_from='2026-01-03T08:00:00+08:00', created_to='2026-01-05T07:59:59+08:00')['items']) == 2
        store.save('conversation','legacy-empty-owner',{'owner_id':'','title':'legacy'})
        assert store.query_documents('conversation',owner_id='admin',search='legacy-empty-owner')['items'][0]['id']=='legacy-empty-owner'
        with pytest.raises(ValueError): store.query_documents('conversation',created_from='not-a-date')
        with pytest.raises(ValueError): store.query_documents('conversation',created_from='2026-01-01T00:00:00')
    finally:
        store.close()


def test_old_worker_cannot_write_any_kind_or_event_after_lease_loss(tmp_path):
    url = 'sqlite:///' + str(tmp_path / 'lease.db')
    old, successor = Store(url), Store(url)
    try:
        old.save('run','old',{'status':'queued','usage':{'model_http_request_count':1,'input_tokens':17,'token_usage_known':True}})
        claimed = old.claim_job('old-worker')
        old.save('invocation','inflight',{'run_id':'old','status':'running','source':'model'})
        old.save('run','next',{'status':'queued'})
        with old.execution_scope(claimed):
            successor._execute(f'UPDATE {successor._worker_slot} SET expires_at=0 WHERE id=1')
            assert successor.claim_job('successor')['id'] == 'next'
            for write in [lambda: old.save('run','old',{'status':'succeeded'}),
                          lambda: old.save('invocation','late',{'run_id':'old','result':'late'}),
                          lambda: old.event('old','completed',{'result':'late'})]:
                with pytest.raises(LeaseLost): write()
        final = old.get('run','old')
        assert final['status'] == 'failed' and final['error']['code'] == 'execution_interrupted'
        assert final['usage']['input_tokens'] == 17 and final['usage']['token_usage_known'] is False
        assert old.get('invocation','late') is None and old.events('old')[-1]['type'] == 'failed'
        assert old.get('invocation','inflight')['status'] == 'cancelled'
    finally:
        old.close(); successor.close()


def test_expiry_before_commit_rolls_back_database_and_owned_file(tmp_path):
    store = Store('sqlite:///' + str(tmp_path / 'rollback.db'), lease_seconds=30)
    path = tmp_path / 'new-artifact'
    try:
        store.save('run','job',{'status':'queued'})
        claimed = store.claim_job('worker')
        with store.execution_scope(claimed):
            def write():
                path.write_text('new data')
                store.on_rollback(lambda: path.unlink(missing_ok=True))
                store.save('evidence','new',{'run_id':'job'})
                store._execute(f'UPDATE {store._worker_slot} SET expires_at=0 WHERE id=1')
            with pytest.raises(LeaseLost): store.atomic(write)
        assert not path.exists() and store.get('evidence','new') is None
    finally:
        store.close()


def backup_source(tmp_path):
    root = tmp_path / 'source-assets'; root.mkdir()
    native = root / 'evidence.json'; native.write_text('{"fact":0.18}')
    store = Store('sqlite:///' + str(tmp_path / 'source.db'))
    key = Fernet.generate_key()
    secret = Vault(store,key).put('synthetic-backup-secret')
    store.save('settings','main',{'artifact_dir':str(root),'queue_limit':3})
    store.save('evidence','e1',{'path':str(native),'owner_id':'one','sha256':__import__('hashlib').sha256(native.read_bytes()).hexdigest()})
    store.save('run','r1',{'status':'partial','owner_id':'one','usage':{'input_tokens':23},'report':{'summary':'saved'}})
    store.save('auth_session','old-session',{'owner_id':'one'})
    return store, root, key, secret


def test_backup_restore_preserves_bytes_references_budget_and_quarantines(tmp_path):
    source, root, key, secret = backup_source(tmp_path)
    target = Store('sqlite:///' + str(tmp_path / 'target.db'))
    try:
        source.save('conversation','user-text',{'messages':[str(root / 'user-mentioned-path')],
            'content':str(root / 'user-mentioned-path')})
        before = source.get('run','r1')
        manifest = create_backup(source,root,tmp_path/'backup',master_key=key)
        assert source.get('run','r1') == before and 'master_key' not in manifest
        out = tmp_path/'restored-assets'
        result = restore_backup(target,out,tmp_path/'backup',master_key=key)
        assert result['status'] == 'restored_quarantined'
        assert (out/'evidence.json').read_bytes() == (root/'evidence.json').read_bytes()
        assert target.get('evidence','e1')['path'] == str(out/'evidence.json')
        assert target.get('run','r1') == before and target.get('settings','main')['queue_limit'] == 3
        assert target.get('conversation','user-text') == source.get('conversation','user-text')
        assert Vault(target,key).get(secret) == 'synthetic-backup-secret'
        assert target.get('auth_session','old-session') is None
        assert target.get('restore_guard','main')['verified'] is False
        slot = target._execute(f'SELECT owner,run_id,expires_at FROM {target._worker_slot}').fetchone()
        assert tuple(slot) == (None,None,0)
    finally:
        source.close();target.close()


def test_restore_rebases_declared_path_alias_without_modifying_user_text(tmp_path):
    source,root,key,_=backup_source(tmp_path)
    target=Store('sqlite:///' + str(tmp_path/'target.db'))
    try:
        alias=tmp_path/'source-alias';alias.symlink_to(root,target_is_directory=True)
        evidence=source.get('evidence','e1');evidence['path']=str(alias/'evidence.json')
        source.save('evidence','e1',evidence)
        source.save('material_read','read1',{'path':str(alias/'evidence.json')})
        source.save('conversation','text',{'messages':[str(alias/'evidence.json')]})
        create_backup(source,alias,tmp_path/'backup',master_key=key)
        out=tmp_path/'restored'
        restore_backup(target,out,tmp_path/'backup',master_key=key)
        assert target.get('evidence','e1')['path']==str(out/'evidence.json')
        assert target.get('material_read','read1')['path']==str(out/'evidence.json')
        assert target.get('conversation','text')['messages']==[str(alias/'evidence.json')]
    finally:
        source.close();target.close()


@pytest.mark.parametrize('tampered',[False,True])
def test_restored_legacy_archive_keeps_strict_readonly_binding_after_path_move(tmp_path,tampered):
    from aiopsstudio.legacy_archive import archived, deny_execution, plan_archive, register_archive
    source,root,key,_=backup_source(tmp_path)
    target=Store('sqlite:///' + str(tmp_path/'target.db'))
    try:
        run=source.get('run','r1');run['config_snapshot']={'settings':{'artifact_dir':str(root)}}
        run['evidence']=[source.get('evidence','e1')]
        source.save('run','r1',run)
        registered=register_archive(source,plan_archive(source))
        assert archived(source,'run',source.get('run','r1'),'one')
        if tampered:
            source.save('run','r1',{**source.get('run','r1'),'report':{'analysis':'unauthorized change'}})
        manifest=create_backup(source,root,tmp_path/'backup',master_key=key)
        if tampered:
            with pytest.raises(BackupError) as error: restore_backup(target,tmp_path/'restored',tmp_path/'backup',master_key=key)
            assert error.value.code=='legacy_archive_integrity' and target.get('run','r1') is None
            return
        restore_backup(target,tmp_path/'restored',tmp_path/'backup',master_key=key)
        restored=target.get('run','r1')
        assert archived(target,'run',restored,'one')
        with pytest.raises(HTTPException): deny_execution(target,'run',restored)
        proof=target.get('legacy_archive','upgrade-v1')['restore_provenance'][-1]
        assert proof['source_plan_digest']==registered['plan_digest'] and proof['backup_signature']==manifest['signature']
        assert source.get('legacy_archive','upgrade-v1')==registered
    finally:
        source.close();target.close()


@pytest.mark.parametrize('missing',[False,True])
def test_backup_checks_every_published_skill_package_and_rebases_declared_path(tmp_path,missing):
    from aiopsstudio.config import ConfigService
    source,root,key,_=backup_source(tmp_path)
    target=Store('sqlite:///' + str(tmp_path/'target.db'))
    try:
        config=ConfigService(source,Vault(source,key),root)
        skill=config.save('skill',{'name':'recovery-package','content':'---\nname: recovery-package\ndescription: 合成恢复包\n---\n固定方法',
            'references':{'references/a.txt':'合成固定参考'}})
        published=config.publish('skill',skill['id'],skill['revision'],channel='experimental')
        snapshot=published['versions'][0]['snapshot']
        package=Path(snapshot['artifact_path'])
        if missing:
            (package/'references/a.txt').unlink()
            with pytest.raises(BackupError) as error: create_backup(source,root,tmp_path/'backup',master_key=key)
            assert error.value.code=='skill_reference_missing' and not (tmp_path/'backup').exists()
            return
        create_backup(source,root,tmp_path/'backup',master_key=key)
        out=tmp_path/'restored'
        restore_backup(target,out,tmp_path/'backup',master_key=key)
        restored=target.get('skill',skill['id'])['versions'][0]['snapshot']
        assert Path(restored['artifact_path'])==out/package.relative_to(root)
        assert (Path(restored['artifact_path'])/'SKILL.md').read_text()==snapshot['content']
        assert (Path(restored['artifact_path'])/'references/a.txt').read_text()=='合成固定参考'
    finally:
        source.close();target.close()


@pytest.mark.parametrize('damage',['key','database','artifact'])
def test_backup_missing_parts_or_wrong_key_fail_before_target_write(tmp_path,damage):
    source,root,key,_ = backup_source(tmp_path)
    target = Store('sqlite:///' + str(tmp_path / 'target.db'))
    try:
        directory=tmp_path/'backup';create_backup(source,root,directory,master_key=key)
        before = target.list('schema_migration')
        if damage=='database': (directory/'database.json').unlink()
        if damage=='artifact': (directory/'artifacts/evidence.json').unlink()
        with pytest.raises(BackupError):
            restore_backup(target,tmp_path/'restore',directory,master_key=Fernet.generate_key() if damage=='key' else key)
        assert target.list('schema_migration') == before and target.get('run','r1') is None
        assert not (tmp_path/'restore').exists()
    finally:
        source.close();target.close()


def test_backup_rejects_active_and_nonempty_restore_without_clearing_target(tmp_path):
    source,root,key,_ = backup_source(tmp_path)
    target = Store('sqlite:///' + str(tmp_path / 'target.db'))
    try:
        source.save('run','active',{'status':'running'})
        with pytest.raises(BackupError) as error: create_backup(source,root,tmp_path/'active-copy',master_key=key)
        assert error.value.code=='active_execution' and not (tmp_path/'active-copy').exists()
        source.delete('run','active');create_backup(source,root,tmp_path/'backup',master_key=key)
        target.save('user','original',{'name':'keep'})
        original = target.get('user','original')
        with pytest.raises(BackupError) as error: restore_backup(target,tmp_path/'restore',tmp_path/'backup',master_key=key)
        assert error.value.code=='target_database_not_empty' and target.get('user','original')==original
    finally:
        source.close();target.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("history_allowed", [True, False])
async def test_explain_uses_real_sdk_but_never_discovers_or_reads_sources(trusted, monkeypatch, history_allowed):
    import httpx
    from aiopsstudio import connectors
    rt,case=trusted
    if history_allowed:
        policy=rt.store.get('egress_policy','main');policy['version']='m2-explicit-history-test'
        policy['types']['history']['fields'].update({
            '/scope':'allow','/previous_run_id':'allow','/truncated':'allow','/notice':'text',
            '/rounds/*/run_id':'allow','/rounds/*/status':'allow','/rounds/*/target':'alias',
            '/rounds/*/window/start':'allow','/rounds/*/window/end':'allow',
            '/rounds/*/user_messages':'allow','/rounds/*/user_messages/*/id':'allow',
            '/rounds/*/user_messages/*/content':'text','/rounds/*/report_summary':'text'})
        rt.store.save('egress_policy','main',policy)
    parent=await attempt(rt,case)
    rt._update(parent['id'],{'status':'partial','report':{'analysis':'原合成报告，根因未知'},'completed_at':'2026-01-01T00:00:00Z'})
    child=await rt.submit({**case['request'],'skill_id':'skill_evaluation','parent_run_id':parent['id'],
        'mode':'explain','message':'请解释原报告','client_request_id':'explain-synthetic'},owner_id='admin')
    sent=[]
    model_factory=rt._model
    model=wire(rt,child,sent)
    async def response(request):
        body=json.loads(request.content);sent.append(body)
        assert 'tools' not in body
        return httpx.Response(200,json={'id':'explanation','object':'chat.completion','created':1,'model':'synthetic',
            'choices':[{'index':0,'message':{'role':'assistant','content':'仅解释保存结果，根因仍未知。'},'finish_reason':'stop'}],
            'usage':{'prompt_tokens':23,'completion_tokens':11,'total_tokens':34}})
    model.root_async_client._client._transport=httpx.MockTransport(response)
    monkeypatch.setattr(rt,'_model',lambda *a,**k:model)
    async def forbidden(*a,**k): raise AssertionError('explanation must not access sources')
    monkeypatch.setattr(connectors,'mcp_discover',forbidden);monkeypatch.setattr(connectors,'mcp_call',forbidden)
    second=None
    try:
        await rt.execute(child)
        final=rt.store.get('run',child['id'])
        if not history_allowed:
            assert final['status']=='failed' and final['error']['code']=='policy_egress_field'
            assert sent==[] and final['usage'].get('model_http_request_count',0)==0
            return
        assert final['status']=='succeeded',final.get('error')
        assert len(sent)==1 and final['usage']['model_http_request_count']==1
        assert final['usage']['source_request_count']==final['usage']['tool_call_count']==0
        assert final['report']['explains_run_id']==parent['id'] and final['report']['facts']==[]
        assert rt.store.get('run',parent['id'])['report']['analysis']=='原合成报告，根因未知'
        second=await rt.submit({**case['request'],'skill_id':'skill_evaluation','parent_run_id':child['id'],
            'mode':'explain','message':'再解释上一个结果','client_request_id':'explain-synthetic-second'},owner_id='admin')
        monkeypatch.setattr(rt,'_model',model_factory)
        second_model=wire(rt,second,sent)
        second_model.root_async_client._client._transport=httpx.MockTransport(response)
        monkeypatch.setattr(rt,'_model',lambda *a,**k:second_model)
        await rt.execute(second)
        repeated=rt.store.get('run',second['id'])
        assert repeated['status']=='succeeded', repeated.get('error')
        assert repeated['report']['explains_run_id']==child['id']
        assert repeated['report']['reference_run_id']==parent['id']
        assert repeated['usage']['source_request_count']==repeated['usage']['tool_call_count']==0
        assert len(sent)==2
    finally:
        await close(rt,child)
        if second: await close(rt,second)


@pytest.mark.asyncio
async def test_queue_limit_applies_to_test_admission_without_cancel_existing(runtime):
    settings=runtime.settings();settings['queue_limit']=1;runtime.store.save('settings','main',settings)
    first=await runtime.submit(tool_payload('queue-one'))
    with pytest.raises(HTTPException) as error: await runtime.submit(tool_payload('queue-two'))
    assert error.value.status_code==429 and runtime.store.get('run',first['id'])['status']=='queued'


@pytest.mark.parametrize('invalid',['cycle','cross_conversation','deleted'])
def test_saved_explanation_reference_refuses_invalid_chain(runtime,invalid):
    from aiopsstudio.runtime import ExecutionStop
    original={'id':'original','thread_id':'same','owner_id':'admin','status':'succeeded',
        'trust_version':'trusted-runtime/1','report':{'analysis':'saved'}}
    parent={**copy.deepcopy(original),'id':'first-explanation','mode':'explain',
        'report':{'mode':'explain','analysis':'saved explanation','reference_run_id':'original'}}
    if invalid=='cycle': parent['report']['reference_run_id']=parent['id']
    if invalid=='cross_conversation': original['thread_id']='another'
    if invalid=='deleted': original['deleted_at']='2026-01-01T00:00:00Z'
    runtime.store.save('run',original['id'],original)
    runtime.store.save('run',parent['id'],parent)
    with pytest.raises((ExecutionStop,HTTPException)):
        runtime._explanation_reference({'id':'new','thread_id':'same','owner_id':'admin'},parent)


def test_terminal_owner_keeps_epoch_until_delivery_release(tmp_path):
    store=Store('sqlite:///' + str(tmp_path/'delivery.db'))
    try:
        store.save('run','first',{'status':'queued'})
        first=store.claim_job('worker')
        store.save('run','second',{'status':'queued'})
        with store.execution_scope(first):
            value=store.get('run','first');value['status']='succeeded';store.save('run','first',value)
        assert store.claim_job('other') is None
        assert store.release_job('first','worker') is True
        assert store.claim_job('other')['id']=='second'
    finally:
        store.close()


@pytest.mark.asyncio
async def test_restore_quarantine_blocks_admission_history_and_expiry(runtime):
    parent=await runtime.submit(tool_payload('quarantine-parent'))
    runtime._update(parent['id'],{'status':'failed','completed_at':'2020-01-01T00:00:00Z'})
    runtime.store.save('restore_guard','main',{'verified':False})
    before=runtime.store.get('run',parent['id'])
    with pytest.raises(HTTPException) as error: await runtime.submit(tool_payload('quarantine-new'))
    assert error.value.status_code==503
    with pytest.raises(HTTPException) as error: runtime.detail(parent['id'])
    assert error.value.status_code==503
    assert await runtime.cleanup_expired()==[]
    assert runtime.store.get('run',parent['id'])==before


def test_restore_refuses_preexisting_checkpoints_even_without_documents(tmp_path):
    source,root,key,_=backup_source(tmp_path)
    target=Store('sqlite:///' + str(tmp_path/'target.db'))
    try:
        create_backup(source,root,tmp_path/'backup',master_key=key)
        target._execute('CREATE TABLE checkpoints (id TEXT PRIMARY KEY,data TEXT)')
        target._execute("INSERT INTO checkpoints VALUES ('unrelated','keep')")
        with pytest.raises(BackupError) as error: restore_backup(target,tmp_path/'restore',tmp_path/'backup',master_key=key)
        assert error.value.code=='target_checkpoints_not_empty'
        assert tuple(target._execute('SELECT * FROM checkpoints').fetchone())==('unrelated','keep')
        assert target.get('run','r1') is None
    finally:
        source.close();target.close()


def test_restore_rechecks_empty_database_after_copy_before_any_delete(tmp_path,monkeypatch):
    from aiopsstudio import backup
    source,root,key,_=backup_source(tmp_path)
    url='sqlite:///' + str(tmp_path/'target.db');target,other=Store(url),Store(url)
    try:
        create_backup(source,root,tmp_path/'backup',master_key=key)
        real_copy=backup.shutil.copyfile
        def concurrent_copy(*args,**kwargs):
            result=real_copy(*args,**kwargs)
            other.save('settings','concurrent',{'must':'remain'})
            return result
        monkeypatch.setattr(backup.shutil,'copyfile',concurrent_copy)
        with pytest.raises(BackupError) as error: restore_backup(target,tmp_path/'restore',tmp_path/'backup',master_key=key)
        assert error.value.code=='target_database_not_empty'
        assert target.get('settings','concurrent')['must']=='remain' and target.get('run','r1') is None
        assert not list((tmp_path/'restore').rglob('*'))
    finally:
        source.close();target.close();other.close()


@pytest.mark.asyncio
async def test_explicit_retry_pins_original_versions_and_keeps_separate_usage(runtime):
    parent=await runtime.submit(tool_payload('retry-pin-parent'))
    runtime._update(parent['id'],{'status':'failed','usage':{**parent['usage'],'input_tokens':31,'model_call_count':1}})
    old=copy.deepcopy(runtime.store.get('run',parent['id']))
    entity=runtime.store.get('mcp','server_fixture')
    new=copy.deepcopy(entity['versions'][0]);new['id']='new-v2';new['snapshot']['name']='new configuration'
    entity['versions'].append(new);entity['current_version']='new-v2';runtime.store.save('mcp',entity['id'],entity)
    retried=await runtime.retry(parent['id'],{'client_request_id':'retry-pin-child'})
    child=runtime.store.get('run',retried['id'])
    assert child['config_snapshot']==old['config_snapshot'] and child['analysis_context']==old['analysis_context']
    assert child['policy_snapshot']==old['policy_snapshot'] and child['config_revision']==old['config_revision']
    assert child['budget']==old['budget'] and child['usage']['input_tokens']==0
    assert child['retry_usage_reference']==old['id'] and child['retry_budget_semantics']=='new_attempt_independent_budget'
    assert runtime.store.get('run',old['id'])==old
    with pytest.raises(HTTPException) as error: await runtime.retry(parent['id'],{'model_id':'other'})
    assert error.value.status_code==422
    entity['blocked']=True;runtime.store.save('mcp',entity['id'],entity)
    with pytest.raises(HTTPException) as error: await runtime.retry(parent['id'],{'client_request_id':'retry-blocked'})
    assert error.value.status_code==409


@pytest.mark.asyncio
async def test_client_cannot_supply_frozen_retry_configuration(runtime):
    with pytest.raises(HTTPException) as error:
        await runtime.submit({**tool_payload('forged-freeze'),'config_snapshot':{'model':{'model':'forged'}}})
    assert error.value.status_code==422
    assert not runtime.store.list('run')


def test_restore_rechecks_copied_bytes_before_database_commit(tmp_path,monkeypatch):
    from aiopsstudio import backup
    source,root,key,_=backup_source(tmp_path)
    target=Store('sqlite:///' + str(tmp_path/'target.db'))
    try:
        create_backup(source,root,tmp_path/'backup',master_key=key)
        real_copy=backup.shutil.copyfile
        def damaged_copy(*args,**kwargs):
            result=real_copy(*args,**kwargs);Path(args[1]).write_text('changed during copy');return result
        monkeypatch.setattr(backup.shutil,'copyfile',damaged_copy)
        with pytest.raises(BackupError) as error: restore_backup(target,tmp_path/'restore',tmp_path/'backup',master_key=key)
        assert error.value.code=='copied_artifact_integrity'
        assert target.get('run','r1') is None and not list((tmp_path/'restore').rglob('*'))
    finally:
        source.close();target.close()


def test_backup_rejects_missing_normal_attachment_blob_and_preserves_source(tmp_path):
    from aiopsstudio.attachments import AttachmentsService
    source,root,key,_=backup_source(tmp_path)
    try:
        source.save('conversation','c',{'owner_id':'one','messages':[]})
        attachments=AttachmentsService(source,root,Vault(source,key))
        record=attachments.upload('one','c',{'name':'sample.txt','media_type':'text/plain','content_base64':'c2FtcGxl'})
        metadata=source.get('attachment',record['id'])
        attachments._path(metadata).unlink()
        with pytest.raises(BackupError) as error: create_backup(source,root,tmp_path/'backup',master_key=key)
        assert error.value.code=='artifact_reference_missing'
        assert source.get('attachment',record['id'])==metadata and not (tmp_path/'backup').exists()
    finally:
        source.close()


def test_restore_never_uses_backup_or_original_artifact_tree_as_target(tmp_path):
    source,root,key,_=backup_source(tmp_path)
    target=Store('sqlite:///' + str(tmp_path/'target.db'))
    try:
        directory=tmp_path/'backup';create_backup(source,root,directory,master_key=key)
        original=(directory/'manifest.json').read_bytes()
        for path in [directory/'artifacts'/'nested',root/'nested',directory]:
            with pytest.raises(BackupError) as error: restore_backup(target,path,directory,master_key=key)
            assert error.value.code=='target_overlaps_backup_or_source'
        assert (directory/'manifest.json').read_bytes()==original
        assert verify_backup(directory,master_key=key)[0]['schema']=='aiopsstudio-private-backup/1'
        assert target.get('run','r1') is None
    finally:
        source.close();target.close()
