"""Complete numbered source paragraphs, preserving source and old decisions."""
import pytest
from presidio.ner.boundary_values import BoundaryValues,BoundaryContext,MAX_NUMBERED_PARAGRAPH_CHARS
from presidio.ner.triton_recognizer import NERProcessingError, merge_window_predictions
from presidio.tests.test_ner_window_value_repair import prediction,recognizer

def numbered(body='ООО Янтарный\nПеревал',eol='\n'):
    lines=['header {metadata}', '1: Документ', '2:', *[f'{n}: {line}' for n,line in enumerate(body.split('\n'),3)],f'{3+len(body.split(chr(10)))}:', 'tail [metadata]']
    return eol.join(lines)

@pytest.mark.parametrize('eol',['\n','\r\n','\r','\u2028','\u2029'])
def test_complete_multiword_and_multiline_source_scope(eol):
    text=numbered(eol=eol);a=text.index('Янтарный');b=a+len('Янтарный')
    bounds=BoundaryValues(text).resolve(a,b)
    assert bounds==(text.index('ООО'),text.index('Перевал')+len('Перевал'))
    assert 'Перевал' in text[slice(*bounds)]

@pytest.mark.parametrize('kind',['PERSON','LOCATION','ORGANIZATION','LOGIN','PASSWORD','AUTH_TOKEN','SECRET_KEY','CONTRACT_NUMBER'])
def test_cut_evidence_protects_the_whole_paragraph_without_relabelling(monkeypatch,kind):
    text=numbered();a=text.index('Янтарный')
    instance,_,_=recognizer(monkeypatch,[prediction(kind,a,a+3,right=True)],[])
    # Exercise the unresolved branch; scalar completion is an earlier, unchanged rule.
    monkeypatch.setattr(instance, '_merge_rechecked_window_predictions',
                        lambda _text, predictions, *args, **kwargs: merge_window_predictions(predictions))
    result,=instance.analyze(text)
    assert (result.start,result.end,result.entity_type)==(text.index('ООО'),text.index('Перевал')+len('Перевал'),kind)

@pytest.mark.parametrize('text',[
    'header {meta}\n1: Документ\n2:\n3: Иван Петров',
    'header {meta}\n1: Документ\n2:\n3: Иван Петров\n4:',
    'header {meta}\n1: Документ\n2:\n3: Иван Петров\n5:\ntail',
    'header {meta}\n1: Документ\n2:\n3: Иван Петров\n3:\ntail',
    'header {meta}\n2:\n3: Иван Петров\n4:\ntail',
    'header {meta}\n1: Документ\n2:\nX Иван Петров\n4:\ntail',
    'header {meta}\n1: Документ\n2:\n3: Иван Петров\n4: продолжение\ntail',
    'header {meta}\n1: ```python\n2:\n3: Иван Петров\n4:\n5: ```\ntail',
    'header {meta}\n1: {\n2:\n3: Иван Петров\n4:\n5: }\ntail',
    'header {meta}\n1: Документ\n2:\n3: Иван Петров; value=opaque\n4:\ntail',
    'header {meta}\n1: Документ\n2:\n3: Иван Петров <tag>\n4:\ntail',
    'header {meta}\n1: Документ (\n2:\n3: Иван Петров\n4:\ntail',
    'header {meta}\n1: Документ\n2:\n3: Иван Петров «Продолжение\n4:\ntail',
    'header {meta}\n1: Документ\n2:\n3: Иван Петров `unclosed\n4:\ntail',
    'header {meta}\n1: Документ\n2:\n3: Иван Петров\n4: - Отдельный пункт\n5:\ntail',
    'header {meta}\n1: Документ\n2:\n3: Иван Петров\n4: https://example.test/path\n5:\ntail',
])
def test_incomplete_malformed_structured_or_inherited_context_is_not_a_boundary(text):
    a=text.index('Иван')
    assert BoundaryValues(text).resolve(a,a+4) is None

def test_unclosed_external_fence_is_not_reinterpreted():
    text='header {meta}\n```python\n'+numbered()+'\n'
    a=text.index('Янтарный')
    assert BoundaryValues(text).resolve(a,a+3) is None

def test_long_scope_keeps_refusal_instead_of_masking_a_large_document():
    text=numbered('Иван Петров '+('описание '*MAX_NUMBERED_PARAGRAPH_CHARS))
    a=text.index('Иван')
    assert BoundaryValues(text).resolve(a,a+4) is None

def test_existing_prose_scope_is_not_replaced_by_a_shorter_numbered_scope():
    text='header\n\n1: Документ\n2:\n3: Иван Петров\n4:\n5: Конец\n\ntail'
    a=text.index('Иван');values=BoundaryValues(text)
    assert values.resolve(a,a+4)==values._resolve_existing(a,a+4)
    assert values._numbered_paragraphs is None

def test_closed_inline_code_does_not_split_the_numbered_paragraph():
    text = numbered('Сведения (ivanova petrova\nПримеры: `docs/readme.md`, `guide.md`)')
    a = text.index('nova')
    bounds = BoundaryValues(text).resolve(a, a + 4, entity_type='PERSON')
    assert text[slice(*bounds)] == 'Сведения (ivanova petrova\n4: Примеры: `docs/readme.md`, `guide.md`)'

def test_artificial_chunk_end_cannot_close_a_numbered_paragraph(monkeypatch):
    full=numbered();a=full.index('Янтарный');local=full[:a+4]
    instance,_,_=recognizer(monkeypatch,[prediction('PERSON',a,a+3,right=True)],[])
    result,=instance.analyze(local,boundary_context=BoundaryContext(BoundaryValues(full)))
    assert result.end==full.index('Перевал')+len('Перевал')>len(local)
    with pytest.raises(NERProcessingError):instance.analyze(local)
