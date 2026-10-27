from test_chat_services import chat
from aiopsstudio.usage import usage_summary


def run(known=True, pricing=None):
    return {'id':'synthetic-run','attempt_id':'synthetic-attempt','status':'partial',
            'created_at':'2026-10-20T00:00:00Z','started_at':'2026-10-20T00:00:03Z','completed_at':'2026-10-20T00:00:13Z',
            'usage':{'input_tokens':250,'output_tokens':50,'token_usage_known':known,'model_http_request_count':2,
                     'provider_usage':[{'input_token_details':{'cache_read':100}}]},
            'config_snapshot':{'model':{'pricing':pricing or {}}}}


def test_unknown_usage_preserves_partial_counts_without_false_cost():
    value=usage_summary(run(False))
    assert value['input_tokens'] is None and value['output_tokens'] is None
    assert value['known_input_tokens']==250 and value['known_output_tokens']==50
    assert value['estimate'] is None and value['provider_retry_count'] is None
    assert value['queue_seconds']==3 and value['execution_seconds']==10


def test_cache_missing_price_is_unknown_and_complete_price_counts_once():
    pricing={'input_per_million':1,'output_per_million':2,'currency':'TEST','as_of':'2026-10-20','version':'synthetic/1'}
    assert usage_summary(run(pricing=pricing))['estimate'] is None
    value=usage_summary(run(pricing={**pricing,'cached_input_per_million':.2}))
    assert value['estimate']==.00027 and value['currency']=='TEST'


def test_unknown_prices_and_invalid_cache_receipt_never_become_zero_cost():
    assert usage_summary(run())['estimate'] is None
    value=run(pricing={'input_per_million':1,'output_per_million':2,'cached_input_per_million':.2,'currency':'TEST','as_of':'2026-10-20','version':'synthetic/1'})
    value['usage']['provider_usage'][0]['input_token_details']['cache_read']=251
    assert usage_summary(value)['estimate'] is None


def test_http_usage_uses_private_fixed_pricing_but_exposes_only_owner_projection(chat):
    from aiopsstudio.usage import configure_usage
    configure_usage(chat.app)  # This fixture attaches Runtime after create_app(enable_runtime=False).
    value=run(pricing={'input_per_million':1,'output_per_million':2,'cached_input_per_million':.2,
                       'currency':'TEST','as_of':'2026-10-20','version':'synthetic/1'})
    value.update(owner_id=chat.user['id'], report=None, evidence=[], messages=[], trust_version='trusted-runtime/1')
    value['config_snapshot']['model'].update(secret_id='SYNTHETIC_PRIVATE_REFERENCE',base_url='https://synthetic-model.invalid/v1')
    chat.store.save('run',value['id'],value)
    response=chat.operator.get('/api/runs/'+value['id']+'/usage')
    assert response.status_code==200 and response.json()['estimate']==.00027
    assert 'SYNTHETIC_PRIVATE_REFERENCE' not in response.text and 'synthetic-model.invalid' not in response.text
    assert chat.admin.get('/api/runs/'+value['id']+'/usage').status_code==404
    record=chat.store.get('run',value['id']);record['deleted_at']='2026-10-20T01:00:00Z';chat.store.save('run',value['id'],record)
    assert chat.operator.get('/api/runs/'+value['id']+'/usage').status_code==404


def test_invalid_or_absent_cache_receipts_preserve_unknown_without_crashing():
    pricing={'input_per_million':1,'output_per_million':2,'cached_input_per_million':.2,'currency':'TEST','as_of':'2026-10-20','version':'synthetic/1'}
    for cache in ['100',-100,True,None]:
        value=run(pricing=pricing);value['usage']['provider_usage'][0]['input_token_details']['cache_read']=cache
        summary=usage_summary(value)
        assert summary['cache_read_tokens'] is None and summary['estimate'] is None
    value=run(pricing=pricing);value['usage']['provider_usage']=[{'input_token_details':['invalid']} ]
    assert usage_summary(value)['cache_read_tokens'] is None
    value['usage']['provider_usage']=[{}]
    assert usage_summary(value)['cache_read_tokens'] is None
    value['usage']={}
    summary=usage_summary(value)
    assert summary['model_requests'] is None and summary['tool_calls'] is None and summary['source_requests'] is None
