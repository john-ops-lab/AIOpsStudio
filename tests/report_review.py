#!/usr/bin/env python3
"""Independent local-file oracle for report integrity and original telemetry.

This does not grade natural-language correctness: compare the emitted original
series with the report separately. No source request or model invocation occurs.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'backend'))
from deploy.local import env, STATE
from easyops.store import Store


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--id', required=True)
    args = parser.parse_args()
    store = Store(env()['EASYOPS_DATABASE_URL'])
    try:
        run = store.get('run', args.id)
        report = run['report']
        ids = {e['id'] for e in run['evidence']}
        citations = set(re.findall(r'\bev_[a-zA-Z0-9.\u2026_-]+', report['analysis']))
        checks = {'terminal_with_model_report': run['status'] in {'partial', 'succeeded'} and report.get('model_generated') is True,
                  'report_has_chinese_content': len(re.findall(r'[\u4e00-\u9fff]', report['analysis'])) > 100,
                  'report_window_matches': report['window'] == {'start': run['analysis_start'], 'end': run['analysis_end']},
                  'all_citations_resolve': bool(citations) and citations.issubset(ids),
                  'report_evidence_set_matches': set(report['evidence_ids']) == ids,
                  'supplement_messages_consumed': all(m['status'] == 'processed' for m in run['messages'] if m['role'] == 'user'),
                  'evidence_hashes_match': True}
        telemetry = []
        for record in run['evidence']:
            body = Path(record['path']).read_bytes()
            checks['evidence_hashes_match'] &= hashlib.sha256(body).hexdigest() == record['sha256']
            if record['tool_name'] != 'query_prometheus':
                continue
            native = json.loads(body)
            value = native.get('structuredContent')
            if not value:
                texts = [x['text'] for x in native.get('content', []) if x.get('type') == 'text']
                value = json.loads(texts[0]) if texts else native
            series = []
            for item in value.get('data', []):
                points = item.get('values') or ([item['value']] if 'value' in item else [])
                numeric = [(point, float(point[1])) for point in points]
                series.append({'labels': item.get('metric', {}), 'returned_points': len(points),
                               'first': points[0] if points else None, 'last': points[-1] if points else None,
                               'min': min(numeric, key=lambda x: x[1])[0] if numeric else None,
                               'max': max(numeric, key=lambda x: x[1])[0] if numeric else None})
            telemetry.append({'evidence_id': record['id'], 'arguments': record['arguments'], 'original_series': series})
        result = {'run_id': run['id'], 'checks': checks, 'original_telemetry': telemetry,
                  'language_answer_acceptance': 'requires_separate_comparison_with_original_telemetry'}
        (STATE / 'evidence' / (run['id'] + '-report-integrity.json')).write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(json.dumps(result, ensure_ascii=False))
        assert all(checks.values()), checks
    finally:
        store.close()


if __name__ == '__main__':
    main()
