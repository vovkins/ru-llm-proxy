"""Exact code evidence may remove only partial statistical organization spans."""
import concurrent.futures
import pytest
from presidio_analyzer import RecognizerResult
from presidio.entity_types import SUPPORTED_ENTITY_TYPES
from presidio.result_merging import merge_results, DETECTION_SOURCE_METADATA_KEY, SOURCE_NER
from ner import code_symbols
from string_spans import StringSpans


def finding(text, word='goin', kind='ORGANIZATION', source=SOURCE_NER, *, last=False):
    at=text.rindex(word) if last else text.index(word)
    return RecognizerResult(kind,at,at+len(word),.9,recognition_metadata={DETECTION_SOURCE_METADATA_KEY:source})


def retained(text, result, output):
    return all(any(r.start<=i<r.end for r in output) for i in range(result.start,result.end))


@pytest.mark.parametrize('source',[
 'function goinvest() { return 1; }','export function goinvest(value: string): void {}',
 'const goinvest = () => 1;','let goinvest = 1;','goinvest = 2;',
 'const result = goinvest();','const result = client.goinvest();',
 'class Client { goinvest() { return 1; } }','const x = { goinvest() {} };',
 'import goinvest from "public-lib";','import { goinvest as handler } from "public-lib";',
 'def goinvest():\n    return 1','class Client:\n    def goinvest(self):\n        pass',
 'const goinvest = () => 1;\ngoinvest();',
 'Code example:\n```typescript\nfunction goinvest() {}\n```',
 '```js\nfunction goinvest() {}\n```',
])
def test_partial_organization_in_a_proven_symbol_is_suppressed(source):
    result=merge_results(source,[finding(source)])
    assert not result.results
    assert 'ner_identifier_fragment_suppressed' in {d.reason for d in result.decisions}


@pytest.mark.parametrize('name,fragment',[
 ('trackPWA','track'),('renderPortal','render'),('goinvest','goin'),
 ('handler','hand'),('delivery','livery'),('snake_case','snake'),('value17','value'),
])
def test_rule_does_not_depend_on_a_dictionary_of_function_names(name,fragment):
    text=f'function {name}() {{}}'
    assert not merge_results(text,[finding(text,fragment)]).results


@pytest.mark.parametrize('source',[
 'goinvest','Output exactly this text and nothing else: goinvest','Логин пользователя: goinvest',
 'const username = "goinvest";','const credentials = { login: "goinvest" };',
 '// goinvest\nfunction ordinary() {}','/* goinvest */ function ordinary() {}',
 'function send() { return "goinvest"; }','send(goinvest);','send(secret=goinvest);',
 'function send(login = "goinvest") {}','const user = client[goinvest]();',
 'const value = `goinvest`;','const value = /goinvest/;',
 'const metadata = { goinvest: "ordinary" };','function incomplete(goinvest',
 'company: goinvest','const = goinvest;','import ordinary from "goinvest";',
 '{"path":"a.ts","content":"function goinvest() {}"}',
 'Example\n```typescript\nfunction goinvest() {}',
 'value = """\n```typescript\nfunction goinvest() {}\n```\n"""',
 '/*\n```typescript\nfunction goinvest() {}\n```\n*/',
])
def test_data_or_unproven_code_does_not_remove_the_partial_finding(source):
    old=finding(source)
    assert retained(source,old,merge_results(source,[old]).results)


@pytest.mark.parametrize('kind',['LOGIN','ORGANIZATION','PASSWORD','AUTH_TOKEN','SECRET_KEY'])
def test_whole_names_and_credential_findings_are_kept_even_in_function_roles(kind):
    text='function goinvest() {}';old=finding(text,'goinvest',kind)
    assert retained(text,old,merge_results(text,[old]).results)


def test_exact_source_position_does_not_exempt_same_value_in_data():
    text='function goinvest() {}\nconst company = "goinvest";'
    symbol=finding(text);data=finding(text,last=True)
    out=merge_results(text,[symbol,data]).results
    assert not any(r.start<symbol.end and r.end>symbol.start for r in out)
    assert retained(text,data,out)


@pytest.mark.parametrize('kind',sorted(SUPPORTED_ENTITY_TYPES))
def test_every_independent_native_type_vetoes_suppression(kind):
    text='function goinvest() {}';native=finding(text,'goinvest',kind,'structural')
    assert retained(text,native,merge_results(text,[finding(text),native]).results)


@pytest.mark.parametrize('kind',sorted(set(SUPPORTED_ENTITY_TYPES)-{'ORGANIZATION'}))
def test_every_independent_ner_type_vetoes_suppression(kind):
    text='function goinvest() {}';other=finding(text,'goinvest',kind)
    original=finding(text)
    # Existing per-type policy may reject the independent finding (e.g. an
    # unconfirmed contract), but it must still veto removal of this fragment.
    assert retained(text,original,merge_results(text,[original,other]).results)


def test_partial_org_must_be_contained_entirely_in_one_identifier():
    text='const company = goinvest();';old=finding(text,'company = goin')
    assert retained(text,old,merge_results(text,[old]).results)


@pytest.mark.parametrize('prefix',['const msg = "Ж😀";\n','const msg = "Ж😀";\r\n'])
def test_utf8_parser_coordinates_are_projected_exactly(prefix):
    text=prefix+'function goinvest() {}'
    assert not merge_results(text,[finding(text)]).results


@pytest.mark.parametrize('failure',['missing','timeout','size'])
def test_parser_failure_keeps_the_detection(failure,monkeypatch):
    text='function goinvest() {}'
    if failure=='missing':monkeypatch.setattr(code_symbols,'_languages',lambda:{})
    elif failure=='timeout':monkeypatch.setattr(code_symbols,'_parse',lambda *a,**kw:None)
    else:text='/*'+('x'*code_symbols.MAX_CODE_BYTES)+'*/ '+text
    old=finding(text)
    assert retained(text,old,merge_results(text,[old]).results)


def test_parallel_parser_requests_have_independent_source_positions():
    texts=['function goinvest() {}','const x = "Ж😀"; function trackPWA() {}']*20
    def check(text):
        word='goinvest' if 'goinvest' in text else 'trackPWA';at=text.index(word)
        return (at,at+len(word)) in code_symbols.javascript_symbol_spans(text,StringSpans(text))
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        assert all(pool.map(check,texts))
