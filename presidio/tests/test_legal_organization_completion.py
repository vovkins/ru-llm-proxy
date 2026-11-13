"""Complete evidence-backed, explicitly terminated legal company values."""
import itertools
import random

import pytest

from presidio.entity_types import SUPPORTED_ENTITY_TYPES
from presidio.result_merging import NER_BOUNDARY_CONTAINER_KEY
from presidio.tests.test_name_refinement import result,summary,merge_results

NAME='Северная Лаборатория'


def partial(text,form):
    return [result(text,form+' '+NAME[:3],score=.91),result(text,'Лаб',score=.72)]


@pytest.mark.parametrize('form',['ООО','АО','ПАО','ОАО','ЗАО'])
@pytest.mark.parametrize('seed',['partial_org','whole_person'])
@pytest.mark.parametrize('wrapper',['line','document','json','python'])
def test_terminated_legal_value_is_fully_masked_with_original_confidence(form,seed,wrapper):
    value=form+' '+NAME
    text={'line':'Получатель: '+value+'.\nСледующий раздел.', 'document':value,
          'json':'{"company":"'+value+'"}', 'python':"company = '"+value+"'"}[wrapper]
    findings=partial(text,form) if seed=='partial_org' else [result(text,NAME,'PERSON',.83)]
    for order in itertools.permutations(findings):
        outcome=merge_results(text,order,score_threshold=.35)
        assert summary(text,outcome)==[('ORGANIZATION',value if seed=='partial_org' else NAME,.72 if seed=='partial_org' else .83)]
        assert 'organization_legal_value_completed' in [d.reason for d in outcome.decisions]


@pytest.mark.parametrize('tail',[' получила письмо.',' и Партнёры.',' Сервис Группа.',
                                '. Дополнение','_service','/path','42'])
def test_unconfirmed_or_longer_name_boundary_is_not_guessed(tail):
    text='ООО '+NAME+tail
    outcome=merge_results(text,partial(text,'ООО'),score_threshold=.35)
    assert 'organization_legal_value_completed' not in [d.reason for d in outcome.decisions]


@pytest.mark.parametrize('findings',['none','prefix_only','partial_person_only','weak','native_only','login_only'])
def test_a_legal_form_or_unaccepted_seed_cannot_create_a_new_detection(findings):
    text='ООО '+NAME
    values={'none':[], 'prefix_only':[result(text,'ООО')],
            'partial_person_only':[result(text,'Сев','PERSON')],
            'weak':[result(text,'ООО'),result(text,'Сев','PERSON',.2)],
            'native_only':[result(text,'Сев','ORGANIZATION',source='structural')],
            'login_only':[result(text,'ООО'),result(text,'Сев','LOGIN')]}
    outcome=merge_results(text,values[findings],score_threshold=.35)
    assert 'organization_legal_value_completed' not in [d.reason for d in outcome.decisions]


def test_list_of_legal_forms_is_not_a_company_value():
    text='Формы: ООО АО ПАО'
    outcome=merge_results(text,[result(text,'ООО А')],score_threshold=.35)
    assert 'organization_legal_value_completed' not in [d.reason for d in outcome.decisions]


@pytest.mark.parametrize('kind',sorted(SUPPORTED_ENTITY_TYPES))
def test_competing_native_values_remain_protected_and_veto_completion(kind):
    text='ООО '+NAME
    native=result(text,'Лаборатория',kind,.99,source='structural')
    outcome=merge_results(text,[*partial(text,'ООО'),native],score_threshold=.35)
    assert 'organization_legal_value_completed' not in [d.reason for d in outcome.decisions]
    assert any(r.start<=native.start and r.end>=native.end for r in outcome.results)


def test_a_boundary_container_is_never_expanded_outside_its_confirmed_source_value():
    text='ООО '+NAME
    findings=partial(text,'ООО');findings[0].recognition_metadata[NER_BOUNDARY_CONTAINER_KEY]=True
    assert 'organization_legal_value_completed' not in [d.reason for d in merge_results(text,findings).decisions]


def test_person_only_request_retains_its_prior_person_evidence():
    text='ООО '+NAME
    person=result(text,NAME,'PERSON',.83)
    assert summary(text,merge_results(text,[person],requested_entities=['PERSON']))==[('PERSON',NAME,.83)]
    assert summary(text,merge_results(text,[person],requested_entities=['ORGANIZATION']))==[('ORGANIZATION',NAME,.83)]


def test_unrelated_organization_and_independent_secret_do_not_supply_seed():
    text='ООО '+NAME+'.\nДругая Компания. token=secret42'
    source=[result(text,'Другая Компания'),result(text,'secret42','SECRET_KEY',.99,source='native_credential')]
    outcome=merge_results(text,source,score_threshold=.35)
    assert 'organization_legal_value_completed' not in [d.reason for d in outcome.decisions]
    assert any(text[r.start:r.end]=='secret42' for r in outcome.results)


def test_completed_company_does_not_join_an_independent_organization_on_the_next_line():
    text='ООО '+NAME+'\nЧужой Бренд'
    findings=[*partial(text,'ООО'),result(text,'Чужой Бренд')]
    outcome=merge_results(text,findings,score_threshold=.35)
    assert summary(text,outcome)==[('ORGANIZATION','ООО '+NAME,.72),('ORGANIZATION','Чужой Бренд',.8)]


@pytest.mark.parametrize('kind',['PERSON','ORGANIZATION'])
def test_completion_does_not_reopen_a_separator_already_protected_by_legacy_joining(kind):
    text='ООО '+NAME+'\nЧужой Бренд'
    findings=[result(text,NAME,kind),result(text,'Чужой Бренд',kind)]
    outcome=merge_results(text,findings,score_threshold=.35)
    assert 'organization_legal_value_completed' not in [d.reason for d in outcome.decisions]
    assert summary(text,outcome)==[(kind,NAME+'\nЧужой Бренд',.8)]


def test_seeded_completion_never_opens_any_accepted_source_character():
    rng=random.Random(2026111223)
    for _ in range(750):
        form=rng.choice(['ООО','АО','ПАО','ОАО','ЗАО'])
        gap=rng.choice([' ','  ','\t','\u00a0'])
        text='Получатель: '+form+gap+NAME+'.'
        end=text.index(NAME)+rng.randint(1,len('Северная'))
        first=result(text,form);first.end=end;first.score=rng.uniform(.35,1)
        second=result(text,'Лаб',score=rng.uniform(.35,1))
        source=[first,second];rng.shuffle(source)
        outcome=merge_results(text,source,score_threshold=.35)
        protected={i for r in outcome.results for i in range(r.start,r.end)}
        assert all(i in protected for r in source for i in range(r.start,r.end))
        left=text.index(form);right=text.index(NAME)+len(NAME)
        assert protected==set(range(left,right))
