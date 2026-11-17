"""Only exact scoped static module sources may disprove an NER AUTH_TOKEN."""
import pytest
from presidio_analyzer import RecognizerResult
from presidio.entity_types import SUPPORTED_ENTITY_TYPES
from presidio.result_merging import merge_results, DETECTION_SOURCE_METADATA_KEY, SOURCE_NER
from ner import code_symbols
from string_spans import StringSpans

PATH='@terminal/ladogadirect01/provider/react'


def finding(text,word=PATH,kind='AUTH_TOKEN',source=SOURCE_NER,*,last=False):
    start=text.rindex(word) if last else text.index(word)
    return RecognizerResult(kind,start,start+len(word),.95,recognition_metadata={DETECTION_SOURCE_METADATA_KEY:source})


def covered(result,output):
    return all(any(r.start<=i<r.end for r in output) for i in range(result.start,result.end))


@pytest.mark.parametrize('source',[
 f"import client from '{PATH}';",f'import client from "{PATH}";',
 f"import {{ usePortal }} from '{PATH}';",f"import type {{ Portal }} from '{PATH}';",
 f"import '{PATH}';",f"export {{ client }} from '{PATH}';",f"export * from '{PATH}';",
 f"Example source:\n```typescript\nimport client from '{PATH}';\n```",
 f"```tsx\nimport client from '{PATH}';\n```",
 f"const title = 'Ж😀';\r\nimport client from '{PATH}';",
])
def test_exact_static_source_is_not_an_auth_token(source):
    result=merge_results(source,[finding(source)])
    assert not result.results
    assert 'ner_module_reference_auth_token_suppressed' in {d.reason for d in result.decisions}


@pytest.mark.parametrize('source',[
 PATH,f'Output exactly this text and nothing else: {PATH}',
 f"const auth_token = '{PATH}';",f"const password = '{PATH}';",f'Authorization: Bearer {PATH}',
 f"// import client from '{PATH}';\nconst x = 1;",f"/* import client from '{PATH}'; */",
 f'const text = "import client from \'{PATH}\';";',
 f"const text = `import client from '{PATH}';`;",f"const lib = require('{PATH}');",
 f"const lib = import('{PATH}');",f"const data = {{ source: '{PATH}' }};",
 f'{{"content":"import client from \'{PATH}\';"}}',
 f"Source\n```typescript\nimport client from '{PATH}';",
 f"import client from '{PATH}' + suffix;",f"import client from '{PATH}'; broken{{",
 f"/*\n```ts\nimport client from '{PATH}';\n```\n*/",
])
def test_identical_data_and_unproven_code_keep_detection(source):
    old=finding(source);assert covered(old,merge_results(source,[old]).results)


@pytest.mark.parametrize('kind',sorted(set(SUPPORTED_ENTITY_TYPES)-{'AUTH_TOKEN','LOGIN'}))
def test_every_other_entity_type_is_retained_in_the_import(kind):
    text=f"import x from '{PATH}';";old=finding(text,kind=kind)
    outcome=merge_results(text,[old])
    if kind=='CONTRACT_NUMBER':
        assert 'contract_unconfirmed' in {d.reason for d in outcome.decisions}
    else:
        assert covered(old,outcome.results)
    assert 'ner_module_reference_auth_token_suppressed' not in {d.reason for d in outcome.decisions}


@pytest.mark.parametrize('kind',sorted(SUPPORTED_ENTITY_TYPES))
def test_any_independent_native_detection_vetoes_the_exemption(kind):
    text=f"import x from '{PATH}';";native=finding(text,kind=kind,source='structural')
    assert covered(native,merge_results(text,[finding(text),native]).results)


@pytest.mark.parametrize('kind',sorted(set(SUPPORTED_ENTITY_TYPES)-{'AUTH_TOKEN'}))
def test_any_independent_ner_type_vetoes_removal_of_the_original_token(kind):
    text=f"import x from '{PATH}';";original=finding(text);other=finding(text,word='ladogadirect01',kind=kind)
    assert covered(original,merge_results(text,[original,other]).results)


@pytest.mark.parametrize('path',[
 '@scope/AbCDeF0123456789TOKEN','@scope/pkg?token=abc123','https://user:secret@example.org/x.js',
 '@scope/pkg#bearer','@scope/../private','@scope/pkg\\x2fprivate','ordinary-lib',
 './private/credentials.json','@scope/.secret','@scope/pkg/../secret','@scope/pkg//secret',
])
def test_unsupported_or_credential_shaped_sources_keep_detection(path):
    text=f"import x from '{path}';";old=finding(text,path)
    assert covered(old,merge_results(text,[old]).results)


@pytest.mark.parametrize('word',['@terminal','ladogadirect01','provider','react'])
def test_partial_auth_tokens_are_not_removed(word):
    text=f"import x from '{PATH}';";old=finding(text,word)
    assert covered(old,merge_results(text,[old]).results)


def test_identical_string_elsewhere_does_not_inherit_code_reference_proof():
    text=f"import x from '{PATH}';\nconst auth_token = '{PATH}';"
    first=finding(text);data=finding(text,last=True);out=merge_results(text,[first,data]).results
    # Reuse as an explicit credential also prevents declassification of the
    # identical code reference: both spellings must remain private.
    assert covered(first,out)
    assert covered(data,out)


@pytest.mark.parametrize('failure',['missing','timeout','size'])
def test_unavailable_or_bounded_parser_fails_closed(failure,monkeypatch):
    text=f"import x from '{PATH}';"
    if failure=='missing':monkeypatch.setattr(code_symbols,'_languages',lambda:{})
    elif failure=='timeout':monkeypatch.setattr(code_symbols,'_parse',lambda *a,**k:None)
    else:text='/*'+('x'*code_symbols.MAX_CODE_BYTES)+'*/ '+text
    old=finding(text);assert covered(old,merge_results(text,[old]).results)
