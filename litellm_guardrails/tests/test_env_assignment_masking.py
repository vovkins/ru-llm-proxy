"""Opt-in ENV54: actual hooks, syntax, restoration and dependency failures."""
import asyncio
import copy
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock
import httpx
import litellm
import pytest
from litellm_guardrails import pii_guardrail as base
from litellm_guardrails.env_assignment_masking import MASKED_ENV_NAMES, RuEnvAssignmentGuardrail
from litellm_guardrails.tests.test_pii_guardrail import _MemoryRedis

FORMATS = {
    'env': lambda k, v: f'{k}={v}',
    'quoted': lambda k, v: f'{k}="{v}"',
    'export': lambda k, v: f'export {k}="{v}"',
    'yaml': lambda k, v: f'{k}: "{v}"',
    'json': lambda k, v: json.dumps({k: v}),
    'list': lambda k, v: f'environment:\n  - {k}={v}',
}

def guard(**kwargs):
    g = RuEnvAssignmentGuardrail(dictionary_substitutions_enabled=False, **kwargs)
    g._redis = _MemoryRedis()
    g._analysis_cache_secret = None
    g._analyze_text = AsyncMock(return_value=[])
    return g

async def process(g, text=None, body=None):
    data = body or {'model': 'local', 'messages': [{'role': 'user', 'content': text}]}
    original = copy.deepcopy(data)
    auth = SimpleNamespace(token='local-scope', api_key='local-key', user_id='local')
    result = await g.async_pre_call_hook(auth, None, data)
    assert data == original
    assert isinstance(result, dict)
    return result, auth

async def echo(g, data, auth, content):
    response = litellm.ModelResponse(choices=[litellm.Choices(index=0,
        message=litellm.Message(role='assistant', content=content), finish_reason='stop')])
    await g.async_post_call_success_hook(data, auth, response)
    assert not any(k.startswith('pii_mapping:') for k in g._redis.values)
    return response.choices[0].message.content

@pytest.mark.asyncio
@pytest.mark.parametrize('key', sorted(MASKED_ENV_NAMES))
@pytest.mark.parametrize('fmt', list(FORMATS))
@pytest.mark.parametrize('value', ['N7vQ2rZ8kL5m', 'Q7!v@R2+k=9#z'])
async def test_all_54_values_hidden_and_restored(key, fmt, value):
    g = guard()
    text = FORMATS[fmt](key, value)
    data, auth = await process(g, text)
    masked = data['messages'][0]['content']
    assert value not in masked
    assert masked.startswith(text[:text.index(value)])
    assert masked.endswith(text[text.index(value) + len(value):])
    assert 'ENV_MASK_V1_' in masked and key in masked
    if fmt == 'json':
        assert list(json.loads(masked)) == [key]
    if fmt in {'env', 'quoted', 'export'}:
        syntax = subprocess.run(['/bin/sh', '-n'], input=masked, text=True, capture_output=True)
        assert syntax.returncode == 0, syntax.stderr
    assert await echo(g, data, auth, masked) == text

@pytest.mark.asyncio
@pytest.mark.parametrize('text', [
    'PWD=/Users/example/project', 'PWD=x', 'PWD=1234',
    'PWD="/Users/example/My Project"', r'PWD=C:\Users\example\project',
    r'{"PWD":"C:\\Users\\example"}', 'PWD="escaped\\\"quote"',
    "PASS='literal#secret'", 'PWD=/looks/like/a/password',
])
async def test_paths_short_passwords_and_escaping(text):
    g = guard()
    data, auth = await process(g, text)
    masked = data['messages'][0]['content']
    if text.startswith('{'): json.loads(masked)
    assert 'ENV_MASK_V1_' in masked
    assert await echo(g, data, auth, masked) == text

@pytest.mark.asyncio
@pytest.mark.parametrize('text', [
    'PWD=$(get-secret)', 'PWD=${REAL_VALUE}', 'PWD="line1\nline2"',
    'PWD=secret; echo other', 'PWD=secret | cat', '{"PWD":1234}',
    "config = {'PWD': 'literal-secret'}", 'PASSWORD: |\n  raw-secret',
    'PASSWORD: >-\n  raw-secret', 'PASSWORD: &anchor',
    'APP_TOKEN_999=literal-secret', 'SERVICE_ENDPOINT=https://user:pass@example.org',
    'Traceback (most recent call last):\nPWD=/tmp/project',
    'server {\nlisten 80;\nserver_name local.example;\n}',
])
async def test_unsupported_or_other_policy_remains_blocked(text):
    g = guard()
    with pytest.raises(Exception) as caught: await process(g, text)
    assert getattr(caught.value, 'status_code', None) == 422
    g._analyze_text.assert_not_awaited()

@pytest.mark.asyncio
async def test_default_class_unchanged():
    g = base.RuPIIGuardrail(dictionary_substitutions_enabled=False)
    g._analyze_text = AsyncMock(return_value=[])
    with pytest.raises(Exception) as caught:
        await g.async_pre_call_hook(SimpleNamespace(), None,
            {'model': 'local', 'messages': [{'role': 'user', 'content': 'PWD=/tmp/project'}]})
    assert getattr(caught.value, 'status_code', None) == 422
    g._analyze_text.assert_not_awaited()

@pytest.mark.asyncio
async def test_all_54_together_no_alias_prefix_collision():
    text = '\n'.join(f'{key}=value{n}z' for n, key in enumerate(sorted(MASKED_ENV_NAMES)))
    g = guard()
    data, auth = await process(g, text)
    masked = data['messages'][0]['content']
    assert masked.count('ENV_MASK_V1_') == 54
    assert await echo(g, data, auth, masked) == text

@pytest.mark.asyncio
async def test_native_protection_survives_and_alias_is_not_reinterpreted():
    g = guard()
    async def analyze(text):
        a, p = text.index('ENV_MASK_V1_'), text.index('Олег Волков')
        return [{'start': a+2, 'end': a+5, 'entity_type': 'PASSWORD', 'score': .9},
            {'start': p, 'end': p+len('Олег Волков'), 'entity_type': 'PERSON', 'score': .9}]
    g._analyze_text = analyze
    text = 'PWD=/tmp/project\n# Олег Волков'
    data, auth = await process(g, text)
    masked = data['messages'][0]['content']
    assert 'ENV_MASK_V1_' in masked and 'Олег Волков' not in masked
    assert await echo(g, data, auth, masked) == text

@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['analyzer', 'redis'])
async def test_fail_open_cannot_discard_mapping(failure):
    g = guard(failure_mode='fail_open', dictionary_substitutions_failure_mode='fail_open')
    if failure == 'analyzer': g._analyze_text = AsyncMock(side_effect=RuntimeError('private error'))
    else: g._redis.setex = AsyncMock(side_effect=RuntimeError('private redis error'))
    with pytest.raises(Exception) as caught: await process(g, 'JIRA_TOKEN=literal-secret')
    assert getattr(caught.value, 'status_code', None) == 503

@pytest.mark.asyncio
@pytest.mark.parametrize('where', ['message', 'extra_body'])
async def test_caller_aliases_not_trusted(where):
    body = {'model': 'local', 'messages': [{'role': 'user', 'content': 'PWD=literal-secret'}]}
    if where == 'message': body['messages'][0]['content'] += '\nENV_MASK_V1_1'
    else: body['extra_body'] = {'unsafe': 'PWD=ENV_MASK_V1_1raw-secret'}
    g = guard()
    with pytest.raises(Exception) as caught: await process(g, body=body)
    assert getattr(caught.value, 'status_code', None) == 422

@pytest.mark.asyncio
async def test_history_tools_system_dictionary():
    g = RuEnvAssignmentGuardrail(dictionary_substitutions_enabled=True,
        dictionary_substitutions_json=json.dumps({'substitutions': [
            {'id': 'local', 'source': 'ExampleCorp', 'replacement': 'NeutralCorp'}]}))
    g._redis = _MemoryRedis(); g._analysis_cache_secret = None; g._analyze_text = AsyncMock(return_value=[])
    body = {'model': 'local', 'system': 'PWD=/system/path', 'messages': [
        {'role': 'user', 'content': 'JIRA_TOKEN=token-one-777'},
        {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'local',
            'content': [{'type': 'text', 'text': 'PASS=pass-two-777\nExampleCorp'}]}]},
        {'role': 'assistant', 'tool_calls': [{'type': 'function', 'id': 'local',
            'function': {'name': 'local', 'arguments': '{"DB_PASS":"pass-three-777"}'}}]},
    ]}
    original = '\n'.join(t[f] for t, f in g._iter_request_text_targets(body))
    data, auth = await process(g, body=body)
    masked = '\n'.join(t[f] for t, f in g._iter_request_text_targets(data))
    for v in ['/system/path', 'token-one-777', 'pass-two-777', 'pass-three-777', 'ExampleCorp']:
        assert v not in masked
    json.loads(data['messages'][2]['tool_calls'][0]['function']['arguments'])
    assert await echo(g, data, auth, masked) == original

@pytest.mark.asyncio
async def test_streaming_split_alias_and_cleanup():
    g = guard(); text = 'JIRA_TOKEN=token-for-stream-777'
    data, auth = await process(g, text)
    masked = data['messages'][0]['content']
    async def chunks():
        for char in masked:
            yield litellm.ModelResponseStream(choices=[litellm.StreamingChoices(index=0, delta={'content': char})])
        yield litellm.ModelResponseStream(choices=[litellm.StreamingChoices(index=0, delta={'content': ''}, finish_reason='stop')])
    output = []
    async for chunk in g.async_post_call_streaming_iterator_hook(auth, chunks(), data):
        output.append(chunk.choices[0].delta.content or '')
    assert ''.join(output) == text
    assert not g._redis.values

@pytest.mark.asyncio
async def test_cache_and_concurrent_scopes(monkeypatch):
    posts = []
    class Client:
        async def get(self, url, **kwargs):
            return httpx.Response(200, json={'status': 'ok', 'ner_state': 'ready',
                'analysis_signature': 'a'*64}, request=httpx.Request('GET', url))
        async def post(self, url, *, json, **kwargs):
            posts.append(json['text'])
            return httpx.Response(200, json={'entities': []}, request=httpx.Request('POST', url))
    monkeypatch.setattr(base, '_get_shared_analyzer_http_client', lambda: Client())
    g = RuEnvAssignmentGuardrail(dictionary_substitutions_enabled=False)
    g._redis = _MemoryRedis(); g._analysis_cache_secret = b'local-cache-key'
    for value in ['first-secret-777', 'second-secret-888']:
        text = 'JIRA_TOKEN='+value
        data, auth = await process(g, text)
        assert await echo(g, data, auth, data['messages'][0]['content']) == text
    assert len(posts) == 1
    async def isolated(value):
        h = guard()
        async def backend(_):
            await asyncio.sleep(0); return []
        h._analyze_text = backend
        text = 'DB_PASS='+value
        data, auth = await process(h, text)
        assert await echo(h, data, auth, data['messages'][0]['content']) == text
    await asyncio.gather(isolated('concurrent-one'), isolated('concurrent-two'))

def test_name_set_is_fixed():
    assert len(MASKED_ENV_NAMES) == 54
    assert {'PWD', 'PASS', 'JIRA_TOKEN', 'DB_PASS'} <= MASKED_ENV_NAMES
    assert 'HOME' not in MASKED_ENV_NAMES


@pytest.mark.asyncio
@pytest.mark.parametrize('diagnostic', [
    'SOME_ENV_MASK_V1_1_PASSWORD=raw-secret-777',
    'JIRA_TOKEN=ENV_MASK_V1_1raw-secret-777',
    'JIRA_TOKEN="ENV_MASK_V1_1"raw-secret-777',
])
async def test_alias_substrings_cannot_hide_raw_extra_fields(diagnostic):
    g = guard()
    body = {'model': 'local', 'messages': [{'role': 'user', 'content': 'PWD=protected-secret'}],
        'extra_body': {'diagnostic': diagnostic}}
    with pytest.raises(Exception) as caught: await process(g, body=body)
    assert getattr(caught.value, 'status_code', None) == 422


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['pre_egress_policy_mode', 'final_payload_leak_check_mode'])
async def test_env_masks_require_both_checks(mode):
    g = guard(**{mode: 'off'})
    with pytest.raises(Exception) as caught: await process(g, 'PWD=protected-secret')
    assert getattr(caught.value, 'status_code', None) == 503


@pytest.mark.asyncio
async def test_pii_block_mode_is_not_relaxed():
    g = guard(pii_mode='block')
    with pytest.raises(Exception) as caught: await process(g, 'PWD=protected-secret')
    assert getattr(caught.value, 'status_code', None) == 422

# Read the public contract without importing Analyzer/model dependencies.
import runpy
from pathlib import Path
SUPPORTED_TYPES = runpy.run_path(str(Path(__file__).resolve().parents[2] / 'presidio/entity_types.py'))['SUPPORTED_ENTITY_TYPES']

@pytest.mark.asyncio
@pytest.mark.parametrize('entity_type', sorted(SUPPORTED_TYPES))
async def test_all_29_entity_types_outside_alias_remain_protected(entity_type):
    g = guard()
    sentinel = 'nativeSensitiveValue7'
    async def analyze(text):
        start = text.index(sentinel)
        return [{'start': start, 'end': start+len(sentinel), 'entity_type': entity_type, 'score': .99}]
    g._analyze_text = analyze
    text = 'PWD=/tmp/project\n# '+sentinel
    data, auth = await process(g, text)
    masked = data['messages'][0]['content']
    assert sentinel not in masked and '/tmp/project' not in masked
    assert await echo(g, data, auth, masked) == text

@pytest.mark.asyncio
@pytest.mark.parametrize('text', [
    'HOME=/Users/example', 'OLDPWD=/tmp/old', 'PATH=/usr/bin:/bin',
    'TOKEN_COUNT=4096', 'TOKEN_TYPE=Bearer', 'PASSWORD_LENGTH=16',
    'KEYBOARD=enabled', 'MAX_TOKENS=2048', 'PASSWORD_POLICY=strict',
])
async def test_ordinary_fields_not_in_fixed_list_are_unchanged(text):
    g = guard()
    data, _ = await process(g, text)
    assert data['messages'][0]['content'] == text
    assert not g._redis.values


@pytest.mark.asyncio
@pytest.mark.parametrize('body', [
    {'model': 'local', 'messages': [{'role': 'user', 'content': 'PASS=literal-secret # repeats literal-secret'}]},
    {'model': 'local', 'messages': [{'role': 'user', 'content': 'JIRA_TOKEN=literal-secret'}, {'role': 'tool', 'content': 'literal-secret'}]},
    {'model': 'local', 'messages': [{'role': 'user', 'content': 'JIRA_TOKEN=literal-secret'}], 'extra_body': {'echo': 'literal-secret'}},
    {'model': 'local', 'messages': [{'role': 'user', 'content': '{"PWD":"p\\u0061ssword777"}'}, {'role': 'tool', 'content': 'password777'}]},
])
async def test_known_values_cannot_escape_in_other_locations(body):
    g = guard()
    with pytest.raises(Exception) as caught: await process(g, body=body)
    assert getattr(caught.value, 'status_code', None) == 422
    assert not g._redis.values


@pytest.mark.asyncio
async def test_backend_cannot_make_json_invalid_or_change_keys():
    g = guard()
    async def backend(text):
        start = text.index('ENV_MASK_V1_')
        return [{'start': start-2, 'end': len(text), 'entity_type': 'PERSON', 'score': .99}]
    g._analyze_text = backend
    with pytest.raises(Exception) as caught: await process(g, '{"PWD":"secret-json-value"}')
    assert getattr(caught.value, 'status_code', None) == 422
    assert not g._redis.values


@pytest.mark.asyncio
async def test_alias_like_substring_inside_actual_secret_does_not_hide_its_repeat():
    g = guard()
    value = 'prefix_ENV_MASK_V1_1_suffix'
    body = {'model': 'local', 'messages': [{'role': 'user', 'content': 'PASS='+value}],
        'extra_body': {'echo': value}}
    with pytest.raises(Exception) as caught: await process(g, body=body)
    assert getattr(caught.value, 'status_code', None) == 422
    assert not g._redis.values


@pytest.mark.asyncio
async def test_responses_continuation_never_overwrites_prior_env_values(monkeypatch):
    from litellm_guardrails.tests.test_responses_history import MemoryStore, response
    monkeypatch.setattr(base, 'LITELLM_SALT_KEY', 'local-state-test-salt')
    g = guard()
    store = MemoryStore()
    g._responses_state_store = AsyncMock(return_value=store)
    auth = SimpleNamespace(token='local-trusted-scope')
    first = {'model': 'local', 'input': 'JIRA_TOKEN=first-env-secret'}
    processed = await g.async_pre_call_hook(auth, None, first, 'aresponses')
    assert processed['input'] == 'JIRA_TOKEN=ENV_MASK_V1_1'
    r1 = response('resp-env-first', 'ENV_MASK_V1_1')
    await g.async_post_call_success_hook(processed, auth, r1)
    assert r1['output'][0]['content'][0]['text'] == 'first-env-secret'
    second = {'model': 'local', 'input': 'JIRA_TOKEN=second-env-secret',
        'previous_response_id': 'resp-env-first'}
    processed = await g.async_pre_call_hook(auth, None, second, 'aresponses')
    assert processed['input'] == 'JIRA_TOKEN=ENV_MASK_V1_2'
    r2 = response('resp-env-second', 'ENV_MASK_V1_1 / ENV_MASK_V1_2')
    await g.async_post_call_success_hook(processed, auth, r2)
    assert r2['output'][0]['content'][0]['text'] == 'first-env-secret / second-env-secret'
    previous = await store.load(store.owner(auth.token), 'local', 'resp-env-first')
    assert previous == {'ENV_MASK_V1_1': 'first-env-secret'}
    third = {'model': 'local', 'input': 'PASS=third-env-secret\nfirst-env-secret',
        'previous_response_id': 'resp-env-second'}
    with pytest.raises(Exception) as caught:
        await g.async_pre_call_hook(auth, None, third, 'aresponses')
    assert getattr(caught.value, 'status_code', None) == 422
