"""Collision tags must not reintroduce protected names from rule identifiers."""
import json
import re
from unittest.mock import AsyncMock

import pytest

from litellm_guardrails.dictionary_policy import (
    DictionaryPolicyAmbiguousRequestError, DictionarySubstitutionPolicy,
)
from litellm_guardrails.restoration_mapping import (
    RestorationMapping, decode_mapping, encode_mapping,
)
from litellm_guardrails.streaming_restoration import restore_arguments, restore_text
from litellm_guardrails.tests.test_anthropic_restoration import (
    AUTH, guard, message, chunks, events, collect,
)
from litellm_guardrails.tests.test_attachment_policy import (
    _guard as attachment_guard,
    _request as attachment_request,
)


RULE = {
    'id': 'alfabank_to_ladoga_finance', 'source': 'Альфа-Банк',
    'replacement': 'Ладога Финанс',
    'match': {'case_sensitive': False, 'whole_phrase': True}, 'restore': True,
}
SYNTHETIC_RULE = {
    'id': 'fableworkshop_fixture', 'source': 'Fable Workshop', 'replacement': 'Cedar Studio',
}
ESCAPED_NEUTRAL_TAG = r'\u003cDICT_1\u003e'


def policy(*rules):
    return DictionarySubstitutionPolicy.from_config({'substitutions': list(rules or [RULE])})


def test_case_collision_does_not_copy_sensitive_rule_id_to_output():
    original = 'Альфа-Банк / АЛЬФА-БАНК / альфа-банк'
    result = policy().apply(original)
    assert result.text == 'Ладога Финанс / <DICT_1> / <DICT_2>'
    assert not re.search(r'alfabank|альфа-банк', result.text, re.I)
    assert restore_text(result.text, result.mapping) == original
    assert result.rule_counts == {RULE['id']: 3}


def test_occupied_literal_and_request_reserved_tags_are_not_reused():
    result = policy().apply(
        'АЛЬФА-БАНК <DICT_2>',
        existing_mapping={'Ладога Финанс': 'Альфа-Банк', '<DICT_1>': 'old'},
        reserved_placeholders={'<DICT_3>'},
    )
    assert result.text == '<DICT_4> <DICT_2>'
    assert result.mapping == {'<DICT_4>': 'АЛЬФА-БАНК'}
    assert restore_text(result.text, result.mapping) == 'АЛЬФА-БАНК <DICT_2>'


def test_different_rules_share_a_collision_free_request_namespace():
    first = {'id': 'private_company_alpha', 'source': 'Company Alpha', 'replacement': 'South'}
    second = {'id': 'private_company_beta', 'source': 'Company Beta', 'replacement': 'West'}
    original = 'Company Alpha COMPANY ALPHA Company Beta COMPANY BETA'
    result = policy(first, second).apply(original)
    assert result.text == 'South <DICT_1> West <DICT_2>'
    assert restore_text(result.text, result.mapping) == original


def test_existing_legacy_and_new_mapping_keys_restore_together():
    mapping = RestorationMapping({
        '<DICT_ALFABANK_TO_LADOGA_FINANCE_1>': 'АЛЬФА-БАНК',
        '<DICT_1>': 'альфа-банк',
    })
    decoded = decode_mapping(encode_mapping(mapping))
    assert restore_text('<DICT_ALFABANK_TO_LADOGA_FINANCE_1> / <DICT_1>', decoded) == 'АЛЬФА-БАНК / альфа-банк'


def test_neutral_tags_restore_json_data_keys_and_values_without_changing_case():
    original = {'value': 'Альфа-Банк', 'АЛЬФА-БАНК': 'альфа-банк'}
    wire = json.dumps(original, ensure_ascii=False)
    result = policy().apply(wire)
    assert result.mapping['<DICT_1>'] == 'АЛЬФА-БАНК'
    mapping = RestorationMapping(result.mapping, json_keys={'<DICT_1>'})
    assert json.loads(restore_arguments(result.text, mapping)) == original
    assert 'ALFABANK' not in result.text


def test_direct_dictionary_apply_reserves_an_escaped_json_key():
    text = '{"Fable Workshop":1,"FABLE WORKSHOP":2,"' + ESCAPED_NEUTRAL_TAG + '":3}'
    result = policy(SYNTHETIC_RULE).apply(text)

    assert json.loads(result.text) == {'Cedar Studio': 1, '<DICT_2>': 2, '<DICT_1>': 3}
    mapping = RestorationMapping(result.mapping, json_keys=set(result.mapping))
    assert json.loads(restore_arguments(result.text, mapping)) == json.loads(text)


def test_dictionary_apply_keeps_escape_sequences_in_plain_text():
    text = 'Fable Workshop / FABLE WORKSHOP ' + ESCAPED_NEUTRAL_TAG
    result = policy(SYNTHETIC_RULE).apply(text)

    assert result.text == 'Cedar Studio / <DICT_1> ' + ESCAPED_NEUTRAL_TAG
    assert restore_text(result.text, result.mapping) == text


def test_json_reference_budget_only_applies_when_a_neutral_tag_is_needed(monkeypatch):
    from litellm_guardrails import dictionary_request_plan

    monkeypatch.setattr(dictionary_request_plan, 'MAX_REFERENCE_CHARACTERS', 1)
    kept = '{"kept":"' + ESCAPED_NEUTRAL_TAG + '"}'
    substitutions = policy(SYNTHETIC_RULE)
    ordinary = substitutions.validate_request(['Fable Workshop', kept])
    assert substitutions.apply('Fable Workshop', request_plan=ordinary).text == 'Cedar Studio'

    ambiguous = 'Fable Workshop / FABLE WORKSHOP'
    plan = substitutions.validate_request([ambiguous, kept])
    with pytest.raises(DictionaryPolicyAmbiguousRequestError, match='planning limit'):
        substitutions.apply(ambiguous, request_plan=plan)


@pytest.mark.asyncio
async def test_json_attachment_reserves_escaped_tag_for_message_text():
    g = attachment_guard()
    g.dictionary_substitution_policy = policy(SYNTHETIC_RULE)
    original = '{"kept":"' + ESCAPED_NEUTRAL_TAG + '"}'
    request, _, read_attachment = attachment_request('aresponses', original, json_file=True)
    block = {'type': 'input_text', 'text': 'Fable Workshop / FABLE WORKSHOP'}
    request['input'][0]['content'].insert(0, block)

    await g.async_pre_call_hook(None, None, request, 'aresponses')

    assert block['text'] == 'Cedar Studio / <DICT_2>'
    assert json.loads(read_attachment()) == {'kept': '<DICT_1>'}


@pytest.mark.asyncio
@pytest.mark.parametrize('api', ['acompletion', 'aresponses'])
@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('normalized', [False, True])
async def test_escaped_literal_reserved_across_native_tool_arguments(api, reverse, normalized):
    g = guard()
    g.dictionary_substitution_policy = policy(SYNTHETIC_RULE)
    g._analyze_text = AsyncMock(return_value=[])
    arguments = '{"kept":"' + ESCAPED_NEUTRAL_TAG + '"}'
    text = 'Fable Workshop / FABLE WORKSHOP'
    if api == 'aresponses':
        user = {'role': 'user', 'content': text}
        tool = {'type': 'function_call', 'call_id': 'call-fixture', 'name': 'inspect', 'arguments': arguments}
        request = {'input': [tool, user] if reverse else [user, tool]}
    else:
        user = {'role': 'user', 'content': text}
        tool = {'role': 'assistant', 'tool_calls': [
            {'id': 'call-fixture', 'type': 'function', 'function': {'name': 'inspect', 'arguments': arguments}},
        ]}
        request = {'messages': [tool, user] if reverse else [user, tool]}

    await g.async_pre_call_hook(None, None, request, api)

    assert user['content'] == 'Cedar Studio / <DICT_2>'
    mapping = decode_mapping(next(iter(g._redis.values.values())))
    returned = json.dumps(json.loads(arguments)) if normalized else arguments
    assert json.loads(restore_arguments(returned, mapping)) == {'kept': '<DICT_1>'}
    assert restore_text(user['content'], mapping) == text


def test_single_spelling_and_dictionary_ambiguity_behavior_are_preserved():
    result = policy().apply('Альфа-Банк')
    assert result.text == 'Ладога Финанс'
    with pytest.raises(DictionaryPolicyAmbiguousRequestError):
        policy().apply('Альфа-Банк Ладога Финанс')


@pytest.mark.asyncio
@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('tool', [False, True])
async def test_real_hooks_reserve_neutral_literals_and_restore_text_and_tools(reverse, stream, tool):
    g = guard()
    g.dictionary_substitution_policy = policy()
    g._analyze_text = AsyncMock(return_value=[])
    values = ['Альфа-Банк АЛЬФА-БАНК альфа-банк', '<DICT_1>']
    if reverse:
        values.reverse()
    if tool:
        messages = [{'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'previous', 'name': 'Write', 'input': {'content': values[0]}}]},
                    {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'previous', 'content': values[1]}]}]
    else:
        messages = [{'role': 'user', 'content': value} for value in values]
    request = {'stream': stream, 'messages': messages}
    await g.async_pre_call_hook(AUTH, None, request, 'anthropic_messages')
    targets = g._iter_request_text_targets(request)
    wire = '\n'.join(target[key] for target, key in targets)
    assert not re.search(r'alfabank|альфа-банк', wire, re.I)
    assert '<DICT_2>' in wire
    # Echo the actual transformed text fields, without double-encoding JSON arguments.
    expected = '\n'.join(values) if not tool else '\n'.join([
        json.dumps({'content': values[0]}, ensure_ascii=False), values[1],
    ])
    if stream:
        output = [event async for event in g.async_post_call_streaming_iterator_hook(
            AUTH, chunks(events(wire, {'content': wire}, split=1)), request)]
        text, result_tool = collect(output)
    else:
        response = message(wire, {'content': wire})
        await g.async_post_call_success_hook(request, AUTH, response)
        text, result_tool = response['content'][0]['text'], response['content'][1]['input']
    assert text == expected
    assert result_tool['content'] == expected
    assert not g._redis.values
