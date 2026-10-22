#!/usr/bin/env python3
"""Mac HTTP/PostgreSQL acceptance; --live explicitly enables two real model rounds.

All accounts/files are owned by this acceptance run. Credentials never enter the
receipt or stdout; test users are disabled in finally. Existing admin history is
read only. Synthetic attachment text is labelled user material, never evidence.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
from pathlib import Path
import secrets
import time
import uuid

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from deploy.settings import load_config
CONFIG = load_config()
STATE = CONFIG['state_dir']
BASE = os.environ.get('EASYOPS_PUBLIC_ORIGIN', f"http://127.0.0.1:{CONFIG['ports']['web']}")
TERMINAL = {'succeeded', 'partial', 'failed', 'needs_input', 'cancelled'}


def connect(username, password):
    c = httpx.Client(base_url=BASE, headers={'Origin': BASE}, timeout=45)
    response = c.post('/api/login', json={'username': username, 'password': password})
    response.raise_for_status()
    return c


def get(c, path):
    r = c.get('/api' + path)
    r.raise_for_status()
    return r.json()


def post(c, path, body):
    r = c.post('/api' + path, json=body)
    r.raise_for_status()
    return r.json()


def wait(c, run_id, timeout=360):
    deadline = time.monotonic() + timeout
    previous = None
    while time.monotonic() < deadline:
        run = get(c, '/runs/' + run_id)
        if previous != run['status']:
            print(json.dumps({'run_id': run_id, 'status': run['status']}), flush=True)
            previous = run['status']
        if run['status'] in TERMINAL:
            return run
        time.sleep(1)
    raise RuntimeError('analysis wait timed out; no automatic retry or budget expansion')


def signature(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--live', action='store_true', help='Run two real read-only CMDB analysis rounds')
    parser.add_argument('--admin-owner', action='store_true', help='Keep the exercised Chat in the initial administrator history for browser inspection')
    parser.add_argument('--ci-id', type=int, help='Use a bounded attribute-only query for an already verified CMDB CI')
    parser.add_argument('--target', help='真实只读分析的目标；必须由使用者明确提供')
    parser.add_argument('--environment', default='demo')
    args = parser.parse_args()
    if args.live and args.ci_id is None and not args.target:
        parser.error('--live 必须提供 --ci-id 或 --target')
    stamp = uuid.uuid4().hex[:10]
    receipt = {'acceptance_id': stamp, 'live_model': args.live, 'environment': 'Mac HTTP + PostgreSQL', 'checks': {}, 'run_ids': []}
    out = STATE / 'evidence' / ('chat-acceptance-' + stamp + '.json')
    out.parent.mkdir(parents=True, exist_ok=True)
    bootstrap_password = (STATE / 'private/admin-password').read_text().strip()
    admin = connect('admin', bootstrap_password)
    accounts, clients = [], []

    def check(name, condition):
        receipt['checks'][name] = bool(condition)
        if not condition:
            raise AssertionError(name)

    try:
        health = admin.get('/healthz')
        health.raise_for_status()
        check('postgresql_runtime', health.json().get('database') == 'postgresql')
        for suffix in (['b'] if args.admin_owner else ['a', 'b']):
            username, password = 'qa-' + suffix + '-' + stamp, secrets.token_urlsafe(24)
            user = post(admin, '/users', {'username': username, 'name': '验收用户 ' + suffix.upper(), 'password': password, 'role': 'operator'})
            accounts.append((user['id'], username, password))
            clients.append(connect(username, password))
        if args.admin_owner:
            b = clients[0]
            a = connect('admin', bootstrap_password)
            clients.append(a)
            login_a = ('admin', bootstrap_password)
        else:
            a, b = clients
            login_a = (accounts[0][1], accounts[0][2])
        owner = get(a, '/session')['user']['id']
        common_key = 'conversation-' + stamp
        ca = post(a, '/conversations', {'title': 'CMDB 连续分析验收', 'client_request_id': common_key})
        cb = post(b, '/conversations', {'title': '独立会话存储验收', 'client_request_id': common_key})
        receipt['conversation_ids'] = [ca['id'], cb['id']]
        check('conversation_user_scoped_idempotency', ca['id'] != cb['id'] and post(a, '/conversations', {'client_request_id': common_key})['id'] == ca['id'])
        check('conversation_list_isolation', ca['id'] not in {x['id'] for x in get(b, '/conversations')['items']} and cb['id'] not in {x['id'] for x in get(a, '/conversations')['items']})
        check('conversation_cross_user_404', b.get('/api/conversations/' + ca['id']).status_code == 404)
        check('admin_private_history_404', admin.get('/api/conversations/' + cb['id']).status_code == 404)
        check('operator_configuration_rejected', b.post('/api/model-configs', json={}).status_code == 403)
        check('operator_test_purpose_rejected', b.post('/api/runs', json={'purpose': 'model_test', 'client_request_id': stamp}).status_code == 403)

        text = '附件功能验收样本。待核对服务：演示服务。此文件属于用户材料，不是源系统证据。'.encode()
        attachment = post(a, '/conversations/' + ca['id'] + '/attachments', {'name': '用户材料-验收.txt', 'media_type': 'text/plain', 'content_base64': base64.b64encode(text).decode()})
        attachment_id = attachment['id']
        own_download = a.get('/api/attachments/' + attachment_id + '/download')
        check('attachment_exact_bytes', own_download.status_code == 200 and own_download.content == text)
        check('attachment_download_disposition', 'attachment' in own_download.headers.get('content-disposition', '') and own_download.headers.get('x-content-type-options') == 'nosniff')
        check('attachment_cross_user_404', b.get('/api/attachments/' + attachment_id).status_code == 404 and b.get('/api/attachments/' + attachment_id + '/download').status_code == 404)
        check('attachment_metadata_no_path', 'path' not in attachment and attachment.get('sha256') == hashlib.sha256(text).hexdigest())
        invalid = a.post('/api/conversations/' + ca['id'] + '/attachments', json={'name': '../invalid.txt', 'media_type': 'text/plain', 'content_base64': '*invalid*'})
        check('invalid_upload_rejected', invalid.status_code in {400, 413, 422})
        check('failed_upload_no_metadata', len(get(a, '/conversations/' + ca['id'])['attachments']) == 1)
        unattached = post(b, '/conversations/' + cb['id'] + '/messages', {'content': '请提示开始分析需要哪些上下文。', 'client_message_id': 'shared-message-' + stamp})
        check('missing_context_saved_not_executed', not unattached.get('run') and bool(unattached.get('message')))

        if args.live:
            skill = next(s for s in get(a, '/skills')['items'] if s.get('name') == 'cmdb-query' and s.get('current_version') and s.get('enabled', True))
            question = (f'读取 CMDB 中 CI {args.ci_id} 的配置。只需原始配置中的名称、类型和更新时间；不查关系、不扩展根因。附件是功能验收用户材料，不是证据。' if args.ci_id else f'查询 CMDB 中 {args.target} 的依赖范围，简要报告找到的关系与未知项。附件只作用户提供的查询提示，不能当作事实证据。')
            body = {'content': question,
                    'client_message_id': 'shared-message-' + stamp, 'attachment_ids': [attachment_id],
                    'run_input': {'skill_id': skill['id'], 'target': f'CI {args.ci_id}' if args.ci_id else args.target, 'environment': args.environment}}
            first = post(a, '/conversations/' + ca['id'] + '/messages', body)
            run_id = first['run']['id']
            receipt['run_ids'].append(run_id)
            repeated = post(a, '/conversations/' + ca['id'] + '/messages', body)
            check('message_idempotency', repeated['message']['id'] == first['message']['id'] and repeated['run']['id'] == run_id)
            check('run_owner_from_session', first['run'].get('owner_id') == owner)
            for path, method in [('/runs/' + run_id, 'GET'), ('/runs/' + run_id + '/events', 'GET'), ('/runs/' + run_id + '/cancel', 'POST'), ('/runs/' + run_id + '/retry', 'POST')]:
                r = b.request(method, '/api' + path, **({'json': {}} if method == 'POST' else {}))
                check('cross_user_' + path.rsplit('/', 1)[-1], r.status_code == 404)
            protocol = {'threadId': ca['id'], 'runId': 'protocol-' + stamp, 'messages': [], 'forwardedProps': {'business_run_id': run_id}}
            check('agui_cross_user_404', b.post('/api/ag-ui/connect', json=protocol).status_code == 404)
            before = {r['id'] for r in get(a, '/runs')['items']}
            with a.stream('POST', '/api/ag-ui/connect', json=protocol) as response:
                response.raise_for_status()
                observed = []
                for line in response.iter_lines():
                    if line.startswith('data:'):
                        observed.append(json.loads(line[5:])['type'])
                        if 'STATE_SNAPSHOT' in observed:
                            break
            check('agui_reconnect_snapshot', 'STATE_SNAPSHOT' in observed)
            check('reconnect_no_new_run', before == {r['id'] for r in get(a, '/runs')['items']})
            r1 = wait(a, run_id)
            check('first_real_model_report', r1['status'] in {'succeeded', 'partial'} and (r1.get('report') or {}).get('source') == 'model' and (r1.get('report') or {}).get('model_generated') is True and bool(r1['report'].get('analysis')))
            frozen = signature(r1.get('report'))
            if r1.get('evidence'):
                check('evidence_cross_user_404', b.get('/api/evidence/' + r1['evidence'][0]['id']).status_code == 404)
            followup = (f'沿用前轮背景，重新读取同一 CI {args.ci_id} 配置，简要说明名称、类型是否仍有依据；不查关系，未知保持未知。' if args.ci_id else f'沿用前轮背景，继续核对 {args.target} 的关系是否完整。请明确未证实内容。')
            second = post(a, '/conversations/' + ca['id'] + '/messages', {'content': followup, 'client_message_id': 'followup-' + stamp})
            r2 = second['run']
            receipt['run_ids'].append(r2['id'])
            check('followup_new_linked_round', r2['id'] != r1['id'] and r2.get('parent_run_id') == r1['id'] and r2.get('conversation_id') == ca['id'])
            r2 = wait(a, r2['id'])
            check('second_real_model_report', r2['status'] in {'succeeded', 'partial'} and (r2.get('report') or {}).get('source') == 'model' and (r2.get('report') or {}).get('model_generated') is True and bool(r2['report'].get('analysis')))
            check('original_report_unchanged', signature(get(a, '/runs/' + r1['id']).get('report')) == frozen)
            check('invocations_user_isolated', not get(b, '/invocations')['items'])
            detail = get(a, '/conversations/' + ca['id'])
            reports = [f for f in detail['attachments'] if f.get('kind') in {'report', 'generated_report'}]
            check('two_report_artifacts', len(reports) == 2 and {f.get('run_id') for f in reports} == set(receipt['run_ids']))
            for report in reports:
                downloaded = a.get('/api/attachments/' + report['id'] + '/download')
                check('report_download_' + report['run_id'], downloaded.status_code == 200 and bool(downloaded.content) and b.get('/api/attachments/' + report['id'] + '/download').status_code == 404)
            receipt['rounds'] = [{'id': r['id'], 'status': r['status'], 'report_source': (r.get('report') or {}).get('source'), 'model_generated': (r.get('report') or {}).get('model_generated'), 'evidence_count': len(r.get('evidence', [])), 'model_call_count': r['usage']['model_call_count']} for r in [r1, r2]]

        post(a, '/logout', {})
        anew = connect(*login_a)
        clients.append(anew)
        restored = get(anew, '/conversations/' + ca['id'])
        check('relogin_attachment_restored', any(f['id'] == attachment_id for f in restored['attachments']))
        if args.live:
            check('relogin_rounds_messages_restored', set(receipt['run_ids']).issubset({r['id'] for r in restored['runs']}) and len(restored['messages']) >= 4)
        check('logout_old_session_rejected', a.get('/api/conversations').status_code == 401)
        receipt['passed'] = True
    except Exception as error:
        receipt['passed'] = False
        receipt['error'] = {'type': type(error).__name__, 'detail': str(error)}
        raise
    finally:
        cleanup = []
        for user_id, _, _ in accounts:
            response = admin.post('/api/users/' + user_id + '/state', json={'enabled': False})
            cleanup.append({'id': user_id, 'disabled': response.status_code == 200})
        receipt['test_accounts'] = cleanup
        for c in clients:
            c.close()
        admin.close()
        out.write_text(json.dumps(receipt, ensure_ascii=False, indent=2))
        print(json.dumps({'receipt': str(out), 'passed': receipt.get('passed'), 'checks': len(receipt['checks']), 'run_ids': receipt['run_ids']}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
