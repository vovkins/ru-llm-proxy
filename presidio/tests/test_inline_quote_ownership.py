"""Contained quotes belong to inline code, enclosing quotes still own it."""
import json
import pytest
from ner.boundary_values import BoundaryValues,_inline_syntax,_Ranges

@pytest.mark.parametrize('code',['purpose:"class"','@AllureId("TC-17")',"key='value'",'call("value", 1)','key="Unicode"'])
def test_closed_inline_owns_fully_contained_quotes(code):
    text='Prose before `'+code+'` and after.'
    v=BoundaryValues(text);v._prepare();closed,unknown=v._markup()
    a=text.index('`');b=text.rindex('`')+1
    assert closed.containing(a,b)==(a,b)
    assert unknown==len(text)
    at=text.index('after');assert v.is_plain_text_span(at,at+5)
    assert not v.is_plain_text_span(a+1,b-1)

@pytest.mark.parametrize('text',[
    'const x = "literal `key=1` tail";',
    "value = 'literal `key=1` tail'",
    json.dumps({'content':'literal `key=1` tail'}),
])
def test_enclosing_string_keeps_ticks_as_data(text):
    v=BoundaryValues(text);v._prepare();closed,_=v._markup()
    a=text.index('`');b=text.rindex('`')+1
    assert closed.containing(a,b) is None
    assert not v.is_plain_text_span(a+1,b-1)

@pytest.mark.parametrize('owner',[(0,8),(3,15),(0,15)])
def test_crossing_or_enclosing_owner_prevents_reinterpretation(owner):
    text='xx `key=1` tail';a=text.index('`');b=text.rindex('`')+1
    closed,_=_inline_syntax(text,_Ranges([]),_Ranges([owner]))
    assert closed.containing(a,b) is None

def test_unclosed_inline_still_blocks_later_recovery():
    text='before `key="value" still unclosed\nPERSON later'
    v=BoundaryValues(text);v._prepare();_,unknown=v._markup()
    assert unknown==text.index('`')
    at=text.index('PERSON');assert not v.is_plain_text_span(at,at+6)

def test_later_genuine_unclosed_delimiter_is_preserved():
    text='before `key="value"` then `unclosed\nPERSON'
    v=BoundaryValues(text);v._prepare();_,unknown=v._markup()
    assert unknown==text.index('`unclosed')
