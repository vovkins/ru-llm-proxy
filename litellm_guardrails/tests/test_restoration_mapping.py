"""The typed Redis envelope is explicit, validated, bounded and backward-readable."""

import json
from unittest.mock import AsyncMock

import pytest

from litellm_guardrails.restoration_mapping import (
    RestorationMapping, decode_mapping, encode_mapping, json_number_spans,
)
from litellm_guardrails.responses_state import (
    MAX_MAPPING_BYTES, ResponsesStateError, ResponsesStateStore, placeholder_counters,
)


def numbers():
    state = RestorationMapping({'<RU_PASSPORT_1>': '122648', '<RU_PASSPORT_2>': '001234'})
    state.add_number('<RU_PASSPORT_1>', '122648')
    return state


def test_typed_envelope_is_explicit_and_old_dictionary_still_reads():
    state = numbers()
    body = encode_mapping(state)
    assert json.loads(body) == {'version': 2, 'values': dict(state), 'json_numbers': ['<RU_PASSPORT_1>']}
    assert decode_mapping(body).json_numbers == {'<RU_PASSPORT_1>'}
    old = decode_mapping(json.dumps(dict(state)))
    assert old == state and not old.json_numbers
    assert json.loads(encode_mapping(old)) == dict(state)


def test_copy_update_and_overwrite_preserve_only_applicable_number_types():
    copied = RestorationMapping(numbers())
    copied.update(numbers())
    assert copied.json_numbers == {'<RU_PASSPORT_1>'}
    assert copied.copy().json_numbers == copied.json_numbers
    assert placeholder_counters(copied) == {'RU_PASSPORT': 2}
    copied.update({'<RU_PASSPORT_1>': 'text'})
    assert not copied.json_numbers
    copied.add_number('<RU_PASSPORT_1>', '122648')
    copied['<RU_PASSPORT_1>'] = 'now a string'
    assert not copied.json_numbers
    assert numbers().json_numbers == {'<RU_PASSPORT_1>'}


@pytest.mark.parametrize('body', [
    [], {'version': 3, 'values': {}, 'json_numbers': []},
    {'version': True, 'values': {}, 'json_numbers': []},
    {'version': 2, 'values': {}, 'json_numbers': ['<RU_PASSPORT_1>']},
    {'version': 2, 'values': {'<RU_PASSPORT_1>': '001234'}, 'json_numbers': ['<RU_PASSPORT_1>']},
    {'version': 2, 'values': {'<RU_PASSPORT_1>': 'NaN'}, 'json_numbers': ['<RU_PASSPORT_1>']},
    {'version': 2, 'values': {'<RU_PASSPORT_1>': '12;13'}, 'json_numbers': ['<RU_PASSPORT_1>']},
    {'version': 2, 'values': {'<RU_PASSPORT_1>': '123'}, 'json_numbers': [['bad']]},
    {'version': 2, 'values': {'<RU_PASSPORT_1>': '123'}, 'json_numbers': ['<RU_PASSPORT_1>'] * 2},
    {'version': 2, 'values': {'<RU_PASSPORT_1>': 123}, 'json_numbers': ['<RU_PASSPORT_1>']},
    {'version': 2, 'values': {}, 'json_numbers': [], 'unknown': True},
    {'version': 2, 'values': {}, 'json_numbers': '<RU_PASSPORT_1>'},
    {'<RU_PASSPORT_1>': 123},
])
def test_corrupt_or_unknown_metadata_never_silently_loses_type(body):
    with pytest.raises(ValueError):
        decode_mapping(json.dumps(body))


def test_history_budget_counts_the_type_metadata_without_raising_quotas():
    state = numbers()
    flat_size = len(encode_mapping(dict(state)).encode())
    state['padding'] = 'x' * (MAX_MAPPING_BYTES - flat_size - len(',"padding":""'))
    assert len(ResponsesStateStore.serialize(dict(state)).encode()) == MAX_MAPPING_BYTES
    with pytest.raises(ResponsesStateError) as failure:
        ResponsesStateStore.serialize(state)
    assert failure.value.status == 413


@pytest.mark.asyncio
async def test_actual_store_saves_and_loads_the_typed_body_without_changing_redis_keys():
    redis = AsyncMock()
    store = ResponsesStateStore(redis, b'local-test', 3600)
    owner = store.owner('trusted-key')
    redis.eval.return_value = ['saved']
    await store.save(owner, 'model', 'response', numbers())
    args = redis.eval.await_args.args
    assert args[1] == 1 and args[2] == 'pii_responses_state:v1:' + owner
    assert args[3] == 'save'
    body = args[6]
    assert json.loads(body)['json_numbers'] == ['<RU_PASSPORT_1>']
    redis.eval.return_value = ['found', body]
    state = await store.load(owner, 'model', 'response')
    assert state == numbers() and state.json_numbers == numbers().json_numbers


@pytest.mark.asyncio
async def test_corrupt_state_load_has_the_existing_closed_error():
    redis = AsyncMock()
    redis.eval.return_value = ['found', '{"version":2,"values":{},"json_numbers":["missing"]}']
    store = ResponsesStateStore(redis, b'local-test', 3600)
    with pytest.raises(ResponsesStateError) as failure:
        await store.load('owner', 'model', 'response')
    assert failure.value.status == 503


def test_source_offsets_ignore_quoted_numbers_and_keep_large_precision():
    source = '{"n":' + '1' * 5000 + ',"s":"122648","e":1e400}'
    spans = json_number_spans(source)
    assert [source[start:end] for start, end in spans] == ['1' * 5000, '1e400']
    assert not json_number_spans('{"n":NaN}')
    assert not json_number_spans('{"n":001234}')
    assert not json_number_spans('Номер паспорта: 122648')
