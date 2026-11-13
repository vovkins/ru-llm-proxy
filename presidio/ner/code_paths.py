"""Expose only verified JS/TS filename suffix syntax; never unmask path names."""
import json
import re
from string_spans import StringSpans
from ner.code_symbols import javascript_symbol_spans

MAX_JSON_PATH_CHARS=65536
_PATH=re.compile(r'(?:[A-Za-z0-9_-][A-Za-z0-9_.-]*/)*[A-Za-z0-9_-][A-Za-z0-9_.-]*\.(?:tsx|jsx|ts|js)\Z')


def _unique_object(pairs):
    value={}
    for key,item in pairs:
        if key in value:raise ValueError('Duplicate key')
        value[key]=item
    return value


def json_code_path_suffixes(text):
    """Return (value-start, suffix-start, value-end) in original JSON offsets."""
    if len(text)>MAX_JSON_PATH_CHARS or not text.lstrip().startswith('{'):return ()
    try:obj=json.loads(text,object_pairs_hook=_unique_object)
    except (ValueError,RecursionError):return ()
    if not isinstance(obj,dict):return ()
    key='path' if set(obj)=={'path','content'} else 'file_path' if set(obj)=={'file_path','content'} else None
    if key is None or not all(isinstance(obj[k],str) for k in [key,'content']):return ()
    path,content=obj[key],obj['content']
    if not _PATH.fullmatch(path) or any(p in {'.','..'} for p in path.split('/')):return ()
    if not content or not javascript_symbol_spans(content,StringSpans(content)):return ()
    from recognizers.json_strings import JsonArgumentView
    view=JsonArgumentView.parse(text,lambda:None)
    if view is None:return ()
    suffix=path.rindex('.')
    return tuple((s.source_offsets[0],s.source_offsets[suffix],s.source_offsets[-1])
                 for s in view.scalars if not s.is_key and s.key==key and s.decoded==path)
