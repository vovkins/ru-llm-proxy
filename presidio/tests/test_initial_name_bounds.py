"""Narrow PERSON recovery must retain every part of a recognized name unit."""
import pytest
from presidio.ner.initial_names import initial_person_ranges
from presidio.ner.boundary_values import BoundaryValues
from presidio.ner.triton_recognizer import merge_window_predictions
from presidio.tests.test_ner_window_value_repair import prediction,recognizer

VALUES=['К. Миронова','С. Е. Волков','Зайцев А. Б.','Н. Ушаков-Орлов','Л.\u00a0Сергеев','Л.\nСергеев','И\u0306. Сергеев']
@pytest.mark.parametrize('value',VALUES)
@pytest.mark.parametrize('form',['parenthesis','speaker'])
def test_complete_name_units(value,form):
    text='header {metadata}\n'+(f'Уточнить: ({value}, ref-72).' if form=='parenthesis' else f'Обсудили. {value}: решение.')
    assert (text.index(value),text.index(value)+len(value)) in list(initial_person_ranges(text))
    a=text.index(value)+2
    assert BoundaryValues(text).resolve(a,a+1,entity_type='PERSON')==(text.index(value),text.index(value)+len(value))

@pytest.mark.parametrize('text',[
 'header {meta}\nФИО: К. Миронова Петровна;',
 'header {meta}\nФИО: К. Миронова, Наталья;',
 'header {meta}\nФИО: И. П. С. Миронова;',
 'header {meta}\nКомпания: ООО (К. Миронова);',
 'header {meta}\npassword: (К. Миронова; tail)',
 'header {meta}\nФИО: К. Миронова—Петрова;',
 'header {meta}\nКод: `(К. Миронова)`;',
 'header {meta}\nКод: ``(К. Миронова)``;',
 'x = (К. Миронова)',
 'metadata {tool}\n3: x = (К. Миронова)',
 'metadata {tool}\n3: const x = (К. Миронова);',
])
def test_ambiguous_extensions_credentials_organization_and_code_are_not_initial_names(text):
    assert list(initial_person_ranges(text))==[]

@pytest.mark.parametrize('kind',['ORGANIZATION','LOGIN','PASSWORD','AUTH_TOKEN','SECRET_KEY','CONTRACT_NUMBER'])
def test_person_syntax_does_not_confirm_other_entity_type(kind):
    text='metadata {tool}\nОтветил: (К. Миронова, ref-72).'
    a=text.index('Миронова')
    assert BoundaryValues(text).resolve(a,a+3,entity_type=kind) is None

def test_existing_accepted_paragraph_is_not_shrunk():
    text='head\n\nОтветил: (К. Миронова, ref-72).\n\ntail';v=BoundaryValues(text);a=text.index('Миронова')
    assert v.resolve(a,a+3,entity_type='PERSON')==v._resolve_existing(a,a+3)

@pytest.mark.parametrize('value',VALUES[:4])
def test_analyzer_cut_name_keeps_whole_unit(monkeypatch,value):
    text=f'header {{metadata}}\nОбсудили. {value}: решение.';a=text.index(value)
    instance,_,_=recognizer(monkeypatch,[prediction('PERSON',a+2,a+len(value),left=True)],[])
    monkeypatch.setattr(instance,'_merge_rechecked_window_predictions',lambda _text,predictions,*a,**k:merge_window_predictions(predictions))
    result,=instance.analyze(text)
    assert (result.start,result.end,result.entity_type)==(a,a+len(value),'PERSON')

def test_initial_name_in_code_fence_keeps_refusal():
    text='head {meta}\n```python\nОписание: (К. Миронова, ref-72).\n```\ntail'
    a=text.index('Миронова');assert BoundaryValues(text).resolve(a,a+3,entity_type='PERSON') is None
