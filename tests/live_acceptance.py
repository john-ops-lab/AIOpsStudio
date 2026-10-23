#!/usr/bin/env python3
"""Mac 上对本机应用的真实端到端验收；仅调用源系统只读工具。"""
from __future__ import annotations
import argparse
from datetime import datetime, timedelta, timezone
import json
import os
import sys
from pathlib import Path
import time
import uuid
import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from deploy.settings import load_config, environment
CONFIG = load_config()
STATE = CONFIG['state_dir']
BASE = environment().get('AIOPSSTUDIO_PUBLIC_ORIGIN', f"http://127.0.0.1:{CONFIG['ports']['web']}")
OUT=STATE/'evidence'
TERMINAL={'succeeded','partial','failed','needs_input','cancelled'}


def client():
    c=httpx.Client(base_url=BASE,headers={'Origin':BASE},timeout=60)
    c.post('/api/login',json={'password':(STATE/'private/admin-password').read_text().strip()}).raise_for_status()
    return c


def wait(c, id, timeout=420):
    end=time.monotonic()+timeout
    last=None
    while time.monotonic()<end:
        r=c.get('/api/runs/'+id);r.raise_for_status();run=r.json()
        if run['status']!=last:
            print(json.dumps({'id':id,'status':run['status']},ensure_ascii=False),flush=True);last=run['status']
        if run['status'] in TERMINAL:
            OUT.mkdir(exist_ok=True)
            (OUT/(id+'.json')).write_text(json.dumps(run,ensure_ascii=False,indent=2))
            print(json.dumps({'id':id,'status':run['status'],'evidence_count':len(run.get('evidence',[])),
                'usage':run.get('usage'),'error':run.get('error'),'limitations':(run.get('report') or {}).get('limitations')},ensure_ascii=False),flush=True)
            return run
        time.sleep(1)
    raise RuntimeError('验收等待超时，保留运行记录，不自动扩大任务预算')


def submit(c,path,body,interaction_probe=False):
    r=c.post(path,json={**body,'client_request_id':str(uuid.uuid4())});r.raise_for_status()
    run=r.json()
    if interaction_probe:
        count=len(c.get('/api/runs').json()['items'])
        message={'client_message_id':str(uuid.uuid4()),'content':'保持当前目标和固定窗口，报告请单列未知项。', 'context':{'run_id':run['id']}}
        first=c.post('/api/runs/'+run['id']+'/messages',json=message);first.raise_for_status()
        second=c.post('/api/runs/'+run['id']+'/messages',json=message);second.raise_for_status()
        assert first.json()['message']['id']==second.json()['message']['id']
        kinds=[]
        with c.stream('POST','/api/ag-ui/connect',json={'threadId':run['thread_id'],'runId':str(uuid.uuid4()),'messages':[]}) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if line.startswith('data:'):
                    kinds.append(json.loads(line[5:])['type'])
                    if 'STATE_SNAPSHOT' in kinds:break
        current=c.get('/api/runs/'+run['id']).json()
        receipt={'run_id':run['id'],'message_id':first.json()['message']['id'],'duplicate_message_same_receipt':True,
                 'attach_types':kinds,'disconnect_did_not_cancel':not current.get('cancel_requested'),
                 'attach_did_not_submit_run':len(c.get('/api/runs').json()['items'])==count}
        assert receipt['disconnect_did_not_cancel'] and receipt['attach_did_not_submit_run']
        OUT.mkdir(parents=True,exist_ok=True)
        (OUT/(run['id']+'-interaction.json')).write_text(json.dumps(receipt,ensure_ascii=False,indent=2))
        print(json.dumps(receipt,ensure_ascii=False),flush=True)
    return wait(c,run['id'])


def main():
    p=argparse.ArgumentParser()
    p.add_argument('action', choices=['preflight','model','cmdb','host','metrics','investigation','wait'])
    p.add_argument('--id', help='显式运行或告警 ID')
    p.add_argument('--server-id', help='用户已配置的 MCP server_id')
    p.add_argument('--model-id', help='用户已配置的模型配置 ID')
    p.add_argument('--resource-model', help='CMDB 中已核对的模型名')
    p.add_argument('--datasource-uid', help='用户已配置的 Prometheus 数据源 UID')
    p.add_argument('--expression', help='明确的只读 PromQL 表达式')
    p.add_argument('--environment', default='demo')
    p.add_argument('--recent',action='store_true',help='使用告警最近触发时间，仍提交明确绝对窗口')
    p.add_argument('--interaction-probe',action='store_true',help='运行中验证消息幂等、AG-UI重连与断订阅不取消')
    a=p.parse_args()
    required = {'model':['model_id'], 'cmdb':['server_id'], 'host':['server_id','resource_model'], 'metrics':['server_id','datasource_uid','expression'], 'investigation':['id'], 'wait':['id']}
    if any(not getattr(a,k) for k in required.get(a.action, [])):
        p.error('该动作必须显式提供对应配置 ID、源查询参数或对象 ID，不能自动选择现场目标')
    c=client();servers=c.get('/api/mcp-servers').json()['items'];models=c.get('/api/model-configs').json()['items']
    if a.action=='preflight':
        from local_secret_scan import known_secret_values
        OUT.mkdir(parents=True, exist_ok=True)
        checks={}
        checks['unauthenticated_rejected']=httpx.get(BASE+'/api/skills').status_code==401
        checks['cross_origin_mutation_rejected']=c.post('/api/skills',json={},headers={'Origin':'https://example.invalid'}).status_code==403
        skills=c.get('/api/skills').json()['items'];checks['published_skills']=len(skills)>=2 and all(x['current_version'] for x in skills)
        checks['published_mcp']=bool(servers) and all(x['current_version'] for x in servers)
        checks['stale_revision_rejected']=c.put('/api/skills/'+skills[0]['id'],json={'expected_revision':-1,'name':skills[0]['name']}).status_code==409
        protected=known_secret_values()
        all_public=''.join(c.get(path).text for path in ['/api/skills','/api/mcp-servers','/api/model-configs','/api/settings','/api/config-audit','/api/overview'])
        checks['stored_secrets_not_returned']=bool(protected) and not any(value in all_public for value in protected)
        checks['postgresql']=c.get('/healthz').json()['database']=='postgresql'
        (OUT/'preflight.json').write_text(json.dumps(checks,ensure_ascii=False,indent=2));print(json.dumps(checks,ensure_ascii=False));assert all(checks.values());return
    if a.action=='wait':run=wait(c,a.id)
    elif a.action=='model':run=submit(c,'/api/model-configs/'+a.model_id+'/test',{})
    elif a.action in {'cmdb','host'}:
        server=next(s for s in servers if s['id']==a.server_id)
        args={} if a.action=='cmdb' else {'model':a.resource_model,'fields':['resource_uid','display_name','hostname','environment','management_ips'],'result_mode':'complete'}
        run=submit(c,'/api/tool-tests',{'server_id':server['id'],'tool_name':'cmdb_list_models' if a.action=='cmdb' else 'cmdb_search_cis','arguments':args})
    elif a.action=='metrics':
        server=next(s for s in servers if s['id']==a.server_id);now=datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        run=submit(c,'/api/tool-tests',{'server_id':server['id'],'tool_name':'query_prometheus','arguments':{'datasourceUid':a.datasource_uid,'expr':a.expression,'queryType':'instant','endTime':now}})
    else:
        skill=next(s for s in c.get('/api/skills').json()['items'] if s['name']=='alert-impact-analysis')
        alerts=c.get('/api/alerts').json()['items'];alert=next(x for x in alerts if str(x['id'])==str(a.id))
        fired=datetime.fromisoformat((alert.get('last_fired_at') if a.recent else alert['fired_at']).replace('Z','+00:00')).replace(microsecond=0)
        run=submit(c,'/api/runs',{'skill_id':skill['id'],'incident_id':str(alert['id']),
            'target':{'type':alert.get('target_type'),'source_id':alert.get('target_id')},'environment':a.environment,
            'analysis_start':(fired-timedelta(minutes=10)).isoformat(),'analysis_end':min(datetime.now(timezone.utc).replace(microsecond=0),fired+timedelta(minutes=10)).isoformat(),
            'title':f"告警 #{alert['id']} 影响分析",'message':'请分析这条历史告警的潜在业务影响与已观测异常，按固定窗口取证。身份和历史遥测不确定时明确保留缺口，禁止把当前拓扑称为故障时拓扑。'},interaction_probe=a.interaction_probe)
    assert run['status'] in {'succeeded','partial','needs_input'},run.get('error')


if __name__=='__main__':main()
