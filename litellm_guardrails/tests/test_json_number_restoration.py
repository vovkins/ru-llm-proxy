"""JSON-number masking must preserve syntax and the original client-side type."""

import json
from unittest.mock import AsyncMock, MagicMock

import litellm
import pytest

from litellm_guardrails.pii_guardrail import RuPIIGuardrail
from litellm_guardrails.restoration_mapping import RestorationMapping
from litellm_guardrails.streaming_restoration import (
    StreamingResponseRestorer, _StreamingJSONRestorer, restore_arguments, restore_text,
)


def mapping():
    return RestorationMapping({
        '<RU_PASSPORT_1>': '122648', '<RU_PASSPORT_2>': '122648',
        '<RU_PASSPORT_3>': '001234',
    }, json_numbers={'<RU_PASSPORT_1>'})


def mask(text, start, end):
    guard = object.__new__(RuPIIGuardrail)
    return guard._mask_text(text, [{
        'start': start, 'end': end, 'entity_type': 'RU_PASSPORT', 'score': .75,
    }])


@pytest.mark.parametrize('literal', ['122648', '-122648', '122648.50', '1.22648e5', '-0.0', '1e400'])
@pytest.mark.parametrize('partial', [False, True])
def test_mask_whole_json_number_literal_without_reserializing(literal, partial):
    text = ' { "passport_number" : ' + literal + ', "ticket":654321 }\n'
    start = text.index(literal)
    end = start + len(literal)
    if partial and len(literal) > 2:
        start += 1
        end -= 1
    masked, state = mask(text, start, end)
    assert masked == ' { "passport_number" : "<RU_PASSPORT_1>", "ticket":654321 }\n'
    assert json.loads(masked)['ticket'] == 654321
    assert state['<RU_PASSPORT_1>'] == literal
    assert state.json_numbers == {'<RU_PASSPORT_1>'}
    assert restore_text(masked, state) == text
    assert restore_arguments(masked, state) == text


@pytest.mark.parametrize('text', [
    '{"passport_number":"122648"}', '{"passport_number":"001234"}',
    '{"note":"Номер паспорта: 122648"}',
    '{"note":"{\\"passport_number\\":122648}"}',
    'Номер паспорта: 122648',
])
def test_numeric_characters_in_strings_and_prose_remain_text(text):
    value = '001234' if '001234' in text else '122648'
    start = text.index(value)
    masked, state = mask(text, start, start + len(value))
    assert not getattr(state, 'json_numbers', ())
    assert restore_text(masked, state) == text


def test_two_detections_in_one_number_keep_coverage_with_one_placeholder():
    text = '{"passport_number":122648}'
    start = text.index('122648')
    guard = object.__new__(RuPIIGuardrail)
    masked, state = guard._mask_text(text, [
        {'start': start, 'end': start + 2, 'entity_type': 'RU_PASSPORT'},
        {'start': start + 3, 'end': start + 6, 'entity_type': 'RU_PASSPORT'},
    ])
    assert masked == '{"passport_number":"<RU_PASSPORT_1>"}'
    assert state == {'<RU_PASSPORT_1>': '122648'}


@pytest.mark.parametrize('escaped', [False, True])
def test_native_json_types_and_escapes_at_every_split(escaped):
    state = mapping()
    text = ('{"number":"<RU_PASSPORT_1>","string":"<RU_PASSPORT_2>",'
            '"leading":"<RU_PASSPORT_3>","note":"id <RU_PASSPORT_1>",'
            '"<RU_PASSPORT_1>":"key","array":["<RU_PASSPORT_1>"],"ticket":654321}')
    if escaped:
        text = text.replace('<', '\\u003c').replace('>', '\\u003e')
    expected = {'number': 122648, 'string': '122648', 'leading': '001234',
                'note': 'id 122648', '<RU_PASSPORT_1>': 'key',
                'array': [122648], 'ticket': 654321}
    assert json.loads(restore_arguments(text, state)) == expected
    for split in range(len(text) + 1):
        restorer = _StreamingJSONRestorer(state)
        result = restorer.push(text[:split]) + restorer.push(text[split:]) + restorer.flush()
        assert json.loads(result) == expected, split


@pytest.mark.parametrize('template', [
    '{"n":"<RU_PASSPORT_1>","s":"<RU_PASSPORT_2>","z":"<RU_PASSPORT_3>"}',
    'Документ: {"n":"<RU_PASSPORT_1>","s":"<RU_PASSPORT_2>","z":"<RU_PASSPORT_3>"}.',
    '```json\n{"n":"<RU_PASSPORT_1>","s":"<RU_PASSPORT_2>","z":"<RU_PASSPORT_3>"}\n```',
])
def test_json_returned_as_text_has_exact_types_without_quote_stripping_keys(template):
    expected = template.replace('"<RU_PASSPORT_1>"', '122648').replace(
        '<RU_PASSPORT_2>', '122648').replace('<RU_PASSPORT_3>', '001234')
    assert restore_text(template, mapping()) == expected
    for split in range(len(template) + 1):
        restorer = StreamingResponseRestorer(mapping())
        events = []
        for part in (template[:split], template[split:]):
            events.extend(restorer.process({'choices': [{'index': 0, 'delta': {'content': part}}]}))
        events.extend(restorer.process({'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]}))
        actual = ''.join(choice['delta'].get('content', '') for event in events for choice in event['choices'])
        assert actual == expected, split


def test_text_json_key_remains_quoted_and_prose_quotes_are_not_typed_values():
    text = '{"<RU_PASSPORT_1>":"<RU_PASSPORT_1>"}'
    assert restore_text(text, mapping()) == '{"122648":122648}'
    assert restore_text('Паспорт "<RU_PASSPORT_1>" получен', mapping()) == 'Паспорт "122648" получен'


def test_long_json_string_is_not_buffered_until_the_closing_quote():
    restorer = _StreamingJSONRestorer(mapping())
    source = '{"code":"' + 'x' * 200_000
    emitted = restorer.push(source)
    assert emitted == source
    assert restorer.push('","n":"<RU_PASSPORT_1>"}') + restorer.flush() == '","n":122648}'


@pytest.mark.parametrize('source', [
    '{"n":"<RU_PASSPORT_1>', '{"n":"<RU_PASS', '{"n":"\\u003cRU_PASSPORT_1>',
])
def test_truncated_numeric_string_never_invents_a_closing_quote(source):
    for split in range(len(source) + 1):
        restorer = _StreamingJSONRestorer(mapping())
        actual = restorer.push(source[:split]) + restorer.push(source[split:]) + restorer.flush()
        assert not actual.endswith('"')
        with pytest.raises(ValueError):
            json.loads(actual)


def test_numeric_restoration_is_not_recursive_through_dictionary_keys():
    state = mapping()
    state['122648'] = 'wrong-recursive-result'
    assert restore_arguments('{"n":"<RU_PASSPORT_1>"}', state) == '{"n":122648}'
    assert restore_text('JSON {"n":"<RU_PASSPORT_1>"}', state) == 'JSON {"n":122648}'


@pytest.mark.parametrize('text', [
    'quoted "C:\\work\\file" and <RU_PASSPORT_1>',
    '{"path":"C:\\work", "n":"<RU_PASSPORT_1>"}',
    '{"n":"\\u003cRU_PASSPORT_1\\u003e"}',
    '{"<RU_PASSPORT_1>":"<RU_PASSPORT_1>"}',
])
def test_typed_text_is_split_independent_and_does_not_require_valid_prose_escapes(text):
    from litellm_guardrails.streaming_restoration import _StreamingTypedTextRestorer
    expected = restore_text(text, mapping())
    for split in range(len(text) + 1):
        restorer = _StreamingTypedTextRestorer(mapping())
        assert restorer.push(text[:split]) + restorer.push(text[split:]) + restorer.flush() == expected


@pytest.mark.parametrize('legacy', [False, True])
def test_numeric_buffers_are_independent_between_calls_and_legacy_functions(legacy):
    restorer = StreamingResponseRestorer(mapping())
    results = {0: '', 1: ''}
    sources = {0: '{"value":"<RU_PASSPORT_1>"}', 1: '{"value":"<RU_PASSPORT_2>"}'}
    for index in range(max(map(len, sources.values()))):
        for call, source in sources.items():
            if index >= len(source):
                continue
            function = {'arguments': source[index]}
            delta = ({'function_call': function} if legacy else
                     {'tool_calls': [{'index': call, 'function': function}]})
            event = {'choices': [{'index': call if legacy else 0, 'delta': delta}]}
            restored = restorer.process(event)[0]['choices'][0]['delta']
            result = restored.get('function_call') if legacy else restored['tool_calls'][0]['function']
            results[call] += result['arguments']
    for choice in ([0, 1] if legacy else [0]):
        restorer.process({'choices': [{'index': choice, 'delta': {}, 'finish_reason': 'tool_calls'}]})
    assert json.loads(results[0]) == {'value': 122648}
    assert json.loads(results[1]) == {'value': '122648'}


@pytest.mark.parametrize('api', ['chat', 'responses'])
@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.asyncio
async def test_hooks_load_typed_state_and_restore_number_then_cleanup(api, stream):
    guard = RuPIIGuardrail()
    guard._redis = AsyncMock()
    guard._redis.get.return_value = json.dumps({
        'version': 2, 'values': dict(mapping()), 'json_numbers': ['<RU_PASSPORT_1>'],
    })
    data = {'metadata': {'pii_request_id': 'numeric-test'}, 'stream': stream}
    text = '{"number":"<RU_PASSPORT_1>","string":"<RU_PASSPORT_2>"}'
    if not stream:
        if api == 'responses':
            target = {'type': 'function_call', 'arguments': text}
            response = {'object': 'response', 'output': [target]}
        else:
            response = litellm.ModelResponse(choices=[litellm.Choices(message=litellm.Message(
                role='assistant', tool_calls=[{'id': 'call-n', 'type': 'function',
                    'function': {'name': 'save', 'arguments': text}}],
            ))])
            target = response.choices[0].message.tool_calls[0].function
        await guard.async_post_call_success_hook(data, MagicMock(), response)
        actual = target['arguments'] if isinstance(target, dict) else target.arguments
    else:
        async def source():
            for char in text:
                if api == 'responses':
                    yield {'type': 'response.function_call_arguments.delta', 'output_index': 0, 'delta': char}
                else:
                    yield {'choices': [{'index': 0, 'delta': {'tool_calls': [
                        {'index': 0, 'function': {'arguments': char}},
                    ]}}]}
            yield ({'type': 'response.function_call_arguments.done', 'output_index': 0, 'arguments': text}
                   if api == 'responses' else {'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'tool_calls'}]})
        received = [event async for event in guard.async_post_call_streaming_iterator_hook(MagicMock(), source(), data)]
        actual = (''.join(event.get('delta', '') for event in received) if api == 'responses' else
                  ''.join(call['function'].get('arguments', '') for event in received for choice in event['choices']
                          for call in choice['delta'].get('tool_calls', [])))
    assert json.loads(actual) == {'number': 122648, 'string': '122648'}
    guard._redis.delete.assert_awaited_once_with('pii_mapping:numeric-test')
