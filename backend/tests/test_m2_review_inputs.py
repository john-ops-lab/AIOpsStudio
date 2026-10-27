"""Malformed mode must reject without accepting messages or dispatching work."""
import copy
import pytest
from test_chat_services import chat, conversation


@pytest.mark.parametrize('mode', [[], {}, None, 17, True])
def test_chat_and_direct_run_reject_nonstring_mode_without_mutation(chat, mode):
    conv=conversation(chat.operator,context=chat.context)
    before=copy.deepcopy(chat.store.get('conversation',conv['id']))
    response=chat.operator.post('/api/conversations/'+conv['id']+'/messages',json={
        'client_message_id':'synthetic-invalid-mode','content':'synthetic input','mode':mode})
    assert response.status_code==422
    assert chat.store.get('conversation',conv['id'])==before
    response=chat.admin.post('/api/runs',json={**chat.context,
        'client_request_id':'synthetic-invalid-direct-mode','mode':mode})
    assert response.status_code==422
    assert chat.store.list('run')==[]
