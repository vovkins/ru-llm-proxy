"""Keep named Anthropic tool selection exact through the Responses/Chat bridge."""
import copy
from unittest.mock import Mock

import litellm
import pytest

from litellm_guardrails import upstream_stream as upstream


def request(endpoint='/v1/messages', count=2, parallel=None):
    values = {
        'model':'openai/test','stream':True,'use_chat_completions_api':True,
        'tool_choice':{'type':'function','name':'Read'},
        'tools':[{'type':'function','name':'Read','description':'Read a file',
                  'parameters':{'type':'object','properties':{'path':{'type':'string'}},'required':['path']}}],
        'litellm_metadata':{'endpoint':'http://proxy'+endpoint},
    }
    if count == 2:
        values['tools'].insert(0,{'type':'function','name':'Write','parameters':{'type':'object'}})
    if parallel is not None:
        values['parallel_tool_calls'] = parallel
    return values


@pytest.mark.asyncio
@pytest.mark.parametrize('endpoint', ['/v1/messages','/anthropic/v1/messages/'])
@pytest.mark.parametrize('count', [1,2])
@pytest.mark.parametrize('parallel', [None,False,True])
async def test_named_choice_requires_only_the_exact_selected_tool(monkeypatch,endpoint,count,parallel):
    manager = Mock(side_effect=AssertionError('Outer bridge must not allocate HTTP client'))
    monkeypatch.setattr(upstream,'_manager',manager)
    kwargs = request(endpoint,count,parallel)
    before = copy.deepcopy(kwargs)
    original_list = kwargs['tools']
    original_choice = kwargs['tool_choice']
    result = await upstream.adapter.async_pre_call_deployment_hook(kwargs,'aresponses')
    assert result is kwargs
    assert kwargs['tool_choice'] == 'required'
    selected = next(tool for tool in before['tools'] if tool['name']=='Read')
    assert kwargs['tools'] == [selected]
    assert kwargs['tools'][0] is not original_list[-1]
    assert original_list == before['tools']
    assert original_choice == before['tool_choice']
    assert kwargs.get('parallel_tool_calls') == parallel
    assert {k:v for k,v in kwargs.items() if k not in {'tools','tool_choice'}} == {
        k:v for k,v in before.items() if k not in {'tools','tool_choice'}}
    manager.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('malformed', ['missing','empty-name','unknown','duplicates','not-list','wrong-kind'])
async def test_invalid_named_choice_rejects_without_mutating_or_logging_names(monkeypatch,malformed,caplog):
    kwargs = request()
    if malformed == 'missing': kwargs['tool_choice'].pop('name')
    elif malformed == 'empty-name': kwargs['tool_choice']['name'] = ''
    elif malformed == 'unknown': kwargs['tool_choice']['name'] = 'PRIVATE_TOOL_NAME'
    elif malformed == 'duplicates': kwargs['tools'].append(copy.deepcopy(kwargs['tools'][-1]))
    elif malformed == 'not-list': kwargs['tools'] = {'name':'Read'}
    elif malformed == 'wrong-kind': kwargs['tools'][-1]['type'] = 'custom'
    before = copy.deepcopy(kwargs)
    manager = Mock(side_effect=AssertionError('Provider transport allocated'))
    monkeypatch.setattr(upstream,'_manager',manager)
    with pytest.raises(litellm.BadRequestError) as exc:
        await upstream.adapter.async_pre_call_deployment_hook(kwargs,'aresponses')
    assert exc.value.status_code == 400
    assert exc.value.code == 'messages_named_tool_invalid'
    assert kwargs == before
    assert 'PRIVATE_TOOL_NAME' not in str(exc.value)+caplog.text
    manager.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('choice',['auto','none','required',None])
async def test_ordinary_choice_does_not_filter_tools(choice):
    kwargs = request()
    kwargs['tool_choice'] = choice
    before = copy.deepcopy(kwargs)
    await upstream.adapter.async_pre_call_deployment_hook(kwargs,'aresponses')
    assert kwargs == before


@pytest.mark.asyncio
async def test_nonstream_named_choice_is_unchanged():
    kwargs = request()
    kwargs['stream'] = False
    before = copy.deepcopy(kwargs)
    await upstream.adapter.async_pre_call_deployment_hook(kwargs,'aresponses')
    assert kwargs == before


@pytest.mark.asyncio
async def test_history_rejection_precedes_named_normalization():
    kwargs = request()
    kwargs['previous_response_id'] = 'private-id'
    before = copy.deepcopy(kwargs)
    with pytest.raises(litellm.BadRequestError) as exc:
        await upstream.adapter.async_pre_call_deployment_hook(kwargs,'aresponses')
    assert exc.value.code == 'responses_chat_bridge_history_unsupported'
    assert kwargs == before


@pytest.mark.asyncio
async def test_direct_responses_named_stream_stays_rejected():
    kwargs = request('/v1/responses')
    before = copy.deepcopy(kwargs)
    with pytest.raises(litellm.BadRequestError) as exc:
        await upstream.adapter.async_pre_call_deployment_hook(kwargs,'aresponses')
    assert exc.value.code == 'responses_chat_bridge_named_tool_unsupported'
    assert kwargs == before


@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['acompletion','aresponses'])
@pytest.mark.parametrize('stream',[False,True])
@pytest.mark.parametrize('disable',[False,True])
async def test_native_parallel_flag_is_forwarded_only_as_a_bool(monkeypatch,kind,stream,disable):
    kwargs = request()
    kwargs['stream'] = stream
    kwargs['tool_choice'] = 'required'
    original = {'tool_choice':{'type':'any','disable_parallel_tool_use':disable},'messages':'PRIVATE_BODY'}
    kwargs['proxy_server_request'] = {'body':original}
    if kind == 'acompletion':
        # Stop after parameter normalization to avoid allocating a stream client.
        kwargs['stream'] = False
    await upstream.adapter.async_pre_call_deployment_hook(kwargs,kind)
    assert kwargs['parallel_tool_calls'] is (not disable)
    assert kwargs['proxy_server_request']['body'] is original
    assert set(kwargs) - {'parallel_tool_calls'} == set(request()) | {'proxy_server_request'}


@pytest.mark.asyncio
@pytest.mark.parametrize('bad',[None,0,1,'true',{},[]])
async def test_non_bool_parallel_preference_is_not_coerced(bad):
    kwargs=request()
    kwargs['stream']=False
    kwargs['proxy_server_request']={'body':{'tool_choice':{'disable_parallel_tool_use':bad}}}
    await upstream.adapter.async_pre_call_deployment_hook(kwargs,'aresponses')
    assert 'parallel_tool_calls' not in kwargs


@pytest.mark.asyncio
async def test_original_request_parallel_flag_is_ignored_for_other_routes():
    kwargs=request('/v1/responses')
    kwargs['stream']=False
    kwargs['proxy_server_request']={'body':{'tool_choice':{'disable_parallel_tool_use':True}}}
    await upstream.adapter.async_pre_call_deployment_hook(kwargs,'aresponses')
    assert 'parallel_tool_calls' not in kwargs
