"""Поиск Base64 в тексте: кандидаты по алфавиту + каскад проверок + логистическая регрессия.

Analyzer вызывает только find_spans(text) -> [(start, end)]: диапазоны в исходном тексте.
Маскирование, placeholder и восстановление выполняет штатный Gateway.

Зачем каскад, а не одна регулярка: по алфавиту Base64 совпадает с идентификаторами кода
(config, __init__, getUserProfileById, src/main/java). Регулярка либо маскирует тысячи
обычных имён, либо пропускает настоящие кодировки. Каскад отделяет одно от другого
по признакам строки; веса фиксированы, sklearn в runtime не нужен.

Каскад для токена-кандидата (is_base64):
  0. Всегда: data:...;base64,..., JWT, PEM-блоки.
  1. Синтаксис: один алфавит (std "+/" или url "-_"), корректная длина и паддинг;
     не hex, не UUID, не только цифры, не путь (src/main/java), не kebab/snake-имя
     (Llama-2-7b-chat-hf), не строка из 1-3 разных символов ("//////").
     Длина 4k+1 (лишний символ при копировании) — от 33 символов, без последнего символа.
  2. Короткая зона 8-15 символов: только если префикс декодируется в ASCII/кириллический
     текст или цифры (YWRtaW4= -> admin, NDUxMCAxMjM0NTY -> 4510 123456); для 8-11 нужен "=".
  3. От 16 символов: декодируется в текст или содержит много нулевых байтов -> Base64.
  4. Иначе логистическая регрессия по 8 признакам символов (randomness_score),
     порог зависит от длины и паддинга; длинные std-токены (>= 64) — при слабом признаке.
  Отдельный проход: рядом с явной пометкой base64/b64 берутся и короткие значения в кавычках.

Декодирование используется только как признак распознавания, по префиксу <= 256 символов:
стоимость O(1) на кандидата независимо от размера блоба. Содержимое не анализируется NER.

Происхождение: b64_masker.py из пакета Claude (SHA256 59e2b297abfa23c1...); коэффициенты
и пороги без изменений. Удалены автономная маскировка с демо и список ID агентских API:
Gateway не маскирует поля id/tool_call_id, а ID внутри текста восстанавливаются в ответе.
"""
from __future__ import annotations

import base64
import binascii
from bisect import bisect_left, bisect_right
import re

MIN_LEN = 16       # минимальная длина core без паддинга для "голого" кандидата
LONG_LEN = 64      # длинные std-токены: достаточно слабого признака случайности
PREFIX = 256       # декодирование и признаки считаются только по началу токена (кратно 4)
MIN_TRIM_LEN = 33  # от этой длины строка длины 4k+1 проверяется без последнего символа

# --- структурные паттерны (маскируются всегда) --------------------------------
DATA_URI = re.compile(r"data:[\w.+-]+/[\w.+-]+(?:;[\w-]+=[\w.-]+)*;base64,[A-Za-z0-9+/_-]{4,}={0,2}")
# JSON-экранирование (\n, \t, \", \\, \uXXXX) в аргументах tool calls: "n" из "\n" входит в алфавит
# Base64 и прилипает к блобу. Заменяется пробелами той же длины — смещения не меняются.
JSON_ESC = re.compile(r'\\(?:[nrtbf"\\]|u[0-9a-fA-F]{4})')
JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*")
PEM = re.compile(r"-----BEGIN [A-Z0-9 ]+-----[\s\S]+?-----END [A-Z0-9 ]+-----")

# --- кандидаты ----------------------------------------------------------------
# Граница слева: не символ алфавита ("=" допустим: token=QUJD...), справа: не символ алфавита и не "=".
CANDIDATE = re.compile(r"(?<![A-Za-z0-9+/_-])[A-Za-z0-9+/_-]{6,}={0,2}(?![A-Za-z0-9+/_=-])")

HEX = re.compile(r"(?:0[xX])?[0-9a-fA-F]+")
UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
VOWELS = set("aeiouyAEIOUY")
PLAIN_TEXT = re.compile(r"[\x20-\x7eЀ-ӿ№«»—–\n\r\t]+")


def _decode(core: str, urlsafe: bool) -> bytes | None:
    s = core + "=" * (-len(core) % 4)
    try:
        if urlsafe:
            return base64.urlsafe_b64decode(s)
        return base64.b64decode(s, validate=True)
    except (binascii.Error, ValueError):
        return None


def _is_meaningful_text(b: bytes, truncated: bool = False) -> bool:
    if len(b) < 4:
        return False
    try:
        t = b.decode("utf-8")
    except UnicodeDecodeError as e:
        # префикс мог разрезать многобайтовый символ (кириллица) на самом конце
        if not truncated or e.start < len(b) - 3:
            return False
        t = b[: e.start].decode("utf-8", errors="ignore")
        if not t:
            return False
    if not PLAIN_TEXT.fullmatch(t):
        return False
    # буквы или цифры (цифры — ради числовых ПДн и паролей)
    return sum(ch.isalnum() for ch in t) / len(t) >= 0.4


def _is_plain_text(b: bytes, min_len: int = 4, min_alnum: float = 0.5) -> bool:
    """Строже _is_meaningful_text, для коротких токенов: случайные байты часто бывают
    валидным UTF-8, но тогда это иврит/арабица/CJK, а не ASCII/кириллица."""
    try:
        t = b.decode("utf-8")
    except UnicodeDecodeError:
        return False
    if len(t) < min_len or not PLAIN_TEXT.fullmatch(t):
        return False
    return sum(ch.isalnum() for ch in t) / len(t) >= min_alnum


# --- явный контекст base64 ---------------------------------------------------
# Короткий Base64 по форме неотличим от слова, но часто стоит рядом с пометкой:
# "Base64: …", {"encoding": "base64", "value": "…"}, "document_base64": "…",
# blob = "…"; base64.b64decode(blob). Рядом с пометкой берутся только литералы в кавычках
# или значение сразу после пометки; голые слова рядом с пометкой (encode, by) не трогаются.
B64_MARKER = re.compile(r"(?i)base64|b64")
IMMEDIATE_MARKER = re.compile(r"(?i)(?:base64|b64)[A-Za-z_]*[\"']?[ \t]*[:=][ \t]*[\"']?$")
WORDLIKE = re.compile(r"[a-z]+|[A-Z]+|[a-z_-]+")   # 'encode', 'RGB', 'big_endian'
SHORT_CAND = re.compile(r"(?<![A-Za-z0-9+/_-])[A-Za-z0-9+/_-]{2,}={0,2}(?![A-Za-z0-9+/_=-])")
CONTEXT_WINDOW = 60


def _context_base64(tok: str, immediate: bool) -> bool:
    """Токен рядом с явной пометкой base64. immediate — пометка прямо перед токеном."""
    core = tok.rstrip("=")
    pad = len(tok) - len(core)
    L = len(core)
    if L < 2 or L % 4 == 1 or (pad and (L + pad) % 4) or B64_MARKER.search(tok):
        return False
    has_url = "-" in core or "_" in core
    if has_url and ("+" in core or "/" in core):
        return False
    if core.isdigit() or (not immediate and WORDLIKE.fullmatch(core)):
        return False
    decoded = _decode(core, urlsafe=has_url)
    if decoded is None:
        return False
    if immediate:                         # "Base64: YQ==" -> a, "Base64: -__v-g==" (бинарь с паддингом)
        return _is_plain_text(decoded, min_len=1) or bool(pad)
    # литерал в кавычках рядом с пометкой: "NTA0NDU2" -> 504456, "MDU3MA==" -> 0570.
    # Не короче 6 символов и 4 декодированных: короткие слова в коде ('fmt', 'big5')
    # случайно декодируются в 1-3 печатных символа.
    return L >= 6 and _is_plain_text(decoded, min_len=4, min_alnum=0.3)


# --- логистическая регрессия -------------------------------------------------
SEGMENT = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|[0-9]+")  # разбиение camelCase/PascalCase
SCORE_THRESHOLD = 1.5          # для 16-23 символов: здесь основная масса идентификаторов


def _threshold(L: int, pad: int) -> float:
    """Длинные идентификаторы без разделителей редки, а скор Base64 растёт с длиной.
    Паддинг "=" — сам по себе сильный признак, поэтому порог ниже."""
    thr = SCORE_THRESHOLD if L < 24 else 0.0 if L < 32 else -1.5
    return thr - 3.0 if pad else thr


PRONOUNCEABLE = re.compile(r"(?=.*[aeiouy])(?!.*[bcdfghjklmnpqrstvwxz]{4})(?!.*[aeiouy]{3})[a-z]+")


def _features(core: str) -> list[float]:
    L = len(core)
    letters = [c for c in core if c.isalpha()]
    nl = max(len(letters), 1)
    segs = SEGMENT.findall(core)                 # kInt4W4A8Static -> k Int 4 W 4 A 8 Static
    alpha = [x for x in segs if not x.isdigit()]
    return [
        sum(c in VOWELS for c in letters) / nl,                      # доля гласных (base64 ~0.23, слова ~0.40)
        sum(c.isdigit() for c in core) / L,                          # доля цифр
        sum(c.isupper() for c in letters) / nl,                      # доля заглавных (base64 ~0.5)
        sum(map(len, segs)) / max(len(segs), 1),                     # средняя длина сегмента (base64 ~1.5-2)
        min(L, 64) / 64,                                             # длина
        min(max((len(x) for x in alpha), default=0), 12) / 12,       # самое длинное "слово"
        sum(len(x) for x in alpha if len(x) >= 4) / L,               # доля символов в "словах" от 4 букв
        sum(len(x) for x in alpha if len(x) >= 3 and PRONOUNCEABLE.fullmatch(x.lower())) / L,  # произносимые куски
    ]


# LogisticRegression на 8 признаках. Негативы — токены из stdlib Python и публичных корпусов
# (код, траектории агентов, function calling, k8s/Terraform, логи, markdown); позитивы —
# случайный Base64 и Base64 от текста. Обучение на половине токенов, проверка на второй.
_W = [-1.02, -6.78, 5.07, -3.22, 7.81, 0.27, -2.03, -5.57]
_B = 7.75


def randomness_score(core: str) -> float:
    """> порога — похоже на Base64, ниже — слово/идентификатор."""
    return sum(a * b for a, b in zip(_W, _features(core))) + _B


# --- пути и составные имена ----------------------------------------------------
WORDY = re.compile(r"[a-z][a-z0-9]*|[A-Z][a-z0-9]*|[A-Z0-9]+|[0-9a-fA-F]+")


def _wordy(seg: str) -> bool:
    """Сегмент пути/kebab-имени похож на слово: usr, Llama, 7b, README, ScaledBlas, хэш."""
    if WORDY.fullmatch(seg):
        return True
    parts = SEGMENT.findall(seg)
    return len(seg) >= 6 and sum(map(len, parts)) / max(len(parts), 1) >= 3.5


def _mostly_wordy(segs: list[str]) -> bool:
    return len(segs) >= 2 and sum(map(_wordy, segs)) >= 0.75 * len(segs)


def _looks_like_path(core: str) -> bool:
    """/usr/local/lib, api/v1/users, github-пути с хэшем коммита и т.п."""
    if "/" not in core or "+" in core:
        return False
    return _mostly_wordy([x for x in core.split("/") if x])


def _looks_like_kebab(core: str) -> bool:
    """Llama-2-7b-chat-hf, max_model_len. В случайном base64url "-" и "_" встречаются
    с частотой 1/32, поэтому много разделителей + словоподобные куски = идентификатор."""
    segs = [x for x in re.split(r"[-_]", core) if x]
    if len(segs) >= 2 and all(map(_wordy, segs)):
        return True                       # diff-31e81597..., gpu_memory_utilization
    n_sep = core.count("-") + core.count("_")
    if n_sep < 2 or n_sep * 8 < len(core):
        return False
    return n_sep * 5 >= len(core) or _mostly_wordy(segs)


def is_base64(tok: str) -> bool:
    core = tok.rstrip("=")
    pad = len(tok) - len(core)
    L = len(core)
    has_std = "+" in core or "/" in core
    has_url = "-" in core or "_" in core
    if has_std and has_url:
        return False
    if L % 4 == 1:
        # Лишний символ при копировании: нестрогий декодер такую строку примет, поэтому длинный
        # токен проверяется без последнего символа. На коротких это давало ложные срабатывания.
        return not pad and L >= MIN_TRIM_LEN and is_base64(core[:-1])
    if pad and (L + pad) % 4:
        return False
    if core.isdigit() or HEX.fullmatch(core) or UUID.fullmatch(core):
        return False
    if L + pad < 8:
        return False

    head = core[:PREFIX]  # PREFIX кратен 4, поэтому префикс тоже валидный Base64
    decoded = _decode(head, urlsafe=has_url)
    if decoded is None:
        return False
    if L >= MIN_LEN and decoded.count(0) / len(decoded) >= 0.25:
        return True                       # бинарь с нулями ("AAAA..."); проверяется ДО правила путей:
                                          # байты 0xFF дают много "/", и такой блоб похож на путь
    if len(set(head)) <= 3:
        return False                      # "//////", "+-+-+-" — разделители, а не данные
    if has_url and _looks_like_kebab(core):
        return False
    if has_std and _looks_like_path(core):
        return False

    if L < MIN_LEN:
        # Короткая зона: слова и Base64 неотличимы по форме, поэтому берётся только
        # Base64 от текста, а для 8-11 символов — ещё и с паддингом.
        if _is_plain_text(decoded) and (pad or L >= 12):
            return True                   # YWRtaW4= (admin), UEBzc3cwcmQh (P@ssw0rd!), 0JjQstCw0L0= (Иван)
        if not pad:
            return False
    elif _is_meaningful_text(decoded, truncated=L > PREFIX):
        return True                       # Base64 от текста (в т.ч. русского)
    if L >= LONG_LEN and not has_url and randomness_score(head) > -3.0:
        return True
    if not (any(c.islower() for c in core) and any(c.isupper() for c in core)):
        return False                      # UPPER_SNAKE, даты, kebab-case — у случайного Base64 так не бывает
    return randomness_score(head) > _threshold(L, pad)


# --- сборка диапазонов ----------------------------------------------------------
MIME_WIDTHS = (64, 76)  # ширина строк MIME/PEM: только такие строки продолжаются следующей
# Перевод строки между строками блока: настоящий или экранированный в JSON (\n, \r\n).
LINE_GAP = re.compile(r"(?:\r?\n|\\r\\n|\\n)[ \t]*")
# Короткая последняя строка блока — целая строка, после неё конец строки, текста или кавычки.
TAIL = re.compile(r"(?:\r?\n|\\r\\n|\\n)[ \t]*([A-Za-z0-9+/]{1,75}={0,2})(?=[ \t]*(?:\r?\n|\\r\\n|\\n|[\"']|$))")


def merge_spans(spans) -> list[tuple[int, int]]:
    """Отсортировать и объединить пересекающиеся или соприкасающиеся диапазоны."""
    merged: list[list[int]] = []
    for s, e in sorted(spans):
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged]


def subtract_spans(text: str, spans, protected) -> list[tuple[int, int]]:
    """Части диапазонов spans вне protected; оба списка — отсортированные без пересечений
    (protected объединяется здесь). Края части очищаются от пробелов, пустые части отбрасываются."""
    protected = merge_spans(protected)
    parts, i = [], 0
    for s, e in spans:
        while i < len(protected) and protected[i][1] <= s:
            i += 1
        cursor, j = s, i
        while j < len(protected) and protected[j][0] < e:
            a, b = protected[j]
            if a > cursor:
                parts.append((cursor, a))
            cursor = max(cursor, b)
            j += 1
        if cursor < e:
            parts.append((cursor, e))
    result = []
    for s, e in parts:
        while s < e and text[s].isspace():
            s += 1
        while e > s and text[e - 1].isspace():
            e -= 1
        if any(ch.isalnum() for ch in text[s:e]):
            result.append((s, e))
    return result


def _covered(intervals, starts, s, e) -> bool:
    i = bisect_right(starts, s) - 1
    return i >= 0 and intervals[i][1] >= e


def _overlaps(intervals, starts, s, e) -> bool:
    i = bisect_left(starts, e) - 1
    return i >= 0 and intervals[i][1] > s


def _valid_block_length(chars: str) -> bool:
    core = chars.rstrip("=")
    pad = len(chars) - len(core)
    return len(core) % 4 != 1 and (not pad or len(chars) % 4 == 0)


def _join_mime_lines(text: str, spans) -> list[tuple[int, int]]:
    """Объединить строки одного MIME/PEM-блока и добавить его короткую последнюю строку.

    Строка продолжается следующей, только если её ширина ровно 64 или 76 символов
    и между ними один перевод строки. Соседство само по себе не объединяет значения.
    """
    blocks: list[list[int]] = []  # [начало, конец, начало последней строки]
    for s, e in spans:
        if blocks:
            start, end, last = blocks[-1]
            if end - last in MIME_WIDTHS and LINE_GAP.fullmatch(text, end, s):
                blocks[-1][1:] = [e, s]
                continue
        blocks.append([s, e, s])
    out = []
    for start, end, last in blocks:
        if end - last in MIME_WIDTHS:
            m = TAIL.match(text, end)
            if m:
                line = m.group(1)
                block = re.sub(r"[^A-Za-z0-9+/=]", "", text[start:end].replace("\\n", "").replace("\\r", "")) + line
                # Обычное слово на следующей строке (UserName, HelloWorld) — не хвост: у хвоста
                # есть паддинг, цифра или +/, а хвост из одних букв должен выглядеть случайным
                # (слабый признак, как для длинных строк; у слов оценка ниже -6).
                looks_encoded = line.endswith("=") or any(c in "+/0123456789" for c in line) or (
                    len(line) >= 4 and randomness_score(line) > -3.0)
                if looks_encoded and _valid_block_length(block):
                    end = m.end(1)
        out.append((start, end))
    return out


def find_spans(text: str) -> list[tuple[int, int]]:
    """Диапазоны Base64 в исходном тексте, отсортированные и без пересечений.

    Время линейно по длине текста: пересечения проверяются бинарным поиском
    по отсортированным диапазонам, а не перебором уже найденного."""
    scan = JSON_ESC.sub(lambda m: " " * len(m.group()), text) if "\\" in text else text
    structural = merge_spans(m.span() for rx in (DATA_URI, JWT, PEM) for m in rx.finditer(scan))
    s_starts = [s for s, _ in structural]
    spans = list(structural)
    for m in CANDIDATE.finditer(scan):
        s, e = m.span()
        if not _covered(structural, s_starts, s, e) and is_base64(m.group()):
            spans.append((s, e))
    found = merge_spans(spans)
    f_starts = [s for s, _ in found]

    # Контекстный проход только в окнах вокруг пометок base64: иначе на коде, где base64
    # упоминается часто, пришлось бы проверять каждое слово документа.
    regions: list[list[int]] = []
    for mk in B64_MARKER.finditer(scan):
        a, b = max(0, mk.start() - CONTEXT_WINDOW - 100), mk.end() + CONTEXT_WINDOW + 100
        if regions and a <= regions[-1][1]:
            regions[-1][1] = b
        else:
            regions.append([a, b])
    context = []
    for ra, rb in regions:
        for m in SHORT_CAND.finditer(scan, ra, min(rb, len(scan))):
            s, e = m.span()
            if _overlaps(found, f_starts, s, e):
                continue
            # кавычки проверяются по исходному тексту: в JSON они экранированы (\"), а в scan уже пробелы
            quoted = s > 0 and text[s - 1] in "\"'" and text[e:e + 1] in ("\"", "'", "\\")
            before = scan[max(0, s - CONTEXT_WINDOW):s]
            immediate = before.rstrip(" \t")[-1:] in (":", "=", '"', "'") and IMMEDIATE_MARKER.search(before) is not None
            if not (quoted or immediate):
                continue
            if (immediate or B64_MARKER.search(before) or B64_MARKER.search(scan[e:e + CONTEXT_WINDOW])) \
                    and _context_base64(m.group(), immediate):
                context.append((s, e))
    return _join_mime_lines(text, merge_spans(found + context))
