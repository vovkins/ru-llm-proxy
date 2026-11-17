"""Add missing characters of NER-backed closed names in numbered source fields.

No new names are detected without NER evidence. Source keys, numbering and
separators remain outside masks; existing results are never changed.
"""
import re
from bisect import bisect_right
from presidio_analyzer import RecognizerResult
from result_merging import DETECTION_SOURCE_METADATA_KEY,SOURCE_NER,MergeDecision,SOURCE_NONE,detection_source

_WORD=r'[А-ЯЁ][а-яё]{1,63}(?:[-\u2010\u2011][А-ЯЁ][а-яё]{1,63}){0,2}'
_INITIALS=r'[А-ЯЁ]\.(?:[ \t\u00a0]{0,8}[А-ЯЁ]\.)?'
_VALUE=rf'(?:(?:им\.|Бюро)[ \t\u00a0]{{1,8}}){_INITIALS}[ \t\u00a0]{{0,8}}{_WORD}'
_LINE=re.compile(r'(?P<n>[1-9][0-9]{0,8}):(?:[ \t]+(?P<body>[^\r\n]*))?\Z')
_FIELD=re.compile(rf'(?P<key>[А-ЯЁA-Z][А-Яа-яЁёA-Za-z_ ]{{0,39}}):[ \t\u00a0]{{1,8}}(?P<value>{_VALUE})[ \t\u00a0]{{0,8}}\Z')
_DATA_KEY=re.compile(r'(?i)(?:данные|наименование|название(?: организации)?|организация|контрагент|data|company|organization)\Z')
MAX_VALUE_CHARS=128

def _candidates(text,check):
    from ner.boundary_values import BoundaryValues,_LINE_END
    values=BoundaryValues(text);values._prepare();inline,unknown=values._markup()
    run=[];start=0
    def completed(records):
        if not records or records[0][0]!=1:return
        for i,(number,a,b,body_start,body) in enumerate(records):
            if not body or i==0:continue
            # A complete source paragraph starts and ends with numbered blanks.
            if records[i-1][4].strip():continue
            ending=next((j for j in range(i+1,len(records)) if not records[j][4].strip()),None)
            if ending is None:continue
            field=_FIELD.fullmatch(body)
            if field is None or _DATA_KEY.fullmatch(field['key']) is None:continue
            left,right=body_start+field.start('value'),body_start+field.end('value')
            if right-left>MAX_VALUE_CHARS or right>unknown:continue
            if any(index.intersects(left,right) for index in (values.string_owners,values.fences,inline,values.references)):continue
            yield left,right
    for separator in list(_LINE_END.finditer(text))+[None]:
        check();end=separator.start() if separator else len(text)
        match=_LINE.fullmatch(text,start,end)
        if match is None or run and int(match['n'])!=run[-1][0]+1:
            yield from completed(run);run=[]
        if match is not None:
            body=match['body'] or '';run.append((int(match['n']),start,end,match.start('body') if body else end,body))
        if separator:start=separator.end()
    yield from completed(run)

def add_numbered_name_field_coverage(text,existing,*,score_threshold=.35,requested_entities=None,check=lambda:None):
    if 'им.' not in text and 'Бюро' not in text:return existing,()
    seeds=[r for r in existing if detection_source(r)==SOURCE_NER and r.entity_type in {'PERSON','ORGANIZATION'} and r.score>=score_threshold]
    if not seeds:return existing,()
    occupied=[]
    for a,b in sorted((r.start,r.end) for r in existing):
        if occupied and a<=occupied[-1][1]:occupied[-1]=(occupied[-1][0],max(b,occupied[-1][1]))
        else:occupied.append((a,b))
    ends=[b for _,b in occupied];added=[]
    for left,right in _candidates(text,check):
        check();support=[r for r in seeds if left<=r.start<r.end<=right and (not requested_entities or r.entity_type in requested_entities)]
        if not support:continue
        anchor=min(support,key=lambda r:r.score);cursor=left
        for a,b in occupied[bisect_right(ends,left):]+[(right,right)]:
            if a>right:a=right
            end=min(a,right)
            while cursor<end and text[cursor].isspace():cursor+=1
            while end>cursor and text[end-1].isspace():end-=1
            if cursor<end:
                added.append(RecognizerResult(anchor.entity_type,cursor,end,anchor.score,recognition_metadata={
                    DETECTION_SOURCE_METADATA_KEY:SOURCE_NER,
                    RecognizerResult.RECOGNIZER_NAME_KEY:'NumberedNameFieldCompletion',
                    'numbered_name_field_completion':True,
                }))
            cursor=max(cursor,b)
            if cursor>=right:break
    if not added:return existing,()
    return tuple(sorted([*existing,*added],key=lambda r:(r.start,r.end,r.entity_type))),(
        MergeDecision('numbered_name_field_coverage_added',SOURCE_NER,SOURCE_NONE),)
