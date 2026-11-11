"""Source JSON escapes and literal backslashes are different restoration types."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import litellm
import pytest

from litellm_guardrails.pii_guardrail import RuPIIGuardrail
from litellm_guardrails.restoration_mapping import (
    RestorationMapping, decode_mapping, encode_mapping, json_atom_spans,
)
from litellm_guardrails.responses_state import (
    MAX_MAPPING_BYTES, ResponsesStateError, ResponsesStateStore,
)
from litellm_guardrails.streaming_restoration import (
    StreamingResponseRestorer, _StreamingJSONRestorer, restore_arguments, restore_text,
)


RAW_VALUES = [
    r'Ivan\nPetrov\"', r'C:\\new\\report.txt', r'literal\\n literal\\\"',
    r'\u0418\u0432\u0430\u043d', r'\ud83d\ude00', r'a\/b\tc\rd\b\f',
]


def mask_source(raw):
    source = '{"value":"' + raw + '","ticket":123456}'
    start = source.index(raw)
    guard = object.__new__(RuPIIGuardrail)
    masked, state = guard._mask_text(source, [
        {'start': start, 'end': start + len(raw), 'entity_type': 'PERSON'},
    ])
    return source, masked, state


def tool_history(api, source):
    function = {'name': 'save', 'arguments': source}
    if api == 'responses':
        target = {'type': 'function_call', 'call_id': 'call-s', **function}
        return {'input': [target]}, target
    return {'messages': [{'role': 'assistant', 'tool_calls': [
        {'id': 'call-s', 'type': 'function', 'function': function},
    ]}]}, function


@pytest.mark.parametrize('raw', RAW_VALUES)
@pytest.mark.parametrize('as_text', [False, True])
def test_source_json_and_json_carried_as_text_at_every_split(raw, as_text):
    source, masked, state = mask_source(raw)
    assert state.json_strings == {'<PERSON_1>'}
    assert state['<PERSON_1>'] == raw
    assert restore_text(masked, state).encode() == source.encode()
    state = decode_mapping(encode_mapping(state))
    arguments = json.dumps({'source': masked}) if as_text else masked
    expected = {'source': source} if as_text else json.loads(source)
    assert json.loads(restore_arguments(arguments, state)) == expected
    for split in range(len(arguments) + 1):
        restorer = _StreamingJSONRestorer(state)
        actual = restorer.push(arguments[:split]) + restorer.push(arguments[split:]) + restorer.flush()
        assert json.loads(actual) == expected, split


def test_accepted_double_json_text_limit_issue_122():
    source, masked, state = mask_source(r'Ivan\nPetrov')
    assert json.loads(restore_arguments(masked, state)) == json.loads(source)
    one_layer = json.dumps({'source': masked})
    assert json.loads(restore_arguments(one_layer, state)) == {'source': source}
    two_layers = json.dumps({'source': one_layer})
    expected = {'source': json.dumps({'source': source})}
    # Accepted limitation #122: record the mismatch, not successful restoration.
    assert json.loads(restore_arguments(two_layers, state)) != expected


@pytest.mark.parametrize('source', [
    r'Ivan\nPetrov', r'path C:\new\report.txt', 'actual\nnewline', r'code: \"quoted\"',
])
def test_plain_text_backslashes_are_not_guessed_to_be_json(source):
    guard = object.__new__(RuPIIGuardrail)
    masked, state = guard._mask_text(source, [
        {'start': 0, 'end': len(source), 'entity_type': 'PASSWORD'},
    ])
    assert not state.json_strings
    assert restore_text(masked, state) == source
    assert json.loads(restore_arguments(json.dumps({'value': masked}), state)) == {'value': source}


@pytest.mark.parametrize('raw', [r'\n', r'\\', r'\"', r'\u0418', r'\ud83d\ude00'])
def test_partial_escape_masks_expand_without_losing_coverage(raw):
    source = '{"value":"x' + raw + 'y","ticket":123456}'
    offset = source.index(raw)
    guard = object.__new__(RuPIIGuardrail)
    for index in range(len(raw)):
        masked, state = guard._mask_text(source, [
            {'start': offset + index, 'end': offset + index + 1, 'entity_type': 'PASSWORD'},
        ])
        assert masked == '{"value":"x<PASSWORD_1>y","ticket":123456}'
        assert state['<PASSWORD_1>'] == raw
        assert restore_text(masked, state) == source
        assert json.loads(restore_arguments(masked, state)) == json.loads(source)


def test_adjacent_findings_joined_by_escape_expansion_preserve_both_tails():
    source = r'{"value":"x\nY"}'
    start = source.index('x')
    guard = object.__new__(RuPIIGuardrail)
    masked, state = guard._mask_text(source, [
        {'start': start, 'end': start + 2, 'entity_type': 'PERSON'},
        {'start': start + 2, 'end': start + 4, 'entity_type': 'PASSWORD'},
    ])
    assert masked == '{"value":"<PERSON_1>"}'
    assert state['<PERSON_1>'] == r'x\nY'
    assert restore_text(masked, state) == source
    assert json.loads(restore_arguments(masked, state)) == json.loads(source)


def test_string_metadata_copy_update_and_legacy_reading():
    _, _, state = mask_source(r'Ivan\nPetrov')
    state.add_number('<RU_PASSPORT_1>', '122648')
    copied = RestorationMapping(state).copy()
    assert copied.json_strings == state.json_strings
    assert copied.json_numbers == state.json_numbers
    updated = RestorationMapping()
    updated.update(copied)
    assert updated.json_strings == {'<PERSON_1>'}
    updated.update({'<PERSON_1>': r'literal\n'})
    assert not updated.json_strings
    updated.add_string('<PERSON_1>', r'Ivan\nPetrov')
    updated['<PERSON_1>'] = 'plain'
    assert not updated.json_strings
    assert state.json_strings == {'<PERSON_1>'}
    legacy = decode_mapping(json.dumps(dict(state)))
    assert not legacy.json_strings
    assert json.loads(restore_arguments('{"value":"<PERSON_1>"}', legacy)) == {
        'value': r'Ivan\nPetrov',
    }
    body = json.loads(encode_mapping(state))
    assert body['version'] == 3 and body['json_numbers'] == ['<RU_PASSPORT_1>']
    restored = decode_mapping(json.dumps(body))
    assert restored.json_strings == state.json_strings and restored.json_numbers == state.json_numbers


@pytest.mark.parametrize('strings,raw,numbers', [
    (['missing'], r'Ivan\nPetrov', []),
    (['<PERSON_1>', '<PERSON_1>'], r'Ivan\nPetrov', []),
    (['<PERSON_1>'], r'bad\q', []),
    (['<PERSON_1>'], 'bad"quote', []),
    (['<PERSON_1>'], '123456', ['<PERSON_1>']),
    ('<PERSON_1>', r'Ivan\nPetrov', []),
    ([{}], r'Ivan\nPetrov', []),
])
def test_invalid_string_provenance_is_rejected(strings, raw, numbers):
    body = {'version': 3, 'values': {'<PERSON_1>': raw},
            'json_strings': strings, 'json_numbers': numbers}
    with pytest.raises(ValueError):
        decode_mapping(json.dumps(body))


def test_existing_budget_counts_string_metadata():
    _, _, state = mask_source(r'Ivan\nPetrov')
    flat_size = len(encode_mapping(dict(state)).encode())
    state['padding'] = 'x' * (MAX_MAPPING_BYTES - flat_size - len(',"padding":""'))
    assert len(ResponsesStateStore.serialize(dict(state)).encode()) == MAX_MAPPING_BYTES
    with pytest.raises(ResponsesStateError) as failure:
        ResponsesStateStore.serialize(state)
    assert failure.value.status == 413


def test_long_values_are_emitted_before_the_closing_quote():
    _, _, state = mask_source(r'Ivan\nPetrov')
    restorer = _StreamingJSONRestorer(state)
    source = '{"code":"' + 'x' * 200_000
    assert restorer.push(source) == source
    assert restorer.push('","value":"<PERSON_1>"}') + restorer.flush() == '","value":"Ivan\\nPetrov"}'


def test_replacements_are_not_recursive_and_argument_keys_do_not_change():
    state = RestorationMapping()
    state.add_string('<PERSON_1>', r'Ivan\n<PERSON_2>')
    state['<PERSON_2>'] = 'wrong-recursive-result'
    actual = restore_arguments('{"<PERSON_1>":"<PERSON_1>"}', state)
    assert json.loads(actual) == {'<PERSON_1>': 'Ivan\n<PERSON_2>'}


def test_escaped_placeholder_and_independent_values():
    source, masked, state = mask_source(r'Ivan\nPetrov')
    escaped = masked.replace('<', r'\u003c').replace('>', r'\u003e')
    for split in range(len(escaped) + 1):
        restorer = _StreamingJSONRestorer(state)
        actual = restorer.push(escaped[:split]) + restorer.push(escaped[split:]) + restorer.flush()
        assert json.loads(actual) == json.loads(source)
    arguments = '{"first":"{\\"nested\\":\\"<PERSON_1>\\"}","second":"<PERSON_1>"}'
    assert json.loads(restore_arguments(arguments, state)) == {
        'first': '{"nested":"Ivan\\nPetrov"}', 'second': 'Ivan\nPetrov',
    }


@pytest.mark.parametrize('arguments', ['{"value":"<PERSON_1>', '{"value":"<PER', '{"value":"\\u003'])
def test_unfinished_string_never_invents_a_closing_quote(arguments):
    _, _, state = mask_source(r'Ivan\nPetrov')
    for split in range(len(arguments) + 1):
        restorer = _StreamingJSONRestorer(state)
        actual = restorer.push(arguments[:split]) + restorer.push(arguments[split:]) + restorer.flush()
        with pytest.raises(ValueError):
            json.loads(actual)
        assert not actual.endswith('"')


@pytest.mark.asyncio
async def test_actual_redis_contract_round_trip_uses_the_same_keys_and_ttl():
    _, _, state = mask_source(r'Ivan\nPetrov')
    redis = AsyncMock()
    store = ResponsesStateStore(redis, b'local-test', 3600)
    owner = store.owner('trusted-key')
    redis.eval.return_value = ['saved']
    await store.save(owner, 'model', 'response', state)
    args = redis.eval.await_args.args
    assert args[2] == 'pii_responses_state:v1:' + owner
    body = args[6]
    assert json.loads(body)['json_strings'] == ['<PERSON_1>']
    redis.eval.return_value = ['found', body]
    loaded = await store.load(owner, 'model', 'response')
    assert loaded == state and loaded.json_strings == state.json_strings


@pytest.mark.parametrize('api', ['chat', 'responses'])
@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('as_text', [False, True])
@pytest.mark.asyncio
async def test_real_hooks_restore_and_clean_typed_string_state(api, stream, as_text):
    raw = r'\u0418\u0432\u0430\u043d\nPetrov\"\ud83d\ude00'
    source, expected_mask, _ = mask_source(raw)
    guard = RuPIIGuardrail()
    guard._redis = AsyncMock()
    guard._redis.get.return_value = None
    history, request_target = tool_history(api, source)
    data = {'model': 'mock', 'stream': stream, **history}
    left = source.index(raw)
    with patch.object(guard, '_analyze_text', return_value=[
        {'start': left, 'end': left + len(raw), 'entity_type': 'PERSON'},
    ]):
        data = await guard.async_pre_call_hook(
            MagicMock(), MagicMock(), data, call_type='responses' if api == 'responses' else 'completion',
        )
    masked = request_target['arguments']
    assert masked == expected_mask
    key, _, serialized = guard._redis.setex.await_args.args
    guard._redis.get.return_value = serialized
    arguments = json.dumps({'source': masked}) if as_text else masked
    expected = {'source': source} if as_text else json.loads(source)
    if not stream:
        if api == 'responses':
            target = {'type': 'function_call', 'arguments': arguments}
            response = {'object': 'response', 'output': [target]}
        else:
            response = litellm.ModelResponse(choices=[litellm.Choices(message=litellm.Message(
                role='assistant', tool_calls=[{'id': 'call-s', 'type': 'function',
                    'function': {'name': 'save', 'arguments': arguments}}],
            ))])
            target = response.choices[0].message.tool_calls[0].function
        await guard.async_post_call_success_hook(data, MagicMock(), response)
        actual = target['arguments'] if isinstance(target, dict) else target.arguments
    else:
        async def events():
            for char in arguments:
                if api == 'responses':
                    yield {'type': 'response.function_call_arguments.delta', 'output_index': 0, 'delta': char}
                else:
                    yield {'choices': [{'index': 0, 'delta': {'tool_calls': [
                        {'index': 0, 'function': {'arguments': char}},
                    ]}}]}
            yield ({'type': 'response.function_call_arguments.done', 'output_index': 0, 'arguments': arguments}
                   if api == 'responses' else {'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'tool_calls'}]})
        restored = [event async for event in guard.async_post_call_streaming_iterator_hook(MagicMock(), events(), data)]
        actual = (''.join(event.get('delta', '') for event in restored) if api == 'responses' else
                  ''.join(call['function'].get('arguments', '') for event in restored for choice in event['choices']
                          for call in choice['delta'].get('tool_calls', [])))
    assert json.loads(actual) == expected
    guard._redis.delete.assert_awaited_once_with(key)


def test_atoms_only_assign_json_provenance_to_valid_documents():
    assert json_atom_spans('prefix {"value":"Ivan\\nPetrov"}') == ([], [])
    assert json_atom_spans('{"value":NaN}') == ([], [])
    numbers, strings = json_atom_spans('{"value":"Ivan\\nPetrov","ticket":123456}')
    assert len(numbers) == 1 and len(strings) == 3


@pytest.mark.parametrize('mode', ['mask', 'block'])
@pytest.mark.parametrize('api', ['chat', 'responses'])
@pytest.mark.asyncio
async def test_source_mask_block_and_private_metadata(mode, api):
    source, _, _ = mask_source(r'Ivan\nPetrov\"')
    guard = RuPIIGuardrail()
    guard.pii_mode = mode
    guard._redis = AsyncMock()
    guard._redis.get.return_value = None
    raw = r'Ivan\nPetrov\"'
    start = source.index(raw)
    findings = [{'start': start, 'end': start + len(raw), 'entity_type': 'PERSON'}]
    data, target = tool_history(api, source)
    with patch.object(guard, '_analyze_text', return_value=findings):
        if mode == 'block':
            with pytest.raises(litellm.UnprocessableEntityError):
                await guard.async_pre_call_hook(MagicMock(), MagicMock(), data,
                                                call_type='responses' if api == 'responses' else 'completion')
            guard._redis.setex.assert_not_awaited()
            assert target['arguments'] == source
        else:
            masked = await guard.async_pre_call_hook(MagicMock(), MagicMock(), data,
                                                    call_type='responses' if api == 'responses' else 'completion')
            assert raw not in target['arguments']
            assert 'json_strings' not in json.dumps(masked)
            state = decode_mapping(guard._redis.setex.await_args.args[2])
            assert state.json_strings == {'<PERSON_1>'}
            assert json.loads(target['arguments']) == {'value': '<PERSON_1>', 'ticket': 123456}


@pytest.mark.parametrize('api', ['chat', 'responses'])
def test_interleaved_argument_states_do_not_share_json_context(api):
    source, masked, state = mask_source(r'Ivan\nPetrov')
    templates = {0: masked, 1: json.dumps({'source': masked})}
    results = {0: '', 1: ''}
    restorer = StreamingResponseRestorer(state)
    for offset in range(max(map(len, templates.values()))):
        for index, template in templates.items():
            if offset >= len(template):
                continue
            piece = template[offset]
            event = ({'type': 'response.function_call_arguments.delta', 'output_index': index, 'delta': piece}
                     if api == 'responses' else {'choices': [{'index': 0, 'delta': {'tool_calls': [
                         {'index': index, 'function': {'arguments': piece}},
                     ]}}]})
            for restored in restorer.process(event):
                if api == 'responses':
                    results[restored['output_index']] += restored['delta']
                else:
                    for call in restored['choices'][0]['delta']['tool_calls']:
                        results[call['index']] += call['function']['arguments']
    assert json.loads(results[0]) == json.loads(source)
    assert json.loads(results[1]) == {'source': source}


@pytest.mark.asyncio
async def test_cancelled_stream_cleans_typed_mapping_without_logging_values(caplog):
    _, _, state = mask_source(r'Ivan\nPetrov')
    guard = RuPIIGuardrail()
    guard._redis = AsyncMock()
    guard._redis.get.return_value = encode_mapping(state)
    closed = asyncio.Event()

    async def events():
        try:
            yield {'type': 'response.function_call_arguments.delta', 'output_index': 0, 'delta': '{"value":"<PER'}
            pytest.fail('Closing the client must not consume another event')
        finally:
            closed.set()

    stream = guard.async_post_call_streaming_iterator_hook(MagicMock(), events(),
        {'metadata': {'pii_request_id': 'string-cancel'}})
    await anext(stream)
    await stream.aclose()
    assert closed.is_set()
    guard._redis.delete.assert_awaited_once_with('pii_mapping:string-cancel')
    assert 'Ivan' not in caplog.text and 'Petrov' not in caplog.text
