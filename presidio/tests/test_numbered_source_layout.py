import copy
import pytest
from presidio_analyzer import RecognizerResult
from entity_types import SUPPORTED_ENTITY_TYPES
from result_merging import SOURCE_NER,DETECTION_SOURCE_METADATA_KEY,NER_PERSON_CONTROL_KEY,NER_BOUNDARY_CONTAINER_KEY
from ner.source_layout import project_numbered_person_layout

def evidence(text,word,kind='PERSON',native=False,control=True):
    a=text.index(word)
    meta={DETECTION_SOURCE_METADATA_KEY:'structural' if native else SOURCE_NER}
    if control:meta.update({NER_PERSON_CONTROL_KEY:True,NER_BOUNDARY_CONTAINER_KEY:True})
    return RecognizerResult(kind,a,a+len(word),.95,recognition_metadata=meta)
def covered(rows):return {i for r in rows for i in range(r.start,r.end)}

@pytest.mark.parametrize('newline',['\n','\r\n','\r','\u2028','\u2029'])
def test_preserve_number_and_field_delimiter_but_not_the_sensitive_key(newline):
    text=newline.join(['before','1: Intro','2:','3: overline','4: Android: references','5:','tail'])
    original=evidence(text,'overline'+newline+'4: Android:')
    before=copy.deepcopy(original.__dict__);out,proof=project_numbered_person_layout(text,(original,))
    values=[text[r.start:r.end] for r in out]
    assert values==['overline','Android']
    assert original.__dict__==before
    assert proof
    syntax={i for a,b in proof[0]['syntax'] for i in range(a,b)}
    assert covered((original,))-syntax==covered(out)
    assert not any(newline in text[r.start:r.end] for r in out)

def test_wrapped_real_person_name_keeps_all_letters_covered():
    text='1: Intro\n2:\n3: Иван\n4: Петров\n5:\n'
    original=evidence(text,'Иван\n4: Петров')
    out,proof=project_numbered_person_layout(text,(original,))
    assert [text[r.start:r.end] for r in out]==['Иван','Петров']

def test_real_boundary_container_without_person_control_flag_is_projected():
    text='1: Intro\n2:\n3: overline\n4: Android: references\n5:\n'
    original=evidence(text,'overline\n4: Android:')
    del original.recognition_metadata[NER_PERSON_CONTROL_KEY]
    out,proof=project_numbered_person_layout(text,(original,))
    assert [text[r.start:r.end] for r in out]==['overline','Android']
    assert proof

@pytest.mark.parametrize('kind',sorted(SUPPORTED_ENTITY_TYPES))
def test_independent_native_evidence_is_never_exposed_or_mutated(kind):
    text='1: Intro\n2:\n3: Иван\n4: Петров\n5:\n'
    original=evidence(text,'Иван\n4: Петров');native=evidence(text,'4:',kind,native=True)
    saved=copy.deepcopy(native.__dict__);out,_=project_numbered_person_layout(text,(original,native))
    assert any(r is native for r in out) and native.__dict__==saved
    assert covered((native,))<=covered(out)

@pytest.mark.parametrize('kind',sorted(set(SUPPORTED_ENTITY_TYPES)-{'PERSON'}))
def test_no_projection_for_other_types_even_with_a_control_flag(kind):
    text='1: Intro\n2:\n3: secret\n4: value\n5:\n';original=evidence(text,'secret\n4: value',kind)
    assert project_numbered_person_layout(text,(original,))==((original,),())

@pytest.mark.parametrize('text',[
    '3: Иван\n4: Петров',
    '1: Intro\n3: Иван\n4: Петров\n5:\n',
    'value = """\n1: Intro\n2:\n3: Иван\n4: Петров\n5:\n"""',
    '```text\n1: Intro\n2:\n3: Иван\n4: Петров\n5:\n```',
    'unknown `\n1: Intro\n2:\n3: Иван\n4: Петров\n5:\n',
])
def test_unproven_source_or_opaque_ownership_does_not_expose_syntax(text):
    original=evidence(text,'Иван\n4: Петров')
    assert project_numbered_person_layout(text,(original,))==((original,),())

def test_regular_ner_detection_and_native_person_do_not_get_the_exemption():
    text='1: Intro\n2:\n3: Иван\n4: Петров\n5:\n'
    for native,control in [(False,False),(True,True)]:
        original=evidence(text,'Иван\n4: Петров',native=native,control=control)
        assert project_numbered_person_layout(text,(original,))==((original,),())

def test_name_in_field_key_stays_hidden_and_only_colon_is_public():
    text='1: Intro\n2:\n3: Иван: value\n4:\n';original=evidence(text,'Иван:')
    out,_=project_numbered_person_layout(text,(original,))
    assert [text[r.start:r.end] for r in out]==['Иван']

def test_cancellation_propagates():
    text='1: Intro\n2:\n3: Иван\n4: Петров\n5:\n';original=evidence(text,'Иван\n4: Петров')
    def cancel():raise RuntimeError('cancelled')
    with pytest.raises(RuntimeError,match='cancelled'):project_numbered_person_layout(text,(original,),check=cancel)
