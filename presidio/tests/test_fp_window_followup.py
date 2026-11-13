"""Preserve sensitive names while reducing proven module/extension mistakes."""
import json
import pytest
from presidio_analyzer import RecognizerResult
from presidio.entity_types import SUPPORTED_ENTITY_TYPES
from presidio.result_merging import merge_results,DETECTION_SOURCE_METADATA_KEY,SOURCE_NER
from ner.boundary_values import BoundaryValues
from ner.code_paths import json_code_path_suffixes
PATH='@terminal/ladogadirect01/provider/react'

def finding(text,word,kind='ORGANIZATION',source=SOURCE_NER):
    at=text.index(word)
    return RecognizerResult(kind,at,at+len(word),.95,recognition_metadata={DETECTION_SOURCE_METADATA_KEY:source})

def covered(result,output):
    return all(any(r.start<=i<r.end for r in output) for i in range(result.start,result.end))

@pytest.mark.parametrize('text',[
 f"import client from '{PATH}';",f"export {{ client }} from '{PATH}';",
 f"import '{PATH}';",f"Example source follows.\n```ts\nimport client from '{PATH}';\n```",
])
def test_login_on_exact_static_module_source_is_not_a_login_value(text):
    assert not merge_results(text,[finding(text,PATH,'LOGIN')]).results

@pytest.mark.parametrize('text',[
 f"const login = '{PATH}';",f'Authorization: Bearer {PATH}',f'User: {PATH}',
 f"// import x from '{PATH}';",f"const x = import('{PATH}');",f"require('{PATH}');",
 f"Source:\n```ts\nimport client from '{PATH}';\n```",
 json.dumps({'content':f"import x from '{PATH}';"}),
])
def test_identical_login_in_data_and_unproven_import_keeps_coverage(text):
    old=finding(text,PATH,'LOGIN');assert covered(old,merge_results(text,[old]).results)

@pytest.mark.parametrize('kind',sorted(SUPPORTED_ENTITY_TYPES))
def test_native_finding_vetoes_login_module_suppression(kind):
    text=f"import x from '{PATH}';";native=finding(text,PATH,kind,'structural')
    assert covered(native,merge_results(text,[finding(text,PATH,'LOGIN'),native]).results)

@pytest.mark.parametrize('part',['@terminal','terminal','ladogadirect01','provider','react'])
def test_partial_login_scope_or_path_component_remains_hidden(part):
    text=f"import x from '{PATH}';";old=finding(text,part,'LOGIN')
    assert covered(old,merge_results(text,[old]).results)

@pytest.mark.parametrize('path',['a.ts','src/ConfidentialCompany.ts','views/Portal.tsx','src/app.jsx','main.js'])
def test_only_filename_extension_is_exposed_and_all_name_characters_stay_masked(path):
    text=json.dumps({'path':path,'content':'const value = 1;'});old=finding(text,path)
    out=merge_results(text,[old]).results;at=text.index(path);suffix=path.rindex('.')
    assert all(any(r.start<=i<r.end for r in out) for i in range(at,at+suffix))
    assert not any(r.start<at+len(path) and r.end>at+suffix for r in out)

@pytest.mark.parametrize('fragment',['.ts','ts'])
def test_extension_only_organization_is_removed_but_no_name_is_touched(fragment):
    text=json.dumps({'path':'a.ts','content':'const value = 1;'})
    assert not merge_results(text,[finding(text,fragment)]).results

@pytest.mark.parametrize('kind',sorted(SUPPORTED_ENTITY_TYPES))
def test_independent_native_path_evidence_keeps_the_entire_original_mask(kind):
    text=json.dumps({'path':'src/company.ts','content':'const value = 1;'})
    original=finding(text,'src/company.ts');native=finding(text,'src/company.ts',kind,'structural')
    assert covered(original,merge_results(text,[original,native]).results)

@pytest.mark.parametrize('obj',[
 {'path':'a.ts','content':'secret'}, {'path':'a.ts','content':''},
 {'path':'a.ts','content':'function broken('}, {'path':'../a.ts','content':'const x = 1;'},
 {'path':'/home/person/a.ts','content':'const x = 1;'},
 {'path':'a.txt','content':'const x = 1;'}, {'path':'a.ts','content':'const x = 1;','company':'Sensitive'},
 {'metadata':{'path':'a.ts'},'content':'const x = 1;'}, {'file_path':'a.ts','path':'b.ts','content':'const x = 1;'},
])
def test_other_data_or_unknown_structure_does_not_authorize_unmasking(obj):
    assert not json_code_path_suffixes(json.dumps(obj))

@pytest.mark.parametrize('text',[
 '{"path":"a.ts","path":"b.ts","content":"const x = 1;"}',
 '{"path":"a.ts","content":"const x = 1;"',
 '[{"path":"a.ts","content":"const x = 1;"}]',
])
def test_ambiguous_or_invalid_json_cannot_supply_path_evidence(text):
    assert not json_code_path_suffixes(text)

def test_unicode_escape_boundaries_preserve_exact_basename_and_suffix_ownership():
    text='{"pa\\u0074h":"\\u0061\\u002e\\u0074s","content":"const x = 1;"}'
    owned=json_code_path_suffixes(text);assert len(owned)==1
    left,suffix,right=owned[0]
    assert text[left:suffix]=='\\u0061'
    assert json.loads('"'+text[suffix:right]+'"')=='.ts'

@pytest.mark.parametrize('role',['Исполнитель','Поставщик','Заказчик','Компания','company'])
def test_unresolved_exact_legal_prefix_resolves_the_whole_closed_legal_value(role):
    text=('материал '*100)+role+': ООО Хвойный Мыс.\n'+('описание '*100)
    at=text.index('ООО');values=BoundaryValues(text)
    assert values.resolve(at,at+3,entity_type='ORGANIZATION')==(at,at+len('ООО Хвойный Мыс'))
    assert values.resolve(at,at+3,entity_type='ORGANIZATION')==(at,at+len('ООО Хвойный Мыс'))

@pytest.mark.parametrize('value',[
 'ООО Хвойный Мыс Новый Сервис','ООО Хвойный\nМыс','ООО Хвойный Мыс и далее',
 'ООО Хвойный Мыс; пароль скрыт', 'ООО Хвойный Мыс ${SECRET}',
])
def test_incomplete_or_unclosed_legal_value_keeps_refusal(value):
    text=('материал '*100)+'Исполнитель: '+value+'\n'+('описание '*100)
    at=text.index('ООО');assert BoundaryValues(text).resolve(at,at+3,entity_type='ORGANIZATION') is None

@pytest.mark.parametrize('kind',['PERSON','LOGIN','AUTH_TOKEN','PASSWORD'])
def test_other_entity_types_cannot_use_the_new_organization_proof(kind):
    text=('материал '*100)+'Исполнитель: ООО Хвойный Мыс.\n'+('описание '*100)
    at=text.index('ООО');assert BoundaryValues(text).resolve(at,at+3,entity_type=kind) is None

@pytest.mark.parametrize('role',['Исполнитель','Поставщик','Заказчик'])
@pytest.mark.parametrize('value',['ООО Лесной Исток','АО «Тихий Берег»','ООО Radar'])
def test_same_company_roles_get_complete_identity_on_successful_paths_too(role,value):
    from recognizers.organization_fields import organization_field_spans
    text=f'{role}: {value}.\n'
    spans=organization_field_spans(text)
    assert len(spans)==1
    left,right=spans[0]
    expected=value.split(' ',1)[1]
    assert text[left:right]==expected

@pytest.mark.parametrize('role',['Исполнитель','Поставщик','Заказчик'])
@pytest.mark.parametrize('value',['Иван Петров','ООО используется для примера','ООО незакрытое значение'])
def test_role_label_without_a_closed_legal_company_is_not_an_organization_rule(role,value):
    from recognizers.organization_fields import organization_field_spans
    assert not organization_field_spans(f'{role}: {value}.\n')

def test_ner_login_used_in_a_real_credential_literal_is_not_declassified_as_a_module():
    text=f"import x from '{PATH}';\nconst login = '{PATH}';"
    old=finding(text,PATH,'LOGIN');out=merge_results(text,[old]).results
    assert covered(old,out)
    at=text.rindex(PATH)
    assert all(any(r.start<=i<r.end for r in out) for i in range(at,at+len(PATH)))


def test_two_confirmed_imports_of_same_module_can_both_be_exempted():
    text=f"import x from '{PATH}';\nexport {{ x }} from '{PATH}';"
    old=finding(text,PATH,'LOGIN');other=RecognizerResult('LOGIN',text.rindex(PATH),text.rindex(PATH)+len(PATH),.95,recognition_metadata={DETECTION_SOURCE_METADATA_KEY:SOURCE_NER})
    assert not merge_results(text,[old,other]).results


def test_unknown_escaped_data_keeps_the_original_module_finding():
    text=f"import x from '{PATH}';\nconst value = '\\u0040terminal/data';"
    old=finding(text,PATH,'LOGIN');assert covered(old,merge_results(text,[old]).results)


def test_a_quoted_example_inside_another_literal_is_not_a_new_credential():
    text=f'''import x from '{PATH}';\nconst text = "login = '{PATH}'";'''
    old=finding(text,PATH,'LOGIN');out=merge_results(text,[old]).results
    assert covered(old,out)
    second=text.rindex(PATH)
    assert not any(r.start<second+len(PATH) and r.end>second for r in out)

@pytest.mark.parametrize('kind',['LOGIN','AUTH_TOKEN'])
def test_credential_context_is_protected_even_when_independent_native_finding_vetoes_module_rule(kind):
    text=f"import x from '{PATH}';\nconst login = '{PATH}';"
    old=finding(text,PATH,kind);native=finding(text,PATH,'AUTH_TOKEN','structural')
    out=merge_results(text,[old,native]).results
    at=text.rindex(PATH)
    assert covered(old,out)
    assert all(any(r.start<=i<r.end for r in out) for i in range(at,at+len(PATH)))

@pytest.mark.parametrize('kind',['PERSON','ORGANIZATION'])
def test_strict_company_field_proves_complete_name_and_correct_type_for_cut_name_fragment(kind):
    text=('материал '*100)+'Исполнитель: ООО Лесной Исток.\n'+('описание '*100)
    at=text.index('Лесной Исток');end=at+len('Лесной Исток')
    bounds=BoundaryValues(text).resolve(end-1,end,entity_type=kind,allow_typed_fields=True)
    assert bounds==(at,end)
    assert bounds.entity_type=='ORGANIZATION'

@pytest.mark.parametrize('kind',['LOGIN','AUTH_TOKEN','PASSWORD','SECRET_KEY'])
def test_company_name_proof_does_not_retype_or_exempt_cut_credentials(kind):
    text=('материал '*100)+'Исполнитель: ООО Лесной Исток.\n'+('описание '*100)
    at=text.index('Лесной Исток');end=at+len('Лесной Исток')
    assert BoundaryValues(text).resolve(end-1,end,entity_type=kind) is None
