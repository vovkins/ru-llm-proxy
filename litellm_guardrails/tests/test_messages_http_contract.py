"""Issues exposed by the real /v1/messages route, not only direct hooks."""
import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import litellm
import pytest
from litellm.types.llms.openai import ResponsesAPIResponse
from litellm.types.responses.main import GenericResponseOutputItem, OutputText
from litellm.llms.anthropic.experimental_pass_through.responses_adapters.transformation import LiteLLMAnthropicToResponsesAPIAdapter
from litellm_guardrails.anthropic_restoration import normalize_messages_bridge_output
from litellm_guardrails.pii_guardrail import RuPIIGuardrail
from litellm_guardrails.request_cancellation import RequestCancellationMiddleware
from litellm_guardrails.tests.test_pii_guardrail import _MemoryRedis


def bridge_reply():
    return ResponsesAPIResponse(id='resp-local',created_at=1,model='mock',status='completed',
        output=[GenericResponseOutputItem(type='message',id='msg-local',status='completed',role='assistant',
            content=[OutputText(type='output_text',text='alias-word',annotations=[])])],
        usage={'input_tokens':1,'output_tokens':1,'total_tokens':2})


def test_bridge_normalization_keeps_exact_serialized_payload():
    response=bridge_reply()
    before=response.model_dump_json()
    adapter=LiteLLMAnthropicToResponsesAPIAdapter()
    assert adapter.translate_response(response)['content']==[]
    normalize_messages_bridge_output({'litellm_metadata':{'endpoint':'http://proxy/v1/messages'}},response,'aresponses')
    assert response.model_dump_json()==before
    assert adapter.translate_response(response)['content'][0]['text']=='alias-word'


@pytest.mark.parametrize('endpoint,kind', [('/v1/responses','aresponses'),('/v1/chat/completions','acompletion'),('/v1/messages','acompletion')])
def test_unrelated_routes_and_call_types_keep_native_objects(endpoint,kind):
    response=bridge_reply();original=response.output[0]
    normalize_messages_bridge_output({'litellm_metadata':{'endpoint':'http://proxy'+endpoint}},response,kind)
    assert response.output[0] is original


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['exception','cancellation'])
async def test_http_scope_cleans_mapping_when_error_callback_loses_request_metadata(failure):
    g=RuPIIGuardrail(dictionary_substitutions_enabled=False);g._redis=_MemoryRedis()
    entered=asyncio.Event()
    async def app(scope,receive,send):
        await g._save_mapping('owned-id',{'alias':'private-original'})
        entered.set()
        if failure=='exception':raise RuntimeError('route lost mutated request data')
        await asyncio.Event().wait()
    incoming=asyncio.Queue();incoming.put_nowait({'type':'http.request','body':b'', 'more_body':False})
    task=asyncio.create_task(RequestCancellationMiddleware(app)({'type':'http','method':'POST'},incoming.get,AsyncMock()))
    await entered.wait()
    if failure=='cancellation':task.cancel()
    with pytest.raises(RuntimeError if failure=='exception' else asyncio.CancelledError):await task
    assert not g._redis.values


@pytest.mark.asyncio
async def test_completed_post_hook_unregisters_fallback_cleanup():
    g=RuPIIGuardrail(dictionary_substitutions_enabled=False);g._redis=_MemoryRedis()
    delete=g._redis.delete;g._redis.delete=AsyncMock(side_effect=delete)
    async def app(scope,receive,send):
        await g._save_mapping('owned-id',{'alias':'original'})
        await g._delete_mapping('owned-id')
    incoming=asyncio.Queue();incoming.put_nowait({'type':'http.request','body':b'', 'more_body':False})
    await RequestCancellationMiddleware(app)({'type':'http','method':'POST'},incoming.get,AsyncMock())
    g._redis.delete.assert_awaited_once_with('pii_mapping:owned-id')


@pytest.mark.asyncio
async def test_scope_cleanup_isolated_between_overlapping_requests():
    g=RuPIIGuardrail(dictionary_substitutions_enabled=False);g._redis=_MemoryRedis()
    entered=[asyncio.Event(),asyncio.Event()];release=[asyncio.Event(),asyncio.Event()]
    async def call(index):
        async def app(scope,receive,send):
            await g._save_mapping('owned-'+str(index),{'alias':str(index)})
            entered[index].set();await release[index].wait()
        incoming=asyncio.Queue();incoming.put_nowait({'type':'http.request','body':b'', 'more_body':False})
        await RequestCancellationMiddleware(app)({'type':'http','method':'POST'},incoming.get,AsyncMock())
    tasks=[asyncio.create_task(call(i)) for i in range(2)]
    await asyncio.gather(*(e.wait() for e in entered))
    release[0].set();await tasks[0]
    assert list(g._redis.values)==['pii_mapping:owned-1']
    release[1].set();await tasks[1]
    assert not g._redis.values


@pytest.mark.parametrize('arguments', ['{"path":"unfinished', '[]', 'null'])
def test_messages_provider_invalid_tool_arguments_fail_before_adapter_discards_them(arguments):
    response=litellm.ModelResponse(choices=[litellm.Choices(index=0,
        message=litellm.Message(role='assistant',tool_calls=[{
            'id':'local','type':'function','function':{'name':'Write','arguments':arguments}
        }]),finish_reason='tool_calls')])
    with pytest.raises(litellm.APIError) as caught:
        normalize_messages_bridge_output({'litellm_metadata':{'endpoint':'http://proxy/v1/messages'}},response,'acompletion')
    assert caught.value.status_code==502
    assert arguments not in str(caught.value)


def test_generic_bridge_function_call_keeps_json_and_fields():
    from litellm.types.responses.main import OutputFunctionToolCall
    response=bridge_reply()
    response.output.append(OutputFunctionToolCall(type='function_call',id='fc',call_id='call',name='Write',arguments='{"path":"a.ts"}',status='completed'))
    before=response.model_dump_json()
    normalize_messages_bridge_output({'litellm_metadata':{'endpoint':'http://proxy/v1/messages'}},response,'aresponses')
    assert response.model_dump_json()==before
    tool=LiteLLMAnthropicToResponsesAPIAdapter().translate_response(response)['content'][1]
    assert tool['type']=='tool_use' and tool['input']=={'path':'a.ts'}


def test_parallel_messages_chat_tools_are_separated_without_changing_arguments_or_usage():
    from litellm_guardrails.anthropic_restoration import SequentialMessagesToolDeltas
    order=SequentialMessagesToolDeltas()
    arguments=['{"content":"alpha"}','{"content":"beta"}']
    events=[]
    for offset in range(0,max(map(len,arguments)),2):
        tools=[{'index':i,'id':'call'+str(i) if offset==0 else None,'type':'function',
            'function':{'name':'Write' if offset==0 else None,'arguments':arg[offset:offset+2]}}
            for i,arg in enumerate(arguments) if offset<len(arg)]
        events.append({'id':'chunk','object':'chat.completion.chunk','choices':[{'index':0,'delta':{'tool_calls':tools},'finish_reason':None}]})
    events.append({'id':'chunk','object':'chat.completion.chunk','choices':[{'index':0,'delta':{},'finish_reason':'tool_calls'}],
        'usage':{'prompt_tokens':7,'completion_tokens':9,'total_tokens':16}})
    output=[]
    for event in events:output.extend(order.process(copy.deepcopy(event)))
    collected={0:'',1:''}
    for event in output:
        calls=event['choices'][0]['delta'].get('tool_calls',[])
        assert len(calls)<=1
        for call in calls:collected[call['index']]+=call['function'].get('arguments') or ''
    assert collected==dict(enumerate(arguments))
    assert sum(event.get('usage') is not None for event in output)==1
    assert output[-1]['choices'][0]['finish_reason']=='tool_calls'
    assert output[-1]['usage']==events[-1]['usage']


@pytest.mark.parametrize('finish', ['length','content_filter','stop'])
def test_incomplete_parallel_tool_set_is_not_fabricated_into_success(finish):
    from litellm_guardrails.anthropic_restoration import SequentialMessagesToolDeltas
    from litellm_guardrails.sse_integrity import IncompleteUpstreamStream
    order=SequentialMessagesToolDeltas()
    order.process({'choices':[{'index':0,'delta':{'tool_calls':[
        {'index':0,'id':'a','function':{'name':'Write','arguments':'{}'}},
        {'index':1,'id':'b','function':{'name':'Write','arguments':'{}'}}]},'finish_reason':None}]})
    with pytest.raises(IncompleteUpstreamStream):
        order.process({'choices':[{'index':0,'delta':{},'finish_reason':finish}]})


@pytest.mark.asyncio
async def test_parallel_tool_protocol_failure_is_visible_to_stream_owner():
    from litellm_guardrails.anthropic_restoration import messages_chat_response
    from litellm_guardrails.sse_integrity import IncompleteUpstreamStream
    import httpx
    frames=[{'choices':[{'index':0,'delta':{'tool_calls':[
        {'index':0,'id':'a','function':{'name':'Write','arguments':'{}'}},
        {'index':1,'id':'b','function':{'name':'Write','arguments':'{bad'}}]},'finish_reason':None}]},
        {'choices':[{'index':0,'delta':{},'finish_reason':'tool_calls'}]}]
    wire=''.join('data: '+json.dumps(v)+'\n\n' for v in frames).encode()
    class Source(httpx.AsyncByteStream):
        async def __aiter__(self):yield wire
        async def aclose(self):pass
    seen=[]
    raw=httpx.Response(200,request=httpx.Request('POST','http://mock/v1/chat/completions'),stream=Source())
    response=messages_chat_response(raw,seen.append)
    with pytest.raises(IncompleteUpstreamStream):
        async for _ in response.aiter_bytes():pass
    assert seen==['invalid_messages_buffered_tool']
    await response.aclose()
