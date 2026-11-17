import copy
import pytest
from presidio_analyzer import RecognizerResult
from entity_types import SUPPORTED_ENTITY_TYPES
from result_merging import SOURCE_NER,DETECTION_SOURCE_METADATA_KEY
from recognizers.numbered_name_fields import add_numbered_name_field_coverage

def source(value):return 'Metadata {tool}\n1: Document\n2:\n3: Данные: '+value+'\n4: comment\n5:\ntail [meta]'
def seed(text,word='Миронова',kind='PERSON',origin=SOURCE_NER):
    start=text.index(word)
    return RecognizerResult(kind,start,start+len(word),.9,recognition_metadata={DETECTION_SOURCE_METADATA_KEY:origin})
def positions(rows):return {i for r in rows for i in range(r.start,r.end)}

@pytest.mark.parametrize('value',['им. К. Миронова','Бюро К. Миронова','им. А. Н. Волкова','Бюро И. С. Петрова-Сидорова'])
def test_complete_numbered_name_value_keeps_keys_and_line_numbers(value):
    text=source(value);surname=value.split()[-1];old=seed(text,surname)
    out,decisions=add_numbered_name_field_coverage(text,(old,))
    assert any(r is old for r in out)
    at=text.index(value);assert set(range(at,at+len(value)))-{i for i,ch in enumerate(text) if ch.isspace()}<=positions(out)
    assert not positions(out)&set(range(text.index('3:'),at))
    assert not any('\n' in text[r.start:r.end] for r in out)
    assert decisions

@pytest.mark.parametrize('kind',sorted(SUPPORTED_ENTITY_TYPES))
def test_native_findings_never_trimmed_mutated_or_replaced(kind):
    text=source('Бюро К. Миронова');old=seed(text);at=text.index('К.')
    native=RecognizerResult(kind,at,at+1,.99,recognition_metadata={'keep':'exact'})
    before=copy.deepcopy(native.__dict__)
    out,_=add_numbered_name_field_coverage(text,(old,native))
    assert any(r is native for r in out) and native.__dict__==before
    assert positions((old,native))<=positions(out)

@pytest.mark.parametrize('text',[
    'Данные: Бюро К. Миронова',source('Бюро К. Миронова и коллеги'),source('Бюро К. Миронова: секрет'),
    source('Бюро К. Миронова').replace('2:\n','2: not blank\n'),
    source('Бюро К. Миронова').replace('5:\n','5: still open\n'),
    source('Бюро К. Миронова').replace('1: Document','10: Document'),
    '```text\n'+source('Бюро К. Миронова')+'\n```',
    'value = """\n'+source('Бюро К. Миронова')+'\n"""',
    'unclosed `\n'+source('Бюро К. Миронова'),
    source('Бюро К. Миронова').replace('Данные:','Password:'),
])
def test_unknown_unclosed_or_code_owned_values_do_not_add_coverage(text):
    old=seed(text);out,decisions=add_numbered_name_field_coverage(text,(old,))
    assert out==(old,) and not decisions

def test_no_ner_seed_no_new_detection():
    text=source('Бюро К. Миронова')
    assert add_numbered_name_field_coverage(text,())==((),())
    native=seed(text,origin='structural')
    assert add_numbered_name_field_coverage(text,(native,))==((native,),())

@pytest.mark.parametrize('key',['Проект','Продукт','Модуль','Класс','Пакет','Project','Product'])
def test_explicit_technical_names_do_not_gain_extra_masks(key):
    text=source('Бюро К. Миронова').replace('Данные:',key+':');old=seed(text)
    assert add_numbered_name_field_coverage(text,(old,))==((old,),())

def test_threshold_and_requested_type_are_respected():
    text=source('Бюро К. Миронова');old=seed(text)
    assert add_numbered_name_field_coverage(text,(old,),score_threshold=.95)==((old,),())
    assert add_numbered_name_field_coverage(text,(old,),requested_entities=['AUTH_TOKEN'])==((old,),())

def test_cancellation_propagates_without_returning_partial_output():
    text=source('Бюро К. Миронова');old=seed(text)
    def cancel():raise RuntimeError('cancelled')
    with pytest.raises(RuntimeError,match='cancelled'):add_numbered_name_field_coverage(text,(old,),check=cancel)
