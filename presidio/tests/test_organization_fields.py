"""Independent, additive name-only evidence from explicitly typed fields."""
import copy
import json
import random

import pytest
from presidio_analyzer import RecognizerResult
from presidio.entity_types import SUPPORTED_ENTITY_TYPES
from presidio.recognizers.organization_fields import (
    CONFIDENCE, organization_field_spans, add_organization_field_coverage,
)

NAME='Кварцевый Парус'
KEYS=['Организация','Название организации','company','company_name','companyName',
      'organization','organization_name','organizationName','org_name']


@pytest.mark.parametrize('key',KEYS)
@pytest.mark.parametrize('form',['ООО','АО','ПАО','ОАО','ЗАО'])
@pytest.mark.parametrize('layout',['plain','period','json','python'])
def test_closed_fields_detect_the_name_without_any_ner_or_legal_prefix_mask(key,form,layout):
    value=form+' '+NAME
    source={'plain':key+': '+value,'period':'Описание. '+key+': '+value+'.\nСледующий раздел.',
            'json':json.dumps({key:value},ensure_ascii=False),'python':key.replace(' ','_')+" = '"+value+"'"}[layout]
    # Russian prose labels are not automatically arbitrary Python identifiers.
    if layout=='python' and key=='Название организации':
        source="company = '"+value+"'"
    left=source.index(NAME)
    assert organization_field_spans(source)==((left,left+len(NAME)),)
    output,decisions=add_organization_field_coverage(source,(),score_threshold=.35)
    assert len(output)==1 and source[output[0].start:output[0].end]==NAME
    assert output[0].score==CONFIDENCE
    assert decisions


@pytest.mark.parametrize('source',[
    'ООО Кварцевый Парус.', 'Отправить письмо Кварцевый Парус.',
    'company: Кварцевый Парус', 'company: ООО',
    'company: ООО Кварцевый Парус Холдинг Центр.',
    'company: ООО Кварцевый Парус согласовал документ.',
    'company: ООО Кварцевый Парус\nХолдинг',
    'company: ООО АО ПАО', 'my_company: ООО Кварцевый Парус',
    'my-company: ООО Кварцевый Парус', 'info.company: ООО Кварцевый Парус',
    'company_extra: ООО Кварцевый Парус', 'company = build("ООО Кварцевый Парус")',
    'company = f"ООО Кварцевый Парус"', 'company = r"ООО Кварцевый Парус"',
    'company = """ООО Кварцевый Парус"""',
    '{"company\': "ООО Кварцевый Парус"}',
    '{"company":"ООО Кварцевый Парус\\nХолдинг"}',
    'company = "ООО ${COMPANY}"', 'company: ООО <ORGANIZATION_1>',
])
def test_ambiguous_untyped_or_unsupported_values_do_not_invent_a_mask(source):
    assert organization_field_spans(source)==()


@pytest.mark.parametrize('ascii_only',[False,True])
def test_json_escapes_have_exact_source_offsets(ascii_only):
    source=json.dumps({'company':'ООО '+NAME,'other':'untouched'},ensure_ascii=ascii_only)
    (left,right),=organization_field_spans(source)
    assert json.loads('"'+source[left:right]+'"')==NAME


@pytest.mark.parametrize('kind',sorted(SUPPORTED_ENTITY_TYPES))
def test_existing_native_findings_are_never_replaced_trimmed_or_mutated(kind):
    source='Организация: ООО '+NAME+'.'
    at=source.index(NAME)
    native=RecognizerResult(kind,at+1,at+4,.99,recognition_metadata={'keep':'exact'})
    before=copy.deepcopy(native.__dict__)
    output,_=add_organization_field_coverage(source,(native,),score_threshold=.35)
    assert any(r is native for r in output) and native.__dict__==before
    old=set(range(native.start,native.end))
    masked={i for r in output for i in range(r.start,r.end)}
    assert old<=masked
    assert all(i in masked for i in range(at,at+len(NAME)) if not source[i].isspace())
    assert not any(i in masked for i in range(source.index('ООО'),source.index('ООО')+3))


def test_filters_and_confidence_do_not_force_detection():
    source='company: ООО '+NAME
    assert add_organization_field_coverage(source,(),requested_entities=['PERSON'])==((),())
    assert add_organization_field_coverage(source,(),score_threshold=.9)==((),())


def _healthy_empty_analyzer(monkeypatch):
    from presidio import analyzer_server as server
    monkeypatch.setattr(server.ner_recognizer,'require_ready',lambda:None)
    monkeypatch.setattr(server.ner_recognizer,'analyze',lambda *a,**kw:[])
    monkeypatch.setattr(server.analyzer,'analyze',lambda **kw:[])
    monkeypatch.setattr(server,'_analyze_document_base64',lambda *a,**kw:[])
    monkeypatch.setattr(server.nlp_engine,'get_supported_languages',lambda:[])
    return server


def test_actual_analyzer_adds_a_name_when_healthy_ner_returns_no_findings(monkeypatch):
    server=_healthy_empty_analyzer(monkeypatch)
    source='Организация: ООО '+NAME+'.'
    reply=server._analyze_sync(server.AnalyzeRequest(text=source,entities=['ORGANIZATION']))
    assert [(e['entity_type'],source[e['start']:e['end']]) for e in reply.entities]==[('ORGANIZATION',NAME)]
    reply=server._analyze_sync(server.AnalyzeRequest(text=source,entities=['PERSON']))
    assert reply.entities==[]


@pytest.mark.parametrize('phase',['readiness','windowing'])
def test_structural_evidence_does_not_bypass_a_required_backend_failure(monkeypatch,phase):
    server=_healthy_empty_analyzer(monkeypatch)
    from presidio.ner import NERProcessingError
    def fail(*a,**kw):raise NERProcessingError(phase=phase,failure_class='window_boundary_unresolved')
    def forbidden(*a,**kw):raise AssertionError('Field rule ran after required backend failure')
    monkeypatch.setattr(server,'add_organization_field_coverage',forbidden)
    monkeypatch.setattr(server.ner_recognizer,'require_ready' if phase=='readiness' else 'analyze',fail)
    with pytest.raises(NERProcessingError):
        server._analyze_sync(server.AnalyzeRequest(text='company: ООО '+NAME))


def test_cancellation_is_propagated_without_partial_output():
    source='company: ООО '+NAME+'.\norganization: АО '+NAME
    class Cancelled(Exception):pass
    count=0
    def check():
        nonlocal count
        count+=1
        if count==3:raise Cancelled()
    with pytest.raises(Cancelled):add_organization_field_coverage(source,(),check=check)


def test_independent_fields_keep_separate_name_bounds():
    source='company: ООО Кварцевый Парус.\norganization: АО Лунный Вектор.'
    assert [source[a:b] for a,b in organization_field_spans(source)]==['Кварцевый Парус','Лунный Вектор']


def test_seeded_owned_fields_preserve_input_and_all_identity_characters():
    rng=random.Random(2026111229)
    alphabet='абвгдежзиклмнопрстуфхцчшэюя'
    for _ in range(750):
        name='На'+''.join(rng.choice(alphabet) for _ in range(rng.randint(2,8)))+' '+'Ве'+''.join(rng.choice(alphabet) for _ in range(rng.randint(2,8)))
        key=rng.choice(KEYS);form=rng.choice(['ООО','АО','ПАО','ОАО','ЗАО'])
        source=json.dumps({key:form+' '+name,'literal':'<ORGANIZATION_1>'},ensure_ascii=rng.choice([True,False]),indent=rng.choice([None,2]))
        original=source
        (left,right),=organization_field_spans(source)
        assert json.loads('"'+source[left:right]+'"')==name
        masked=source[:left]+'<ORGANIZATION_2>'+source[right:]
        assert masked.replace('<ORGANIZATION_2>',source[left:right]).encode()==original.encode()
        assert json.loads(masked)['literal']=='<ORGANIZATION_1>'
