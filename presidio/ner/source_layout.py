"""Keep display syntax outside confirmed PERSON values in numbered source.

Every non-layout character of a positive finding remains covered. Independent
native evidence on proposed syntax vetoes the exclusion, for every entity type.
"""

import re
from itertools import chain

from result_merging import (
    NER_BOUNDARY_CONTAINER_KEY,
    NER_PERSON_CONTROL_KEY,
    SOURCE_NER,
    _copy_name,
    detection_source,
)


_RECORD = re.compile(
    r"[ \t]{0,3}(?P<n>[1-9][0-9]{0,8}):(?P<space>[ \t]+|$)"
    r"(?P<body>[^\r\n]*)\Z"
)
_KEY = re.compile(r"[^\W\d_][\w -]{0,63}(?P<colon>:)(?=[ \t]|$)")
_BREAK = re.compile(r"\r\n|\r|\n|\u2028|\u2029")


def _syntax(text, check):
    """Recognize consecutive display records starting at one, never lone labels."""
    run = []
    start = 0

    def finish(records):
        if len(records) < 2 or records[0][0] != 1:
            return
        for index, (_, left, _, prefix_end, body) in enumerate(records):
            if index:
                yield records[index - 1][2], left
            yield left, prefix_end
            key = _KEY.match(body)
            if key:
                # The key itself can be a sensitive name. Keep its letters hidden.
                yield prefix_end + key.start("colon"), prefix_end + key.end("colon")

    for separator in chain(_BREAK.finditer(text), (None,)):
        check()
        end = separator.start() if separator else len(text)
        match = _RECORD.fullmatch(text, start, end)
        if match is None or (run and int(match["n"]) != run[-1][0] + 1):
            yield from finish(run)
            run = []
        if match:
            run.append((int(match["n"]), start, end, match.start("body"), match["body"]))
        if separator:
            start = separator.end()
    yield from finish(run)


def project_numbered_person_layout(text, results, *, check=lambda: None):
    """Subtract proven display syntax after required positive-evidence checks.

    Strings, fences, inline/reference markup and unknown ownership stay opaque.
    Ordinary NER findings and native findings do not receive this projection.
    """
    eligible_ids = set()
    for result in results:
        metadata = result.recognition_metadata or {}
        if (result.entity_type == "PERSON" and detection_source(result) == SOURCE_NER
                and (metadata.get(NER_PERSON_CONTROL_KEY) is True
                     or metadata.get(NER_BOUNDARY_CONTAINER_KEY) is True)):
            eligible_ids.add(id(result))
    if not eligible_ids:
        return results, ()

    from ner.boundary_values import BoundaryValues

    values = BoundaryValues(text)
    values._prepare()
    inline, unknown = values._markup()
    opaque = (values.string_owners, values.fences, inline, values.references)
    native = [result for result in results if detection_source(result) != SOURCE_NER]
    syntax = tuple(
        (left, right) for left, right in _syntax(text, check)
        if left < right <= unknown
        and not any(index.intersects(left, right) for index in opaque)
        and not any(result.start < right and left < result.end for result in native)
    )
    output, proof = [], []
    for result in results:
        check()
        if id(result) not in eligible_ids:
            output.append(result)
            continue
        exclusions = sorted(
            (max(left, result.start), min(right, result.end))
            for left, right in syntax if left < result.end and result.start < right
        )
        if not exclusions:
            output.append(result)
            continue
        merged = []
        for left, right in exclusions:
            if merged and left <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(right, merged[-1][1]))
            else:
                merged.append((left, right))

        cursor, pieces = result.start, []
        for left, right in merged:
            if cursor < left:
                pieces.append((cursor, left))
            cursor = max(cursor, right)
        if cursor < result.end:
            pieces.append((cursor, result.end))
        for left, right in pieces:
            item = _copy_name(result, start=left, end=right)
            item.recognition_metadata["numbered_source_layout_projected"] = True
            output.append(item)
        proof.append({
            "entity_type": result.entity_type, "original": [result.start, result.end],
            "syntax": merged, "values": pieces,
        })
    return tuple(sorted(output, key=lambda item: (item.start, item.end, item.entity_type))), tuple(proof)
