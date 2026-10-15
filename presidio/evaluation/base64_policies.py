#!/usr/bin/env python3
"""Reproducible comparison of PRESIDIO_ANALYZER_BASE64_POLICY=content and logistic.

Run from the repository root:

    python presidio/evaluation/base64_policies.py --corpus PATH [PATH ...] --output report.json

The corpus is any set of directories/files with ordinary code and text. Pinned
sources used for the published numbers are listed in docs/base64-logistic.md.

1. Files are read in sorted order and split into fragments of at most 2000
   characters on line boundaries.
2. Clean fragments: every BASE64_DATA mask is counted and categorised
   (decodes to text / binary / not canonical Base64) for manual review.
3. Insertions: a seeded generator inserts one synthetic Base64 value into a
   fragment; a value counts as found only if its whole range is covered.
4. Timing on long clean histories and on inputs with many Base64 values.

Each policy runs in a separate process. The Base64 path reuses the Analyzer
helpers (document pass, outer chunks, fragment suppression, merge) with the
Base64 and JSON recognizers only; spaCy, NER and other recognizers are not run.
No network access.
"""
import argparse
import base64
import binascii
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import platform
import random
import re
import statistics
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
SEED = 20261014
FRAGMENT = 2000
EXTENSIONS = {".py", ".md", ".json", ".yaml", ".yml", ".txt", ".toml", ".cfg", ".js", ".ts", ".go", ".java"}
MAX_FILE_BYTES = 1_000_000
REFERENCE_REGEXES = {
    "regex [A-Za-z0-9+/_-]{16,}": r"(?<![A-Za-z0-9+/_-])[A-Za-z0-9+/_-]{16,}={0,2}(?![A-Za-z0-9+/_=-])",
    "regex {16,} + upper, lower, digit": (
        r"(?<![A-Za-z0-9+/_-])(?=[A-Za-z0-9+/_-]*[a-z])(?=[A-Za-z0-9+/_-]*[A-Z])(?=[A-Za-z0-9+/_-]*[0-9])"
        r"[A-Za-z0-9+/_-]{16,}={0,2}(?![A-Za-z0-9+/_=-])"
    ),
}


# --- corpus and synthetic insertions -------------------------------------------------------

def load_fragments(paths, max_characters):
    """Files ordered by a hash of their path relative to the given root, so the
    selection under --max-characters is the same on every machine."""
    files = {}
    for raw in paths:
        root = Path(raw)
        candidates = [root] if root.is_file() else (p for p in root.rglob("*") if p.is_file())
        for path in candidates:
            if path.suffix in EXTENSIONS and path.stat().st_size <= MAX_FILE_BYTES:
                relative = f"{root.name}/{path.relative_to(root) if root.is_dir() else ''}"
                files[hashlib.sha1(relative.encode()).hexdigest()] = path
    digest = hashlib.sha256()
    fragments = []
    selected = 0
    for key in sorted(files):
        if sum(map(len, fragments)) >= max_characters:
            break
        try:
            text = files[key].read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        selected += 1
        digest.update(hashlib.sha256(text.encode()).digest())
        current = ""
        for line in text.splitlines(keepends=True):
            if current and len(current) + len(line) > FRAGMENT:
                fragments.append(current)
                current = ""
            current += line[: FRAGMENT * 2]
        if current.strip():
            fragments.append(current)
    return fragments, {"files": selected, "fragments": len(fragments),
                       "characters": sum(map(len, fragments)), "sha256": digest.hexdigest()}


TEXTS = {
    "text_ru": ["Иван", "Петров Иван Сергеевич", "паспорт 4510 123456", "+7 915 123-45-67",
                "г. Москва, ул. Тверская, д. 7, кв. 12", "СНИЛС 112-233-445 95"],
    "text_en": ["admin:P@ssw0rd!", "ivan.petrov@example.com", "secret", "test",
                '{"user":"ivan","pass":"qwerty123"}', "postgres://app:Sup3rS3cret@db:5432/prod"],
    "digits": ["504456", "19850315", "4276123456789012", "1234"],
    "short_sensitive": ["a@b.co", "a@b.ru", "x@y.io", "p@q.org", "ab@c.ru"],
}
CONTEXTS = {
    "text": "{v}",
    "quoted": '"{v}"',
    "json": '{{"data": "{v}"}}',
    "key=value": "token={v}",
    "url": "https://api.example.org/v1/items?sig={v}&page=2",
    "ru_sentence": "Вот строка {v} из лога.",
}


def make_value(rng):
    kind = rng.choice(["random_bytes", "random_bytes", "text_ru", "text_en", "digits", "zero_bytes",
                       "short_sensitive", "mime"])
    if kind == "random_bytes":
        raw = bytes(rng.randrange(256) for _ in range(rng.choice([3, 6, 9, 12, 16, 24, 32, 48, 96, 192])))
    elif kind == "zero_bytes":
        raw = bytes([rng.randrange(8), 0, 0, 0, rng.randrange(256), 0, 0, 0]) + bytes(rng.choice([4, 8, 16]))
    elif kind == "mime":
        raw = bytes(rng.randrange(256) for _ in range(rng.choice([100, 171, 300, 500])))
    else:
        raw = rng.choice(TEXTS[kind]).encode()
    urlsafe = kind != "mime" and rng.random() < 0.3
    value = (base64.urlsafe_b64encode if urlsafe else base64.b64encode)(raw).decode()
    if kind != "mime" and rng.random() < 0.3:
        value = value.rstrip("=")
    if kind == "mime":
        width = rng.choice([64, 76])
        value = "\n".join(value[i:i + width] for i in range(0, len(value), width))
        context = "mime"
    else:
        context = rng.choice(list(CONTEXTS))
    return kind, value, context


def insertions(fragments, count):
    rng = random.Random(SEED)
    rows = []
    for index in range(count):
        base = rng.choice(fragments)
        kind, value, context = make_value(rng)
        snippet = value if context == "mime" else CONTEXTS[context].format(v=value)
        breaks = [i for i, ch in enumerate(base) if ch == "\n"] or [len(base)]
        position = rng.choice(breaks)
        prefix = base[:position] + "\n"
        text = prefix + snippet + "\n" + base[position:]
        start = len(prefix) + snippet.index(value)
        core = len(value.replace("\n", "").rstrip("="))
        rows.append({"id": index, "kind": kind, "context": context, "text": text,
                     "start": start, "end": start + len(value),
                     "length": "<12" if core < 12 else "12-15" if core < 16 else "16+"})
    return rows


def many_values(count):
    value = base64.b64encode(b'password="Synthetic-Pass-2026"').decode()
    return "\n".join('"' + value + '"' for _ in range(count))


# --- one policy, separate process -----------------------------------------------------------

def worker(policy, data_path):
    os.environ["PRESIDIO_ANALYZER_BASE64_POLICY"] = policy
    sys.path.insert(0, str(ROOT / "presidio"))
    sys.path.insert(0, str(ROOT))
    import analyzer_server as server
    from presidio_analyzer import EntityRecognizer
    from recognizers.base64_data import Base64DataRecognizer
    from recognizers.base64_logistic import find_spans
    from recognizers.json_strings import JsonStringRecognizer
    from result_merging import merge_results
    from text_chunking import plan_text_chunks

    local = [r for r in server.analyzer.registry.recognizers
             if isinstance(r, (Base64DataRecognizer, JsonStringRecognizer))]

    def analyze(text):
        """Base64 part of analyzer_server.analyze with BASE64_DATA requested."""
        chunks = plan_text_chunks(text)
        results, starts, ends = [], [], []
        if len(chunks) > 1 or server._document_base64_cascade_enabled():
            results = server._analyze_document_base64(server.AnalyzeRequest(text=text, language="ru"))
            for start, end in sorted((r.start, r.end) for r in results
                                     if (r.recognition_metadata or {}).get("encoded_container") is True):
                starts.append(start)
                ends.append(max(end, ends[-1] if ends else 0))
        for chunk in chunks:
            part = text[chunk.start:chunk.end]
            found = []
            for recognizer in local:
                found += recognizer.analyze(part, recognizer.supported_entities)
            found = EntityRecognizer.remove_duplicates(found)  # as AnalyzerEngine.analyze does
            results += [server._shift_recognizer_result(r, chunk.start) for r in found
                        if server._is_complete_chunk_result(r, chunk, len(text))
                        and not server._is_document_base64_fragment(r, chunk.start, starts, ends)]
        return merge_results(text, results, score_threshold=0.35).results

    data = json.loads(Path(data_path).read_text())
    report = {"policy": policy}
    masks, categories = Counter(), Counter()
    samples = Counter()
    for text in data["fragments"]:
        for r in analyze(text):
            if r.entity_type == "BASE64_DATA":
                value = text[r.start:r.end]
                masks["BASE64_DATA"] += 1
                categories[_category(value)] += 1
                # Only a short prefix is stored: corpora can contain real tokens.
                samples[f"{value[:6]}…({len(value)})"] += 1
    report["clean"] = {"base64_masks": masks["BASE64_DATA"], "categories": dict(categories),
                       "top_values": samples.most_common(30)}
    groups = {}
    for row in data["insertions"]:
        spans = [(r.start, r.end) for r in analyze(row["text"])]
        found = _covered(row["start"], row["end"], spans)
        for key in ("all", "length:" + row["length"], "kind:" + row["kind"], "context:" + row["context"]):
            groups.setdefault(key, [0, 0])
            groups[key][0] += found
            groups[key][1] += 1
    report["insertions"] = groups
    timing = {}
    rng = random.Random(SEED)
    for size in (50_000, 200_000, 400_000):
        text = ""
        while len(text) < size:
            text += rng.choice(data["fragments"])
        timing[f"clean_{size}"] = _time(lambda: analyze(text[:size]))
    for count in (1000, 4000, 16000):
        text = many_values(count)
        timing[f"many_values_{count}"] = _time(lambda: analyze(text), repeat=3)
        if policy == "logistic":
            timing[f"many_values_{count}_find_spans"] = _time(lambda: find_spans(text), repeat=3)
        covered = sum(1 for r in analyze(text) if r.entity_type == "BASE64_DATA")
        timing[f"many_values_{count}"]["masked_values"] = covered
    report["timing"] = timing
    print(json.dumps(report, ensure_ascii=False))


def _category(value):
    core = re.sub(r"\s", "", value)
    try:
        raw = base64.b64decode(core + "=" * (-len(core.rstrip("=")) % 4), altchars=b"-_", validate=True)
    except (binascii.Error, ValueError):
        return "not canonical Base64"
    try:
        text = raw.decode("utf-8")
        if text and all(ch.isprintable() or ch in "\r\n\t" for ch in text):
            return "decodes to text"
    except UnicodeDecodeError:
        pass
    return "binary"


def _covered(start, end, spans):
    cursor = start
    for a, b in sorted(spans):
        if a > cursor:
            break
        cursor = max(cursor, b)
        if cursor >= end:
            return True
    return False


def _time(function, repeat=5):
    function()
    walls = []
    for _ in range(repeat):
        started = time.perf_counter()
        function()
        walls.append(time.perf_counter() - started)
    return {"median_seconds": round(statistics.median(walls), 4)}


def reference_regexes(data):
    out = {}
    for name, pattern in REFERENCE_REGEXES.items():
        rx = re.compile(pattern)
        clean = sum(len(rx.findall(text)) for text in data["fragments"])
        groups = {}
        for row in data["insertions"]:
            spans = [m.span() for m in rx.finditer(row["text"])]
            for key in ("all", "length:" + row["length"]):
                groups.setdefault(key, [0, 0])
                groups[key][0] += _covered(row["start"], row["end"], spans)
                groups[key][1] += 1
        out[name] = {"clean": {"base64_masks": clean}, "insertions": groups}
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--corpus", nargs="+", help="directories or files with ordinary code and text")
    parser.add_argument("--insertions", type=int, default=4000)
    parser.add_argument("--max-characters", type=int, default=12_000_000)
    parser.add_argument("--output", type=Path, default=Path("base64-policies.json"))
    parser.add_argument("--worker", choices=("content", "logistic"), help=argparse.SUPPRESS)
    parser.add_argument("--data", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        return worker(args.worker, args.data)
    if not args.corpus:
        parser.error("--corpus is required")
    fragments, corpus = load_fragments(args.corpus, args.max_characters)
    data = {"fragments": fragments, "insertions": insertions(fragments, args.insertions)}
    data_path = args.output.with_suffix(".data.json")
    data_path.write_text(json.dumps(data, ensure_ascii=False))
    report = {"environment": {"python": platform.python_version(), "platform": platform.platform()},
              "seed": SEED, "corpus": corpus, "insertions": args.insertions, "policies": {}}
    for policy in ("content", "logistic"):
        completed = subprocess.run([sys.executable, __file__, "--worker", policy, "--data", str(data_path)],
                                   check=True, capture_output=True, text=True)
        report["policies"][policy] = json.loads(completed.stdout.strip().splitlines()[-1])
    report["reference_regexes"] = reference_regexes(data)
    data_path.unlink()
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=1))
    print(summary(report))


def summary(report):
    def pct(group):
        return f"{group[0]}/{group[1]} ({100 * group[0] / group[1]:.0f}%)" if group and group[1] else "—"
    rows = ["| Variant | Clean BASE64_DATA masks | Found ≥16 | Found 12–15 | Found <12 |", "|---|---:|---:|---:|---:|"]
    variants = {**{f"policy {k}": v for k, v in report["policies"].items()}, **report["reference_regexes"]}
    for name, item in variants.items():
        g = item["insertions"]
        rows.append(f"| {name} | {item['clean']['base64_masks']} | {pct(g.get('length:16+'))} | "
                    f"{pct(g.get('length:12-15'))} | {pct(g.get('length:<12'))} |")
    rows += ["", "| Timing, median seconds | content | logistic |", "|---|---:|---:|"]
    for key in report["policies"]["content"]["timing"]:
        cells = []
        for policy in ("content", "logistic"):
            item = report["policies"][policy]["timing"][key]
            masked = f" ({item['masked_values']} masked)" if "masked_values" in item else ""
            cells.append(f"{item['median_seconds']}{masked}")
        rows.append(f"| {key} | {cells[0]} | {cells[1]} |")
    return "\n".join(rows)


if __name__ == "__main__":
    main()
