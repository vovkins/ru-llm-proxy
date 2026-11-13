"""Native /v1/messages reply and SSE restoration, including tool-written code."""
import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import litellm
import pytest

from litellm_guardrails.pii_guardrail import RuPIIGuardrail
from litellm_guardrails.anthropic_restoration import (
    AnthropicStreamRestorer, SSEEventRestorer, restore_tool_input,
)
from litellm_guardrails.restoration_mapping import RestorationMapping, encode_mapping
from litellm_guardrails.sse_integrity import IncompleteUpstreamStream
from litellm_guardrails.tests.test_pii_guardrail import _MemoryRedis

AUTH = SimpleNamespace(token='local-test', api_key='local', user_id='local')
RULES = {'substitutions': [
    {'id': 'package', 'source': 'ExampleBranddirect', 'replacement': 'ladogadirect01',
     'match': {'case_sensitive': True, 'whole_phrase': False}},
    {'id': 'identifier', 'source': 'ExampleBrandDirect', 'replacement': 'LadogaDirect02',
     'match': {'case_sensitive': True, 'whole_phrase': False}},
]}
ORIGINAL = "import { useExampleBrandDirectContext } from '@terminal/ExampleBranddirect/provider/react';"


def guard(mapping=None):
    g = RuPIIGuardrail(dictionary_substitutions_enabled=True, dictionary_substitutions_json=json.dumps(RULES))
    g._redis = _MemoryRedis()
    g._analysis_cache_secret = None
    g._analyze_text = AsyncMock(return_value=[])
    if mapping is not None:
        g._redis.values['pii_mapping:local'] = encode_mapping(mapping)
    return g


def message(text, tool_input=None):
    blocks = [{'type': 'text', 'text': text}]
    if tool_input is not None:
        blocks.append({'type': 'tool_use', 'id': 'tool_local', 'name': 'Write', 'input': tool_input})
    return {'id': 'msg_local', 'type': 'message', 'role': 'assistant', 'model': 'mock',
            'content': blocks, 'stop_reason': 'tool_use' if tool_input is not None else 'end_turn',
            'usage': {'input_tokens': 7, 'output_tokens': 9}}


async def chunks(values):
    for value in values:
        yield value


def events(text, tool_input, *, split=1):
    result = [{'type': 'message_start', 'message': message('')}]
    result += [{'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}}]
    result += [{'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': text[i:i+split]}}
               for i in range(0, len(text), split)]
    result += [{'type': 'content_block_stop', 'index': 0},
               {'type': 'content_block_start', 'index': 1, 'content_block': {'type': 'tool_use', 'id': 'tool_local', 'name': 'Write', 'input': {}}}]
    wire = json.dumps(tool_input, ensure_ascii=True)
    result += [{'type': 'content_block_delta', 'index': 1, 'delta': {'type': 'input_json_delta', 'partial_json': wire[i:i+split]}}
               for i in range(0, len(wire), split)]
    result += [{'type': 'content_block_stop', 'index': 1},
               {'type': 'message_delta', 'delta': {'stop_reason': 'tool_use', 'stop_sequence': None}, 'usage': {'output_tokens': 9}},
               {'type': 'message_stop'}]
    return result


def sse(values, newline='\n'):
    return ''.join('event: '+value['type']+newline+'data: '+json.dumps(value, ensure_ascii=False)+newline+newline for value in values)


def decode_sse(wire):
    result = []
    for record in wire.replace('\r\n', '\n').replace('\r', '\n').split('\n\n'):
        lines = [line[5:].lstrip(' ') for line in record.splitlines() if line.startswith('data:')]
        if lines:
            result.append(json.loads('\n'.join(lines)))
    return result


def collect(values):
    text = ''; arguments = ''
    for value in values:
        if value['type'] == 'content_block_start' and value['content_block']['type'] == 'text':
            text += value['content_block']['text']
        if value['type'] == 'content_block_delta':
            delta = value['delta']
            text += delta.get('text', '')
            arguments += delta.get('partial_json', '')
    return text, json.loads(arguments)


@pytest.mark.asyncio
async def test_native_message_restores_imports_text_and_nested_tool_input_then_next_turn():
    g = guard()
    request = {'model': 'mock', 'messages': [{'role': 'user', 'content': ORIGINAL}]}
    request = await g.async_pre_call_hook(AUTH, None, request, 'anthropic_messages') or request
    wire = request['messages'][0]['content']
    assert 'ExampleBrand' not in wire and 'ladogadirect01' in wire
    reply = message(wire, {'path': 'a.ts', 'content': wire, 'nested': [wire, {'flag': True, 'n': 42, 'none': None}]})
    before_usage = copy.deepcopy(reply['usage'])
    await g.async_post_call_success_hook(request, AUTH, reply)
    assert reply['content'][0]['text'] == ORIGINAL
    assert reply['content'][1]['input']['content'] == ORIGINAL
    assert reply['content'][1]['input']['nested'] == [ORIGINAL, {'flag': True, 'n': 42, 'none': None}]
    assert reply['content'][1]['id'] == 'tool_local' and reply['content'][1]['name'] == 'Write'
    assert reply['usage'] == before_usage and not g._redis.values
    # History seen by the next request contains originals, never stale aliases.
    next_request = {'model': 'mock', 'messages': [{'role': 'assistant', 'content': reply['content']},
        {'role': 'user', 'content': 'Update '+ORIGINAL}]}
    await g.async_pre_call_hook(AUTH, None, next_request, 'anthropic_messages')
    assert 'ExampleBrand' not in json.dumps(next_request)


@pytest.mark.asyncio
@pytest.mark.parametrize('word', ['goinvest', 'ladoga'])
async def test_same_request_local_placeholder_is_reversible(word):
    g = guard({'<LOGIN_1>': word})
    reply = message('<LOGIN_1>', {'content': '<LOGIN_1>'})
    await g.async_post_call_success_hook({'metadata': {'pii_request_id': 'local'}}, AUTH, reply)
    assert reply['content'][0]['text'] == word
    assert reply['content'][1]['input']['content'] == word


@pytest.mark.asyncio
async def test_false_positive_partial_identifier_restores_and_signed_blocks_are_unchanged():
    g = guard({'<ORGANIZATION_1>': 'track'})
    reply = message('<ORGANIZATION_1>PWA', {'content': '<ORGANIZATION_1>PWA'})
    signed = {'type': 'thinking', 'thinking': '<ORGANIZATION_1>', 'signature': 'opaque-signature'}
    reply['content'].append(copy.deepcopy(signed))
    await g.async_post_call_success_hook({'metadata': {'pii_request_id': 'local'}}, AUTH, reply)
    assert reply['content'][0]['text'] == 'trackPWA'
    assert reply['content'][1]['input']['content'] == 'trackPWA'
    assert reply['content'][2] == signed


@pytest.mark.parametrize('value', ['Тверь "центр"\nC:\\folder\\file.ts\t😀', 'abc\\n literal', "x' y\" z"])
def test_tool_values_json_escaping_is_valid(value):
    original = {'path': 'a.ts', 'content': '<LOCATION_1>', 'nested': ['<LOCATION_1>', None, 1, False]}
    assert restore_tool_input(original, {'<LOCATION_1>': value}) == {'path': 'a.ts', 'content': value, 'nested': [value, None, 1, False]}
    assert original['content'] == '<LOCATION_1>'


def test_tool_typed_json_provenance():
    mapping = RestorationMapping()
    mapping.add_number('<RU_PASSPORT_1>', '123456')
    mapping.add_string('<PERSON_1>', r'Ivan\nPetrov\"')
    assert restore_tool_input({'number': '<RU_PASSPORT_1>', 'person': '<PERSON_1>'}, mapping) == {'number': 123456, 'person': 'Ivan\nPetrov"'}


@pytest.mark.asyncio
@pytest.mark.parametrize('transport', ['objects', 'bytes', 'string'])
@pytest.mark.parametrize('newline', ['\n', '\r\n', '\r'])
@pytest.mark.parametrize('boundary', [1, 7, 4096])
async def test_stream_text_and_tool_json_across_event_and_transport_boundaries(transport, newline, boundary):
    mapping = {'ladogadirect01': 'ExampleBranddirect', '<LOGIN_1>': 'goinvest', '<ORGANIZATION_1>': 'track'}
    text = 'ladogadirect01 <LOGIN_1> <ORGANIZATION_1>PWA'
    value = {'content': "import x from '@terminal/ladogadirect01/api';", 'name': '<LOGIN_1>', 'function': '<ORGANIZATION_1>PWA', 'city': 'Тверь 😀'}
    source_events = events(text, value, split=2)
    if transport == 'objects':
        parts = copy.deepcopy(source_events)
    else:
        wire = sse(source_events, newline)
        if transport == 'bytes': wire = wire.encode('utf-8')
        parts = [wire[i:i+boundary] for i in range(0, len(wire), boundary)]
    g = guard(mapping)
    request = {'stream': True, 'metadata': {'pii_request_id': 'local'}}
    output = [part async for part in g.async_post_call_streaming_iterator_hook(AUTH, chunks(parts), request)]
    if transport == 'objects': decoded = output
    elif transport == 'bytes': decoded = decode_sse(b''.join(output).decode('utf-8'))
    else: decoded = decode_sse(''.join(output))
    restored_text, restored_input = collect(decoded)
    assert restored_text == 'ExampleBranddirect goinvest trackPWA'
    assert restored_input == {'content': "import x from '@terminal/ExampleBranddirect/api';", 'name': 'goinvest', 'function': 'trackPWA', 'city': 'Тверь 😀'}
    assert decoded[-1] == {'type': 'message_stop'}
    assert sum(e['type'] == 'message_stop' for e in decoded) == 1
    assert next(e for e in decoded if e['type'] == 'message_delta')['usage'] == {'output_tokens': 9}
    assert not g._redis.values


@pytest.mark.asyncio
async def test_independent_interleaved_blocks_and_signed_deltas():
    values = [{'type': 'message_start', 'message': message('')},
        {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': 'ladoga'}},
        {'type': 'content_block_start', 'index': 1, 'content_block': {'type': 'thinking', 'thinking': '', 'signature': ''}},
        {'type': 'content_block_start', 'index': 2, 'content_block': {'type': 'text', 'text': ''}},
        {'type': 'content_block_delta', 'index': 2, 'delta': {'type': 'text_delta', 'text': 'ladogadirect01'}},
        {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': 'direct01'}},
        {'type': 'content_block_delta', 'index': 1, 'delta': {'type': 'signature_delta', 'signature': 'ladogadirect01'}},
        *[{'type': 'content_block_stop', 'index': i} for i in [0, 2, 1]], {'type': 'message_stop'}]
    g = guard({'ladogadirect01': 'Original'})
    output = [part async for part in g.async_post_call_streaming_iterator_hook(AUTH, chunks(values), {'metadata': {'pii_request_id': 'local'}, 'stream': True})]
    content = {0: '', 2: ''}
    for event in output:
        if event['type'] == 'content_block_start' and event['index'] in content: content[event['index']] += event['content_block']['text']
        if event['type'] == 'content_block_delta' and event['index'] in content: content[event['index']] += event['delta']['text']
    assert content == {0: 'Original', 2: 'Original'}
    assert next(e for e in output if e['type'] == 'content_block_delta' and e['index'] == 1)['delta']['signature'] == 'ladogadirect01'


@pytest.mark.asyncio
async def test_truncated_sse_and_cancellation_delete_mapping_without_terminal():
    g = guard({'ladogadirect01': 'Original'})
    output = []
    with pytest.raises(IncompleteUpstreamStream):
        async for event in g.async_post_call_streaming_iterator_hook(AUTH, chunks([b'event: message_start\ndata: {"type":"message_start"}']), {'metadata': {'pii_request_id': 'local'}, 'stream': True}):
            output.append(event)
    assert not output and not g._redis.values
    g = guard({'ladogadirect01': 'Original'})
    async def cancelled():
        yield {'type': 'message_start', 'message': message('')}
        raise asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        async for _ in g.async_post_call_streaming_iterator_hook(AUTH, cancelled(), {'metadata': {'pii_request_id': 'local'}, 'stream': True}): pass
    assert not g._redis.values


@pytest.mark.asyncio
async def test_existing_mixed_alias_request_is_rejected_with_structured_422():
    g = guard()
    with pytest.raises(Exception) as caught:
        await g.async_pre_call_hook(AUTH, None, {'messages': [{'role': 'user', 'content': 'ExampleBranddirect ladogadirect01'}]})
    assert getattr(caught.value, 'status_code', None) == 422
    assert getattr(caught.value, 'type', None) == 'dictionary_substitution_ambiguous'
    g._analyze_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_nested_native_tool_input_is_analyzed_as_json_then_restored_as_object():
    g = guard()
    calls = []
    async def analyze(text):
        calls.append(text)
        value = 'person-value-777'
        start = text.index(value)
        return [{'entity_type': 'PERSON', 'start': start, 'end': start+len(value), 'score': .99}]
    g._analyze_text = analyze
    original = {'content': ORIGINAL, 'nested': [{'person': 'person-value-777'}], 'n': 42}
    data = {'model': 'mock', 'messages': [{'role': 'assistant', 'content': [
        {'type': 'tool_use', 'id': 'call-local', 'name': 'Write', 'input': copy.deepcopy(original)}]}]}
    await g.async_pre_call_hook(AUTH, None, data, 'anthropic_messages')
    tool = data['messages'][0]['content'][0]
    assert isinstance(tool['input'], dict) and tool['input']['n'] == 42
    assert 'ExampleBrand' not in json.dumps(tool) and 'person-value-777' not in json.dumps(tool)
    assert len(calls) == 1 and isinstance(json.loads(calls[0]), dict)
    reply = message('', copy.deepcopy(tool['input']))
    await g.async_post_call_success_hook(data, AUTH, reply)
    assert reply['content'][1]['input'] == original


@pytest.mark.asyncio
async def test_failed_analysis_does_not_replace_native_input_with_a_string():
    g = guard()
    original = {'value': 'unmasked-original'}
    data = {'messages': [{'role': 'assistant', 'content': [
        {'type': 'tool_use', 'id': 'call-local', 'name': 'Write', 'input': copy.deepcopy(original)}]}]}
    g._analyze_text = AsyncMock(side_effect=RuntimeError('synthetic analyzer failure'))
    with pytest.raises(Exception): await g.async_pre_call_hook(AUTH, None, data)
    assert data['messages'][0]['content'][0]['input'] == original


@pytest.mark.asyncio
@pytest.mark.parametrize('stream', [False, True])
async def test_real_litellm_adapter_and_proxy_callback_chain(monkeypatch, stream):
    import sys
    from types import ModuleType
    from litellm.caching.caching import DualCache
    from litellm.proxy.utils import ProxyLogging
    from litellm.proxy._types import UserAPIKeyAuth
    from litellm.llms.anthropic.experimental_pass_through.adapters.transformation import AnthropicAdapter
    # The callback dispatcher imports the proxy's configured router. No DB,
    # provider or model router is needed for this real dispatcher/adapter test.
    proxy_server = ModuleType('litellm.proxy.proxy_server')
    proxy_server.llm_router = None
    monkeypatch.setitem(sys.modules, proxy_server.__name__, proxy_server)
    g = guard()
    g.default_on = True
    g.event_hook = 'post_call'
    monkeypatch.setattr(litellm, 'callbacks', [g])
    logging = ProxyLogging(user_api_key_cache=DualCache())
    auth = UserAPIKeyAuth(api_key='local-test', token='local-test')
    request = {'model': 'mock', 'stream': stream, 'messages': [{'role': 'user', 'content': ORIGINAL}]}
    await g.async_pre_call_hook(auth, None, request, 'anthropic_messages')
    wire = request['messages'][0]['content']
    arguments = json.dumps({'path': 'a.ts', 'content': wire})
    adapter = AnthropicAdapter()
    if not stream:
        chat = litellm.ModelResponse(model='gpt-6-sol', usage={'prompt_tokens': 7, 'completion_tokens': 9, 'total_tokens': 16}, choices=[litellm.Choices(index=0, finish_reason='tool_calls',
            message=litellm.Message(role='assistant', content=wire, tool_calls=[
                {'id': 'call_local', 'type': 'function', 'function': {'name': 'Write', 'arguments': arguments}}]))])
        native = adapter.translate_completion_output_params(chat)
        result = await logging.post_call_success_hook(data=request, response=native, user_api_key_dict=auth)
        assert next(block for block in result['content'] if block['type'] == 'text')['text'] == ORIGINAL
        assert next(block for block in result['content'] if block['type'] == 'tool_use')['input']['content'] == ORIGINAL
    else:
        completion = []
        for i in range(0, len(wire), 3):
            completion.append(litellm.ModelResponseStream(choices=[litellm.StreamingChoices(index=0, delta={'content': wire[i:i+3]})]))
        for i in range(0, len(arguments), 3):
            completion.append(litellm.ModelResponseStream(choices=[litellm.StreamingChoices(index=0, delta={'tool_calls': [
                {'index': 0, 'id': 'call_local' if i == 0 else None, 'type': 'function', 'function': {'name': 'Write' if i == 0 else None, 'arguments': arguments[i:i+3]}}]})]))
        completion.append(litellm.ModelResponseStream(choices=[litellm.StreamingChoices(index=0, delta={}, finish_reason='tool_calls')]))
        native_stream = adapter.translate_completion_output_params_streaming(chunks(completion), model='gpt-6-sol', is_async=True)
        restored = [item async for item in logging.async_post_call_streaming_iterator_hook(response=native_stream, user_api_key_dict=auth, request_data=request)]
        wire_out = ''.join(value.decode('utf-8') if isinstance(value, bytes) else value for value in restored)
        text, tool = collect(decode_sse(wire_out))
        assert text == ORIGINAL and tool['content'] == ORIGINAL
    assert not g._redis.values


@pytest.mark.asyncio
async def test_raw_sse_multibyte_utf8_and_cache_usage_are_preserved():
    source = events('Тверь 😀 ladogadirect01', {'content': 'ladogadirect01'}, split=50)
    source[0]['message']['usage'] = {'input_tokens': 7, 'output_tokens': 0,
        'cache_read_input_tokens': 19, 'cache_creation_input_tokens': 23}
    raw = sse(source).encode('utf-8')
    g = guard({'ladogadirect01': 'Оригинал'})
    output = [part async for part in g.async_post_call_streaming_iterator_hook(AUTH,
        chunks([raw[i:i+1] for i in range(len(raw))]), {'metadata': {'pii_request_id': 'local'}, 'stream': True})]
    decoded = decode_sse(b''.join(output).decode('utf-8'))
    assert collect(decoded) == ('Тверь 😀 Оригинал', {'content': 'Оригинал'})
    assert decoded[0]['message']['usage'] == source[0]['message']['usage']


@pytest.mark.asyncio
async def test_malformed_or_oversized_tool_json_cannot_emit_success(monkeypatch):
    from litellm_guardrails import anthropic_restoration as module
    for partial, limit in [('{"content":"unfinished', 1000), ('{"content":"too big"}', 5)]:
        monkeypatch.setattr(module, 'MAX_TOOL_ARGUMENT_BYTES', limit)
        g = guard({'ladogadirect01': 'Original'})
        values = [{'type': 'message_start', 'message': message('')},
            {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'tool_use', 'name': 'Write', 'id': 'local', 'input': {}}},
            {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'input_json_delta', 'partial_json': partial}},
            {'type': 'content_block_stop', 'index': 0}, {'type': 'message_stop'}]
        output = []
        with pytest.raises(IncompleteUpstreamStream):
            async for event in g.async_post_call_streaming_iterator_hook(AUTH, chunks(values), {'metadata': {'pii_request_id': 'local'}, 'stream': True}):
                output.append(event)
        assert not any(event['type'] == 'message_stop' for event in output)
        assert not g._redis.values


@pytest.mark.asyncio
async def test_ambiguous_error_does_not_expose_dictionary_terms():
    g = guard()
    with pytest.raises(Exception) as caught:
        await g.async_pre_call_hook(AUTH, None, {'messages': [{'role': 'user', 'content': 'ExampleBranddirect ladogadirect01'}]})
    exposed = json.dumps(caught.value.provider_specific_fields)
    assert 'ExampleBranddirect' not in exposed and 'ladogadirect01' not in exposed
    assert caught.value.provider_specific_fields['error']['code'] == 'dictionary_substitution_ambiguous'


@pytest.mark.asyncio
@pytest.mark.parametrize('malformed', [False,True])
async def test_native_stream_without_mapping_keeps_valid_data_and_rejects_bad_json(malformed):
    g=guard()
    values=events('ordinary text',{'content':'ordinary code'},split=2)
    if malformed:
        first=next(e for e in values if e['type']=='content_block_delta' and e['delta']['type']=='input_json_delta')
        first['delta']['partial_json']='{broken'
    output=[]
    async def run():
        async for item in g.async_post_call_streaming_iterator_hook(AUTH,chunks(values),{'stream':True}):output.append(item)
    if malformed:
        with pytest.raises(IncompleteUpstreamStream):await run()
        assert not any(e['type']=='message_stop' for e in output)
    else:
        await run();assert collect(output)==('ordinary text',{'content':'ordinary code'})
    assert not g._redis.values
