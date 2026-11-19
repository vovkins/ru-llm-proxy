"""Close a disputed span after the existing boundary resolver fails."""

import regex
from string_spans import StringSpans

JOINERS = "-'\u2010\u2011\u2019"
WORD = regex.compile(rf"\p{{L}}[\p{{L}}\p{{M}}]*(?:[{JOINERS}]\p{{L}}[\p{{L}}\p{{M}}]*)*\.?")
SPACE = " \t\u00a0"
SEPARATORS = frozenset("\r\n\u2028\u2029[]:|()=/,;.!?")


def name_form(s):
    if len(s) == 2 and s[0].isupper() and s[1] == ".":
        return True
    s = s.rstrip(".")
    return (
        len(s) >= 2 and s[0].isupper() and any(c.islower() for c in s) and bool(WORD.fullmatch(s))
    )


def owner_bounds(text, start, end):
    strings = StringSpans(text)
    owner = strings.at(start)
    other = strings.at(end - 1)
    if owner is not other:
        return None
    if owner is not None:
        if not owner.content_start <= start < end <= owner.content_end:
            return None
        return owner.content_start, owner.content_end
    left, right = 0, len(text)
    for literal in strings.spans:
        if literal.end <= start:
            left = max(left, literal.end)
        elif literal.start >= end:
            right = min(right, literal.start)
            break
        elif literal.start < end and start < literal.end:
            return None
    return left, right


def neighbour(text, edge, direction, limits):
    lo, hi = limits
    at = edge
    if direction < 0:
        while at > lo and text[at - 1] in SPACE:
            at -= 1
        if at <= lo:
            return None, "value_edge"
        c = text[at - 1]
        if c in SEPARATORS or c.isdigit():
            return None, "separator"
        words = list(WORD.finditer(text, lo, at))
        m = words[-1] if words and words[-1].end() == at else None
    else:
        while at < hi and text[at] in SPACE:
            at += 1
        if at >= hi:
            return None, "value_edge"
        c = text[at]
        if c in SEPARATORS or c.isdigit():
            return None, "separator"
        m = WORD.match(text, at, hi)
    if m is None:
        return None, "other_delimiter"
    return (m, "name_word") if name_form(m.group()) else (None, "non_name_word")


def _close_directionally(text, cluster, check):
    from . import triton_recognizer as nr

    a = min(p.entity.start for p in cluster.predictions)
    b = max(p.entity.end for p in cluster.predictions)
    kind = cluster.entity_type
    original = (a, b)
    left = not any(p.entity.start == a and not p.touches_left_boundary for p in cluster.predictions)
    right = not any(p.entity.end == b and not p.touches_right_boundary for p in cluster.predictions)
    event = dict(
        entity_type=kind,
        start=a,
        end=b,
        left_cut=left,
        right_cut=right,
        rule="unresolved",
        added_characters=0,
        closure_capped=False,
    )
    limits = owner_bounds(text, a, b)
    if limits is None:
        event["reason"] = "cross_literal"
        return None, event
    lo, hi = limits
    new = [a, b]
    reasons = []
    capped = False
    for side, cut in [(0, left), (1, right)]:
        check()
        if not cut:
            continue
        edge = new[side]
        direction = -1 if side == 0 else 1
        # Existing positive word fragments are completed only toward the observed cut.
        inside = 0 < edge < len(text) and text[edge - 1].isalpha() and text[edge].isalpha()
        if inside:
            if side == 0:
                while new[0] > lo and (text[new[0] - 1].isalpha() or text[new[0] - 1] in JOINERS):
                    new[0] -= 1
            else:
                while new[1] < hi and (text[new[1]].isalpha() or text[new[1]] in JOINERS):
                    new[1] += 1
        words = 0
        while True:
            m, why = neighbour(text, new[side], direction, limits)
            if why != "name_word":
                # Document/value limits are a cap, not independently proven full-name boundaries.
                if why == "value_edge":
                    capped = True
                reasons.append(why)
                break
            if words == 2:
                capped = True
                reasons.append("two_word_cap")
                break
            new[side] = m.start() if side == 0 else m.end()
            words += 1
    if not lo <= new[0] <= a < b <= new[1] <= hi:
        event["reason"] = "outside_value"
        return None, event
    event.update(
        closed_start=new[0],
        closed_end=new[1],
        rule="directional_names" if tuple(new) != original else "delimiter",
        added_characters=new[1] - new[0] - (b - a),
        closure_capped=capped,
        boundary_reasons=reasons,
    )
    return (
        nr.WindowEntityPrediction(
            nr.EntityPrediction(kind, *new, min(p.entity.score for p in cluster.predictions)),
            min(p.window_index for p in cluster.predictions),
            False,
            False,
        ),
        event,
    )


def partition(predictions):
    groups = {}
    for p in predictions:
        groups.setdefault(p.entity.entity_type, []).append(p)
    output = []
    for kind, items in sorted(groups.items()):
        ordered = sorted(items, key=lambda p: (p.entity.start, p.entity.end))
        cluster = []
        end = -1
        for p in ordered:
            if cluster and p.entity.start >= end:
                output.append(cluster)
                cluster = []
            cluster.append(p)
            end = max(end, p.entity.end) if len(cluster) > 1 else p.entity.end
        if cluster:
            output.append(cluster)
    return output


def close_cluster(text, cluster, check):
    from . import triton_recognizer as nr
    if cluster.entity_type != "PERSON":
        raise ValueError("Boundary closure requires PERSON evidence")

    raw_a = min(p.entity.start for p in cluster.predictions)
    raw_b = max(p.entity.end for p in cluster.predictions)
    limits = owner_bounds(text, raw_a, raw_b)
    # Mixed identifiers are protected as whole non-whitespace tokens.
    if limits is not None:
        lo, hi = limits
        a, b = raw_a, raw_b
        while a > lo and not text[a - 1].isspace():
            a -= 1
        while b < hi and not text[b].isspace():
            b += 1
        token = text[a:b]
        if any(c.isdigit() or c == "_" for c in token):
            # Preserve actual syntax outside a known literal. An unmaskable mixed token
            # is refused rather than silently reverting to a partial token.
            owner = StringSpans(text).at(raw_a)
            if owner is None and any(c in '{}[]()=,:"' for c in token):
                return None, dict(
                    entity_type="PERSON",
                    start=raw_a,
                    end=raw_b,
                    rule="mixed_token_refused",
                    reason="syntax_crossing",
                    added_characters=0,
                    closure_capped=False,
                )
            score = min(p.entity.score for p in cluster.predictions)
            event = dict(
                entity_type="PERSON",
                start=raw_a,
                end=raw_b,
                closed_start=a,
                closed_end=b,
                rule="mixed_token",
                added_characters=b - a - (raw_b - raw_a),
                closure_capped=False,
                left_cut=any(p.touches_left_boundary for p in cluster.predictions),
                right_cut=any(p.touches_right_boundary for p in cluster.predictions),
            )
            return (
                nr.WindowEntityPrediction(
                    nr.EntityPrediction("PERSON", a, b, score),
                    min(p.window_index for p in cluster.predictions),
                    False,
                    False,
                ),
                event,
            )
    return _close_directionally(text, cluster, check)
