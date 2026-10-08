"""Compare isolated Analyzer variants without changing production settings."""

from __future__ import annotations

import argparse
import hashlib
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .corpus import DEFAULT_CORPUS_PATH, load_corpus
from .metrics import evaluate_predictions
from .run_baseline import AnalyzerClient


@dataclass(frozen=True)
class ContextCase:
    case_id: str
    text: str
    entity_type: str
    value: str
    positive: bool = True


# Expected values are fixed before comparing engines. Negative pairs concern
# the named type, not every possible entity in the surrounding sentence.
CONTEXT_CASES = (
    ContextCase("kpp", "КПП получателя: 770801001", "RU_KPP", "770801001"),
    ContextCase(
        "kpp_requisites", "Реквизиты получателя: 770801001", "RU_KPP", "770801001"
    ),
    ContextCase(
        "kpp_bare",
        "Код строки 770801001 указан в таблице",
        "RU_KPP",
        "770801001",
        False,
    ),
    ContextCase("kpp_invalid", "КПП: 000000000", "RU_KPP", "000000000", False),
    ContextCase(
        "address_bare", "Адрес регистрации: ул Ленина 10", "RU_ADDRESS", "ул Ленина 10"
    ),
    ContextCase(
        "address_multiword",
        "Адрес: ул Академика Королева 12",
        "RU_ADDRESS",
        "ул Академика Королева 12",
    ),
    ContextCase(
        "address_explicit",
        "Адрес: ул. Ленина, д. 10",
        "RU_ADDRESS",
        "ул. Ленина, д. 10",
    ),
    ContextCase(
        "address_currency",
        "Адрес вопроса: ул Ленина 10 рублей стоит билет",
        "RU_ADDRESS",
        "ул Ленина 10",
        False,
    ),
    ContextCase(
        "address_duration",
        "ул Маршала Жукова 5 лет обсуждали",
        "RU_ADDRESS",
        "ул Маршала Жукова 5",
        False,
    ),
    ContextCase("inn", "ИНН: 7707083893", "RU_INN", "7707083893"),
    ContextCase("inn_bare", "7707083893", "RU_INN", "7707083893"),
    ContextCase("inn_invalid", "ИНН: 7707083894", "RU_INN", "7707083894", False),
    ContextCase(
        "phone", "Телефон: +7 903 123 45 67", "PHONE_NUMBER", "+7 903 123 45 67"
    ),
    ContextCase("phone_short", "Цена 12345 рублей", "PHONE_NUMBER", "12345", False),
    ContextCase(
        "email", "Почта: test@example.com", "EMAIL_ADDRESS", "test@example.com"
    ),
    ContextCase("ogrn", "ОГРН: 1027700132195", "RU_OGRN", "1027700132195"),
    ContextCase(
        "ogrn_invalid", "ОГРН: 1027700132196", "RU_OGRN", "1027700132196", False
    ),
    ContextCase("ogrnip", "ОГРНИП: 304500116000157", "RU_OGRNIP", "304500116000157"),
    ContextCase(
        "ogrnip_invalid",
        "ОГРНИП: 304500116000158",
        "RU_OGRNIP",
        "304500116000158",
        False,
    ),
    ContextCase("bik", "БИК банка: 044525225", "RU_BIK", "044525225"),
    ContextCase(
        "bik_bare", "Номер 044525225 указан в таблице", "RU_BIK", "044525225", False
    ),
    ContextCase(
        "settlement",
        "Расчетный счет 40702810900000000000, БИК 044525225",
        "RU_SETTLEMENT_ACCOUNT",
        "40702810900000000000",
    ),
    ContextCase(
        "settlement_invalid",
        "Расчетный счет 40702810900000000001, БИК 044525225",
        "RU_SETTLEMENT_ACCOUNT",
        "40702810900000000001",
        False,
    ),
    ContextCase(
        "correspondent",
        "БИК 044525225, к/с 30101810400000000225",
        "RU_CORRESPONDENT_ACCOUNT",
        "30101810400000000225",
    ),
    ContextCase(
        "correspondent_invalid",
        "БИК 044525225, к/с 30101810400000000226",
        "RU_CORRESPONDENT_ACCOUNT",
        "30101810400000000226",
        False,
    ),
    ContextCase("snils", "СНИЛС 001-234-567 84", "RU_SNILS", "001-234-567 84"),
    ContextCase(
        "snils_invalid", "СНИЛС 001-234-567 85", "RU_SNILS", "001-234-567 85", False
    ),
    ContextCase("passport", "Паспорт РФ 45 12 №678901", "RU_PASSPORT", "45 12 №678901"),
    ContextCase(
        "passport_foreign", "Загранпаспорт 75 1234567", "RU_PASSPORT", "75 1234567"
    ),
    ContextCase(
        "passport_military", "Военный билет АН 1234567", "RU_PASSPORT", "АН 1234567"
    ),
    ContextCase(
        "passport_birth",
        "Свидетельство о рождении II-МЮ №456789",
        "RU_PASSPORT",
        "II-МЮ №456789",
    ),
    ContextCase(
        "card", "Карта: 4111 1111 1111 1111", "CREDIT_CARD", "4111 1111 1111 1111"
    ),
    ContextCase(
        "card_invalid",
        "Карта: 4111 1111 1111 1112",
        "CREDIT_CARD",
        "4111 1111 1111 1112",
        False,
    ),
    ContextCase("ip", "Шлюз 10.0.0.1", "INTERNAL_IP", "10.0.0.1"),
    ContextCase("public_ip", "Внешний IP 203.0.113.42", "INTERNAL_IP", "203.0.113.42"),
    ContextCase("ipv6", "IPv6 2001:db8::1", "INTERNAL_IP", "2001:db8::1"),
    ContextCase("domain", "Сервис api.internal", "INTERNAL_DOMAIN", "api.internal"),
    ContextCase("host", "Хост k8s-controller-us1", "HOSTNAME", "k8s-controller-us1"),
    ContextCase(
        "db",
        "DB_URL=postgresql://reader:secret@db.internal:5432/app",
        "DB_URL",
        "postgresql://reader:secret@db.internal:5432/app",
    ),
    ContextCase(
        "jwt",
        "JWT=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0ZXN0In0.c3ludGhldGlj",
        "JWT",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0ZXN0In0.c3ludGhldGlj",
    ),
    ContextCase(
        "bearer",
        "Authorization: Bearer synthetic-bearer-token-12345",
        "BEARER_TOKEN",
        "synthetic-bearer-token-12345",
    ),
    ContextCase(
        "private_key",
        "-----BEGIN PRIVATE KEY-----\nU3ludGhldGljT25seUtleQ==\n-----END PRIVATE KEY-----",
        "PRIVATE_KEY",
        "-----BEGIN PRIVATE KEY-----\nU3ludGhldGljT25seUtleQ==\n-----END PRIVATE KEY-----",
    ),
    ContextCase(
        "api_key",
        "API_KEY=synthetic-api-key-12345",
        "API_KEY",
        "synthetic-api-key-12345",
    ),
    ContextCase(
        "secret",
        "SECRET_KEY=synthetic-secret-key-12345",
        "SECRET_KEY",
        "synthetic-secret-key-12345",
    ),
    ContextCase(
        "auth",
        "AUTH_TOKEN=synthetic-auth-token-12345",
        "AUTH_TOKEN",
        "synthetic-auth-token-12345",
    ),
    ContextCase("login", "PGUSER=analytics", "LOGIN", "analytics"),
    ContextCase(
        "password", "PASSWORD=SyntheticPass42!", "PASSWORD", "SyntheticPass42!"
    ),
    ContextCase(
        "person", "Клиент Иван Петров подписал документ.", "PERSON", "Иван Петров"
    ),
    ContextCase("location", "Встреча состоится в Москве.", "LOCATION", "Москве"),
    ContextCase(
        "organization",
        "Документы подготовлены для ООО Вектор.",
        "ORGANIZATION",
        "ООО Вектор",
    ),
    ContextCase(
        "contract",
        "Госконтракт № 0173100004521000123.",
        "CONTRACT_NUMBER",
        "0173100004521000123",
    ),
)


def check_context_case(case, entities):
    start = case.text.index(case.value)
    end = start + len(case.value)
    covering = [r for r in entities if r["start"] <= start and r["end"] >= end]
    typed = any(r["entity_type"] == case.entity_type for r in covering)
    detected = any(
        r["entity_type"] == case.entity_type and r["start"] < end and r["end"] > start
        for r in entities
    )
    return {
        "case_id": case.case_id,
        "entity_type": case.entity_type,
        "positive": case.positive,
        "typed_covered": typed,
        "target_detected": detected,
        "masking_covered": bool(covering),
        "passed": typed if case.positive else not detected,
        "predicted_types": sorted({r["entity_type"] for r in entities}),
    }


def run(analyzer_url, variant):
    client = AnalyzerClient(analyzer_url, timeout_seconds=120)
    report = {"variant": variant, "corpora": {}}
    with urllib.request.urlopen(
        analyzer_url.rstrip("/") + "/api/v1/health", timeout=10
    ) as response:
        report["health"] = json.load(response)
    report["analysis_signature"] = report["health"]["analysis_signature"]
    for name in ("ner_migration", "instruction_regressions"):
        path = DEFAULT_CORPUS_PATH.with_name(name + ".jsonl")
        cases = load_corpus(path)
        predictions = {case.case_id: client.analyze(case) for case in cases}
        report["corpora"][name] = evaluate_predictions(cases, predictions)
        report["corpora"][name]["corpus_sha256"] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
    rows = []
    for case in CONTEXT_CASES:
        request = urllib.request.Request(
            analyzer_url.rstrip("/") + "/api/v1/analyze",
            data=json.dumps({"text": case.text, "score_threshold": 0.35}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=120) as response:
            payload = json.load(response)
        rows.append(check_context_case(case, payload["entities"]))
    report["context_cases"] = rows
    report["context_passed"] = sum(row["passed"] for row in rows)
    positives = [row for row in rows if row["positive"]]
    negatives = [row for row in rows if not row["positive"]]
    report["context_summary"] = {
        "positive": len(positives),
        "masking_covered": sum(row["masking_covered"] for row in positives),
        "typed_covered": sum(row["typed_covered"] for row in positives),
        "negative": len(negatives),
        "negative_passed": sum(row["passed"] for row in negatives),
    }
    report["entity_filters"] = []
    for entity_type in ("PERSON", "LOCATION", "ORGANIZATION", "CONTRACT_NUMBER"):
        case = next(case for case in CONTEXT_CASES if case.entity_type == entity_type)
        request = urllib.request.Request(
            analyzer_url.rstrip("/") + "/api/v1/analyze",
            data=json.dumps(
                {"text": case.text, "score_threshold": 0.35, "entities": [entity_type]}
            ).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                row = check_context_case(case, json.load(response)["entities"])
                row["http_status"] = response.status
        except urllib.error.HTTPError as exc:
            row = {"entity_type": entity_type, "http_status": exc.code, "passed": False}
        report["entity_filters"].append(row)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analyzer-url", required=True)
    parser.add_argument("--variant", choices=("a", "b", "c"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = run(args.analyzer_url, args.variant)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                "variant": args.variant,
                "context_passed": report["context_passed"],
                "context_total": len(CONTEXT_CASES),
                "corpora": {
                    name: result["aggregate"]
                    for name, result in report["corpora"].items()
                },
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
