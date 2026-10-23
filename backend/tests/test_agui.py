"""The protocol adapter subscribes to existing durable work; it does not run agents."""
import json
import copy
from types import SimpleNamespace
import pytest

from cryptography.fernet import Fernet
from fastapi import HTTPException
from fastapi.testclient import TestClient

from aiopsstudio.agui import configure_agui, protocol_binding_id
from aiopsstudio.main import create_app
from aiopsstudio.store import Store
from test_runtime import runtime
from test_runtime_ownership import http_clients
from m1_fixtures import analysis_defaults


def setup(tmp_path):
    store = Store('sqlite:///' + str(tmp_path / 'adapter.sqlite'))
    app = create_app(store=store, testing=True, enable_runtime=False,
        master_key=Fernet.generate_key(), password='adapter-test-password-only',
        public_origin='http://testserver', artifact_dir=tmp_path/'artifacts')
    stopped=[]
    async def cancel(id, *, owner_id=None):
        stopped.append(id)
        return store.get('run',id)
    def detail(id, *, owner_id=None):
        return {**store.get('run',id), 'events':store.events(id)}
    def require_owned(id, owner_id):
        record=store.get('run',id)
        if not record or (record.get('owner_id') or 'admin')!=owner_id:
            raise HTTPException(404,'分析不存在')
        return record
    app.state.runtime=SimpleNamespace(detail=detail,cancel=cancel,require_owned=require_owned)
    configure_agui(app)
    client=TestClient(app,headers={'Origin':'http://testserver'})
    client.post('/api/login',json={'password':'adapter-test-password-only'}).raise_for_status()
    store.save('run','older',{'thread_id':'thread','status':'succeeded','created_at':'2026-01-01','messages':[]})
    store.save('run','newer',{'thread_id':'thread','status':'running','created_at':'2026-01-02','messages':[]})
    return client,store,stopped


def test_reconnect_replays_database_state_without_starting_work(tmp_path):
    client,store,stopped=setup(tmp_path)
    body={'threadId':'thread','runId':'protocol-old','messages':[], 'forwardedProps':{'business_run_id':'older'}}
    first=client.post('/api/ag-ui',json=body)
    assert first.status_code==200
    events=[json.loads(line[5:]) for line in first.text.splitlines() if line.startswith('data:')]
    assert events[0]['type']=='RUN_STARTED' and events[-1]['type']=='RUN_FINISHED'
    second=client.post('/api/ag-ui',json=body)
    assert second.status_code==200 and len(store.list('run'))==2 and stopped==[]
    assert store.get('agui_binding',protocol_binding_id('admin','protocol-old'))['business_run_id']=='older'


def test_exact_protocol_stop_does_not_cancel_newer_attempt(tmp_path):
    client,store,stopped=setup(tmp_path)
    store.save('agui_binding','protocol-old',{'thread_id':'thread','business_run_id':'older'})
    response=client.post('/api/ag-ui/stop/thread',json={'runId':'protocol-old'})
    assert response.status_code==200 and stopped==['older']
    assert store.get('run','newer')['status']=='running'
    assert client.post('/api/ag-ui/stop/thread',json={'runId':'unknown'}).status_code==404
    assert stopped==['older']


def test_thread_mismatch_is_rejected_before_projection(tmp_path):
    client,store,stopped=setup(tmp_path)
    response=client.post('/api/ag-ui',json={'threadId':'other','runId':'x','messages':[], 'forwardedProps':{'business_run_id':'older'}})
    assert response.status_code==409 and not store.get('agui_binding','x') and stopped==[]


def test_administrator_cannot_project_another_users_thread_or_binding(tmp_path):
    client,store,stopped=setup(tmp_path)
    store.save('run','private',{'owner_id':'another-user','thread_id':'private-thread','status':'succeeded','created_at':'2026-01-03','messages':[{'id':'hidden','role':'assistant','content':'private body'}]})
    foreign_key=protocol_binding_id('another-user','shared-protocol')
    store.save('agui_binding',foreign_key,{'owner_id':'another-user','thread_id':'private-thread','business_run_id':'private'})
    body={'threadId':'private-thread','runId':'shared-protocol','messages':[], 'forwardedProps':{'business_run_id':'private'}}
    for path in ('/api/ag-ui','/api/ag-ui/connect'):
        response=client.post(path,json=body)
        assert response.status_code==404 and 'private body' not in response.text
    assert client.get('/api/ag-ui/status/private-thread').status_code==404
    assert client.post('/api/ag-ui/stop/private-thread',json={'runId':'shared-protocol'}).status_code==404
    assert stopped==[] and not store.get('agui_binding',protocol_binding_id('admin','shared-protocol'))
    assert store.get('agui_binding',foreign_key)['business_run_id']=='private'


def test_binding_scope_allows_same_protocol_identifier_for_another_owner(tmp_path):
    client,store,stopped=setup(tmp_path)
    foreign_key=protocol_binding_id('another-user','shared-protocol')
    store.save('agui_binding',foreign_key,{'owner_id':'another-user','thread_id':'private-thread','business_run_id':'unrelated'})
    response=client.post('/api/ag-ui',json={'threadId':'thread','runId':'shared-protocol','messages':[], 'forwardedProps':{'business_run_id':'older'}})
    assert response.status_code==200
    assert store.get('agui_binding',protocol_binding_id('admin','shared-protocol'))['business_run_id']=='older'
    assert store.get('agui_binding',foreign_key)['business_run_id']=='unrelated'


def chat_projection(tmp_path, runtime, monkeypatch):
    """Protocol fixtures finish synthetic work on projection; no model or worker runs."""
    app,store,users,(_,alice,_)=http_clients(tmp_path)
    for kind in ('model','skill','mcp'):
        for record in runtime.store.list(kind):
            store.save(kind,record['id'],record)
    settings=runtime.store.get('settings','main')
    settings['artifact_dir']=str(tmp_path/'artifacts')
    for target in ('original-target','new-target'):
        mapping=copy.deepcopy(settings['identity_mapping']['records'][0])
        mapping['canonical']['id']=target
        mapping['native_id']=target
        settings['identity_mapping']['records'].append(mapping)
    store.save('settings','main',settings)
    app.state.runtime.artifact_dir=tmp_path/'artifacts'
    configure_agui(app)
    original_detail=app.state.runtime.detail
    def detail(id, *,owner_id=None):
        current=store.get('run',id)
        if current['status'] in {'running','queued'}:
            app.state.runtime._finish(id,'partial',{'analysis':'synthetic protocol report','source':'platform_template','model_generated':False})
        return original_detail(id,owner_id=owner_id)
    monkeypatch.setattr(app.state.runtime,'detail',detail)
    return app,store,users[0]['id'],alice


def test_explicit_terminal_conversation_accepts_new_message_and_replays_once(tmp_path,runtime,monkeypatch):
    app,store,owner,client=chat_projection(tmp_path,runtime,monkeypatch)
    conversation=client.post('/api/conversations',json={'client_request_id':'conv','context':{**analysis_defaults(),'skill_id':'skill_fixture'}}).json()
    first=client.post('/api/conversations/'+conversation['id']+'/messages',json={'client_message_id':'first','content':'first question'}).json()['run']
    app.state.runtime._finish(first['id'],'partial',{'analysis':'original report','source':'platform_template'})
    body={'threadId':conversation['id'],'runId':'protocol-followup','messages':[{'id':'new-followup','role':'user','content':'check latency'}],
          'forwardedProps':{'business_run_id':first['id']}}
    first_response=client.post('/api/ag-ui',json=body)
    assert first_response.status_code==200
    newest=max(store.list('run'),key=lambda r:r['created_at'])
    assert newest['id']!=first['id'] and newest['parent_run_id']==first['id']
    assert newest['owner_id']==owner and newest['conversation_id']==conversation['id']
    assert client.post('/api/ag-ui',json=body).status_code==200
    assert client.post('/api/ag-ui',json={**body,'runId':'protocol-replay-new-id'}).status_code==200
    history=app.state.conversations.detail(conversation['id'],owner)
    assert len(history['runs'])==2 and len([m for m in history['messages'] if m.get('client_message_id')=='new-followup'])==1
    assert store.get('run',first['id'])['report']['analysis']=='original report'
    assert store.get('agui_binding',protocol_binding_id(owner,'protocol-followup'))['business_run_id']==newest['id']
    assert store.get('agui_binding',protocol_binding_id(owner,'protocol-followup'))['requested_business_run_id']==first['id']


@pytest.mark.parametrize('changed',[False,True])
def test_active_conversation_protocol_message_is_saved_and_respects_context(tmp_path,runtime,monkeypatch,changed):
    app,store,owner,client=chat_projection(tmp_path,runtime,monkeypatch)
    context={**analysis_defaults(),'skill_id':'skill_fixture','target':'original-target'}
    conversation=client.post('/api/conversations',json={'client_request_id':'conv','context':context}).json()
    first=client.post('/api/conversations/'+conversation['id']+'/messages',json={'client_message_id':'first','content':'first question'}).json()['run']
    forwarded={'business_run_id':first['id'],**({'target':'new-target'} if changed else {})}
    response=client.post('/api/ag-ui',json={'threadId':conversation['id'],'runId':'protocol-active','messages':[{'id':'active-note','role':'user','content':'additional material'}],'forwardedProps':forwarded})
    assert response.status_code==200
    history=app.state.conversations.detail(conversation['id'],owner)
    note=next(m for m in history['messages'] if m.get('client_message_id')=='active-note')
    selected=store.get('run',note['run_id'])
    assert any(m.get('client_message_id')==note['id'] and m['content']=='additional material' for m in selected['messages'])
    assert len(store.list('run'))==(2 if changed else 1)
    assert selected['target']==('new-target' if changed else 'original-target')
    assert (selected['id']!=first['id'])==changed


def test_explicit_terminal_legacy_message_retries_once_by_message_id(tmp_path,runtime,monkeypatch):
    app,store,owner,client=chat_projection(tmp_path,runtime,monkeypatch)
    first=client.post('/api/runs',json={**analysis_defaults(),'skill_id':'skill_fixture','message':'first legacy question','client_request_id':'legacy-first'}).json()
    app.state.runtime._finish(first['id'],'partial',{'analysis':'original legacy report','source':'platform_template'})
    body={'threadId':first['thread_id'],'runId':'legacy-followup','messages':[{'id':'legacy-followup-id','role':'user','content':'continue legacy analysis'}],
          'forwardedProps':{'business_run_id':first['id']}}
    assert client.post('/api/ag-ui',json=body).status_code==200
    assert client.post('/api/ag-ui',json=body).status_code==200
    assert client.post('/api/ag-ui',json={**body,'runId':'legacy-replay-new-protocol'}).status_code==200
    runs=store.list('run')
    assert len(runs)==2
    second=next(r for r in runs if r['id']!=first['id'])
    assert second['parent_run_id']==first['id'] and second['messages'][0]['client_message_id']=='legacy-followup-id'


def test_empty_messages_only_subscribe_and_missing_context_is_not_silent(tmp_path,runtime,monkeypatch):
    app,store,owner,client=chat_projection(tmp_path,runtime,monkeypatch)
    empty=client.post('/api/ag-ui',json={'threadId':'unstarted','runId':'empty','messages':[],
                                       'forwardedProps':{'skill_id':'skill_fixture'}})
    assert empty.status_code==404 and store.list('run')==[]
    conversation=client.post('/api/conversations',json={'client_request_id':'empty-context'}).json()
    response=client.post('/api/ag-ui',json={'threadId':conversation['id'],'runId':'missing-context',
        'messages':[{'id':'question-without-context','role':'user','content':'analyze this'}],'forwardedProps':{}})
    assert response.status_code==422 and store.list('run')==[]
    saved=app.state.conversations.detail(conversation['id'],owner)
    assert len(saved['messages'])==1 and saved['messages'][0]['status']=='not_executed'
    assert not store.get('agui_binding',protocol_binding_id(owner,'missing-context'))
