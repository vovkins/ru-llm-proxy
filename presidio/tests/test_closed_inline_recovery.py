"""A bounded final fallback for cut entities in complete inline-code values."""
import random
import threading

import pytest

from presidio.entity_types import SUPPORTED_ENTITY_TYPES
from presidio.ner.boundary_values import BoundaryContext, BoundaryValues, MAX_INLINE_VALUE_CHARS
from presidio.ner.text_processing import normalize_for_ner
from presidio.tests.test_ner_window_value_repair import prediction, recognizer


@pytest.mark.parametrize('kind', sorted(SUPPORTED_ENTITY_TYPES))
@pytest.mark.parametrize('cuts', [(0,5,True,False),(3,8,False,True),(2,6,True,True)])
def test_recovery_keeps_all_positive_evidence_inside_the_whole_closed_value(monkeypatch,kind,cuts):
    value='Alpha Meadow'
    source='const rows = [0]; `owner='+value+'` end'
    normalized=normalize_for_ner(source).text
    at=normalized.index(value)
    left,right,left_cut,right_cut=cuts
    instance,_,calls=recognizer(monkeypatch,[prediction(kind,at+left,at+right,left=left_cut,right=right_cut)],[])
    # Exercise the unresolved-only stage. Earlier credential syntax repair may
    # already return a word; this patch deliberately leaves that success intact.
    predictions, output=instance._resolve_boundary_values(
        normalize_for_ner(source),
        [prediction(kind,at+left,at+right,left=left_cut,right=right_cut)], [],
        BoundaryContext(BoundaryValues(source)),
    )
    assert predictions==[] and len(output)==1
    result=output[0]
    assert source[result.start:result.end]==value
    assert result.entity_type==kind
    assert calls==[]
    assert source[result.start-6:result.start]=='owner='
    assert source[result.end:result.end+1]=='`'


@pytest.mark.parametrize('kind',['PERSON','ORGANIZATION'])
def test_full_recognizer_uses_the_new_container_after_legacy_recovery_fails(monkeypatch,kind):
    source='const rows = [0]; `owner=Alpha Meadow` end'
    at=source.index('Alpha')
    instance,_,calls=recognizer(monkeypatch,[prediction(kind,at+2,at+6,left=True,right=True)],[])
    result,=instance.analyze(source)
    assert source[result.start:result.end]=='Alpha Meadow'
    assert calls==[True]


@pytest.mark.parametrize('value,marker', [('Alpha Meadow','`'),('Се\u0301верин 😀','``'),('user_alpha_47','`'),('a:b/c@example.test','`')])
def test_full_source_offsets_are_preserved(value,marker):
    source='const rows = [0]; '+marker+'owner='+value+marker+' end'
    left=source.index(value)
    actual=BoundaryValues(source).resolve(left+1,left+len(value)-1,entity_type='PERSON')
    assert actual==(left,left+len(value))


@pytest.mark.parametrize('tail',[
    '`owner=Alpha Meadow',
    '`owner=Alpha\\Meadow`',
    '`owner=Alpha${suffix}Meadow`',
    '`owner=Alpha\nMeadow`',
    '```\nowner=Alpha Meadow',
    '`unclosed\nother `owner=Alpha Meadow`',
    '`owner='+'A'*(MAX_INLINE_VALUE_CHARS+1)+'`',
])
def test_ambiguous_or_unsupported_code_keeps_the_safe_refusal(tail):
    source='const rows = [0]; '+tail
    value='Alpha' if 'Alpha' in source else 'AAA'
    left=source.index(value)
    assert BoundaryValues(source).resolve(left,left+3,entity_type='PERSON') is None


def test_value_cannot_be_used_to_cover_an_outside_prediction():
    source='const rows = [0]; outside `owner=Alpha Meadow` end'
    left=source.index('outside');right=source.index('Meadow')+len('Meadow')
    assert BoundaryValues(source).resolve(left,right,entity_type='PERSON') is None


def test_existing_complete_literals_and_fences_keep_their_original_container():
    source='const row = {"name":"Alpha Meadow"};'
    left=source.index('Alpha')
    assert BoundaryValues(source).resolve(left+2,left+6,entity_type='PERSON')==(left,left+len('Alpha Meadow'))
    source='before\n```\nAlpha Meadow\n```\nafter'
    left=source.index('Alpha')
    assert BoundaryValues(source).resolve(left+2,left+6,entity_type='PERSON')==(left,left+len('Alpha Meadow'))


def test_independent_values_do_not_merge_across_their_delimiters():
    source='const rows=[0]; `owner=Alpha Meadow` and `owner=Beta River` end'
    values=BoundaryValues(source)
    for word in ('Alpha Meadow','Beta River'):
        left=source.index(word)
        assert values.resolve(left+1,left+4,entity_type='PERSON')==(left,left+len(word))


def test_seeded_source_container_properties():
    rng=random.Random(2026111219)
    for _ in range(750):
        value='X'+''.join(rng.choice('abcXYZ012_ -:;😀яё\u0301') for _ in range(rng.randint(2,120)))+'Y'
        marker='`'*rng.randint(1,3)
        key='cfg.'+''.join(rng.choice('abcXYZ_012') for _ in range(rng.randint(2,20)))
        assignment=key+' = '
        source='const rows=[0]; '+marker+assignment+value+marker+' end'
        left=source.index(marker)+len(marker)+len(assignment)
        a=rng.randrange(len(value));b=rng.randint(a+1,len(value))
        bounds=BoundaryValues(source).resolve(left+a,left+b,entity_type='PERSON')
        assert bounds==(left,left+len(value))
        assert bounds[0]<=left+a<left+b<=bounds[1]
        masked=source[:bounds[0]]+'<PERSON_1>'+source[bounds[1]:]
        assert masked.replace('<PERSON_1>',source[slice(*bounds)]).encode()==source.encode()


def test_plain_names_in_inline_markup_are_not_a_code_value_boundary():
    source='Notes [1] from a review (text `Alice` afterwards)'
    at=source.index('Alice')
    assert BoundaryValues(source).resolve(at,at+5,entity_type='PERSON') is None
