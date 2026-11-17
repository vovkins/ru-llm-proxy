"""Request-wide alias identity and JSON data-key restoration regressions."""
import copy
import json
import re
from unittest.mock import AsyncMock

import litellm
import pytest
from litellm.proxy._types import ProxyException

from litellm_guardrails.tests.test_anthropic_restoration import (
    AUTH, RULES, guard, message, events, chunks, collect,
)
from litellm_guardrails.dictionary_policy import DictionarySubstitutionPolicy
from litellm_guardrails.restoration_mapping import (
    RestorationMapping, RestorationKeyCollision, encode_mapping, decode_mapping,
)
from litellm_guardrails.streaming_restoration import (
    _StreamingJSONRestorer, restore_arguments, restore_text,
)


@pytest.mark.asyncio
@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('stream', [False, True])
async def test_literal_masks_are_reserved_across_the_whole_request(reverse, stream):
    g = guard()
    g.dictionary_substitutions_enabled = False
    async def analyze(text):
        return [{'start': m.start(), 'end': m.end(), 'entity_type': 'PERSON'}
                for m in re.finditer('Alice', text)]
    g._analyze_text = AsyncMock(side_effect=analyze)
    values = ['Alice', '<PERSON_1> <PERSON_2> <PERSON_' + '9' * 5000 + '>']
    if reverse:
        values.reverse()
    request = {'stream': stream, 'messages': [{'role': 'user', 'content': x} for x in values]}
    await g.async_pre_call_hook(AUTH, None, request, 'anthropic_messages')
    wire = '\n'.join(m['content'] for m in request['messages'])
    assert 'Alice' not in wire
    assert '<PERSON_3>' in wire
    if stream:
        output = [e async for e in g.async_post_call_streaming_iterator_hook(
            AUTH, chunks(events(wire, {'content': wire}, split=13)), request)]
        text, tool = collect(output)
    else:
        reply = message(wire, {'content': wire})
        await g.async_post_call_success_hook(request, AUTH, reply)
        text, tool = reply['content'][0]['text'], reply['content'][1]['input']
    assert text == '\n'.join(values) == tool['content']
    assert not g._redis.values


@pytest.mark.asyncio
@pytest.mark.parametrize('reverse', [False, True])
async def test_dictionary_ambiguity_in_separate_fields_blocks_before_analysis(reverse):
    g = guard()
    values = ['ExampleBranddirect', 'ladogadirect01']
    if reverse:
        values.reverse()
    request = {'messages': [{'role': 'user', 'content': x} for x in values]}
    before = copy.deepcopy(request)
    with pytest.raises(ProxyException) as exc:
        await g.async_pre_call_hook(AUTH, None, request, 'anthropic_messages')
    assert exc.value.status_code == 422
    assert request['messages'] == before['messages']
    g._analyze_text.assert_not_awaited()
    assert not g._redis.values


@pytest.mark.asyncio
async def test_dictionary_case_variants_and_literal_dynamic_alias_roundtrip_across_fields():
    rules = copy.deepcopy(RULES)
    rules['substitutions'][0]['match']['case_sensitive'] = False
    g = guard()
    g.dictionary_substitution_policy = DictionarySubstitutionPolicy.from_config(rules)
    values = ['ExampleBranddirect', 'EXAMPLEBRANDDIRECT EXAMPLEBRANDDIRECT', '<DICT_PACKAGE_1>']
    request = {'messages': [{'role': 'user', 'content': x} for x in values]}
    await g.async_pre_call_hook(AUTH, None, request, 'anthropic_messages')
    wire = '\n'.join(m['content'] for m in request['messages'])
    assert 'EXAMPLEBRANDDIRECT' not in wire
    reply = message(wire)
    await g.async_post_call_success_hook(request, AUTH, reply)
    assert reply['content'][0]['text'] == '\n'.join(values)


def test_dictionary_standalone_preserves_case_variants():
    rules = copy.deepcopy(RULES)
    rules['substitutions'][0]['match']['case_sensitive'] = False
    policy = DictionarySubstitutionPolicy.from_config(rules)
    original = 'ExampleBranddirect EXAMPLEBRANDDIRECT examplebranddirect'
    result = policy.apply(original)
    assert restore_text(result.text, result.mapping) == original


@pytest.mark.asyncio
@pytest.mark.parametrize('dictionary', [False, True])
@pytest.mark.parametrize('stream', [False, True])
async def test_native_history_data_keys_roundtrip_and_mapping_provenance(dictionary, stream):
    g = guard()
    g.dictionary_substitutions_enabled = dictionary
    word = 'ExampleBranddirect' if dictionary else 'Alice'
    async def analyze(text):
        return [] if dictionary else [
            {'start': m.start(), 'end': m.end(), 'entity_type': 'PERSON'}
            for m in re.finditer('Alice', text)]
    g._analyze_text = AsyncMock(side_effect=analyze)
    original = {word: {'nested': [{word: 42}, None, True]}, 'value': word}
    block = {'type': 'tool_use', 'id': 'call_local', 'name': 'Write', 'input': copy.deepcopy(original)}
    request = {'stream': stream, 'messages': [
        {'role': 'assistant', 'content': [block]}, {'role': 'user', 'content': word}]}
    await g.async_pre_call_hook(AUTH, None, request, 'anthropic_messages')
    masked = block['input']
    assert word not in json.dumps(masked)
    if stream:
        output = [e async for e in g.async_post_call_streaming_iterator_hook(
            AUTH, chunks(events('', masked, split=1)), request)]
        _, tool = collect(output)
    else:
        reply = message('', masked)
        await g.async_post_call_success_hook(request, AUTH, reply)
        tool = reply['content'][1]['input']
    assert tool == original
    assert not g._redis.values


@pytest.mark.parametrize('split', [1, 2, 7, 10000])
def test_provenance_keys_restore_escapes_and_independent_nested_objects(split):
    mapping = RestorationMapping({'alias': 'Тверь "центр"\\x\n😀'}, json_keys={'alias'})
    mapping = decode_mapping(encode_mapping(mapping))
    source = {'alias': [{'alias': 1}, {'alias': 2}], 'other': 'alias'}
    wire = json.dumps(source, ensure_ascii=True)
    restorer = _StreamingJSONRestorer(mapping)
    actual = ''.join(restorer.push(wire[i:i+split]) for i in range(0, len(wire), split)) + restorer.flush()
    key = mapping['alias']
    assert json.loads(actual) == {key: [{key: 1}, {key: 2}], 'other': key}


@pytest.mark.parametrize('split', [1, 7, 10000])
@pytest.mark.parametrize('source', [
    '{"alias":1,"original":2}', '{"original":1,"alias":2}',
    '{"a\\u006cias":1,"original":2}',
])
def test_colliding_restored_keys_fail_closed_instead_of_losing_data(split, source):
    mapping = RestorationMapping({'alias': 'original'}, json_keys={'alias'})
    restorer = _StreamingJSONRestorer(mapping)
    with pytest.raises(RestorationKeyCollision):
        for i in range(0, len(source), split):
            restorer.push(source[i:i+split])


@pytest.mark.asyncio
async def test_collision_post_hook_returns_safe_502_and_deletes_mapping():
    g = guard(RestorationMapping({'alias': 'original'}, json_keys={'alias'}))
    reply = message('', {'alias': 1, 'original': 2})
    with pytest.raises(ProxyException) as exc:
        await g.async_post_call_success_hook({'metadata': {'pii_request_id': 'local'}}, AUTH, reply)
    assert exc.value.status_code == 502
    assert not g._redis.values


@pytest.mark.asyncio
@pytest.mark.parametrize('api', ['chat', 'responses'])
@pytest.mark.parametrize('stream', [False, True])
async def test_provenance_keys_work_in_other_supported_response_apis(api, stream):
    mapping = RestorationMapping({'alias': 'original'}, json_keys={'alias'})
    g = guard(mapping)
    request = {'metadata': {'pii_request_id': 'local'}}
    wire = '{"alias":{"alias":"alias"}}'
    if not stream:
        if api == 'chat':
            response = litellm.ModelResponse(choices=[{'index':0,'message':{'role':'assistant',
                'tool_calls':[{'id':'call_local','type':'function','function':{'name':'Write','arguments':wire}}]},
                'finish_reason':'tool_calls'}])
            function = response.choices[0].message.tool_calls[0].function
        else:
            function = {'type':'function_call','id':'fc_local','call_id':'call_local','name':'Write','arguments':wire}
            response = {'object':'response','output':[function]}
        await g.async_post_call_success_hook(request, AUTH, response)
        restored = function.arguments if api == 'chat' else function['arguments']
    else:
        if api == 'chat':
            values = [{'choices':[{'index':0,'delta':{'tool_calls':[{'index':0,'function':{'arguments':c}}]},
                                    'finish_reason':None}]} for c in wire]
            values.append({'choices':[{'index':0,'delta':{},'finish_reason':'tool_calls'}]})
        else:
            values = [{'type':'response.function_call_arguments.delta','output_index':0,
                       'item_id':'fc_local','delta':c} for c in wire]
            values.append({'type':'response.function_call_arguments.done','output_index':0,
                           'item_id':'fc_local','arguments':wire})
        output = [e async for e in g.async_post_call_streaming_iterator_hook(AUTH,chunks(values),request)]
        if api == 'chat':
            restored = ''.join(c['function'].get('arguments','') for e in output
                for choice in e.get('choices',[]) for c in choice['delta'].get('tool_calls',[]))
        else:
            restored = ''.join(e['delta'] for e in output if e['type']=='response.function_call_arguments.delta')
    assert json.loads(restored) == {'original':{'original':'original'}}
    assert not g._redis.values


def test_v4_provenance_preserves_numeric_and_escaped_string_types():
    mapping = RestorationMapping({'alias':'original'}, json_keys={'alias'})
    mapping.add_number('<NUMBER_1>', '123456')
    mapping.add_string('<PERSON_1>', r'\u0041lice')
    mapping.json_keys.add('<PERSON_1>')
    mapping = decode_mapping(encode_mapping(mapping))
    actual = restore_arguments('{"alias":"<NUMBER_1>","<PERSON_1>":"<PERSON_1>"}',mapping)
    assert json.loads(actual) == {'original':123456,'Alice':'Alice'}


@pytest.mark.asyncio
async def test_literal_mask_in_tool_schema_is_also_reserved():
    g = guard()
    g._analyze_text = AsyncMock(return_value=[{'start':0,'end':5,'entity_type':'PERSON'}])
    request = {'messages':[{'role':'user','content':'Alice'}],
               'tools':[{'type':'function','function':{'name':'Write','parameters':{
                   'type':'object','properties':{'<PERSON_1>':{'type':'string'}}}}}]}
    await g.async_pre_call_hook(AUTH,None,request,'completion')
    assert request['messages'][0]['content'] == '<PERSON_2>'


@pytest.mark.asyncio
async def test_inherited_key_provenance_survives_alias_reused_only_as_value():
    g = guard()
    inherited = RestorationMapping({'ladogadirect01':'ExampleBranddirect'}, json_keys={'ladogadirect01'})
    g._prepare_responses_state = AsyncMock(return_value=inherited)
    request = {'messages':[{'role':'user','content':'ExampleBranddirect'}]}
    await g.async_pre_call_hook(AUTH,None,request,'anthropic_messages')
    reply = message('', {'ladogadirect01':42})
    await g.async_post_call_success_hook(request,AUTH,reply)
    assert reply['content'][1]['input'] == {'ExampleBranddirect':42}


def test_preflight_uses_selected_longest_matches_without_false_ambiguity():
    policy = DictionarySubstitutionPolicy.from_config({'substitutions':[
        {'id':'short','source':'ExampleBrand','replacement':'shortAlias'},
        {'id':'long','source':'ExampleBrand Group','replacement':'longAlias'},
    ]})
    policy.validate_request(['ExampleBrand Group', 'shortAlias'])
    assert policy.apply('ExampleBrand Group').text == 'longAlias'


def test_schema_keys_without_provenance_remain_unchanged():
    assert restore_arguments('{"alias":"alias"}', {'alias': 'original'}) == '{"alias":"original"}'


@pytest.mark.parametrize('keys', [42, ['alias'], {'missing'}, {1}])
def test_invalid_key_provenance_cannot_be_persisted(keys):
    mapping = RestorationMapping({'alias': 'original'})
    mapping.json_keys = keys
    with pytest.raises(ValueError):
        encode_mapping(mapping)
