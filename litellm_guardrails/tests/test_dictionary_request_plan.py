"""Request-local collision resolution with literal preservation and native coverage."""
import asyncio
import copy
import json
import re
from unittest.mock import AsyncMock

import pytest

from litellm_guardrails.dictionary_policy import DictionarySubstitutionPolicy, DictionaryPolicyAmbiguousRequestError
from litellm_guardrails import pii_guardrail as module
from litellm_guardrails.pii_guardrail import RuPIIGuardrail
from litellm_guardrails.restoration_mapping import RestorationMapping
from litellm_guardrails.streaming_restoration import restore_text
from litellm_guardrails.tests.test_anthropic_restoration import AUTH, RULES, guard, message, events, chunks, collect
from presidio.entity_types import SUPPORTED_ENTITY_TYPES


def enabled():
    g = guard()
    g.dictionary_collision_resolution = 'unique_aliases'
    return g


@pytest.mark.parametrize('values', [
    ['ExampleBranddirect ladogadirect01'],
    ['ladogadirect01 ExampleBranddirect'],
    ['ExampleBranddirect', 'ladogadirect01'],
    ['ladogadirect01', 'ExampleBranddirect'],
    ['Compare ExampleBranddirect and ladogadirect01 as independent names.'],
    ["import x from '@terminal/ExampleBranddirect/provider/react';", "Cannot find module '@terminal/ladogadirect01/provider/react'"],
])
@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.asyncio
async def test_mixed_literals_are_allowed_and_roundtrip_without_guessing(values, stream):
    g = enabled()
    data = {'stream':stream,'messages':[{'role':'user','content':x} for x in values]}
    await g.async_pre_call_hook(AUTH,None,data,'anthropic_messages')
    wire = '\n'.join(x['content'] for x in data['messages'])
    assert 'ExampleBranddirect' not in wire and 'ladogadirect01' in wire
    if stream:
        output=[x async for x in g.async_post_call_streaming_iterator_hook(AUTH,chunks(events(wire,{'content':wire},split=3)),data)]
        restored,tool=collect(output)
    else:
        response=message(wire,{'content':wire});await g.async_post_call_success_hook(data,AUTH,response)
        restored=response['content'][0]['text'];tool=response['content'][1]['input']
    assert restored=='\n'.join(values) and tool['content']==restored
    assert not g._redis.values


def test_same_spelling_has_one_stable_alias_across_all_fields():
    policy=DictionarySubstitutionPolicy.from_config(RULES)
    texts=['ExampleBranddirect ExampleBranddirect','ladogadirect01','ExampleBranddirect']
    plan=policy.plan_request(texts)
    outputs=[policy.apply(x,request_plan=plan) for x in texts]
    token=plan.aliases['package','ExampleBranddirect']
    assert outputs[0].text==token+' '+token and outputs[2].text==token
    assert re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',token)
    assert all(restore_text(x.text,x.mapping)==old for x,old in zip(outputs,texts))
    with pytest.raises(TypeError):plan.aliases['package','ExampleBranddirect']='broken'


def test_case_variants_remain_distinct_but_repeated_variants_are_stable():
    config=copy.deepcopy(RULES);config['substitutions'][0]['match']['case_sensitive']=False
    policy=DictionarySubstitutionPolicy.from_config(config)
    text='ExampleBranddirect EXAMPLEBRANDDIRECT examplebranddirect EXAMPLEBRANDDIRECT ladogadirect01'
    plan=policy.plan_request([text]);out=policy.apply(text,request_plan=plan)
    assert len(plan.aliases)==3 and len(set(plan.aliases.values()))==3
    assert out.text.split()[1]==out.text.split()[3]
    assert restore_text(out.text,out.mapping)==text


@pytest.mark.asyncio
async def test_json_keys_and_values_keep_exact_provenance():
    g=enabled()
    obj={'ExampleBranddirect':{'content':'ExampleBranddirect ladogadirect01'}}
    data={'messages':[{'role':'assistant','content':[{'type':'tool_use','id':'old','name':'Write','input':copy.deepcopy(obj)}]},{'role':'user','content':[{'type':'tool_result','tool_use_id':'old','content':'ok'}]}]}
    await g.async_pre_call_hook(AUTH,None,data,'anthropic_messages')
    wire=data['messages'][0]['content'][0]['input']
    response=message('',wire);await g.async_post_call_success_hook(data,AUTH,response)
    assert response['content'][1]['input']==obj
    assert not g._redis.values


def test_decoded_json_literals_and_unmasked_schema_strings_reserve_fresh_aliases():
    policy=DictionarySubstitutionPolicy.from_config(RULES)
    initial=policy.plan_request(['ExampleBranddirect ladogadirect01'])
    first=initial.aliases['package','ExampleBranddirect']
    escaped=''.join('\\u'+format(ord(c),'04x') for c in first)
    reference='{"literal":"'+escaped+'"}'
    plan=policy.plan_request(['ExampleBranddirect'],reference_texts=['ladogadirect01',reference])
    assert plan.aliases['package','ExampleBranddirect']!=first
    result=policy.apply('ExampleBranddirect',request_plan=plan)
    assert restore_text(reference+' '+result.text,result.mapping)==reference+' ExampleBranddirect'


@pytest.mark.parametrize('kind',sorted(SUPPORTED_ENTITY_TYPES))
@pytest.mark.asyncio
async def test_every_independent_native_type_remains_masked(kind):
    g=enabled();value='nativeValueZ929'
    async def analyze(text,*args):
        at=text.index(value)
        return [{'start':at,'end':at+len(value),'entity_type':kind,'score':1.0}]
    g._analyze_text=AsyncMock(side_effect=analyze)
    original='ExampleBranddirect ladogadirect01 '+value
    data={'messages':[{'role':'user','content':original}]}
    await g.async_pre_call_hook(AUTH,None,data,'anthropic_messages')
    wire=data['messages'][0]['content'];assert value not in wire and 'ExampleBranddirect' not in wire
    response=message(wire);await g.async_post_call_success_hook(data,AUTH,response)
    assert response['content'][0]['text']==original


@pytest.mark.asyncio
async def test_signed_thinking_is_unchanged():
    g=enabled();signed={'type':'thinking','thinking':'ladogadirect01 literal','signature':'opaque-signature'}
    data={'messages':[{'role':'assistant','content':[copy.deepcopy(signed)]},{'role':'user','content':'ExampleBranddirect ladogadirect01'}]}
    await g.async_pre_call_hook(AUTH,None,data,'anthropic_messages')
    assert data['messages'][0]['content'][0]==signed


@pytest.mark.asyncio
async def test_concurrent_requests_do_not_share_alias_plans():
    g=enabled()
    async def analyze(text,*args):await asyncio.sleep(0);return []
    g._analyze_text=AsyncMock(side_effect=analyze)
    async def roundtrip(value):
        data={'messages':[{'role':'user','content':value}]}
        await g.async_pre_call_hook(AUTH,None,data,'anthropic_messages')
        wire=data['messages'][0]['content'];response=message(wire)
        await g.async_post_call_success_hook(data,AUTH,response)
        assert response['content'][0]['text']==value
    await asyncio.gather(roundtrip('ExampleBranddirect ladogadirect01'),roundtrip('Alfa literal ExampleBranddirect ladogadirect01'))
    assert module._DICTIONARY_REQUEST_PLAN.get() is None and not g._redis.values


def test_plan_cannot_be_reused_with_other_policy_or_new_original():
    policy=DictionarySubstitutionPolicy.from_config(RULES);plan=policy.plan_request(['ExampleBranddirect ladogadirect01'])
    other=DictionarySubstitutionPolicy.from_config(RULES)
    with pytest.raises(DictionaryPolicyAmbiguousRequestError):other.apply('ExampleBranddirect',request_plan=plan)
    with pytest.raises(DictionaryPolicyAmbiguousRequestError):policy.apply('ExampleBrandDirect',request_plan=plan)


def test_resource_limits_fail_closed_without_mutating_input(monkeypatch):
    from litellm_guardrails import dictionary_request_plan as planner
    policy=DictionarySubstitutionPolicy.from_config(RULES)
    monkeypatch.setattr(planner,'MAX_ALLOCATION_ATTEMPTS',0)
    with pytest.raises(DictionaryPolicyAmbiguousRequestError):policy.plan_request(['ExampleBranddirect ladogadirect01'])


def test_invalid_resolution_mode_is_rejected():
    with pytest.raises(ValueError):RuPIIGuardrail(dictionary_collision_resolution='ignore')


@pytest.mark.asyncio
async def test_strict_default_retains_old_rejection_behavior():
    g=guard();data={'messages':[{'role':'user','content':'ExampleBranddirect ladogadirect01'}]}
    with pytest.raises(Exception) as error:await g.async_pre_call_hook(AUTH,None,data,'anthropic_messages')
    assert error.value.status_code==422


@pytest.mark.asyncio
async def test_future_dictionary_mask_is_reserved_before_native_allocation():
    g=enabled()
    g.dictionary_substitution_policy=DictionarySubstitutionPolicy.from_config({'substitutions':[
        {'id':'company','source':'ExampleBranddirect','replacement':'<PERSON_1>'}]})
    async def analyze(text,*args):
        return [{'start':m.start(),'end':m.end(),'entity_type':'PERSON','score':1.0} for m in re.finditer('Alice',text)]
    g._analyze_text=AsyncMock(side_effect=analyze)
    data={'messages':[{'role':'user','content':'Alice'},{'role':'user','content':'ExampleBranddirect'}]}
    await g.async_pre_call_hook(AUTH,None,data,'anthropic_messages')
    assert [m['content'] for m in data['messages']]==['<PERSON_2>','<PERSON_1>']
    response=message(' '.join(m['content'] for m in data['messages']))
    await g.async_post_call_success_hook(data,AUTH,response)
    assert response['content'][0]['text']=='Alice ExampleBranddirect'


@pytest.mark.asyncio
async def test_numeric_tool_value_keeps_its_type_with_a_dictionary_collision():
    g=enabled();original={'company':'ExampleBranddirect ladogadirect01','passport':1234567890}
    async def analyze(text,*args):
        return [{'start':m.start(),'end':m.end(),'entity_type':'RU_PASSPORT','score':1.0} for m in re.finditer('1234567890',text)]
    g._analyze_text=AsyncMock(side_effect=analyze)
    data={'messages':[{'role':'assistant','content':[{'type':'tool_use','id':'old','name':'Write','input':copy.deepcopy(original)}]},{'role':'user','content':[{'type':'tool_result','tool_use_id':'old','content':'ok'}]}]}
    await g.async_pre_call_hook(AUTH,None,data,'anthropic_messages')
    wire=data['messages'][0]['content'][0]['input'];assert wire['passport']!=1234567890
    response=message('',wire);await g.async_post_call_success_hook(data,AUTH,response)
    assert response['content'][1]['input']==original and type(response['content'][1]['input']['passport']) is int


@pytest.mark.asyncio
async def test_analyzer_failure_keeps_request_and_cleans_plan_context():
    g=enabled();g._analyze_text=AsyncMock(side_effect=RuntimeError('controlled failure'))
    data={'messages':[{'role':'user','content':'ExampleBranddirect ladogadirect01'}]};before=copy.deepcopy(data)
    with pytest.raises(Exception):await g.async_pre_call_hook(AUTH,None,data,'anthropic_messages')
    assert data['messages']==before['messages'] and not g._redis.values
    assert module._DICTIONARY_REQUEST_PLAN.get() is None


@pytest.mark.asyncio
async def test_unavailable_redis_cannot_send_unrestorable_request():
    g=enabled();g._redis.setex=AsyncMock(side_effect=RuntimeError('controlled storage failure'))
    data={'messages':[{'role':'user','content':'ExampleBranddirect ladogadirect01'}]}
    with pytest.raises(Exception):await g.async_pre_call_hook(AUTH,None,data,'anthropic_messages')
    assert data['messages'][0]['content']=='ExampleBranddirect ladogadirect01'
    assert module._DICTIONARY_REQUEST_PLAN.get() is None


def test_unmatched_request_does_not_add_new_reference_limits(monkeypatch):
    from litellm_guardrails import dictionary_request_plan as planner
    policy=DictionarySubstitutionPolicy.from_config(RULES)
    monkeypatch.setattr(planner,'MAX_REFERENCE_CHARACTERS',1)
    plan=policy.plan_request(['ordinary text'],reference_texts=['ordinary reference'])
    assert not plan.aliases and policy.apply('ordinary text',request_plan=plan).text=='ordinary text'


def test_invalid_reference_is_rejected_when_allocation_is_needed():
    policy=DictionarySubstitutionPolicy.from_config(RULES)
    with pytest.raises(DictionaryPolicyAmbiguousRequestError):policy.plan_request(['ExampleBranddirect'],reference_texts=[None])
