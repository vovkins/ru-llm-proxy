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


RULE = {
    'id': 'alfabank_to_ladoga_finance', 'source': 'Альфа-Банк',
    'replacement': 'Ладога Финанс',
    'match': {'case_sensitive': False, 'whole_phrase': True}, 'restore': True,
}


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
