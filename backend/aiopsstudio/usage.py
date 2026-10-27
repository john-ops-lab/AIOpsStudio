"""Owner-scoped attempt accounting; absent provider receipts remain unknown."""
from __future__ import annotations
import math
from datetime import datetime, timezone
from fastapi import Request
from .identity import actor_id


def _instant(value):
    try:
        result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return result if result.tzinfo else result.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _seconds(start, end):
    a, b = _instant(start), _instant(end)
    return round(max(0, (b - a).total_seconds()), 3) if a and b else None


def _rate(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return None
    return value


def usage_summary(run):
    usage = run.get('usage') or {}
    model = (run.get('config_snapshot') or {}).get('model') or {}
    pricing = model.get('pricing') or {}
    known = usage.get('token_usage_known') is True
    input_tokens, output_tokens = usage.get('input_tokens'), usage.get('output_tokens')
    receipts = usage.get('provider_usage')
    cached = 0 if isinstance(receipts, list) and receipts else None
    for item in receipts if isinstance(receipts, list) else []:
        details = item.get('input_token_details') if isinstance(item, dict) else None
        value = details.get('cache_read') if isinstance(details, dict) else None
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            cached = None
            break
        cached += value
    estimate, reason = None, '未提供完整用量或版本化单价'
    incoming, outgoing = _rate(pricing.get('input_per_million')), _rate(pricing.get('output_per_million'))
    cache_price = _rate(pricing.get('cached_input_per_million'))
    if known and all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in (input_tokens, output_tokens)):
        if incoming is not None and outgoing is not None and pricing.get('currency') and pricing.get('as_of') and pricing.get('version'):
            if cached is None:
                reason = '供应商未提供完整有效的缓存用量回执'
            elif cached and cache_price is None:
                reason = '供应商返回缓存用量，但未提供对应缓存单价'
            elif cached > input_tokens:
                reason = '缓存与总输入用量无法对账'
            else:
                estimate = round(((input_tokens - cached) * incoming + output_tokens * outgoing + cached * (cache_price or 0)) / 1_000_000, 8)
                reason = '根据供应商已返回用量和本轮固定单价估算；不包含未披露费用'
    now = datetime.now(timezone.utc).isoformat()
    terminal = run.get('status') in {'succeeded', 'partial', 'failed', 'cancelled', 'needs_input'}
    return {
        'run_id': run['id'], 'attempt_id': run.get('attempt_id'), 'mode': run.get('mode', 'analyze'),
        'queue_seconds': _seconds(run.get('created_at'), run.get('started_at') or (run.get('completed_at') if terminal else now)),
        'execution_seconds': _seconds(run.get('started_at'), run.get('completed_at') or now),
        'model_requests': usage.get('model_http_request_count'), 'logical_model_calls': usage.get('model_call_count'),
        'tool_calls': usage.get('tool_call_count'), 'source_requests': usage.get('source_request_count'),
        'mcp_requests': usage.get('mcp_request_count'), 'mcp_upstream_requests': usage.get('mcp_upstream_http_request_count'),
        'input_tokens': input_tokens if known else None, 'output_tokens': output_tokens if known else None,
        'known_input_tokens': input_tokens, 'known_output_tokens': output_tokens, 'token_usage_known': known,
        'cache_read_tokens': cached,
        'provider_retry_count': None, 'provider_retry_notice': '供应商内部重试未提供完整回执，显示未知',
        'estimate': estimate, 'currency': pricing.get('currency') if estimate is not None else None,
        'price_as_of': pricing.get('as_of'), 'price_version': pricing.get('version'), 'estimate_notice': reason,
    }


def configure_usage(app):
    @app.get('/api/runs/{id}/usage')
    async def detail(id: str, request: Request):
        # require_owned() rechecks owner AND latest source/material authorization;
        # only the accounting projection leaves this private boundary.
        run = app.state.runtime.require_owned(id, actor_id(request), allow_legacy_archive=True)
        return usage_summary(run)
