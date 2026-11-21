"""Stage 5: reproducible editorial checks, not an AI-origin classifier.

Only the Python standard library is required. No text leaves the process unless
the caller supplies a LanguageTool URL. Non-loopback URLs require allow_remote.
All offsets returned by this module refer to the original Python string; line
and column numbers are one-based. LanguageTool's UTF-16 offsets are converted.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import math
from pathlib import Path
import re
import statistics
import tempfile
from typing import Any, Iterable, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


RULESET_VERSION = "ru-science-2.0.0"
EN_RULESET_VERSION = "en-abstract-basic-1.0.0"
DEFAULT_REQUIRED_TOOLS = ("ru_science_lint", "languagetool")
_WORDS = re.compile(r"[^\W\d_]+(?:[-’'][^\W\d_]+)*", re.UNICODE)
_REFERENCE_HEADING = re.compile(
    r"^(?P<hash>#{1,6}\s+)?(?:\d+[.)]?\s+)?"
    r"(?:список\s+(?:использованной\s+)?литературы|литература|"
    r"библиографический\s+список|библиография|references|bibliography)\s*[:.]?\s*$",
    re.I,
)
_ABBREVIATIONS = re.compile(
    r"(?<!\w)(?:т\s*\.\s*е\s*\.|т\s*\.\s*к\s*\.|т\s*\.\s*д\s*\.|"
    r"т\s*\.\s*п\s*\.|др\.|рис\.|табл\.|стр\.|см\.|ср\.|"
    r"им\.|проф\.|акад\.|доц\.|канд\.|ред\.|вып\.|гг?\.|с\.|"
    r"e\s*\.\s*g\s*\.|i\s*\.\s*e\s*\.|et\s+al\.)",
    re.I,
)
_CONNECTIVES = (
    "таким образом", "однако", "вместе с тем", "следовательно",
    "при этом", "согласно", "вероятно", "возможно",
)
_RULES = (
    ("filler_opening", r"\b(?:на сегодняшний день|в современном мире|в настоящее время)\b",
     "warning", "Проверьте, нужна ли временная рамка; при необходимости назовите период."),
    ("announcing_emphasis", r"\b(?:следует отметить|важно подчеркнуть|необходимо отметить),?\s+что\b",
     "warning", "Проверьте, добавляет ли вводный оборот смысл или научное противопоставление."),
    ("promotional_metaphor", r"\b(?:открывает новые горизонты|играет ключевую роль|революционн[а-яё]*)\b",
     "warning", "Замените рекламную оценку проверяемым механизмом или обоснуйте её."),
    ("unqualified_superlative", r"\b(?:уникальн[а-яё]*|беспрецедентн[а-яё]*|наилучш[а-яё]*|лучший|лучшая|лучшее|лучшие)\b",
     "warning", "Проверьте критерий сравнения; устойчивый научный термин можно сохранить с обоснованием."),
    ("priority_claim", r"\b(?:впервые|несомненно|безусловно|очевидно)\b",
     "warning", "Проверьте основание приоритета или степени уверенности; автоматическая замена не требуется."),
    ("translated_cliche", r"\b(?:пролива(?:ет|ют) свет|ключевым аспектом является|демонстриру(?:ет|ют) (?:значительный )?потенциал|вносит (?:значительный|существенный) вклад в понимание)\b",
     "warning", "Возможный переводной штамп: уточните, какой результат, механизм или предел применения описан. Осмысленную формулировку можно сохранить."),
    ("split_predicate", r"\b(?:осуществлени[ея]|осуществля(?:ется|ются|ть)|осуществить|осуществлено)\s+(?:оценк[аиу]|анализ[а]?|расч[её]т[а]?|измерени[яйе])\b|\b(?:проведени[ея]|провести|проводится|проведено)\s+(?:анализ[а]?|оценк[аиу]|сопоставлени[ея])\b",
     "warning", "Возможное расщепление сказуемого: проверьте, можно ли назвать действие глаголом без потери смысла. Термин или название процедуры сохраняйте."),
    ("placeholder", r"\b(?:TODO|TBD|FIXME|SOURCE_NEEDED)\b|\[(?:нужен источник|добавить[^\]\n]*)\]|\{\{[^}\n]+\}\}",
     "error", "В рукописи осталась незаполненная служебная пометка."),
)


def manuscript_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _blank(chars: list[str], start: int, end: int) -> None:
    for i in range(start, end):
        if chars[i] not in "\r\n":
            chars[i] = " "


def mask_nonprose(text: str, *, mask_quotes: bool = True) -> str:
    """Mask references, code, formulas and direct quotes, preserving offsets.

    This is a conservative Markdown/plain-text scanner, not a TeX parser.
    Excluded content remains present in the input hash and final human review.
    """
    chars = list(text)
    lines = list(re.finditer(r"[^\n]*(?:\n|$)", text))
    fence: tuple[str, int] | None = None
    reference_level: int | None = None
    frontmatter = text.startswith("---\n") or text.startswith("---\r\n")
    for n, line in enumerate(lines):
        value = line.group().rstrip("\r\n")
        if frontmatter:
            _blank(chars, line.start(), line.end())
            if n > 0 and value.strip() in ("---", "..."):
                frontmatter = False
            continue
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", value)
        if fence is not None:
            _blank(chars, line.start(), line.end())
            if marker and marker.group(1)[0] == fence[0] and len(marker.group(1)) >= fence[1]:
                fence = None
            continue
        if marker:
            fence = (marker.group(1)[0], len(marker.group(1)))
            _blank(chars, line.start(), line.end())
            continue
        heading = re.match(r"^(#{1,6})\s+", value)
        if reference_level is not None:
            if heading and len(heading.group(1)) <= reference_level:
                reference_level = None
            else:
                _blank(chars, line.start(), line.end())
                continue
        ref = _REFERENCE_HEADING.fullmatch(value.strip())
        if ref:
            reference_level = len(ref.group("hash").strip()) if ref.group("hash") else 6
            _blank(chars, line.start(), line.end())
        elif re.match(r"^\s*>", value):
            _blank(chars, line.start(), line.end())

    # Code and math before quotation scanning so quote marks inside them do not
    # accidentally consume neighbouring prose. All masks keep original length.
    patterns = [
        r"(`+)[^`\n]*?\1", r"\$\$[\s\S]*?\$\$", r"\\\[[\s\S]*?\\\]",
        r"\\\([\s\S]*?\\\)", r"(?<!\\)\$(?!\$)[^$\n]+(?<!\\)\$",
        r"https?://[^\s<>]+", r"\[@[^\]\n]+\]", r"\[(?:SRC|INF|EXP):[^\]\n]+\]",
    ]
    if mask_quotes:
        patterns.extend([r"«[^»]*»", r"“[^”]*”", r'"[^"\n]+"'])
    for pattern in patterns:
        current = "".join(chars)
        for match in re.finditer(pattern, current):
            _blank(chars, match.start(), match.end())
    return "".join(chars)


def _protected_spans(text: str, terms: Sequence[str]) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for term in terms:
        term = str(term).strip()
        if term:
            pattern = r"(?<!\w)" + re.escape(term).replace(r"\ ", r"\s+") + r"(?!\w)"
            spans.extend((m.start(), m.end()) for m in re.finditer(pattern, text, re.I))
    return spans


def _is_protected(start: int, end: int, spans: Iterable[tuple[int, int]]) -> bool:
    return any(a <= start and end <= b for a, b in spans)


def _paragraphs(visible: str) -> list[tuple[int, int]]:
    result: list[tuple[int, int]] = []
    cadence = list(visible)
    for line in re.finditer(r"[^\n]*(?:\n|$)", visible):
        if re.match(r"\s*(?:#{1,6}\s|\|)", line.group()):
            _blank(cadence, line.start(), line.end())
    for part in re.finditer(r"\S[\s\S]*?(?=\n\s*\n|\Z)", "".join(cadence)):
        a, b = part.span()
        content = visible[a:b]
        if _WORDS.search(content):
            result.append((a, b))
    return result


def sentence_spans(visible: str) -> list[tuple[int, int]]:
    """Approximate sentence segmentation with RU abbreviations and decimals."""
    protected_dots: set[int] = set()
    for pattern in (_ABBREVIATIONS, re.compile(r"(?<!\w)[А-ЯЁA-Z]\.(?=\s*[А-ЯЁA-Z])")):
        for match in pattern.finditer(visible):
            protected_dots.update(i for i in range(match.start(), match.end()) if visible[i] == ".")
    spans: list[tuple[int, int]] = []
    for a, b in _paragraphs(visible):
        start = a
        for m in re.finditer(r"[.!?]+(?=\s|$)", visible[a:b]):
            i, end = a + m.start(), a + m.end()
            if m.group() == "." and i in protected_dots:
                continue
            # The lookahead already excludes the interior dot in 1.25; this
            # explicit guard also documents the numeric boundary requirement.
            if i > 0 and end < len(visible) and visible[i - 1].isdigit() and visible[end].isdigit():
                continue
            if _WORDS.search(visible[start:end]):
                spans.append((start, end))
            start = end
            while start < b and visible[start].isspace():
                start += 1
        if start < b and _WORDS.search(visible[start:b]):
            spans.append((start, b))
    return spans


def _distribution(values: Sequence[int]) -> dict[str, float | int | None]:
    if not values:
        return {"count": 0, "mean": None, "median": None, "p10": None, "p25": None, "p75": None, "p90": None, "cv": None}
    ordered = sorted(values)
    def percentile(p: float) -> float:
        point = (len(ordered) - 1) * p
        lo = math.floor(point)
        hi = math.ceil(point)
        return ordered[lo] + (ordered[hi] - ordered[lo]) * (point - lo)
    mean = statistics.fmean(values)
    return {
        "count": len(values), "mean": round(mean, 3),
        "p10": round(percentile(0.1), 3), "p90": round(percentile(0.9), 3),
        "median": round(percentile(0.5), 3), "p25": round(percentile(0.25), 3),
        "p75": round(percentile(0.75), 3),
        "cv": round(statistics.pstdev(values) / mean, 3) if mean else None,
    }


def tempo_profile(texts: Sequence[str]) -> dict[str, Any]:
    sentence_lengths: list[int] = []
    paragraph_lengths: list[int] = []
    frequencies: Counter[str] = Counter()
    word_count = 0
    usable_samples = 0
    hashes: list[str] = []
    for text in texts:
        visible = mask_nonprose(text)
        words = _WORDS.findall(visible)
        if not words:
            continue
        usable_samples += 1
        hashes.append(manuscript_sha256(text))
        word_count += len(words)
        sentence_lengths.extend(len(_WORDS.findall(visible[a:b])) for a, b in sentence_spans(visible))
        paragraph_lengths.extend(len(_WORDS.findall(visible[a:b])) for a, b in _paragraphs(visible))
        for connector in _CONNECTIVES:
            frequencies[connector] += len(re.findall(r"\b" + re.escape(connector) + r"\b", visible, re.I))
    return {
        "status": "not_supplied" if not usable_samples else (
            "provisional" if usable_samples < 2 or word_count < 1000 else "sample_based"
        ),
        "sample_count": usable_samples, "sample_sha256": hashes, "word_count": word_count,
        "confidence": "none" if not usable_samples else "low" if usable_samples < 2 or word_count < 1000 else "descriptive_only",
        "sentence_words": _distribution(sentence_lengths), "paragraph_words": _distribution(paragraph_lengths),
        "connectives_per_1000_words": {
            key: round(1000 * frequencies[key] / word_count, 3) if word_count else None
            for key in _CONNECTIVES
        },
        "interpretation": "Описательные метрики образцов; не оценка авторства и не норматив качества.",
        "limitations": "Эвристическая сегментация; образцы должны совпадать по языку и жанру. Порог 1000 слов и 2 образца — настройка проекта.",
    }


def profile_style(samples: Sequence[str], *, mode: str = "author_samples") -> dict[str, Any]:
    """Profile supplied texts, or record an explicitly requested neutral style."""
    if mode not in ("author_samples", "neutral_requested"):
        raise ValueError("Style mode must be author_samples or neutral_requested")
    profile = tempo_profile(samples)
    profile["mode"] = mode
    if mode == "neutral_requested":
        if profile["sample_count"]:
            raise ValueError("A neutral style request must not simultaneously claim an author baseline")
        profile["status"] = "neutral_requested"
        profile["interpretation"] = "Нейтральный режим указан вызывающей стороной; индивидуальный стиль автора по образцам не заявляется."
    return profile


def _finding(text: str, rule: str, start: int, end: int, severity: str, message: str, *, tool: str = "ru_science_lint") -> dict[str, Any]:
    identity = hashlib.sha256(f"{tool}:{rule}:{start}:{end}:{text[start:end]}".encode()).hexdigest()[:12]
    return {
        "id": f"{tool}:{identity}", "tool": tool, "rule": rule, "severity": severity,
        "line": text.count("\n", 0, start) + 1,
        "column": start - text.rfind("\n", 0, start), "offset": start, "length": end - start,
        "excerpt": text[start:end], "message": message, "resolution": "pending",
    }


def _json_sha256(value: Any) -> str:
    return manuscript_sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False))


def _term_pattern(term: str) -> str:
    return r"(?<!\w)" + re.escape(term).replace(r"\ ", r"\s+") + r"(?!\w)"


def _glossary_findings(text: str, visible: str, glossary: dict[str, Any]) -> list[dict[str, Any]]:
    """Find explicit equations of non-equivalent terms, not mere coexistence.

    Literal preferred/allowed forms only: this is not a semantic contradiction
    detector, and it deliberately misses paraphrases rather than inventing them.
    """
    entries = glossary.get("entries", [])
    if not isinstance(entries, list):
        raise ValueError("glossary.entries must be an array")
    by_id = {entry.get("id"): entry for entry in entries if isinstance(entry, dict) and entry.get("id")}
    pairs: set[tuple[str, str]] = set()
    findings: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("glossary entries must be objects")
        for relation in entry.get("relations", []):
            if not isinstance(relation, dict) or relation.get("type") != "not_equivalent":
                continue
            target = by_id.get(relation.get("target_id"))
            if not target or not entry.get("id"):
                continue  # The glossary contract validator reports broken links.
            pair = tuple(sorted((entry["id"], target["id"])))
            if pair in pairs:
                continue
            pairs.add(pair)
            left = [entry.get("preferred", ""), *entry.get("allowed_variants", [])]
            right = [target.get("preferred", ""), *target.get("allowed_variants", [])]
            for a, b in ((left, right), (right, left)):
                a_pattern = "(?:" + "|".join(_term_pattern(t) for t in a if isinstance(t, str) and t.strip()) + ")"
                b_pattern = "(?:" + "|".join(_term_pattern(t) for t in b if isinstance(t, str) and t.strip()) + ")"
                if a_pattern == "(?:)" or b_pattern == "(?:)":
                    continue
                patterns = (
                    a_pattern + r"\s*(?:[,—–-]\s*)?(?:то есть|т\.\s*е\.|иначе говоря|это|эквивалент(?:ен|на|но|ны)?|равнознач(?:ен|на|но|ны)?|синоним)\s+" + b_pattern,
                    a_pattern + r"\s*(?:=|и)\s*" + b_pattern + r"\s*(?:[,—–-]\s*)?(?:одно и то же|синонимы|эквивалентны|равнозначны)\b",
                )
                for pattern in patterns:
                    for match in re.finditer(pattern, visible, re.I):
                        # Do not flag an author explicitly rejecting a false
                        # synonymy, or presenting it as a known misconception.
                        prefix = visible[max(0, match.start() - 100):match.start()]
                        prefix = re.split(r"[.!?\n]", prefix)[-1]
                        if re.search(r"\b(?:не|нельзя|ошибочн[а-яё]*|неверн[а-яё]*)\b", prefix, re.I):
                            continue
                        item = _finding(text, "not_equivalent_terms", *match.span(), "warning", f"Словарь запрещает отождествление {pair[0]} и {pair[1]}. Проверьте явную связку эквивалентности; совместное упоминание терминов допустимо.")
                        item["glossary_ids"] = list(pair)
                        findings.append(item)
    return list({item["id"]: item for item in findings}.values())


_GENITIVE_LEXEMES = {
    "анализа", "оценки", "качества", "метода", "методов", "модели", "моделей",
    "прогноза", "прогнозов", "системы", "систем", "данных", "результатов",
    "параметров", "процессов", "атмосферы", "температуры", "скорости", "ветра",
    "структуры", "поля", "полей", "ряда", "рядов", "свойств", "среды",
}


def _genitive_candidates(text: str, visible: str, protected: list[tuple[int, int]]) -> list[dict[str, Any]]:
    """Conservative surface heuristic; no grammatical case is asserted."""
    candidates: list[list[re.Match[str]]] = []
    chain: list[re.Match[str]] = []
    for word in re.finditer(r"[А-Яа-яЁё]+", visible):
        token = word.group().casefold()
        nominal_like = token in _GENITIVE_LEXEMES or bool(re.search(r"(?:ции|ций|ния|ний|ости|стей|тельности|тельностей)$", token))
        adjacent = not chain or visible[chain[-1].end():word.start()].isspace()
        if not nominal_like or not adjacent:
            if len(chain) >= 4:
                candidates.append(chain)
            chain = []
        if nominal_like:
            chain.append(word)
    if len(chain) >= 4:
        candidates.append(chain)
    findings = []
    for chain in candidates:
        start, end = chain[0].start(), chain[-1].end()
        if _is_protected(start, end, protected):
            continue
        item = _finding(text, "genitive_chain_candidate", start, end, "warning", "Возможная цепочка именных зависимостей из четырёх и более слов: проверьте, понятно ли, что к чему относится. Падежи не разбирались; это поверхностная эвристика, термин можно сохранить.")
        item["confidence"] = "surface_heuristic"
        findings.append(item)
    return findings


def _typography_findings(text: str, visible: str, protected: list[tuple[int, int]]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    patterns = (
        ("unit_spacing", r"(?<![\w.,])\d+(?:[.,]\d+)?(?:%|°[CСF]|кг|г|мг|км(?:/ч)?|см|мм|м(?:/с)?|мс|с|ч|Па|гПа|кПа|МПа|К|Вт|кВт|Гц)(?!\w)", "Между числом и обозначением единицы или % обычно нужен пробел; проверьте требования журнала. Угловые градусы без обозначения температуры не затрагиваются."),
        ("prose_dash", r"(?<=[А-Яа-яЁё])\s+[-–]\s+(?=[А-Яа-яЁё])", "Возможное пунктуационное тире, набранное дефисом/коротким тире. Проверьте контекст и типографику журнала; диапазоны и дефисы внутри слов не исправляются."),
    )
    for rule, pattern, message in patterns:
        for match in re.finditer(pattern, visible):
            if not _is_protected(*match.span(), protected):
                findings.append(_finding(text, rule, *match.span(), "warning", message))
    # Inspect only quotation glyphs, never lint quoted language. Formula/code/
    # bibliography masking is retained; Russian guillemets are already valid.
    typography = mask_nonprose(text, mask_quotes=False)
    for match in re.finditer(r'"[^"\n]*[А-Яа-яЁё][^"\n]*"|“[^”\n]*[А-Яа-яЁё][^”\n]*”', typography):
        findings.append(_finding(text, "russian_quote_glyphs", *match.span(), "warning", "Для русской внешней цитаты обычно используют «ёлочки». Проверьте вложенность и правила журнала; содержание цитаты не изменяйте."))
    return findings


def check_english_abstract(publication: dict[str, Any], text: str) -> dict[str, Any]:
    """A deterministic, explicitly limited check of publication.abstract.en."""
    if not isinstance(publication, dict):
        raise ValueError("publication must be an object")
    abstract = publication.get("abstract", {})
    if not isinstance(abstract, dict):
        raise ValueError("publication.abstract must be an object")
    english = abstract.get("en", "")
    if not isinstance(english, str):
        raise ValueError("publication.abstract.en must be a string")
    visible = mask_nonprose(english)
    findings: list[dict[str, Any]] = []
    patterns = (
        ("english_placeholder", _RULES[-1][1], "error", "В английской аннотации осталась служебная пометка."),
        ("cyrillic_in_english_abstract", r"[А-Яа-яЁё]{3,}(?:\s+[А-Яа-яЁё]{3,})*", "warning", "Кириллица в английской аннотации: проверьте перевод или обоснуйте сохранённое имя/термин."),
        ("english_repeated_word", r"\b([A-Za-z]{2,})\s+\1\b", "warning", "Повтор соседнего слова: проверьте, не опечатка ли это; грамматически необходимый повтор допустим."),
    )
    if english.strip():
        for rule, pattern, severity, message in patterns:
            for match in re.finditer(pattern, visible, re.I):
                findings.append(_finding(english, rule, *match.span(), severity, message, tool="en_abstract_lint"))
        for start, end in sentence_spans(visible):
            if len(_WORDS.findall(visible[start:end])) > 45:
                findings.append(_finding(english, "english_long_sentence", start, end, "warning", "Более 45 слов: проверьте читаемость английской аннотации; это эвристика, не норма журнала.", tool="en_abstract_lint"))
    for item in findings:
        item.update(location="publication.abstract.en", offset_basis="publication.abstract.en")
    findings.sort(key=lambda item: (item["offset"], item["rule"]))
    return {
        "tool": "en_abstract_lint", "version": EN_RULESET_VERSION,
        "execution_status": "completed" if english.strip() else "not_supplied",
        "disposition": "not_assessed" if not english.strip() else "fail" if any(f["severity"] == "error" for f in findings) else "needs_review" if findings else "pass",
        "manuscript_sha256": manuscript_sha256(text), "publication_sha256": _json_sha256(publication),
        "input_sha256": manuscript_sha256(english), "language": "en", "findings": findings,
        "scope": "basic_local_abstract_checks_only; no complete English grammar or translation equivalence assessment",
        "location": "publication.abstract.en",
    }


def analyze_text(text: str, *, language: str = "ru", protected_terms: Sequence[str] = (), author_samples: Sequence[str] = (), author_style_mode: str = "author_samples", glossary: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run local RU editorial heuristics and compare descriptive author tempo."""
    visible = mask_nonprose(text)
    protected = _protected_spans(visible, protected_terms)
    findings: list[dict[str, Any]] = []
    status = "completed" if language.lower().split("-")[0] == "ru" else "unsupported_language"
    if status == "completed":
        for rule, pattern, severity, message in _RULES:
            for match in re.finditer(pattern, visible, re.I):
                if rule == "placeholder" or not _is_protected(*match.span(), protected):
                    findings.append(_finding(text, rule, *match.span(), severity, message))
        findings.extend(_genitive_candidates(text, visible, protected))
        findings.extend(_typography_findings(text, visible, protected))
        findings.extend(_glossary_findings(text, visible, glossary or {}))
        if not _WORDS.search(visible):
            findings.append(_finding(text, "no_prose", 0, 0, "error", "Не найден проверяемый основной текст."))
        seen_paragraphs: dict[str, int] = {}
        openings: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for start, end in _paragraphs(visible):
            paragraph = visible[start:end]
            words = _WORDS.findall(paragraph)
            norm = re.sub(r"\s+", " ", paragraph).strip().casefold()
            if len(words) >= 12 and norm in seen_paragraphs:
                findings.append(_finding(text, "repeated_paragraph", start, end, "warning", "Абзац дословно повторяется; проверьте необходимость повторения."))
            seen_paragraphs[norm] = start
            if len(words) >= 6:
                first = list(_WORDS.finditer(paragraph))[:3]
                key = " ".join(m.group().casefold() for m in first)
                openings[key].append((start + first[0].start(), start + first[-1].end()))
        for spans in openings.values():
            if len(spans) >= 3:
                for start, end in spans:
                    findings.append(_finding(text, "repeated_opening", start, end, "warning", "Одинаковое начало у трёх или более абзацев; проверьте логику переходов."))
        for start, end in sentence_spans(visible):
            if len(_WORDS.findall(visible[start:end])) > 45:
                findings.append(_finding(text, "long_sentence", start, end, "warning", "Более 45 слов: проверьте читаемость, сохранив определения и условия вывода. Это эвристика, не запрет."))
    findings.sort(key=lambda f: (f["offset"], f["rule"]))
    return {
        "tool": "ru_science_lint", "version": RULESET_VERSION,
        "execution_status": status, "manuscript_sha256": manuscript_sha256(text),
        "language": language, "findings": findings,
        "disposition": "not_assessed" if status != "completed" else (
            "fail" if any(f["severity"] == "error" for f in findings) else "needs_review" if findings else "pass"
        ),
        "author_profile": profile_style(author_samples, mode=author_style_mode), "manuscript_tempo": tempo_profile([text]),
        "protected_terms": list(protected_terms),
        "glossary": glossary or {}, "glossary_sha256": _json_sha256(glossary or {}),
        "origin_detection": "not_performed; editorial signals do not establish AI authorship",
    }


def _utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _utf16_index(text: str, offset: int) -> int:
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise ValueError("Invalid LanguageTool UTF-16 offset")
    position = 0
    for index, char in enumerate(text):
        if position == offset:
            return index
        position += 2 if ord(char) > 0xFFFF else 1
        if position > offset:
            raise ValueError("LanguageTool offset splits a UTF-16 surrogate pair")
    if position == offset:
        return len(text)
    raise ValueError("LanguageTool offset is outside the checked chunk")


def _chunks(text: str, max_units: int) -> list[tuple[int, str]]:
    if not isinstance(max_units, int) or isinstance(max_units, bool) or max_units < 32:
        raise ValueError("max_chunk_chars must be an integer of at least 32 UTF-16 units")
    chunks: list[tuple[int, str]] = []
    start = 0
    while start < len(text):
        end, units = start, 0
        while end < len(text):
            size = 2 if ord(text[end]) > 0xFFFF else 1
            if units + size > max_units:
                break
            units += size
            end += 1
        if end < len(text):
            lower = start + (end - start) // 2
            candidates = [m.end() for m in re.finditer(r"\n\s*\n|[.!?]\s+|\s+", text[start:end])]
            candidates = [start + i for i in candidates if start + i >= lower]
            if candidates:
                end = candidates[-1]
        chunks.append((start, text[start:end]))
        start = end
    return chunks


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _endpoint(url: str, allow_remote: bool) -> tuple[str, str]:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("LanguageTool URL must use http or https and contain a host")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("Credentials, query parameters and fragments are not allowed in the endpoint URL")
    hostname = parts.hostname.lower().rstrip(".")
    try:
        local = ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        local = hostname == "localhost"
    if not local and not allow_remote:
        raise PermissionError("Remote manuscript transfer requires explicit allow_remote=True")
    if hostname in ("api.languagetool.org", "languagetool.org", "www.languagetool.org"):
        raise ValueError("Do not automate the free public LanguageTool endpoint; use self-hosted or a contracted API")
    path = parts.path.rstrip("/")
    if path in ("", "/v2"):
        path = "/v2/check"
    elif not path.endswith("/v2/check"):
        raise ValueError("LanguageTool URL must point to /v2/check (or the server root)")
    return urlunsplit((parts.scheme, parts.netloc, path, "", "")), "loopback" if local else "remote_explicit"


def check_languagetool(text: str, *, url: str | None, language: str = "ru", protected_terms: Sequence[str] = (), allow_remote: bool = False, timeout: float = 20.0, max_chunk_chars: int = 10000) -> dict[str, Any]:
    """Call a supplied LanguageTool server; incomplete or invalid results fail."""
    result: dict[str, Any] = {
        "tool": "languagetool", "execution_status": "not_run", "disposition": "not_assessed",
        "manuscript_sha256": manuscript_sha256(text), "language": language,
        "protected_terms": list(protected_terms), "max_chunk_chars": max_chunk_chars,
        "findings": [], "raw_chunks": [], "completed_chunks": 0,
    }
    if not url:
        result["reason"] = "LanguageTool URL was not supplied; no network request was made."
        return result
    try:
        endpoint, kind = _endpoint(url, allow_remote)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be a positive finite number")
        result.update({"endpoint": endpoint, "endpoint_kind": kind})
        visible = mask_nonprose(text)
        protected = _protected_spans(visible, protected_terms)
        chunks = [(start, chunk) for start, chunk in _chunks(visible, max_chunk_chars) if _WORDS.search(chunk)]
        if not chunks:
            raise ValueError("No prose is available for LanguageTool checking")
        result["total_chunks"] = len(chunks)
        versions: set[str] = set()
        # Disable ambient proxies as well as redirects: an explicit loopback
        # check must not silently send the manuscript through another service.
        opener = build_opener(ProxyHandler({}), _NoRedirect())
        for chunk_index, (start, chunk) in enumerate(chunks):
            request = Request(endpoint, data=urlencode({"language": language, "text": chunk}).encode("utf-8"), headers={"Content-Type": "application/x-www-form-urlencoded; charset=utf-8", "Accept": "application/json"}, method="POST")
            with opener.open(request, timeout=timeout) as response:
                data = response.read(5_000_001)
            if len(data) > 5_000_000:
                raise ValueError("LanguageTool response exceeds the 5 MB safety limit")
            body = json.loads(data.decode("utf-8"))
            result["raw_chunks"].append({"index": chunk_index, "start_codepoint": start, "utf16_length": _utf16_length(chunk), "checked_text_sha256": manuscript_sha256(chunk), "response": body})
            if not isinstance(body, dict) or not isinstance(body.get("matches"), list):
                raise ValueError("LanguageTool response has no valid matches array")
            warnings = body.get("warnings", {})
            if not isinstance(warnings, dict) or warnings.get("incompleteResults"):
                raise ValueError("LanguageTool reported incomplete results")
            reported_language = body.get("language", {}).get("code", "") if isinstance(body.get("language", {}), dict) else ""
            if reported_language and language != "auto" and reported_language.lower().split("-")[0] != language.lower().split("-")[0]:
                raise ValueError("LanguageTool returned a different language than requested")
            software = body.get("software", {})
            if isinstance(software, dict) and software.get("version"):
                versions.add(str(software["version"]))
            for match in body["matches"]:
                if not isinstance(match, dict) or not isinstance(match.get("rule"), dict):
                    raise ValueError("Malformed LanguageTool match")
                offset, length = match.get("offset"), match.get("length")
                if not isinstance(length, int) or isinstance(length, bool) or length < 0:
                    raise ValueError("Invalid LanguageTool match length")
                a = start + _utf16_index(chunk, offset)
                b = start + _utf16_index(chunk, offset + length)
                rule = match["rule"]
                if not isinstance(rule.get("id"), str) or not isinstance(match.get("message"), str):
                    raise ValueError("LanguageTool match lacks rule ID or message")
                if rule.get("issueType") == "misspelling" and _is_protected(a, b, protected):
                    continue
                finding = _finding(text, rule["id"], a, b, "warning", match["message"], tool="languagetool")
                finding["replacements"] = match.get("replacements", [])
                finding["issue_type"] = rule.get("issueType")
                result["findings"].append(finding)
            result["completed_chunks"] += 1
        if len(versions) > 1:
            raise ValueError("LanguageTool version changed during this check")
        result["version"] = next(iter(versions)) if versions else "not_reported"
        result["execution_status"] = "completed"
        result["disposition"] = "needs_review" if result["findings"] else "pass"
    except PermissionError as exc:
        result["reason"] = str(exc)
    except HTTPError as exc:
        result["execution_status"] = "unavailable" if exc.code in (401, 403, 408, 429, 502, 503, 504) else "failed"
        result["reason"] = f"LanguageTool HTTP {exc.code}; no redirect or fallback was followed."
    except (URLError, TimeoutError, ConnectionError, OSError) as exc:
        result["execution_status"] = "unavailable"
        result["reason"] = f"LanguageTool connection failed: {type(exc).__name__}"
    except (ValueError, TypeError, KeyError, UnicodeError) as exc:
        result["execution_status"] = "failed"
        result["reason"] = str(exc)
    return result


def import_glavred_report(path: str | Path | None, text: str) -> dict[str, Any]:
    """Import an attested actual check without inventing a Glavred API."""
    result: dict[str, Any] = {
        "tool": "glavred", "execution_status": "not_run", "disposition": "not_assessed",
        "manuscript_sha256": manuscript_sha256(text), "findings": [],
        "reason": "No attested Glavred report was supplied; no API request was made.",
    }
    if path is None:
        return result
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("tool") not in ("glavred", "glvrd"):
            raise ValueError("Import must identify tool=glavred (or glvrd)")
        if not isinstance(data.get("version"), str) or not data["version"].strip():
            raise ValueError("Glavred import needs version; use not_reported if the UI does not expose it")
        if data.get("manuscript_sha256") != result["manuscript_sha256"]:
            raise ValueError("Glavred report belongs to a different manuscript SHA-256")
        if "status" in data and "execution_status" in data and data["status"] != data["execution_status"]:
            raise ValueError("Conflicting external status fields")
        if data.get("status", data.get("execution_status")) != "completed":
            raise ValueError("Glavred import must describe an actually completed check")
        attestation = data.get("attestation", {})
        if not isinstance(attestation, dict) or attestation.get("actual_run") is not True:
            raise ValueError("Glavred report needs attestation.actual_run=true")
        if not str(attestation.get("performed_by", "")).strip() or attestation.get("method") not in ("manual_ui", "official_api"):
            raise ValueError("Glavred report needs performed_by and method=manual_ui|official_api")
        if not isinstance(attestation.get("statement"), str) or len(attestation["statement"].strip()) < 10:
            raise ValueError("Glavred report needs a textual statement attesting to the actual check")
        checked = datetime.fromisoformat(str(data.get("checked_at", "")).replace("Z", "+00:00"))
        if checked.tzinfo is None:
            raise ValueError("Glavred checked_at must include a timezone")
        if not isinstance(data.get("raw_report"), (str, dict, list)) or not data["raw_report"]:
            raise ValueError("Glavred import needs a non-empty raw_report, not just a claimed score")
        result.update({
            "execution_status": "completed", "disposition": "needs_review",
            "checked_at": data["checked_at"], "provenance": "user_attested_import",
            "version": data["version"],
            "attestation": attestation, "raw_report": data["raw_report"],
            "reason": "Imported attestation and matching text hash; provider execution was not independently observed by this program.",
        })
    except (OSError, ValueError, TypeError, KeyError) as exc:
        result["execution_status"] = "failed"
        result["reason"] = str(exc)
    return result


def run_quality(manuscript: str | Path, *, output_dir: str | Path, language: str = "ru", protected_terms: Sequence[str] = (), author_samples: Sequence[str] = (), author_style_mode: str = "author_samples", languagetool_url: str | None = None, allow_remote: bool = False, timeout: float = 20.0, max_chunk_chars: int = 10000, glavred_report: str | Path | None = None, required_tools: Sequence[str] = DEFAULT_REQUIRED_TOOLS, execution_profile: str = "strict_local", glossary: dict[str, Any] | None = None, publication: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run stage 5 for a UTF-8 file and persist quality.json plus raw LT data.

    ``author_samples`` contains text, not paths. Missing required services keep
    required_complete false. Completion does not close editorial findings or
    replace independent scientific review.
    """
    if execution_profile not in ("strict_local", "offline_sandbox"):
        raise ValueError("execution_profile must be strict_local or offline_sandbox")
    glossary = {} if glossary is None else glossary
    publication = {} if publication is None else publication
    if not isinstance(glossary, dict) or not isinstance(publication, dict):
        raise ValueError("glossary and publication must be objects")
    required = tuple("glavred" if name == "glvrd" else name for name in required_tools)
    unknown = set(required) - {"ru_science_lint", "languagetool", "glavred"}
    if unknown or not required:
        raise ValueError(f"Unknown or empty required_tools: {sorted(unknown)}")
    manuscript_path = Path(manuscript)
    # read_bytes/decode preserves CRLF in the hash and offset space.
    text = manuscript_path.read_bytes().decode("utf-8")
    local = analyze_text(text, language=language, protected_terms=protected_terms, author_samples=author_samples, author_style_mode=author_style_mode, glossary=glossary)
    lt = check_languagetool(text, url=languagetool_url, language=language, protected_terms=protected_terms, allow_remote=allow_remote, timeout=timeout, max_chunk_chars=max_chunk_chars)
    glavred = import_glavred_report(glavred_report, text)
    english = check_english_abstract(publication, text)
    if execution_profile == "offline_sandbox":
        for check, supplied, skip_reason in ((lt, bool(languagetool_url), "no_endpoint_configured"), (glavred, glavred_report is not None, "no_attested_report_supplied")):
            if not supplied and check["execution_status"] == "not_run":
                check.update(
                    execution_status="skipped_no_runtime", request_attempted=False,
                    skip_reason=skip_reason,
                    reason="offline_sandbox explicitly selected; " + check["reason"] + " No completed external check is claimed.",
                )
    checks = [local, lt, glavred, english]
    for check in checks:
        check["status"] = check["execution_status"]
        check["execution_profile"] = execution_profile
    by_tool = {check["tool"]: check for check in checks}
    incomplete = [name for name in required if by_tool[name]["execution_status"] != "completed"]
    statuses = {by_tool[name]["execution_status"] for name in incomplete}
    overall = "completed" if not incomplete else "failed" if "failed" in statuses else "unavailable" if "unavailable" in statuses else "partial" if statuses == {"skipped_no_runtime"} else "not_run"
    findings = [f for check in checks for f in check["findings"]]
    has_errors = any(f["severity"] == "error" for f in findings)
    draft_complete = not has_errors and local["execution_status"] == "completed" and (
        not incomplete or execution_profile == "offline_sandbox" and all(
            name in ("languagetool", "glavred") and by_tool[name]["execution_status"] == "skipped_no_runtime"
            for name in incomplete
        )
    )
    report = {
        "schema_version": "2.0", "stage": 5, "execution_profile": execution_profile,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "manuscript_sha256": manuscript_sha256(text), "manuscript_path": str(manuscript_path),
        "language": language, "execution_status": overall,
        "required_tools": list(required), "required_complete": not incomplete,
        "missing_required_tools": incomplete, "has_blocking_findings": has_errors,
        "draft_quality_complete": draft_complete,
        "ready_for_editor_review": draft_complete,
        "glossary_sha256": _json_sha256(glossary), "publication_sha256": _json_sha256(publication),
        "scope": {
            "main_text": "Russian editorial heuristics; grammar only when a supplied provider actually completed",
            "english_abstract": english["execution_status"] + ": " + english["scope"],
            "unassessed": ["scientific truth", "translation equivalence", "AI authorship"],
        },
        "tools": by_tool, "findings": findings,
        "author_profile": local["author_profile"], "manuscript_tempo": local["manuscript_tempo"],
        "editor_review": "pending", "scientific_review": "pending",
        "warnings": (["Обязательные проверки выполнены не полностью."] if incomplete else []) + (
            ["Образцы автора не предоставлены; индивидуальный стиль не заявляется."]
            if local["author_profile"]["status"] == "not_supplied" else []
        ) + (["Автономный профиль: пропущенные сервисы не выполнены; допускается только выпуск с явно указанными ограничениями, без готовности к подаче."] if execution_profile == "offline_sandbox" else []) + ["Редакторские метрики не устанавливают научность или происхождение текста."],
        "origin_detection": "not_performed; no AI-authorship probability is produced",
    }
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    raw_dir = destination / "raw"
    if raw_dir.is_symlink():
        raise ValueError("Raw output directory must not be a symbolic link")
    raw_dir.mkdir(parents=True, exist_ok=True)
    report["raw_artifacts"] = []
    raw_records = [
        (local, dict(local)),
        (lt, {"tool": "languagetool", "manuscript_sha256": report["manuscript_sha256"], "execution_status": lt["execution_status"], "language": language, "protected_terms": list(protected_terms), "max_chunk_chars": max_chunk_chars, "chunks": lt.pop("raw_chunks"), "execution_profile": execution_profile, **{key: lt[key] for key in ("reason", "skip_reason", "request_attempted") if key in lt}}),
        (english, {**english, "publication": publication}),
    ]
    if glavred.get("raw_report"):
        raw_records.append((glavred, {"tool": "glavred", "execution_status": "completed", "version": glavred["version"], "checked_at": glavred["checked_at"], "manuscript_sha256": report["manuscript_sha256"], "attestation": glavred["attestation"], "raw_report": glavred.pop("raw_report"), "execution_profile": execution_profile}))
    else:
        raw_records.append((glavred, dict(glavred)))
    for check, raw_data in raw_records:
        raw_bytes = (json.dumps(raw_data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        raw_sha = hashlib.sha256(raw_bytes).hexdigest()
        relative = f"raw/{check['tool']}.{raw_sha[:16]}.json"
        raw_path = destination / relative
        if raw_path.is_symlink() or not raw_path.resolve().is_relative_to(destination.resolve()):
            raise ValueError("Raw output path must remain within the report directory")
        raw_path.write_bytes(raw_bytes)
        check["raw_report_path"] = relative
        check["raw_report_sha256"] = raw_sha
        report["raw_artifacts"].append({"tool": check["tool"], "path": relative, "sha256": raw_sha, "manuscript_sha256": report["manuscript_sha256"]})
    output = destination / "quality.json"
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=destination, prefix=".quality-", suffix=".json", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        temporary.replace(output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return report


def verify_raw_artifacts(report: dict[str, Any], output_dir: str | Path, text: str, *, glossary: dict[str, Any] | None = None, publication: dict[str, Any] | None = None, execution_profile: str | None = None) -> list[str]:
    """Check report consistency against current text and recorded provider data.

    This detects incomplete executions and accidental summary tampering. It is
    not cryptographic proof that a remote service or an independent person ran.
    Missing required executions remain the workflow engine's responsibility.
    """
    errors: list[str] = []
    if not isinstance(report, dict) or not isinstance(report.get("tools"), dict):
        return ["quality: tools должен быть объектом"]
    tools = report["tools"]
    if set(tools) != {"ru_science_lint", "languagetool", "glavred", "en_abstract_lint"}:
        errors.append("quality: отсутствует или неизвестен инструмент обязательного формата v2")
    profile = report.get("execution_profile", "strict_local")
    if profile not in ("strict_local", "offline_sandbox"):
        errors.append("quality: неизвестный профиль исполнения")
    if execution_profile is not None and profile != execution_profile:
        errors.append("quality: профиль исполнения не соответствует текущему brief")
    if glossary is not None and report.get("glossary_sha256") != _json_sha256(glossary):
        errors.append("quality: хеш словаря не соответствует текущему glossary")
    if publication is not None and report.get("publication_sha256") != _json_sha256(publication):
        errors.append("quality: хеш издательского блока не соответствует текущему publication")
    raw_items = report.get("raw_artifacts")
    if not isinstance(raw_items, list):
        return ["quality: raw_artifacts должен быть массивом"]
    root = Path(output_dir).resolve()
    text_hash = manuscript_sha256(text)
    if report.get("manuscript_sha256") != text_hash:
        errors.append("quality: SHA-256 текущего текста не совпадает")
    by_tool: dict[str, tuple[dict[str, Any], Path]] = {}
    for item in raw_items:
        if not isinstance(item, dict) or not isinstance(item.get("tool"), str) or not isinstance(item.get("path"), str):
            errors.append("quality: некорректная запись raw_artifacts")
            continue
        name = item["tool"]
        if name in by_tool:
            errors.append(f"{name}: дублирующиеся raw artifacts")
            continue
        execution = tools.get(name)
        if not isinstance(execution, dict):
            errors.append(f"{name}: raw artifact не связан с инструментом")
            continue
        path = (root / item["path"]).resolve()
        if Path(item["path"]).is_absolute() or not path.is_relative_to(root):
            errors.append(f"{name}: путь raw выходит за каталог отчётов")
            continue
        try:
            raw_bytes = path.read_bytes()
            actual_hash = hashlib.sha256(raw_bytes).hexdigest()
            if item.get("sha256") != actual_hash:
                errors.append(f"{name}: SHA-256 raw artifact не совпадает")
                continue
            if execution.get("raw_report_path") != item["path"] or execution.get("raw_report_sha256") != actual_hash:
                errors.append(f"{name}: указатель инструмента не совпадает с raw manifest")
            data = json.loads(raw_bytes.decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("raw must be a JSON object")
            if item.get("manuscript_sha256") != text_hash or data.get("manuscript_sha256") != text_hash:
                errors.append(f"{name}: исходный отчёт относится к другому тексту")
            if data.get("tool") != name:
                errors.append(f"{name}: имя инструмента внутри raw не совпадает")
            if data.get("execution_status") != execution.get("execution_status"):
                errors.append(f"{name}: сырой статус расходится со сводным")
            if data.get("execution_profile") != profile or execution.get("execution_profile") != profile:
                errors.append(f"{name}: профиль исполнения raw/summary расходится с quality")
            by_tool[name] = (data, path)
        except (OSError, ValueError, UnicodeError) as exc:
            errors.append(f"{name}: исходный отчёт недоступен/некорректен ({type(exc).__name__})")

    global_findings = report.get("findings")
    expected_findings: list[dict[str, Any]] = []
    for name, execution in tools.items():
        if not isinstance(execution, dict):
            errors.append(f"{name}: отчёт инструмента должен быть объектом")
            continue
        if execution.get("status") != execution.get("execution_status"):
            errors.append(f"{name}: status и execution_status расходятся")
        if execution.get("manuscript_sha256") != text_hash:
            errors.append(f"{name}: инструмент проверял другую версию текста")
        findings = execution.get("findings")
        if not isinstance(findings, list) or any(not isinstance(f, dict) or not isinstance(f.get("id"), str) for f in findings):
            errors.append(f"{name}: findings должен содержать объекты с id")
            continue
        expected_findings.extend(findings)
        state = execution.get("execution_status")
        if state == "skipped_no_runtime":
            if profile != "offline_sandbox" or name not in ("languagetool", "glavred"):
                errors.append(f"{name}: skipped_no_runtime допустим только для внешнего сервиса в offline_sandbox")
            if name not in by_tool:
                errors.append(f"{name}: пропуск должен иметь исходную квитанцию")
            else:
                raw, _ = by_tool[name]
                expected_reason = "no_endpoint_configured" if name == "languagetool" else "no_attested_report_supplied"
                if raw.get("skip_reason") != expected_reason or execution.get("skip_reason") != expected_reason:
                    errors.append(f"{name}: не указан допустимый факт отсутствия конфигурации/отчёта")
                if raw.get("request_attempted") is not False or execution.get("request_attempted") is not False:
                    errors.append(f"{name}: пропуск не должен выдавать попытку запуска за отсутствие среды")
                if not isinstance(raw.get("reason"), str) or not raw["reason"].strip() or execution.get("reason") != raw.get("reason"):
                    errors.append(f"{name}: причина пропуска отсутствует или изменена")
                if raw.get("chunks") or raw.get("raw_report") or raw.get("attestation"):
                    errors.append(f"{name}: пропуск не может содержать заявленный результат сервиса")
            if findings or execution.get("disposition") != "not_assessed" or execution.get("completed_chunks", 0) != 0:
                errors.append(f"{name}: пропущенная проверка не имеет выполненных результатов")
        if name == "en_abstract_lint":
            if name not in by_tool:
                errors.append("en_abstract_lint: нет raw с проверяемым издательским блоком")
                continue
            raw, _ = by_tool[name]
            try:
                recorded_publication = raw.get("publication")
                if not isinstance(recorded_publication, dict):
                    raise ValueError("publication must be an object")
                current_en = check_english_abstract(recorded_publication, text)
                if report.get("publication_sha256") != current_en["publication_sha256"]:
                    errors.append("en_abstract_lint: хеш publication расходится с quality")
                if publication is not None and recorded_publication != publication:
                    errors.append("en_abstract_lint: проверялся другой издательский блок")
                for key in ("version", "execution_status", "disposition", "findings", "input_sha256", "publication_sha256", "scope", "location"):
                    if raw.get(key) != current_en[key] or execution.get(key) != current_en[key]:
                        errors.append(f"en_abstract_lint: {key} не соответствует фактическому входу")
            except (TypeError, ValueError, KeyError) as exc:
                errors.append(f"en_abstract_lint: недействительный raw ({exc})")
            continue
        if execution.get("execution_status") != "completed":
            continue
        if name not in by_tool:
            errors.append(f"{name}: завершённая проверка не имеет доступного raw artifact")
            continue
        raw, raw_path = by_tool[name]
        if raw.get("execution_status") != "completed":
            errors.append(f"{name}: сырой отчёт не подтверждает завершённую проверку")
            continue
        try:
            if name == "ru_science_lint":
                terms = raw.get("protected_terms")
                if not isinstance(terms, list) or any(not isinstance(term, str) for term in terms):
                    raise ValueError("protected_terms must be an array of strings")
                recorded_glossary = raw.get("glossary", {})
                if not isinstance(recorded_glossary, dict):
                    raise ValueError("glossary must be an object")
                current = analyze_text(text, language=raw.get("language", "ru"), protected_terms=terms, glossary=recorded_glossary)
                if glossary is not None and recorded_glossary != glossary:
                    errors.append("ru_science_lint: проверялся другой словарь")
                if execution.get("glossary") != recorded_glossary or raw.get("glossary_sha256") != current["glossary_sha256"] or execution.get("glossary_sha256") != current["glossary_sha256"] or report.get("glossary_sha256") != current["glossary_sha256"]:
                    errors.append("ru_science_lint: словарь raw/summary не соответствует хешам")
                if raw.get("version") != RULESET_VERSION or execution.get("version") != RULESET_VERSION:
                    errors.append("ru_science_lint: изменились правила; запустите проверку заново")
                if execution.get("protected_terms") != terms or execution.get("language") != raw.get("language"):
                    errors.append("ru_science_lint: настройки raw и summary различаются")
                if raw.get("findings") != findings or current["findings"] != findings:
                    errors.append("ru_science_lint: находки не соответствуют текущему тексту и правилам")
                if execution.get("disposition") != current["disposition"]:
                    errors.append("ru_science_lint: disposition расходится с фактическими находками")
            elif name == "languagetool":
                chunks = raw.get("chunks")
                maximum = raw.get("max_chunk_chars")
                terms = raw.get("protected_terms")
                if not isinstance(chunks, list) or not chunks:
                    raise ValueError("completed LanguageTool needs non-empty recorded responses")
                if not isinstance(terms, list) or any(not isinstance(term, str) for term in terms):
                    raise ValueError("LanguageTool protected_terms must be a string array")
                if execution.get("max_chunk_chars") != maximum or execution.get("protected_terms") != terms or execution.get("language") != raw.get("language"):
                    errors.append("languagetool: настройки raw и summary различаются")
                visible = mask_nonprose(text)
                expected_chunks = [(start, chunk) for start, chunk in _chunks(visible, maximum) if _WORDS.search(chunk)]
                if len(chunks) != len(expected_chunks) or execution.get("completed_chunks") != len(expected_chunks) or execution.get("total_chunks") != len(expected_chunks):
                    raise ValueError("LanguageTool does not cover every required chunk")
                protected = _protected_spans(visible, terms)
                recalculated: list[dict[str, Any]] = []
                versions: set[str] = set()
                for index, (entry, (start, chunk)) in enumerate(zip(chunks, expected_chunks)):
                    if not isinstance(entry, dict) or entry.get("index") != index or entry.get("start_codepoint") != start or entry.get("utf16_length") != _utf16_length(chunk) or entry.get("checked_text_sha256") != manuscript_sha256(chunk):
                        raise ValueError("LanguageTool chunk positions/hash do not match checked text")
                    body = entry.get("response")
                    if not isinstance(body, dict) or not isinstance(body.get("matches"), list):
                        raise ValueError("LanguageTool response must contain matches array")
                    warnings = body.get("warnings", {})
                    if not isinstance(warnings, dict) or warnings.get("incompleteResults"):
                        raise ValueError("LanguageTool returned incomplete results")
                    software = body.get("software", {})
                    if isinstance(software, dict) and software.get("version"):
                        versions.add(str(software["version"]))
                    reported_language = body.get("language", {}).get("code", "") if isinstance(body.get("language", {}), dict) else ""
                    requested_language = raw.get("language", "")
                    if not isinstance(requested_language, str) or (reported_language and requested_language != "auto" and reported_language.lower().split("-")[0] != requested_language.lower().split("-")[0]):
                        raise ValueError("LanguageTool language differs from request")
                    for match in body["matches"]:
                        if not isinstance(match, dict) or not isinstance(match.get("rule"), dict):
                            raise ValueError("Malformed LanguageTool match")
                        offset, length = match.get("offset"), match.get("length")
                        if not isinstance(length, int) or isinstance(length, bool) or length < 0:
                            raise ValueError("Invalid match length")
                        a = start + _utf16_index(chunk, offset)
                        b = start + _utf16_index(chunk, offset + length)
                        rule = match["rule"]
                        if not isinstance(rule.get("id"), str) or not isinstance(match.get("message"), str):
                            raise ValueError("Missing LanguageTool rule or message")
                        if rule.get("issueType") == "misspelling" and _is_protected(a, b, protected):
                            continue
                        finding = _finding(text, rule["id"], a, b, "warning", match["message"], tool="languagetool")
                        finding["replacements"] = match.get("replacements", [])
                        finding["issue_type"] = rule.get("issueType")
                        recalculated.append(finding)
                if recalculated != findings:
                    errors.append("languagetool: находки summary расходятся с сырыми ответами")
                version = next(iter(versions)) if len(versions) == 1 else "not_reported" if not versions else None
                if version != execution.get("version"):
                    errors.append("languagetool: версия сервера расходится с raw")
                if execution.get("disposition") != ("needs_review" if findings else "pass"):
                    errors.append("languagetool: disposition расходится с находками")
            elif name == "glavred":
                imported = import_glavred_report(raw_path, text)
                if imported["execution_status"] != "completed":
                    errors.append("glavred: raw import не имеет действительного подтверждения")
                for key in ("attestation", "version", "checked_at", "provenance", "disposition", "findings"):
                    if execution.get(key) != imported.get(key):
                        errors.append(f"glavred: {key} расходится с исходным импортом")
            else:
                errors.append(f"{name}: нет поддерживаемого raw validator")
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            errors.append(f"{name}: недействительный raw отчёт: {exc}")

    def finding_map(values: Any) -> dict[str, dict] | None:
        if not isinstance(values, list) or any(not isinstance(f, dict) or not isinstance(f.get("id"), str) for f in values):
            return None
        mapping = {f["id"]: f for f in values}
        return mapping if len(mapping) == len(values) else None
    global_map, expected_map = finding_map(global_findings), finding_map(expected_findings)
    if global_map is None or expected_map is None or global_map != expected_map:
        errors.append("quality: общий список findings не совпадает с результатами инструментов")
    actual_blockers = any(f.get("severity") == "error" for f in expected_findings)
    if report.get("has_blocking_findings") is not actual_blockers:
        errors.append("quality: has_blocking_findings расходится с фактическими находками")
    required = report.get("required_tools")
    if not isinstance(required, list) or not required or any(not isinstance(name, str) for name in required) or len(set(required)) != len(required) or any(name not in ("ru_science_lint", "languagetool", "glavred") for name in required):
        errors.append("quality: неверный список обязательных инструментов")
    else:
        incomplete = [name for name in required if not isinstance(tools.get(name), dict) or tools[name].get("execution_status") != "completed"]
        if report.get("required_complete") is not (not incomplete) or report.get("missing_required_tools") != incomplete:
            errors.append("quality: итог обязательных проверок расходится с реальными статусами")
        local_complete = isinstance(tools.get("ru_science_lint"), dict) and tools["ru_science_lint"].get("execution_status") == "completed"
        draft_complete = not actual_blockers and local_complete and (
            not incomplete or profile == "offline_sandbox" and all(
                name in ("languagetool", "glavred") and isinstance(tools.get(name), dict) and tools[name].get("execution_status") == "skipped_no_runtime"
                for name in incomplete
            )
        )
        if report.get("draft_quality_complete") is not draft_complete or report.get("ready_for_editor_review") is not draft_complete:
            errors.append("quality: черновой допуск расходится с фактическими проверками")
    return list(dict.fromkeys(errors))
