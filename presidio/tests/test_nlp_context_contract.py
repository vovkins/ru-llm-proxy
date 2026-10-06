"""Document why empty NLP artifacts cannot replace context-aware analysis."""

import pytest

from presidio_analyzer import AnalyzerEngine, RecognizerRegistry
from presidio_analyzer.nlp_engine import NlpEngineProvider, NoOpNlpEngine

from presidio.evaluation.spacy_ablation import CONTEXT_CASES, check_context_case
from presidio.recognizers import ALL_RECOGNIZERS


@pytest.fixture(scope="module", params=("a", "b", "c"))
def context_engine(request):
    variant = request.param
    if variant == "c":
        nlp = NoOpNlpEngine(models=[{"lang_code": "ru", "model_name": ""}])
        nlp.load()
    else:
        nlp = NlpEngineProvider(
            nlp_configuration={
                "nlp_engine_name": "spacy",
                "models": [{"lang_code": "ru", "model_name": "ru_core_news_sm"}],
            }
        ).create_engine()
        if variant == "b":
            nlp.nlp["ru"].disable_pipe("ner")
    registry = RecognizerRegistry(supported_languages=["ru"])
    for recognizer in ALL_RECOGNIZERS:
        registry.add_recognizer(recognizer())
    engine = AnalyzerEngine(
        registry=registry, nlp_engine=nlp, supported_languages=["ru"]
    )
    return variant, engine


@pytest.mark.parametrize(
    "case",
    [
        case
        for case in CONTEXT_CASES
        if case.case_id
        in {
            "kpp",
            "address_bare",
            "address_multiword",
            "inn",
            "bik",
            "passport_foreign",
            "passport_military",
            "passport_birth",
        }
    ],
    ids=lambda case: case.case_id,
)
def test_context_dependency_is_explicit(context_engine, case):
    variant, engine = context_engine
    results = engine.analyze(case.text, language="ru", score_threshold=0.35)
    row = check_context_case(case, [result.to_dict() for result in results])
    assert row["typed_covered"] is (variant != "c")
