"""Structural and cross-artifact validation, never a substitute for scientific review."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[2]
MARKER = re.compile(r"\[(SRC|INF|EXP):([A-Za-z0-9._-]+)\]")
RAW_MARKER = re.compile(r"\[(?:SRC|INF|EXP):[^\]\n]*\]")
PLACEHOLDER = re.compile(r"\[(?:TODO|TBD|FILL|УТОЧНИТЬ|ЗАПОЛНИТЬ)(?:[^\]]*)\]", re.I)


class ContractError(ValueError):
    """An artifact is missing, malformed, stale, or scientifically unready."""


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_path(root: Path, relative: str) -> Path:
    """Resolve artifact paths without escaping the run (including via symlinks)."""
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root.resolve()):
        raise ContractError(f"Путь выходит за каталог запуска: {relative}")
    return candidate


def bundle_hash(run: Path, names: list[str]) -> str:
    digest = hashlib.sha256()
    for name in sorted(set(names)):
        path = safe_path(run, name)
        digest.update(name.encode("utf-8"))
        digest.update((sha256(path) if path.is_file() else "MISSING").encode("ascii"))
    return digest.hexdigest()


def linked_artifacts(run: Path, groups: tuple[str, ...] = ("author", "sources", "analysis")) -> list[str]:
    """Include data/source/log bytes, not just their user-supplied manifest hashes."""
    names: list[str] = []
    brief_path = run / "brief.json"
    if "author" in groups and brief_path.exists():
        brief = read_json(brief_path)
        names.extend(brief.get("author_style", {}).get("sample_paths", []))
    if "sources" in groups and (run / "sources.jsonl").exists():
        for source in read_jsonl(run / "sources.jsonl"):
            if source.get("local_path"):
                names.append(source["local_path"])
    if "analysis" in groups and (run / "analysis/data-manifest.json").exists():
        for dataset in read_json(run / "analysis/data-manifest.json").get("datasets", []):
            names.extend(entry["path"] for entry in dataset.get("files", []) if isinstance(entry, dict) and entry.get("path"))
    if "analysis" in groups and (run / "analysis/results.json").exists():
        for result in read_json(run / "analysis/results.json").get("results", []):
            if result.get("log_path"):
                names.append(result["log_path"])
            names.extend(result.get("code_paths", []))
    return names


def quality_context(run: Path) -> str:
    return bundle_hash(run, ["brief.json", "glossary.json", "style-profile.json"])


def review_context(run: Path, chief: bool = False) -> str:
    names = [
        "brief.json", "journal-profile.json", "style-profile.json", "sources.jsonl",
        "evidence.jsonl", "glossary.json", "school-map.md", "search-log.jsonl",
        "research-plan.md", "literature-review.md", "outline.md", "manuscript.md",
        "analysis/analysis-plan.md", "analysis/data-manifest.json", "analysis/results.json",
        "analysis/plan-freeze.json", "reports/source-audit.json", "reports/quality.json",
        "reports/editorial-review.json",
    ]
    names.extend(linked_artifacts(run))
    if chief:
        names.extend(["reports/domain-review.json", "reports/methods-review.json", "revision-log.md"])
    return bundle_hash(run, names)


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"), parse_constant=_reject_nonfinite)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"Не удалось прочитать JSON {path.name}: {exc}") from exc


def _reject_nonfinite(value: str) -> None:
    raise ContractError(f"JSON не допускает нечисловое/бесконечное значение {value}")


def read_jsonl(path: Path) -> list[dict]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ContractError(f"Не удалось прочитать {path.name}: {exc}") from exc
    rows = []
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line, parse_constant=_reject_nonfinite)
        except json.JSONDecodeError as exc:
            raise ContractError(f"{path.name}:{number}: некорректный JSON") from exc
        if not isinstance(row, dict):
            raise ContractError(f"{path.name}:{number}: ожидается объект")
        rows.append(row)
    return rows


def write_json(path: Path, data: Any) -> None:
    write_text(path, json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".writing-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def schema_errors(data: Any, name: str, label: str = "") -> list[str]:
    schema = read_json(ROOT / "schemas" / f"{name}.schema.json")
    errors = sorted(Draft202012Validator(schema).iter_errors(data), key=lambda e: str(list(e.path)))
    return [f"{label or name} {'.'.join(map(str, error.path))}: {error.message}" for error in errors]


def indexed(rows: list[dict], label: str) -> tuple[dict[str, dict], list[str]]:
    result, errors = {}, []
    for row in rows:
        if not isinstance(row, dict):
            errors.append(f"{label}: записи должны быть объектами JSON")
            continue
        identity = row.get("id")
        if not isinstance(identity, str):
            errors.append(f"{label}: отсутствует строковый id")
        elif identity in result:
            errors.append(f"{label}: повторный id {identity}")
        else:
            result[identity] = row
    return result, errors


def validate_sources(run: Path) -> tuple[dict[str, dict], list[str]]:
    rows = read_jsonl(run / "sources.jsonl")
    sources, errors = indexed(rows, "sources")
    if not rows:
        errors.append("Каталог источников пуст")
    dois: dict[str, str] = {}
    for source in rows:
        sid = source.get("id", "?")
        current = schema_errors(source, "source", sid)
        errors.extend(current)
        if current:
            continue
        if source["status"] == "included":
            if not source["metadata_verified"]:
                errors.append(f"{sid}: библиографические сведения не проверены")
            if not source.get("verification_url") or not source.get("verified_at"):
                errors.append(f"{sid}: нужны verification_url и verified_at")
        if source["access"] == "selected_pages" and not source.get("read_locator"):
            errors.append(f"{sid}: укажите реально прочитанные страницы в read_locator")
        doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", source.get("doi") or "", flags=re.I).strip().lower()
        if doi:
            if not re.match(r"^10\.\d{4,9}/\S+$", doi):
                errors.append(f"{sid}: некорректный формат DOI")
            elif doi in dois:
                errors.append(f"{sid}: DOI дублирует {dois[doi]}; объедините записи")
            else:
                dois[doi] = sid
        if source.get("local_path"):
            local = safe_path(run, source["local_path"])
            if not local.is_file():
                errors.append(f"{sid}: локальный источник отсутствует")
            elif not source.get("content_sha256") or sha256(local) != source["content_sha256"]:
                errors.append(f"{sid}: hash локального источника отсутствует или устарел")
    return sources, errors


def validate_glossary(run: Path, sources: dict) -> list[str]:
    glossary = read_json(run / "glossary.json")
    errors = schema_errors(glossary, "glossary")
    if errors:
        return errors
    terms, more = indexed(glossary["entries"], "glossary")
    errors.extend(more)
    for term in terms.values():
        for sid in term["source_ids"]:
            if sid not in sources:
                errors.append(f"{term['id']}: неизвестный источник {sid}")
            elif sources[sid]["access"] not in ("full_text", "selected_pages"):
                errors.append(f"{term['id']}: определение требует прочитанного текста/страницы {sid}")
            if not term["locators"].get(sid):
                errors.append(f"{term['id']}: нет места определения в {sid}")
        for relation in term["relations"]:
            if relation["target_id"] not in terms:
                errors.append(f"{term['id']}: неизвестный термин отношения {relation['target_id']}")
    for phrase in glossary["phrasebook"]:
        for sid in phrase["source_ids"]:
            if sid not in sources:
                errors.append(f"phrasebook: неизвестный источник {sid}")
    return errors


def validate_evidence(run: Path, sources: dict) -> tuple[dict, list[str]]:
    rows = read_jsonl(run / "evidence.jsonl")
    claims, errors = indexed(rows, "evidence")
    results_artifact = read_json(run / "analysis/results.json")
    result_rows = results_artifact.get("results", []) if isinstance(results_artifact, dict) else []
    result_ids = {row.get("id") for row in result_rows if isinstance(row, dict)} if isinstance(result_rows, list) else set()
    if not rows:
        errors.append("Реестр утверждений пуст")
    for claim in rows:
        cid = claim.get("id", "?")
        current = schema_errors(claim, "evidence", cid)
        errors.extend(current)
        if current:
            continue
        if claim["kind"] == "source" and not claim["source_ids"]:
            errors.append(f"{cid}: утверждению источника нужен source_ids")
        if claim["kind"] == "inference" and not (claim.get("depends_on") or claim.get("result_ids")):
            errors.append(f"{cid}: выводу нужны depends_on и/или result_ids")
        if claim["kind"] == "source" and claim.get("result_ids"):
            errors.append(f"{cid}: собственные результаты не являются сообщением внешнего источника")
        for rid in claim.get("result_ids", []):
            if rid not in result_ids:
                errors.append(f"{cid}: неизвестный собственный результат {rid}")
        for sid in claim["source_ids"]:
            source = sources.get(sid)
            if source is None:
                errors.append(f"{cid}: неизвестный источник {sid}")
                continue
            if source.get("status") != "included":
                errors.append(f"{cid}: источник {sid} не включён в обзор")
            if not claim["locators"].get(sid):
                errors.append(f"{cid}: отсутствует точное место в {sid}")
            if claim["verification"] == "verified":
                access = source.get("access")
                if access in ("metadata_only", "unavailable"):
                    errors.append(f"{cid}: метаданные {sid} не подтверждают содержание")
                if access == "abstract_only" and claim.get("support_span") != "abstract":
                    errors.append(f"{cid}: вывод по {sid} должен ограничиваться аннотацией")
        for dependency in claim.get("depends_on", []):
            if dependency not in claims:
                errors.append(f"{cid}: неизвестное основание {dependency}")
            elif claims[dependency].get("verification") != "verified" and claim["verification"] == "verified":
                errors.append(f"{cid}: вывод основан на непроверенном {dependency}")
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(identity: str) -> None:
        if identity in visiting:
            errors.append(f"Циклическое доказательство: {identity}")
            return
        if identity in visited or identity not in claims:
            return
        visiting.add(identity)
        for dependency in claims[identity].get("depends_on", []):
            visit(dependency)
        visiting.remove(identity)
        visited.add(identity)

    for identity in claims:
        visit(identity)
    return claims, errors


def validate_results(run: Path, empirical_required: bool) -> tuple[dict, list[str]]:
    artifact = read_json(run / "analysis/results.json")
    errors = schema_errors(artifact, "results")
    if errors:
        return {}, errors
    results, more = indexed(artifact["results"], "results")
    errors.extend(more)
    manifest = read_json(run / "analysis/data-manifest.json")
    manifest_errors = schema_errors(manifest, "data-manifest")
    if manifest_errors:
        return results, errors + manifest_errors
    datasets, more = indexed(manifest["datasets"], "datasets")
    errors.extend(more)
    for did, dataset in datasets.items():
        if dataset.get("kind") not in ("empirical", "synthetic", "simulation"):
            errors.append(f"{did}: неизвестный вид данных")
        files = dataset.get("files", [])
        if not files:
            errors.append(f"{did}: отсутствует перечень локальных файлов данных")
        for entry in files:
            if not isinstance(entry, dict) or not entry.get("path"):
                errors.append(f"{did}: файлы должны иметь path и sha256")
                continue
            path = safe_path(run, entry["path"])
            if not path.is_file() or sha256(path) != entry.get("sha256"):
                errors.append(f"{did}: данные отсутствуют или изменились: {entry['path']}")
    if empirical_required and not any(d.get("kind") == "empirical" for d in datasets.values()):
        errors.append("Эмпирическая статья требует реальных данных с происхождением")
    if empirical_required and not results:
        errors.append("Эмпирическая статья требует реально полученных результатов")
    plan_hash = sha256(run / "analysis/analysis-plan.md") if (run / "analysis/analysis-plan.md").exists() else None
    brief = read_json(run / "brief.json")
    freeze = None
    if brief.get("study_mode") == "confirmatory":
        freeze_path = run / "analysis/plan-freeze.json"
        if not freeze_path.is_file():
            errors.append("Подтверждающий анализ требует предварительной фиксации плана")
        else:
            freeze = read_json(freeze_path)
            if freeze.get("analysis_plan_sha256") != plan_hash:
                errors.append("План изменён после предварительной фиксации")
    for rid, result in results.items():
        if not re.fullmatch(r"R\d{3,}", rid):
            errors.append(f"{rid}: ожидается идентификатор R001")
        for field in ("value", "unit", "kind", "dataset_ids", "command", "executed_at", "log_path", "log_sha256", "analysis_plan_sha256"):
            if field not in result:
                errors.append(f"{rid}: отсутствует {field}")
        if result.get("kind") not in ("empirical", "synthetic", "simulation", "analytical"):
            errors.append(f"{rid}: неизвестный kind результата")
        try:
            execution_time = datetime.fromisoformat(result["executed_at"])
            if execution_time.tzinfo is None:
                errors.append(f"{rid}: время выполнения должно содержать часовой пояс")
        except (ValueError, TypeError):
            errors.append(f"{rid}: некорректное время выполнения")
        if result.get("analysis_plan_sha256") != plan_hash:
            errors.append(f"{rid}: результат относится к другой версии плана анализа")
        if freeze:
            try:
                executed = datetime.fromisoformat(result.get("executed_at", ""))
                frozen = datetime.fromisoformat(freeze.get("frozen_at", ""))
                if executed.tzinfo is None or frozen.tzinfo is None or executed <= frozen:
                    errors.append(f"{rid}: результат должен быть получен после фиксации плана")
            except (ValueError, TypeError):
                errors.append(f"{rid}: некорректная хронология подтверждающего анализа")
        for did in result.get("dataset_ids", []):
            if did not in datasets:
                errors.append(f"{rid}: неизвестные данные {did}")
        if result.get("kind") == "empirical":
            if not result.get("dataset_ids") or any(datasets.get(d, {}).get("kind") != "empirical" for d in result.get("dataset_ids", [])):
                errors.append(f"{rid}: синтетика/моделирование не равны эмпирическому подтверждению")
        if result.get("log_path"):
            log = safe_path(run, result["log_path"])
            if not log.is_file() or sha256(log) != result.get("log_sha256"):
                errors.append(f"{rid}: журнал вычислений отсутствует или изменился")
    if not results and not artifact.get("not_applicable_reason"):
        errors.append("Отсутствие собственных результатов требует not_applicable_reason")
    return results, errors


def manuscript_errors(run: Path, claims: dict, results: dict) -> list[str]:
    text = (run / "manuscript.md").read_text(encoding="utf-8")
    errors = []
    if len(text.strip()) < 80:
        errors.append("Рукопись пуста или содержит только заголовок")
    if PLACEHOLDER.search(text):
        errors.append("В рукописи остались незаполненные placeholders")
    markers = MARKER.findall(text)
    if not markers:
        errors.append("В рабочей рукописи нет связей с доказательствами")
    if len(RAW_MARKER.findall(text)) != len(markers):
        errors.append("Повреждённый маркер доказательства")
    if re.search(r"\[(?:SRC|INF|EXP):", MARKER.sub("", text)):
        errors.append("Незакрытый или некорректный маркер доказательства")
    for category, identity in markers:
        if category == "EXP":
            if identity not in results:
                errors.append(f"[EXP:{identity}]: результат не зарегистрирован")
        elif identity not in claims:
            errors.append(f"[{category}:{identity}]: утверждение не зарегистрировано")
        else:
            claim = claims[identity]
            expected = "source" if category == "SRC" else "inference"
            if claim.get("kind") != expected:
                errors.append(f"[{category}:{identity}]: неверный тип доказательства")
            if claim.get("verification") != "verified":
                errors.append(f"[{category}:{identity}]: утверждение не проверено")
    return list(dict.fromkeys(errors))


REVIEW_DIMENSIONS = (
    "scientific_validity", "novelty", "evidence", "methods", "reproducibility",
    "russian_school", "terminology", "readability", "author_style", "journal_fit",
)


def validate_review(run: Path, filename: str, role: str, brief: dict) -> tuple[dict, list[str]]:
    report = read_json(run / "reports" / filename)
    errors = schema_errors(report, "review", filename)
    if errors:
        return report, errors
    if report["reviewer_role"] != role:
        errors.append(f"{filename}: неверная роль рецензента")
    if report["manuscript_sha256"] != sha256(run / "manuscript.md"):
        errors.append(f"{filename}: рецензия относится к другой версии рукописи")
    if report.get("review_context_sha256") != review_context(run, chief=role == "chief-reviewer"):
        errors.append(f"{filename}: изменились источники, данные или отчёты; рецензию нужно повторить")
    writer_ids = {identity.strip().casefold() for identity in brief["writer_ids"]}
    editorial_path = run / "reports/editorial-review.json"
    if editorial_path.exists():
        editorial = read_json(editorial_path)
        writer_ids.add(str(editorial.get("editor_id", "")).strip().casefold())
    if report["reviewer_id"].strip().casefold() in writer_ids or not report["independent_of_author"]:
        errors.append(f"{filename}: требуется рецензент, независимый от авторов")
    if brief["review_mode"] != "independent_sessions":
        errors.append("Самопроверка в одном сеансе не заменяет независимую рецензию")
    if report["decision"] not in ("accept", "minor_revision"):
        errors.append(f"{filename}: решение {report['decision']} требует доработки")
    issue_ids = set()
    for issue in report["issues"]:
        if issue["id"] in issue_ids:
            errors.append(f"{filename}: повтор issue id {issue['id']}")
        issue_ids.add(issue["id"])
        if issue["status"] == "open" and issue["severity"] in ("major", "blocker"):
            errors.append(f"{filename}: не устранено {issue['id']}")
    return report, errors
