#!/usr/bin/env python3
"""Exercise the installed official Node runtime without dispatching more work."""
import argparse
import json
import uuid
from live_acceptance import client, OUT, TERMINAL


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--id', required=True, help='Existing terminal business run')
    args = parser.parse_args()
    c = client()
    try:
        original = c.get('/api/runs/' + args.id).json()
        assert original['status'] in TERMINAL
        before = len(c.get('/api/runs').json()['items'])
        body = {'threadId': original['thread_id'], 'runId': str(uuid.uuid4()), 'state': {},
                'messages': [], 'tools': [], 'context': [], 'forwardedProps': {'business_run_id': args.id}}
        path = '/api/copilotkit/agent/easyops/run'
        rounds = []
        for _ in range(2):
            response = c.post(path, json=body)
            response.raise_for_status()
            types = [json.loads(line[5:])['type'] for line in response.text.splitlines() if line.startswith('data:')]
            assert types[0] == 'RUN_STARTED' and types[-1] == 'RUN_FINISHED'
            assert 'STATE_SNAPSHOT' in types and 'MESSAGES_SNAPSHOT' in types
            rounds.append(types)
        after = c.get('/api/runs/' + args.id).json()
        checks = {'business_run_id': args.id, 'event_types': rounds,
                  'no_new_run_from_attach': len(c.get('/api/runs').json()['items']) == before,
                  'no_model_or_tool_dispatch': after['usage'] == original['usage'],
                  'snapshot_unchanged': all(after.get(k) == original.get(k) for k in
                                            ['config_revision', 'skill_version', 'attempt_id', 'budget', 'analysis_start', 'analysis_end'])}
        assert all(checks[k] for k in ['no_new_run_from_attach', 'no_model_or_tool_dispatch', 'snapshot_unchanged'])
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / 'agui-node-attach.json').write_text(json.dumps(checks, ensure_ascii=False, indent=2))
        print(json.dumps(checks, ensure_ascii=False))
    finally:
        c.close()


if __name__ == '__main__':
    main()
