"""Dictionary exclusions must not discard surrounding positive NER evidence."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import litellm
import pytest

from litellm_guardrails.pii_guardrail import RuPIIGuardrail
from litellm_guardrails.restoration_mapping import RestorationMapping
from litellm_guardrails.streaming_restoration import restore_text, restore_arguments


def guard():
    return RuPIIGuardrail(dictionary_substitutions_enabled=True, dictionary_substitutions_file='',
        dictionary_substitutions_json=json.dumps({'substitutions': [
            {'id': 'synthetic', 'source': 'SourceBank', 'replacement': 'Northern Trust'},
        ]}))


@pytest.mark.parametrize('exclusions,expected', [
    ((), [(0, 10, 'PERSON')]),
    (((0, 10),), []),
    (((0, 4),), [(4, 10, 'PERSON')]),
    (((7, 10),), [(0, 7, 'PERSON')]),
    (((3, 6),), [(0, 3, 'PERSON'), (6, 10, 'PERSON')]),
    (((7, 9), (2, 4)), [(0, 2, 'PERSON'), (4, 7, 'PERSON'), (9, 10, 'PERSON')]),
    (((1, 5), (3, 7), (7, 8)), [(0, 1, 'PERSON'), (8, 10, 'PERSON')]),
    (((12, 14),), [(0, 10, 'PERSON')]),
])
def test_only_the_excluded_ranges_are_subtracted(exclusions, expected):
    instance = guard()
    result = instance._normalize_entities('abcdefghijklmn', [{'entity_type': 'PERSON', 'start': 0, 'end': 10}],
                                          exclusion_spans=exclusions)
    assert result == expected


@pytest.mark.parametrize('source', [
    'Ivan SourceBank Petrov',
    '{"value":"Ivan SourceBank Petrov","ticket":123456}',
    '{"value":"Ivan\\n SourceBank\\\"Petrov","ticket":123456}',
])
def test_mask_and_exact_restore_keep_dictionary_and_both_sides(source):
    instance = guard()
    replacement = instance.dictionary_substitution_policy.apply(source)
    assert replacement.substitution_count == 1
    left = replacement.text.index('Ivan')
    right = replacement.text.index('Petrov') + len('Petrov')
    entities = [{'entity_type': 'PERSON', 'start': left, 'end': right, 'score': .8}]
    masked, mapping = instance._mask_text(replacement.text, entities,
                                         exclusion_spans=replacement.replacement_spans)
    assert 'Ivan' not in masked and 'Petrov' not in masked
    assert 'Northern Trust' in masked
    assert len(mapping) == 2
    combined = RestorationMapping(replacement.mapping)
    combined.update(mapping)
    assert restore_text(masked, combined) == source
    if source.startswith('{'):
        assert restore_arguments(masked, combined) == source
        assert json.loads(masked)['ticket'] == 123456
    else:
        assert json.loads(restore_arguments(json.dumps(masked), combined)) == source


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['mask', 'block'])
async def test_broad_finding_is_not_ignored_in_mask_or_block(mode):
    instance = guard()
    instance.pii_mode = mode
    instance._redis = AsyncMock()
    instance._redis.get.return_value = None
    transformed = 'Ivan Northern Trust Petrov'
    entities = [{'entity_type': 'PERSON', 'start': 0, 'end': len(transformed), 'score': .8}]
    data = {'messages': [{'role': 'user', 'content': 'Ivan SourceBank Petrov'}]}
    with patch.object(instance, '_analyze_text', return_value=entities):
        if mode == 'block':
            with pytest.raises(litellm.UnprocessableEntityError):
                await instance.async_pre_call_hook(MagicMock(), MagicMock(), data)
            instance._redis.setex.assert_not_awaited()
        else:
            result = await instance.async_pre_call_hook(MagicMock(), MagicMock(), data)
            assert result['messages'][0]['content'] == '<PERSON_1>Northern Trust<PERSON_2>'


def test_dictionary_only_finding_stays_excluded():
    instance = guard()
    transformed = instance.dictionary_substitution_policy.apply('SourceBank')
    masked, mapping = instance._mask_text(transformed.text,
        [{'entity_type': 'ORGANIZATION', 'start': 0, 'end': len(transformed.text)}],
        exclusion_spans=transformed.replacement_spans)
    assert masked == 'Northern Trust' and not mapping
