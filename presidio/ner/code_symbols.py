"""Syntax-only JS/TS symbol evidence; no execution or string-data exemption."""
from functools import lru_cache
import re
import time

MAX_CODE_BYTES = 256 * 1024
MAX_PARSE_SECONDS = .05
_LANGS = {'js':'js','javascript':'js','jsx':'js','ts':'ts','typescript':'ts','tsx':'tsx'}
_FENCE = re.compile(r'(?m)^ {0,3}(`{3,}|~{3,})([^\r\n]*)')
_ROLES = {'identifier','property_identifier','type_identifier'}
_MODULE_PART = r'[a-z0-9][a-z0-9._-]*'
_SCOPED_MODULE = re.compile(r'@'+_MODULE_PART+r'/'+_MODULE_PART+r'(?:/'+_MODULE_PART+r')*\Z')


@lru_cache(maxsize=1)
def _languages():
    try:
        from tree_sitter import Language
        import tree_sitter_javascript as js
        import tree_sitter_typescript as ts
        return {'js':Language(js.language()),'ts':Language(ts.language_typescript()),'tsx':Language(ts.language_tsx())}
    except (ImportError,AttributeError,TypeError,ValueError):
        return {}


def _parse(source, language):
    from tree_sitter import Parser
    parser=Parser(language);deadline=time.perf_counter()+MAX_PARSE_SECONDS
    def read(offset,point):
        return source[offset:offset+4096]
    def progress(*_):return time.perf_counter()>deadline
    try:
        tree=parser.parse(read,encoding='utf8',progress_callback=progress)
    except (ValueError,TypeError,RuntimeError,RecursionError):
        return None
    return tree if tree is not None and not tree.root_node.has_error else None


def _roles(tree):
    proved=set();opaque=[];stack=[tree.root_node]
    def add(node):
        if node is not None and node.type in _ROLES:
            proved.add((node.start_byte,node.end_byte))
    def callee(node):
        while node is not None and node.type=='member_expression':
            add(node.child_by_field_name('property'))
            node=node.child_by_field_name('object')
        add(node)
    while stack:
        node=stack.pop();kind=node.type
        if kind in {'string','template_string','comment','regex'}:
            opaque.append((node.start_byte,node.end_byte))
            continue
        if kind in {'function_declaration','generator_function_declaration','function_expression',
                    'generator_function','class_declaration','class','method_definition'}:
            add(node.child_by_field_name('name'))
        elif kind=='variable_declarator':
            add(node.child_by_field_name('name'))
        elif kind in {'assignment_expression','augmented_assignment_expression'}:
            left=node.child_by_field_name('left')
            if left is not None and left.type=='identifier':add(left)
        elif kind=='call_expression':callee(node.child_by_field_name('function'))
        elif kind=='new_expression':callee(node.child_by_field_name('constructor'))
        elif kind in {'import_clause','import_specifier','namespace_import'}:
            for child in node.named_children:
                if child.type=='identifier':add(child)
        stack.extend(reversed(node.named_children))
    return proved,opaque


def _character_spans(source, spans):
    needed={p for span in spans for p in span};mapping={};offset=0
    for i,ch in enumerate(source):
        if offset in needed:mapping[offset]=i
        offset+=len(ch.encode('utf8'))
    if offset in needed:mapping[offset]=len(source)
    return {(mapping[a],mapping[b]) for a,b in spans if a in mapping and b in mapping}


def _module_sources(tree):
    """Return plain ASCII scoped package sources of static import/export only."""
    spans=set();stack=[tree.root_node]
    while stack:
        node=stack.pop()
        if node.type in {'string','template_string','comment','regex'}:
            continue
        if node.type in {'import_statement','export_statement'}:
            source=node.child_by_field_name('source')
            if source is not None and source.type=='string':
                raw=source.text
                if raw[:1] in {b"'",b'"'} and raw[-1:]==raw[:1]:
                    try:value=raw[1:-1].decode('ascii')
                    except UnicodeDecodeError:value=''
                    if _SCOPED_MODULE.fullmatch(value):
                        spans.add((source.start_byte+1,source.end_byte-1))
        stack.extend(reversed(node.named_children))
    return spans


def _javascript_spans(text, strings, extract):
    languages=_languages()
    if not languages:return set()
    try:encoded=text.encode('utf8')
    except UnicodeEncodeError:return set()
    if len(encoded)>MAX_CODE_BYTES:return set()
    fences=list(_FENCE.finditer(text))
    explicit_document = (
        len(fences)==2 and not text[:fences[0].start()].strip()
        and fences[0][2].strip() in _LANGS and not fences[1][2].strip()
        and fences[0][1][0]==fences[1][1][0] and len(fences[1][1])>=len(fences[0][1])
        and not text[fences[1].end():].strip()
    )
    proved=set();valid_whole=False
    for language in (() if explicit_document else languages.values()):
        tree=_parse(encoded,language)
        if tree is not None:
            valid_whole=True;roles=extract(tree)
            proved.update(_character_spans(text,roles))
    # A valid whole program already owns comments/templates; do not reinterpret
    # a Markdown example inside them as independent code.
    if valid_whole:return proved
    if _FENCE.search(text) is None:return proved
    from ner.boundary_values import BoundaryValues
    values=BoundaryValues(text);values._prepare();_closed,unknown=values._markup()
    opening=None
    for match in _FENCE.finditer(text):
        marker,info=match[1],match[2].strip()
        if opening is None:
            if strings.at(match.start()) is not None:continue
            opening=(match,marker,_LANGS.get(info))
        elif marker[0]==opening[1][0] and len(marker)>=len(opening[1]) and not info:
            first,_,language=opening;opening=None
            if language is None or first.start()>unknown:continue
            prefix=text[:first.start()]
            if prefix.rfind('/*')>prefix.rfind('*/'):continue
            start=first.end()
            if text[start:start+2]=='\r\n':start+=2
            elif text[start:start+1] in {'\r','\n'}:start+=1
            source=text[start:match.start()]
            tree=_parse(source.encode('utf8'),languages[language])
            if tree is not None:
                roles=extract(tree)
                proved.update((start+a,start+b) for a,b in _character_spans(source,roles))
    return proved


def javascript_symbol_spans(text, strings):
    """Return only source-coordinate symbols in complete supported syntax."""
    return _javascript_spans(text,strings,lambda tree:_roles(tree)[0])


def javascript_module_spans(text, strings):
    """Return only exact source coordinates of plain scoped static imports."""
    return _javascript_spans(text,strings,_module_sources)
