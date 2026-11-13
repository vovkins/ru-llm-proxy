"""Additional bounded name forms in explicit company fields, not prose."""
import json
import random
import pytest
from presidio_analyzer import RecognizerResult
from presidio.entity_types import SUPPORTED_ENTITY_TYPES
from presidio.recognizers.organization_fields import organization_field_spans,add_organization_field_coverage

NAMES=['Аквамарин','Northwind','Northwind Labs','Контур24','«Регион 42»',
       '«星联科技»','"Orion-24"','“Northwind & Partners”','„Медный Остров“',
       '«O\'Connor & Co.»',"'Горизонт'",'«ООО»']

@pytest.mark.parametrize('name',NAMES)
@pytest.mark.parametrize('form',['ООО','АО','ПАО','ОАО','ЗАО'])
@pytest.mark.parametrize('layout',['plain','json-unicode','json-ascii'])
def test_supported_name_forms_keep_the_legal_prefix_outside_the_new_mask(name,form,layout):
    value=form+' '+name
    source='organization: '+value if layout=='plain' else json.dumps({'company':value},ensure_ascii=layout=='json-ascii')
    (left,right),=organization_field_spans(source)
    decoded=source[left:right] if layout=='plain' else json.loads('"'+source[left:right]+'"')
    assert decoded==name
    assert left>source.index(form) if layout=='plain' else left>source.index(':')


@pytest.mark.parametrize('value',[
    'ООО ""','ООО «   »','ООО «...»','ООО «${COMPANY}»','ООО "<ORGANIZATION_1>"',
    'ООО «getUser()»','ООО "foo/bar"','ООО «foo\\bar»','ООО «Alpha\nBeta»',
    'ООО «Alpha"','ООО "Alpha»','ООО «Alpha» extra',
    'ООО «'+'А'*129+'»','ООО '+'A'*41,
    'ООО North Wind Research Company','ООО Northwind_private',
    'ООО `Northwind`','ООО АО ПАО',
])
def test_unsupported_or_code_shaped_value_is_not_truncated_into_a_name(value):
    assert organization_field_spans('company: '+value)==()
    assert organization_field_spans(json.dumps({'company':value},ensure_ascii=True))==()


def test_quoted_legal_word_inside_the_name_is_not_treated_as_a_public_prefix():
    source='company: ООО «ООО»'
    (left,right),=organization_field_spans(source)
    assert source[left:right]=='«ООО»'


@pytest.mark.parametrize('kind',sorted(SUPPORTED_ENTITY_TYPES))
def test_old_native_result_is_preserved_inside_quoted_name(kind):
    source='company: ООО «Northwind Labs»'
    at=source.index('Northwind');old=RecognizerResult(kind,at+1,at+5,.99,recognition_metadata={'keep':'exact'})
    output,_=add_organization_field_coverage(source,(old,))
    assert any(r is old and r.score==.99 and r.recognition_metadata=={'keep':'exact'} for r in output)
    masked={i for r in output for i in range(r.start,r.end)}
    assert all(i in masked for i in range(source.index('«'),source.index('»')+1) if not source[i].isspace())


def test_untyped_prose_is_not_promoted_to_an_explicit_company_field():
    assert organization_field_spans('Получатель ООО «Northwind».')==()
    assert organization_field_spans('ООО Northwind Labs')==()


def test_quoted_values_remain_separate_in_adjacent_fields():
    source='company: ООО «Речной Порт».\norganization: АО "Northwind".'
    assert [source[a:b] for a,b in organization_field_spans(source)]==['«Речной Порт»','"Northwind"']


def test_seeded_quote_and_escape_projection_roundtrip():
    rng=random.Random(2026111231)
    for _ in range(750):
        words=['На'+''.join(rng.choice('абвгдежзиклмнопрстуфхцчшэюя') for _ in range(rng.randint(2,12))),
               'Sky'+''.join(rng.choice('abcxyz012') for _ in range(rng.randint(1,8)))]
        opening,closing=rng.choice([('«','»'),('“','”'),('„','“'),('"','"')])
        name=opening+' '.join(words)+closing
        source=json.dumps({rng.choice(['company','Организация']):rng.choice(['ООО','АО','ПАО'])+' '+name,
                           'literal':'<ORGANIZATION_1>'},ensure_ascii=rng.choice([True,False]),indent=rng.choice([None,2]))
        (left,right),=organization_field_spans(source)
        assert json.loads('"'+source[left:right]+'"')==name
        masked=source[:left]+'<ORGANIZATION_2>'+source[right:]
        assert masked.replace('<ORGANIZATION_2>',source[left:right]).encode()==source.encode()
        assert json.loads(masked)['literal']=='<ORGANIZATION_1>'
