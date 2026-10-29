"""Frozen diagnostic and public-data inputs for the E9 comparison.

PyArrow is an optional research-reader dependency, not an Analyzer dependency.
Raw public rows are downloaded separately at the exact revision below.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import replace
import hashlib
import json
from pathlib import Path

from .corpus import CorpusCase, EntitySpan, TARGET_ENTITY_TYPES
from .spacy_ablation import CONTEXT_CASES
from .tiny2_comparison import ComparisonCase, manifest
from .tiny2_components import DIAGNOSTIC_CORPORA, load_diagnostic_cases

PII_REVISION = "cd6a18ace16daf23e79247ccf1e2b245d4054654"
PII_FILES_SHA256 = {
    "entity.parquet": "6e0c77d566c2f04e7213917b26f1038336e28fc006551fac7a7f44f7627d5f96",
    "domain.parquet": "7ef3574273c0fe3a987981e38e32cd2764031a82766ca5b235bfccd1f27058eb",
}
PII_TYPES = {
    "NAME": "PERSON", "ADDRESS": "RU_ADDRESS", "EMAIL": "EMAIL_ADDRESS",
    "PHONE_NUMBER": "PHONE_NUMBER", "BANK_CARD_NUMBER": "CREDIT_CARD",
    "INN": "RU_INN", "KPP": "RU_KPP", "OGRN": "RU_OGRN", "OGRNIP": "RU_OGRNIP",
    "SNILS": "RU_SNILS", "PASSPORT_NUMBER": "RU_PASSPORT", "TOKEN": "AUTH_TOKEN",
    "CVC": "CVC_OUT_OF_SCOPE",
}


def diagnostic_cases(root):
    data = root / "presidio/evaluation/data"
    cases = []
    for name in (*DIAGNOSTIC_CORPORA, "tiny2_component_study"):
        loaded = load_diagnostic_cases(data / (name + ".jsonl"))
        annotated = frozenset(e.entity_type for c in loaded for e in c.expected)
        scope = annotated if name in {"person_dialogue_research", "deterministic_rules_review"} else TARGET_ENTITY_TYPES | annotated
        for case in loaded:
            cases.append(ComparisonCase(replace(case, case_id=name + "__" + case.case_id),
                                        name, scope, name == "instruction_regressions"))
    for context in CONTEXT_CASES:
        expected = ()
        if context.positive:
            start = context.text.index(context.value)
            expected = (EntitySpan(context.entity_type, start, start + len(context.value)),)
        case = CorpusCase("context__" + context.case_id, context.text, expected, bool(expected),
                          ("positive" if expected else "negative", "existing_context"))
        cases.append(ComparisonCase(case, "all_class_context", frozenset({context.entity_type})))
    for row in json.loads((root / "tests/fixtures/ner-value-repair.json").read_text()):
        expected = []
        for value, kind in row.get("analyzer_expected", row["expected"]):
            start = row["text"].index(value)
            expected.append(EntitySpan(kind, start, start + len(value)))
        case = CorpusCase("value_repair__" + row["id"].replace("-", "_"), row["text"], tuple(expected), True,
                          ("positive", "existing_value_boundary"))
        cases.append(ComparisonCase(case, "value_repair", frozenset(e.entity_type for e in expected)))
    return cases


def dkb_cases(root):
    directory = root / "tests/fixtures/dkb"
    cases = []
    for row in json.loads((directory / "manifest.json").read_text()):
        raw = (directory / row["file"]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != row["sha256"]:
            raise ValueError("DKB input differs from its frozen manifest")
        text = raw.decode("utf-8")
        spans = tuple(EntitySpan(e["entities"][0], e["start"], e["end"]) for e in row["expected"])
        for form in ("plain", "json"):
            source, expected = text, spans
            if form == "json":
                source = json.dumps({"source": text}, ensure_ascii=False)
                cursor = source.index(": ") + 3
                offsets = [cursor]
                for char in text:
                    cursor += len(json.dumps(char, ensure_ascii=False)[1:-1])
                    offsets.append(cursor)
                expected = tuple(EntitySpan(e.entity_type, offsets[e.start], offsets[e.end]) for e in spans)
            case = CorpusCase("dkb_" + form + "__" + row["file"].replace(".", "_"), source, expected, bool(expected),
                              ("positive" if expected else "negative", "existing_dkb", form))
            cases.append(ComparisonCase(case, "dkb_" + form,
                                        frozenset(e.entity_type for e in expected) or TARGET_ENTITY_TYPES))
    return cases


def window_cases():
    cases = []
    probes = (
        ("person", "Ответственный: Анна Соколова.", "Анна Соколова", "PERSON"),
        ("password", "PASSWORD=Alp/ha+42!?", "Alp/ha+42!?", "PASSWORD"),
        ("contract", "Договор № 2026/АГ/6581.", "2026/АГ/6581", "CONTRACT_NUMBER"),
        ("unicode", "🙂 Клиент Ива\u0301н Петров.", "Ива\u0301н Петров", "PERSON"),
    )
    for count in (0, 374, 380, 386, 700, 1500):
        for name, fragment, value, kind in probes:
            text = "текст " * count + "\n" + fragment + "\n" + "запись " * 400
            start = text.index(value)
            case = CorpusCase(f"window__{name}_{count}", text,
                              (EntitySpan(kind, start, start + len(value)),), True,
                              ("positive", "window", "frozen_before_e9_inference"))
            cases.append(ComparisonCase(case, "windows", frozenset({kind}), origin="diagnostic_new_not_independent"))
    return cases


def public_cases(directory):
    import pyarrow.parquet as pq
    cases, files = [], {}
    for split in ("entity", "domain"):
        path = directory / (split + ".parquet")
        files[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
        if files[path.name] != PII_FILES_SHA256[path.name]:
            raise ValueError("PII-Bench file differs from the pinned revision")
        rows = pq.read_table(path).to_pylist()
        if len(rows) != {"entity": 910, "domain": 900}[split]:
            raise ValueError("Unexpected PII-Bench split size")
        for row in rows:
            expected = []
            for span in row["entities"]:
                if row["text"][span["start"]:span["end"]] != span["text"]:
                    raise ValueError("Public annotation text/offset mismatch")
                expected.append(EntitySpan(PII_TYPES[span["type"]], span["start"], span["end"]))
            case = CorpusCase("pii_" + split + "__" + row["id"], row["text"], tuple(expected), False,
                              ("positive" if expected else "negative", "public_control", row["domain"]))
            # Complete for the benchmark's 13 classes, not all project classes.
            cases.append(ComparisonCase(case, "pii_" + split, frozenset(PII_TYPES.values()), False,
                                        origin="public_first_local_evaluation_training_overlap_unknown"))
    return cases, files


def load_cases(root: Path, public: Path | None = None):
    cases = diagnostic_cases(root) + dkb_cases(root) + window_cases()
    sources = {}
    if public is not None:
        external, sources = public_cases(public)
        cases.extend(external)
    rows = manifest(cases)
    ids = [row["case_id"] for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate comparison IDs")
    texts = Counter(row["text_sha256"] for row in rows)
    return cases, {
        "case_count": len(cases), "corpus_counts": dict(Counter(item.corpus for item in cases)),
        "unique_texts": len(texts), "duplicate_text_occurrences": sum(n - 1 for n in texts.values()),
        "manifest_sha256": hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest(),
        "public_dataset": {"repository": "hivetrace/pii-bench", "revision": PII_REVISION,
                           "license": "apache-2.0", "files_sha256": sources,
                           "type_mapping": PII_TYPES,
                           "notes": ["Not proven identical to author validation (8224 rows).",
                                     "Author already evaluated PII-Bench; not independent of model selection.",
                                     "ADDRESS full address maps to RU_ADDRESS; LOCATION constituents may still cover characters.",
                                     "TOKEN maps strictly to AUTH_TOKEN; alternate secret classes remain visible in untyped coverage.",
                                     "CVC is explicitly outside approved product scope; no new detector is added.",
                                     "Clean/extra character counts refer only to annotation scope, not every project class."]},
    }
