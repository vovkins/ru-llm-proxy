"""Isolated CPU studies, not production profiles or acceptance gates.

The serve command changes only its own process. Diagnostic label compensation
does not reconstruct a pre-training checkpoint or modify the trusted artifact.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
import importlib.metadata
import json
from pathlib import Path
import statistics
import time
import urllib.error
import urllib.request

from .corpus import CorpusCase, EntitySpan, _parse_record, load_corpus
from .metrics import evaluate_predictions
from .spacy_ablation import CONTEXT_CASES, check_context_case

DATA = Path(__file__).with_name("data")
RUNTIME = Path("/tmp/tiny2-study-runtime.json")
DIAGNOSTIC_CORPORA = (
    "ner_migration", "instruction_regressions", "name_context_regressions",
    "name_field_header_control", "name_field_holdout", "person_dialogue_research",
    "person_organization_regressions", "deterministic_rules_review",
)
NAME_LABEL_IDS = (1, 2, 3, 4, 5, 6)


def load_diagnostic_cases(path):
    # Partial research corpora need not contain every class or a critical case;
    # retain the existing record/schema validation, not migration gate rules.
    cases = []
    for index, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        record = json.loads(line)
        if "family" in record:
            family = record.pop("family")
            if not isinstance(family, str) or not family:
                raise ValueError("Invalid deterministic-rule family")
            record["tags"] = [*record["tags"], "family:" + family]
        cases.append(_parse_record(record, line_number=index))
    if not cases or len({case.case_id for case in cases}) != len(cases):
        raise ValueError("Empty corpus or duplicate case IDs")
    return tuple(cases)


def classify_for_study(logits, o_bias, *, compensate_names=False):
    """Change selection only; scores still refer to delivered-model logits."""
    import numpy as np
    from ner.decoding import classify_logits
    if not compensate_names:
        return classify_logits(logits, o_bias)
    choices = logits.copy()
    choices[..., NAME_LABEL_IDS] -= np.float32(2.0)
    ids, _ = classify_logits(choices, o_bias)
    probabilities = np.exp(logits - np.max(logits, axis=-1, keepdims=True))
    probabilities /= probabilities.sum(axis=-1, keepdims=True)
    return ids, np.take_along_axis(probabilities, ids[..., None], axis=-1)[..., 0]


def serve(args):
    import model_profiles
    profile = model_profiles.PROFILES["tiny2"]
    model_profiles.PROFILES["tiny2"] = replace(
        profile, o_logit_bias=args.o_bias,
        spacy_disabled_pipes=() if args.spacy == "full" else profile.spacy_disabled_pipes,
        spacy_sentencizer=args.spacy != "full",
    )
    from ner import triton_recognizer
    triton_recognizer.resolve_recheck = lambda _profile: args.recheck
    if args.compensate_names:
        triton_recognizer.classify_logits = lambda logits, bias: classify_for_study(
            logits, bias, compensate_names=True,
        )
    started = time.perf_counter()
    import analyzer_server
    metadata = {
        "spacy": args.spacy, "o_bias": args.o_bias,
        "compensate_name_selection": args.compensate_names,
        "recheck": args.recheck,
        "active_pipes": analyzer_server.nlp_engine.nlp["ru"].pipe_names,
        "disabled_pipes": analyzer_server.nlp_engine.nlp["ru"].disabled,
        "spacy_recognizer_entities": [r.supported_entities for r in analyzer_server.analyzer.registry.recognizers
                                      if r.__class__.__name__ == "SpacyRecognizer"],
        "analyzer_import_seconds": time.perf_counter() - started,
        "packages": {name: importlib.metadata.version(name) for name in
                     ("spacy", "ru-core-news-sm", "presidio-analyzer", "numpy", "tokenizers", "httpx")},
        "sources": {str(p.relative_to(Path(analyzer_server.__file__).parent)):
                    hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in analyzer_server._analysis_source_files()},
    }
    metadata["study_signature"] = hashlib.sha256(json.dumps(
        {k: v for k, v in metadata.items() if k != "analyzer_import_seconds"},
        sort_keys=True,
    ).encode()).hexdigest()
    RUNTIME.write_text(json.dumps(metadata, indent=2) + "\n")
    import uvicorn
    uvicorn.run(analyzer_server.app, host="0.0.0.0", port=5001, log_level="warning")


def union_coverage(case, predictions):
    """Keep union masking coverage separate from single-span exact metrics."""
    covered = set()
    for item in predictions:
        covered.update(range(item["start"], item["end"]))
    expected = {i for value in case.expected for i in range(value.start, value.end)}
    return {
        "fully_masked_values": sum(all(i in covered for i in range(value.start, value.end))
                                   for value in case.expected),
        "expected_values": len(case.expected),
        "expected_characters": len(expected),
        "masked_expected_characters": len(expected & covered),
        "extra_masked_characters": len(covered - expected),
    }


def scoped_metrics(cases, predictions, *, scope=None):
    """Unannotated classes are not automatically false positives in a study."""
    if scope is None:
        scope = {value.entity_type for case in cases for value in case.expected}
    filtered = {key: [item for item in values if item["entity_type"] in scope]
                for key, values in predictions.items()}
    result = evaluate_predictions(cases, filtered)
    result["evaluation_scope"] = sorted(scope)
    return result


def shell_cases():
    """Fixed-tokenizer boundary probes, with explicit expected secret ranges."""
    from ner.tokenization import ContentTokenizer
    tokenizer = ContentTokenizer("/opt/models/rubert-tiny2-secret-detection")
    cases = []
    templates = (
        ("prompt", "bash-$ tool --flag", None),
        ("zsh_prompt", "zsh> tool --flag", None),
        ("argument", "bash-$ tool '-$'", "-$"),
        ("password", "PASSWORD=-$", "-$"),
        ("token", "AUTH_TOKEN=TestToken42!?", "TestToken42!?"),
        ("unicode_argument", "🙂 bash-$ tool '!?$'", "!?$"),
    )
    for count in (0, 374, 378, 382, 386, 694, 698, 702, 1500):
        for name, template, value in templates:
            text = "текст " * count + "\n" + template + "\n" + "запись " * 450
            expected = ()
            if value is not None:
                start = text.rindex(value)
                kind = "AUTH_TOKEN" if name == "token" else "PASSWORD"
                expected = (EntitySpan(kind, start, start + len(value)),)
            tokens = tokenizer(text)
            target = text.index(template) + (template.rindex(value) if value else template.index("$" if "$" in template else ">"))
            index = next(i for i, (a, b) in enumerate(tokens["offset_mapping"]) if a <= target < b)
            case = CorpusCase(f"shell_{name}_{count}", text, expected, bool(expected),
                              ("positive" if expected else "negative", "shell_boundary"))
            cases.append((case, {"tokens": len(tokens["input_ids"]), "target_token": index}))
    return cases


def _request(url, path, payload=None):
    request = urllib.request.Request(
        url.rstrip("/") + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        return json.load(response)


def windows_from_metrics(text):
    return sum(float(line.split()[-1]) for line in text.splitlines()
               if line.startswith("ru_presidio_analyzer_ner_windows_processed_sum{"))


def window_count(url):
    with urllib.request.urlopen(url.rstrip("/") + "/metrics", timeout=10) as response:
        return windows_from_metrics(response.read().decode())


def nlp_probe(runtime):
    import spacy
    nlp = spacy.load("ru_core_news_sm", disable=runtime["disabled_pipes"])
    if runtime["spacy"] == "light":
        nlp.add_pipe("sentencizer", first=True)
    texts = ("Реквизиты получателя: 770801001.", "Доставка в Нижний Новгород.",
             "Клиент Марина Орлова. Верни строку.", "ООО «Летний сад». Покажи ответ.")
    rows = []
    for text in texts:
        doc = nlp(text)
        rows.append({"text": text,
                     "tokens": [{"start": t.idx, "lemma": t.lemma_, "pos": t.pos_,
                                 "sentence_start": t.is_sent_start} for t in doc],
                     "spacy_entities": [{"start": e.start_char, "end": e.end_char, "label": e.label_}
                                        for e in doc.ents]})
    return rows


def benchmark(url):
    rows = []
    for count in (1, 100, 1000):
        text = "Служебная запись без чувствительных значений. " * count + "Ответственный: Анна Соколова."
        samples = []
        for _ in range(3):
            start = time.perf_counter()
            _request(url, "/api/v1/analyze", {"text": text, "score_threshold": .35})
            samples.append(time.perf_counter() - start)
        rows.append({"characters": len(text), "median_seconds": statistics.median(samples),
                     "samples_seconds": samples})
    return rows


def evaluate(args):
    report = {"variant": args.variant, "evaluator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "runtime": json.loads(RUNTIME.read_text()),
              "health": _request(args.url, "/api/v1/health"), "corpora": {}}
    groups = {}
    if args.split == "development":
        groups.update({name: [(case, {}) for case in load_diagnostic_cases(DATA / (name + ".jsonl"))]
                       for name in DIAGNOSTIC_CORPORA})
    study = load_corpus(DATA / "tiny2_component_study.jsonl")
    groups[args.split] = [(case, {}) for case in study if args.split in case.tags]
    if args.split == "shell":
        groups = {"shell": shell_cases()}
    for name, entries in groups.items():
        rows, predictions, durations = [], {}, []
        for case, extra in entries:
            before = window_count(args.url) if name == "shell" else None
            started = time.perf_counter()
            try:
                response = _request(args.url, "/api/v1/analyze", {
                    "text": case.text, "score_threshold": case.score_threshold,
                })
                entities = response["entities"]
                status = "ok"
            except urllib.error.HTTPError as error:
                entities, status = [], f"http_{error.code}"
            elapsed = time.perf_counter() - started
            if before is not None:
                extra = {**extra, "processed_windows": window_count(args.url) - before}
            durations.append(elapsed)
            predictions[case.case_id] = entities
            rows.append({"id": case.case_id, "status": status, "seconds": elapsed,
                         "characters": len(case.text), **extra,
                         "expected": [{"entity_type": e.entity_type, "start": e.start, "end": e.end}
                                      for e in case.expected],
                         "predictions": [{k: value for k, value in item.items() if k != "text"}
                                         for item in entities],
                         "coverage": union_coverage(case, entities)})
        metrics = scoped_metrics(
            [case for case, _ in entries], predictions,
            scope={"LOGIN", "PASSWORD", "AUTH_TOKEN", "SECRET_KEY"} if name == "shell" else None,
        )
        report["corpora"][name] = {
            "metrics": metrics, "rows": rows,
            "median_seconds": statistics.median(durations),
            "failures": sum(row["status"] != "ok" for row in rows),
        }
    if args.split == "development":
        contexts = []
        for case in CONTEXT_CASES:
            response = _request(args.url, "/api/v1/analyze", {"text": case.text, "score_threshold": .35})
            contexts.append(check_context_case(case, response["entities"]))
        report["contexts"] = contexts
        report["nlp_probe"] = nlp_probe(report["runtime"])
        report["benchmark"] = benchmark(args.url)
    report["server_memory_kib"] = {line.split(":", 1)[0]: int(line.split()[1])
                                   for line in Path("/proc/1/status").read_text().splitlines()
                                   if line.startswith(("VmRSS:", "VmHWM:"))}
    report["data_sha256"] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                             for p in DATA.glob("*.jsonl")}
    report["note"] = "Diagnostic open data; no independent validation or acceptance threshold. HTTP failures are refusals, not successful leaks."
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"variant": args.variant, "split": args.split,
                      "cases": sum(len(x["rows"]) for x in report["corpora"].values()),
                      "failures": sum(x["failures"] for x in report["corpora"].values())}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    server = commands.add_parser("serve")
    server.add_argument("--spacy", choices=("full", "light"), required=True)
    server.add_argument("--o-bias", type=float, choices=(0., 1.), required=True)
    server.add_argument("--compensate-names", action="store_true")
    server.add_argument("--recheck", choices=("off", "shell-prompt"), default="off")
    runner = commands.add_parser("evaluate")
    runner.add_argument("--url", default="http://127.0.0.1:5001")
    runner.add_argument("--variant", required=True)
    runner.add_argument("--split", choices=("development", "control", "shell"), required=True)
    runner.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    (serve if args.command == "serve" else evaluate)(args)


if __name__ == "__main__":
    main()
