"""Preserve ordinary references; retain wider bounds for unknown path values."""
import pytest
from presidio.ner.boundary_values import BoundaryValues, BoundaryContext
from presidio.ner import triton_recognizer as tr
from presidio.tests.test_ner_window_value_repair import prediction, recognizer

def source(value):
    return 'header {metadata}\n1: Документ\n2:\n3: Описание (оверлей\n4: Mobile: '+value+').\n5:\ntail [metadata]'

@pytest.mark.parametrize('value,allowed', [
    ('`references/alpha.md`, `beta.md`, правила `*.mobile.md`', True),
    ('`records/olgasmi.md`, `beta.md`', True),
    ('`token_R8m2q6X9.md`, `beta.md`', False),
    ('`Проект-Речной.md`, `beta.md`', False),
    ('`secret.md`, `beta.md`', False),
    ('`https://example.test/a.md`, `beta.md`', False),
    ('`one.md`', False),
])
def test_shortening_requires_valid_reference_record(value,allowed):
    text=source(value);a=text.index('рлей');v=BoundaryValues(text)
    bounds=v.resolve(a,a+4)
    assert bounds is not None
    assert v.allows_subword_narrowing(bounds,a,a+4) is allowed

@pytest.mark.parametrize('value,short', [
    ('`references/alpha.md`, `beta.md`', True),
    ('`token_R8m2q6X9.md`, `beta.md`', False),
    ('`Проект-Речной.md`, `beta.md`', False),
])
def test_recognizer_applies_guard_after_positive_word_decision(monkeypatch,value,short):
    text=source(value);a=text.index('оверлей');fragment=a+3
    instance,_,_=recognizer(monkeypatch,[prediction('PERSON',fragment,a+7,right=True)],[])
    # Isolate the new guard. Neural/geometry qualification is separately covered
    # by test_subword_artifact_word and real ONNX regression runs.
    monkeypatch.setattr(tr,'_subword_artifact_word',lambda *args,**kwargs:(a,a+7))
    result,=instance.analyze(text,boundary_context=BoundaryContext(BoundaryValues(text)))
    if short:
        assert text[result.start:result.end]=='оверлей'
    else:
        gold=value.split('`')[1];b=text.index(gold)
        assert result.start<=b<b+len(gold)<=result.end

def test_plain_numbered_prose_has_no_reference_specific_restriction():
    text=source('обычная инструкция');a=text.index('рлей');v=BoundaryValues(text)
    assert v.allows_subword_narrowing(v.resolve(a,a+4),a,a+4)

def test_an_earlier_unknown_reference_is_not_hidden_by_a_later_valid_record():
    text='header {metadata}\n1: Документ\n2:\n3: Описание (оверлей\n4: Mobile: `unknown_ref.md`, `two.md`\n5: Desktop: `one.md`, `two.md`).\n6:\ntail [metadata]'
    a=text.index('рлей');v=BoundaryValues(text)
    assert not v.allows_subword_narrowing(v.resolve(a,a+4),a,a+4)
