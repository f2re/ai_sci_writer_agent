"""Seven-stage artifact workflow with conservative readiness and stale-review detection."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .contracts import (
    ROOT, MARKER, PLACEHOLDER, REVIEW_DIMENSIONS, ContractError, indexed,
    manuscript_errors, now, read_json, read_jsonl, safe_path, schema_errors,
    linked_artifacts, quality_context, review_context,
    sha256, validate_evidence, validate_glossary, validate_results,
    validate_review, validate_sources, write_json, write_text,
)


def workflow() -> dict:
    return read_json(ROOT / "config/workflow.json")


def stages() -> list[dict]:
    return workflow()["stages"]


def stage_by_id(identity: str) -> dict:
    for stage in stages():
        if stage["id"] == identity or stage["id"].split("_", 1)[1] == identity:
            return stage
    raise ContractError(f"Неизвестный этап: {identity}")


def policy_hash() -> str:
    """Invalidate work for executable contracts, not editorial prompt revisions.

    Breaking gate behavior changes require a config.contract_version bump. Prompt
    and implementation bytes remain separately recorded for reproducibility.
    """
    digest = hashlib.sha256()
    paths = []
    for folder, pattern in (("schemas", "*.json"), ("config", "*.json")):
        paths.extend((ROOT / folder).rglob(pattern))
    for path in sorted(paths):
        digest.update(str(path.relative_to(ROOT)).encode())
        digest.update((sha256(path) if path.is_file() else "MISSING").encode())
    return digest.hexdigest()


def provenance_hashes() -> dict:
    """Record the instructions/runtime used without treating copy edits as gates."""
    result = {}
    for name, paths in (
        ("instructions_sha256", [ROOT / "AGENTS.md", *(ROOT / "agents").rglob("*.md"), *(ROOT / "skills").rglob("*.md")]),
        ("implementation_sha256", list((ROOT / "src/sci_writer").glob("*.py"))),
    ):
        digest = hashlib.sha256()
        for path in sorted(paths):
            digest.update(str(path.relative_to(ROOT)).encode())
            digest.update((sha256(path) if path.is_file() else "MISSING").encode())
        result[name] = digest.hexdigest()
    return result


def readiness_requirements(brief: dict) -> dict:
    """Describe the ceiling imposed by execution conditions, never an approval."""
    profile = brief.get("execution_profile", "strict_local")
    mode = brief.get("review_mode", "independent_sessions")
    limitations = []
    if profile == "offline_sandbox":
        limitations.append("offline_quality")
    independent = mode == "independent_sessions"
    if not independent:
        limitations.append("self_audit")
    return {
        "execution_profile": profile, "review_mode": mode,
        "independent_peer_review": independent,
        "full_readiness_eligible": not limitations,
        "required_limitation_codes": limitations,
        "meaning": "Policy ceiling only; substantive reviews and all evidence gates remain required.",
    }


def artifact_hash(run: Path, names: list[str]) -> str:
    digest = hashlib.sha256()
    expanded = list(names)
    if "brief.json" in names:
        expanded.extend(linked_artifacts(run, ("author",)))
    if "sources.jsonl" in names:
        expanded.extend(linked_artifacts(run, ("sources",)))
    if "analysis/results.json" in names:
        expanded.extend(linked_artifacts(run, ("analysis",)))
        expanded.append("analysis/plan-freeze.json")
    if "reports/quality.json" in names and (run / "reports/quality.json").exists():
        report = read_json(run / "reports/quality.json")
        expanded.extend("reports/" + entry["path"] for entry in report.get("raw_artifacts", []) if isinstance(entry, dict) and entry.get("path"))
    for name in sorted(set(expanded)):
        path = safe_path(run, name)
        digest.update(name.encode())
        digest.update((sha256(path) if path.is_file() else "MISSING").encode())
    return digest.hexdigest()


def _state(run: Path) -> dict:
    path = safe_path(run, ".workflow/state.json")
    if not path.exists():
        raise ContractError("Запуск не инициализирован: выполните sci-writer init")
    data = read_json(path)
    if not isinstance(data, dict) or not isinstance(data.get("completed"), dict):
        raise ContractError("Некорректный .workflow/state.json")
    return data


@contextmanager
def state_lock(run: Path) -> Iterator[None]:
    lock = safe_path(run, ".workflow/lock")
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise ContractError("Другой процесс изменяет состояние запуска; дождитесь его завершения") from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))
        yield
    finally:
        lock.unlink(missing_ok=True)


def init_run(run: Path, topic: str, article_type: str = "review", language: str = "ru", *,
             execution_profile: str = "strict_local", review_mode: str = "independent_sessions") -> dict:
    if execution_profile not in ("strict_local", "offline_sandbox"):
        raise ContractError("execution_profile: ожидается strict_local или offline_sandbox")
    if review_mode not in ("independent_sessions", "single_session_self_audit", "single_session"):
        raise ContractError("review_mode: неизвестный режим рецензирования")
    if language not in ("ru", "ru-RU") or article_type not in ("review", "original", "theoretical", "protocol", "methods"):
        raise ContractError("Неподдерживаемые язык или тип статьи")
    if not isinstance(topic, str) or len(topic.strip()) < 3:
        raise ContractError("Нужна содержательная тема запуска")
    run = run.resolve()
    if run.exists() and any(run.iterdir()):
        raise ContractError("Каталог запуска не пуст; существующие материалы не перезаписаны")
    run.mkdir(parents=True, exist_ok=True)
    brief = {
        "schema_version": "2.0", "run_id": run.name, "topic": topic,
        "language": language, "article_type": article_type,
        "research_questions": [], "empirical_required": article_type == "original",
        "study_mode": "exploratory" if article_type == "original" else "not_applicable",
        "writer_ids": ["writer-session-01"], "review_mode": review_mode,
        "execution_profile": execution_profile, "publication": {},
        "author_style": {"sample_paths": [], "samples_requested": False},
        "quality": {"required_tools": ["ru_science_lint", "languagetool"]},
        "external_processing_authorized": False,
        "assumptions": [],
        "open_questions": [],
    }
    artifacts = {
        "brief.json": brief,
        "journal-profile.json": {
            "status": "unverified", "name": None, "issn": None,
            "index_targets": [], "checked_at": None, "sources": [],
            "requirements": [], "ai_policy": None, "format_checked": False,
        },
        "style-profile.json": {
            "mode": "neutral_requested", "samples_requested": False,
            "sample_count": 0, "sample_hashes": [], "metrics": None,
            "preferences": {}, "limitations": ["Образцы автора пока не предоставлены."],
        },
        "glossary.json": {"entries": [], "phrasebook": []},
        "analysis/data-manifest.json": {"datasets": []},
        "analysis/results.json": {"results": [], "not_applicable_reason": ""},
        "reports/source-audit.json": {
            "status": "not_run", "unverified_claim_ids": [], "unlinked_assertions": [],
            "domestic_foundation_assessment": {"status": "not_run", "reason": ""},
        },
    }
    for relative, data in artifacts.items():
        write_json(safe_path(run, relative), data)
    for name in ("sources.jsonl", "search-log.jsonl", "evidence.jsonl"):
        write_text(run / name, "")
    documents = {
        "school-map.md": "Карта российской научной школы",
        "literature-review.md": "Критический обзор литературы",
        "research-plan.md": "Исследовательские вопросы и критерии выводов",
        "analysis/analysis-plan.md": "План анализа и границы доказательств",
        "outline.md": "План аргументации статьи",
        "manuscript.md": "Рабочая рукопись",
        "revision-log.md": "Журнал замечаний и изменений",
    }
    for name, title in documents.items():
        write_text(safe_path(run, name), f"# {title}\n\n[TODO: заполнить на соответствующем этапе]\n")
    write_json(run / ".workflow/state.json", {"version": "2.0", "created_at": now(), "completed": {}, "history": []})
    return {"run": str(run), "next_stage": "01_intake", "status": "initialized", "topic": topic, **readiness_requirements(brief)}


def status(run: Path) -> list[dict]:
    state = _state(run)
    policy = policy_hash()
    upstream = policy
    dependency_ready = True
    result = []
    for stage in stages():
        output = artifact_hash(run, stage["outputs"])
        saved = state["completed"].get(stage["id"])
        if not saved:
            phase = "pending"
        elif not dependency_ready or saved.get("input_hash") != upstream or saved.get("output_hash") != output:
            phase = "stale"
        else:
            phase = "complete"
        result.append({"id": stage["id"], "title": stage["title"], "status": phase, "completed_at": saved.get("at") if saved else None})
        dependency_ready = dependency_ready and phase == "complete"
        upstream = hashlib.sha256((upstream + stage["id"] + output).encode()).hexdigest()
    return result


def next_task(run: Path) -> dict:
    pending = next((item for item in status(run) if item["status"] != "complete"), None)
    if pending is None:
        return {"status": "all_stages_complete", "next": "sci-writer finalize " + str(run)}
    stage = stage_by_id(pending["id"])
    task = (
        f"# {stage['id']}: {stage['title']}\n\n"
        f"Каталог запуска: `{run.resolve()}`.\n\n"
        f"Прочитайте AGENTS.md и {workflow()['coordinator']} в репозитории. "
        "Исполняйте следующие роли в отдельных сеансах, где требуется независимость. "
        "Этот файл задаёт работу агенту; CLI не вызывает языковую модель.\n\n"
        + "\n".join(f"- `{path}`" for path in stage["agents"])
        + "\n\n## Обязательные результаты\n\n"
        + "\n".join(f"- `{path}`" for path in stage["outputs"])
        + f"\n\nПроверьте `sci-writer validate {run} --stage {stage['id']}`. "
        f"Затем `sci-writer complete {run} {stage['id']} --actor <session-id>`.\n"
        "Не редактируйте .workflow/state.json вручную. Если рукопись изменена редактором, "
        "повторно закройте 04_draft, затем запустите 05_quality и новые рецензии.\n"
    )
    path = safe_path(run, ".workflow/tasks/" + stage["id"] + ".md")
    write_text(path, task)
    return {**pending, "agents": stage["agents"], "outputs": stage["outputs"], "task_file": str(path), "task": task,
            "readiness_requirements": readiness_requirements(read_json(run / "brief.json"))}


def _documents_ready(run: Path, paths: list[str]) -> list[str]:
    errors = []
    for name in paths:
        path = safe_path(run, name)
        if not path.is_file():
            errors.append(f"Нет обязательного файла: {name}")
            continue
        if path.suffix == ".md":
            text = path.read_text(encoding="utf-8")
            if len(text.strip()) < 40 or PLACEHOLDER.search(text):
                errors.append(f"{name}: незавершённый документ")
    return errors


def _intake_errors(run: Path, brief: dict) -> list[str]:
    errors = schema_errors(brief, "brief")
    if errors:
        return errors
    from .publishing import publication_errors
    errors.extend(publication_errors(brief.get("publication", {})))
    required = brief["quality"]["required_tools"]
    if "ru_science_lint" not in required or not any(x in required for x in ("languagetool", "glavred")):
        errors.append("Этап 5 требует локальный lint и как минимум одну реальную сервисную проверку")
    profile = read_json(run / "style-profile.json")
    mode = profile.get("mode") if isinstance(profile, dict) else None
    if mode not in ("author_samples", "neutral_requested", "neutral_approved"):
        errors.append("style-profile.json: укажите реальный режим авторского профиля")
    if not brief["author_style"]["sample_paths"]:
        if not brief["author_style"]["samples_requested"]:
            errors.append("Запросите образцы автора; продолжайте с явно обозначенным нейтральным стилем")
        if mode == "author_samples":
            errors.append("Профиль автора нельзя заявлять без образцов")
    elif mode != "author_samples":
        errors.append("Предоставлены образцы; постройте и проверьте профиль автора")
    else:
        sample_paths = brief["author_style"]["sample_paths"]
        hashes = profile.get("sample_hashes", [])
        actual = []
        for path_string in sample_paths:
            path = safe_path(run, path_string)
            if not path.is_file():
                errors.append(f"Нет предоставленного образца: {path_string}")
            else:
                actual.append(sha256(path))
        if sorted(actual) != sorted(hashes):
            errors.append("Авторский профиль не соответствует текущим образцам")
    journal = read_json(run / "journal-profile.json")
    if not isinstance(journal, dict) or journal.get("status") not in ("unverified", "verified"):
        errors.append("journal-profile.json: нужен status unverified|verified")
    elif journal["status"] == "verified":
        if not journal.get("name") or not journal.get("sources") or not journal.get("checked_at"):
            errors.append("Проверенному профилю журнала нужны название, источники и дата проверки")
    return errors


def _quality_errors(run: Path, brief: dict) -> list[str]:
    from .quality import verify_raw_artifacts

    report = read_json(run / "reports/quality.json")
    editorial = read_json(run / "reports/editorial-review.json")
    errors = []
    current = sha256(run / "manuscript.md")
    if not isinstance(report, dict) or not isinstance(editorial, dict):
        return ["Отчёты этапа 5 должны быть JSON objects"]
    for name, data in (("quality", report), ("editorial-review", editorial)):
        if data.get("manuscript_sha256") != current:
            errors.append(f"{name}: проверка относится к другой версии рукописи")
    if report.get("context_sha256") != quality_context(run):
        errors.append("Изменились профиль автора, термины или настройки; повторите quality")
    if editorial.get("quality_report_sha256") != sha256(run / "reports/quality.json"):
        errors.append("Редакторский разбор относится к другому отчёту quality")
    profile = brief.get("execution_profile", "strict_local")
    offline = profile == "offline_sandbox"
    if report.get("execution_profile", "strict_local") != profile:
        errors.append("quality: профиль исполнения не соответствует brief")
    if not report.get("required_complete") and not (offline and report.get("draft_quality_complete")):
        errors.append("Не все обязательные инструменты завершили проверку или допустимый автономный проход")
    tools = report.get("tools", {})
    for name in brief["quality"]["required_tools"]:
        execution = tools.get(name, {}) if isinstance(tools, dict) else {}
        skipped = offline and name != "ru_science_lint" and execution.get("status") == "skipped_no_runtime"
        if execution.get("status") != "completed" and not skipped:
            errors.append(f"{name}: status должен быть completed, сейчас {execution.get('status', 'missing')}")
    if report.get("has_blocking_findings"):
        errors.append("Инструментальная проверка содержит блокирующие замечания")
    errors.extend(verify_raw_artifacts(
        report, run / "reports", (run / "manuscript.md").read_bytes().decode("utf-8"),
        glossary=read_json(run / "glossary.json"), publication=brief.get("publication", {}), execution_profile=profile,
    ))
    raw = report.get("raw_artifacts", [])
    for item in raw:
        if not isinstance(item, dict) or not item.get("path"):
            errors.append("quality: повреждён реестр исходных отчётов")
            continue
        path = safe_path(run / "reports", item["path"])
        if not path.is_file() or sha256(path) != item.get("sha256"):
            errors.append(f"Исходный отчёт отсутствует или изменился: {item['path']}")
        if item.get("manuscript_sha256") != current:
            errors.append(f"Исходный отчёт {item['path']} относится к другой рукописи")
    for name in brief["quality"]["required_tools"]:
        if not any(item.get("tool") == name for item in raw if isinstance(item, dict)):
            errors.append(f"{name}: нет исходного отчёта с SHA-256")
    if not editorial.get("editor_id") or not isinstance(editorial.get("decisions"), list):
        return errors + ["editorial-review: нужны editor_id и decisions"]
    decisions = {}
    for entry in editorial["decisions"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("finding_id"), str):
            errors.append("Некорректное решение редактора")
            continue
        if entry["finding_id"] in decisions:
            errors.append(f"Повторное решение редактора: {entry['finding_id']}")
        decisions[entry["finding_id"]] = entry
    for finding in report.get("findings", []):
        if finding.get("severity") == "error":
            errors.append(f"Не исправлена ошибка: {finding.get('id')}")
        decision = decisions.get(finding.get("id"))
        if not decision:
            errors.append(f"Редактор не рассмотрел {finding.get('id')}")
        elif decision.get("disposition") not in ("accepted", "false_positive") or len(decision.get("reason", "")) < 10:
            errors.append(f"Для текущего {finding.get('id')} нужно обоснованное accepted/false_positive; fixed требует повторного запуска")
    return errors


def validate_stage(run: Path, identity: str) -> list[str]:
    stage = stage_by_id(identity)
    errors = _documents_ready(run, stage["outputs"])
    if errors:
        return errors
    try:
        brief = read_json(run / "brief.json")
        errors.extend(_intake_errors(run, brief))
        if errors or stage["id"] == "01_intake":
            return errors
        sources, found = validate_sources(run)
        errors.extend(found)
        if errors:
            return errors
        errors.extend(validate_glossary(run, sources))
        searches = read_jsonl(run / "search-log.jsonl")
        if not searches:
            errors.append("Нет журнала поиска литературы")
        for search in searches:
            if not all(search.get(k) for k in ("database", "query", "searched_at")):
                errors.append("search-log: нужны database, query, searched_at")
        if stage["id"] == "02_research" or errors:
            return errors
        claims, found = validate_evidence(run, sources)
        errors.extend(found)
        results, found = validate_results(run, brief["empirical_required"])
        errors.extend(found)
        audit = read_json(run / "reports/source-audit.json")
        if not isinstance(audit, dict) or audit.get("status") != "completed":
            errors.append("Аудит источников не выполнен")
        else:
            if audit.get("sources_sha256") != sha256(run / "sources.jsonl") or audit.get("evidence_sha256") != sha256(run / "evidence.jsonl"):
                errors.append("Аудит источников относится к другой версии каталога/утверждений")
            if audit.get("unverified_claim_ids") or audit.get("unlinked_assertions"):
                errors.append("Аудит содержит непроверенные утверждения или непокрытые тезисы")
            foundation = audit.get("domestic_foundation_assessment", {})
            if foundation.get("status") not in ("found", "limited", "none_found") or len(foundation.get("reason", "")) < 20:
                errors.append("Нужна мотивированная оценка российской научной школы")
            if foundation.get("status") == "found" and not any(s.get("tradition") == "russian" and s.get("status") == "included" and s.get("is_foundational") and s.get("access") in ("full_text", "selected_pages") for s in sources.values()):
                errors.append("Заявлена российская фундаментальная основа без прочитанного источника")
        if stage["id"] == "03_design" or errors:
            return errors
        errors.extend(manuscript_errors(run, claims, results))
        if stage["id"] == "04_draft" or errors:
            return errors
        errors.extend(_quality_errors(run, brief))
        if stage["id"] == "05_quality" or errors:
            return errors
        domain, found = validate_review(run, "domain-review.json", "domain-reviewer", brief)
        errors.extend(found)
        methods, found = validate_review(run, "methods-review.json", "methods-reviewer", brief)
        errors.extend(found)
        independence = brief["review_mode"] == "independent_sessions"
        if independence and str(domain.get("reviewer_id", "")).strip().casefold() == str(methods.get("reviewer_id", "")).strip().casefold():
            errors.append("Две независимые рецензии требуют разных reviewer_id")
        if stage["id"] == "06_review" or errors:
            return errors
        final, found = validate_review(run, "final-review.json", "chief-reviewer", brief)
        errors.extend(found)
        if independence and str(final.get("reviewer_id", "")).strip().casefold() in {str(domain.get("reviewer_id", "")).strip().casefold(), str(methods.get("reviewer_id", "")).strip().casefold()}:
            errors.append("Главный рецензент должен быть отдельным агентом")
        ceiling = readiness_requirements(brief)
        limited = not ceiling["full_readiness_eligible"]
        if limited:
            if final.get("scientific_ready") is not False or final.get("ready_for_submission") is not False:
                errors.append("Автономный режим или самопроверка допускают reviewed_manuscript с scientific_ready=false и ready_for_submission=false")
            if final.get("independent_peer_review") is not independence:
                errors.append("Итоговый отчёт должен честно указать independent_peer_review")
            if not final.get("limitations") or not all(isinstance(x, str) and len(x.strip()) >= 10 for x in final.get("limitations", [])):
                errors.append("Ограниченному выпуску нужны содержательные limitations")
            if set(ceiling["required_limitation_codes"]) != set(final.get("limitation_codes", [])):
                errors.append("limitation_codes должны соответствовать фактическим ограничениям: " + ", ".join(ceiling["required_limitation_codes"]))
        elif final.get("scientific_ready") is not True:
            errors.append("Главный рецензент не подтвердил научную готовность")
        if not limited and final.get("limitation_codes"):
            errors.append("limitation_codes противоречат полному режиму проверки")
        if "independent_peer_review" in final and final["independent_peer_review"] is not independence:
            errors.append("independent_peer_review не соответствует фактическому режиму рецензирования")
        if not isinstance(final.get("ready_for_submission"), bool) or not isinstance(final.get("limitations"), list):
            errors.append("Итоговая рецензия требует ready_for_submission и limitations")
        scores = final.get("scores", {})
        for dimension in REVIEW_DIMENSIONS:
            if dimension not in scores:
                errors.append(f"Нет мотивированной оценки: {dimension}")
        for dimension in ("scientific_validity", "evidence", "methods", "reproducibility"):
            if scores.get(dimension, {}).get("applicable") is not True:
                errors.append(f"Научная готовность требует содержательной оценки {dimension}")
        if not brief["author_style"]["sample_paths"] and scores.get("author_style", {}).get("score") is not None:
            errors.append("Без образцов автора нельзя выставлять балл сходства с автором")
        journal = read_json(run / "journal-profile.json")
        if journal.get("status") != "verified" and scores.get("journal_fit", {}).get("score") is not None:
            errors.append("Без проверенного профиля журнала journal_fit остаётся N/A")
        if final.get("ready_for_submission"):
            if limited:
                errors.append("Готовность к подаче требует strict_local и independent_sessions")
            journal = read_json(run / "journal-profile.json")
            if journal.get("status") != "verified" or not journal.get("format_checked") or not journal.get("ai_policy"):
                errors.append("Подача требует проверенных правил журнала, формата и AI policy")
            if any(i.get("status") == "open" for r in (domain, methods, final) for i in r.get("issues", [])):
                errors.append("Готовность к подаче невозможна при открытых замечаниях")
            from .publishing import publication_errors
            errors.extend(publication_errors(brief.get("publication", {}), journal.get("publication_required_fields", [])))
        return errors
    except (ContractError, OSError, UnicodeError, TypeError, KeyError, AttributeError) as exc:
        return errors + [str(exc)]


def validate(run: Path, identity: str | None = None) -> dict:
    selected = [stage_by_id(identity)] if identity else stages()
    outcomes = {stage["id"]: validate_stage(run, stage["id"]) for stage in selected}
    return {"ok": all(not errors for errors in outcomes.values()), "stages": outcomes}


def complete(run: Path, identity: str, actor: str) -> dict:
    stage = stage_by_id(identity)
    if not actor.strip():
        raise ContractError("Нужен действительный идентификатор исполнителя")
    with state_lock(run):
        stage_list = stages()
        position = stage_list.index(stage)
        statuses = status(run)
        unready = [x["id"] for x in statuses[:position] if x["status"] != "complete"]
        if unready:
            raise ContractError("Сначала повторно завершите зависимости: " + ", ".join(unready))
        errors = validate_stage(run, stage["id"])
        if stage["id"] == "04_draft":
            brief = read_json(run / "brief.json")
            if actor.strip().casefold() not in {str(x).strip().casefold() for x in brief.get("writer_ids", [])}:
                errors.append("Исполнитель 04_draft должен быть зарегистрирован в brief.writer_ids")
        if stage["id"] == "07_release":
            final = read_json(run / "reports/final-review.json")
            if actor.strip().casefold() != str(final.get("reviewer_id", "")).strip().casefold():
                errors.append("Последний этап закрывает фактический главный рецензент или автор итоговой самопроверки")
        if errors:
            raise ContractError("\n".join(errors))
        state = _state(run)
        upstream = policy_hash()
        for previous in stage_list[:position]:
            upstream = hashlib.sha256((upstream + previous["id"] + artifact_hash(run, previous["outputs"])).encode()).hexdigest()
        state["completed"][stage["id"]] = {"at": now(), "actor": actor, "input_hash": upstream, "output_hash": artifact_hash(run, stage["outputs"]), **provenance_hashes()}
        invalidated = []
        for downstream in stage_list[position + 1:]:
            if state["completed"].pop(downstream["id"], None) is not None:
                invalidated.append(downstream["id"])
        state["history"].append({"at": now(), "stage": stage["id"], "actor": actor, "invalidated": invalidated})
        write_json(safe_path(run, ".workflow/state.json"), state)
        return {"stage": stage["id"], "status": "complete", "invalidated": invalidated}


def freeze_plan(run: Path) -> dict:
    path = safe_path(run, "analysis/analysis-plan.md")
    if _documents_ready(run, ["analysis/analysis-plan.md"]):
        raise ContractError("Сначала заполните план анализа")
    target = safe_path(run, "analysis/plan-freeze.json")
    record = {"analysis_plan_sha256": sha256(path), "frozen_at": now(), "meaning": "local_timestamp_only_not_external_preregistration"}
    if target.exists():
        previous = read_json(target)
        if previous["analysis_plan_sha256"] != record["analysis_plan_sha256"]:
            raise ContractError("План изменён после фиксации. Оформите amendment и новый запуск исследования")
        return previous
    write_json(target, record)
    return record


def _render_manuscript(run: Path) -> tuple[str, list[dict], dict]:
    from .publishing import format_reference, publication_header
    sources, source_errors = validate_sources(run)
    claims, errors = validate_evidence(run, sources)
    if source_errors or errors:
        raise ContractError("\n".join(source_errors + errors))
    text = (run / "manuscript.md").read_text(encoding="utf-8")
    ordered: list[str] = []
    mapping: dict = {}
    removed = "\x00SCI_WRITER_REMOVED_MARKER\x00"

    def source_ids(cid: str, seen: set[str]) -> list[str]:
        if cid in seen:
            return []
        seen.add(cid)
        claim = claims[cid]
        answer = list(claim["source_ids"])
        for dependency in claim.get("depends_on", []):
            answer.extend(source_ids(dependency, seen))
        return list(dict.fromkeys(answer))

    def replace(match: re.Match) -> str:
        kind, identity = match.groups()
        if kind == "EXP":
            mapping[match.group()] = {"result_id": identity, "display": ""}
            return removed
        ids = source_ids(identity, set())
        for sid in ids:
            if sid not in ordered:
                ordered.append(sid)
        display = "[" + ", ".join(str(ordered.index(sid) + 1) for sid in ids) + "]" if ids else ""
        mapping[match.group()] = {"claim_id": identity, "source_ids": ids, "result_ids": claims[identity].get("result_ids", []), "display": display}
        return display or removed

    rendered = MARKER.sub(replace, text)
    rendered = re.sub(r"[ \t]*" + re.escape(removed) + r"[ \t]*(?=[.,;:!?…\)\]»])", "", rendered)
    rendered = rendered.replace(removed, "")
    brief = read_json(run / "brief.json")
    publication = brief.get("publication", {})
    citation_style = publication.get("citation_style", "gost-r-7.0.5-2008")
    bibliography = []
    for index, sid in enumerate(ordered, 1):
        source = sources[sid]
        reference = publication.get("reference_overrides", {}).get(sid) or format_reference(source, style=citation_style)
        bibliography.append(f"{index}. {reference}")
    language = brief["language"]
    heading = "Литература" if language.startswith("ru") else "References"
    if bibliography:
        rendered = rendered.rstrip() + "\n\n## " + heading + "\n\n" + "\n\n".join(bibliography)
    header = publication_header(publication)
    if header.strip():
        # The reviewed metadata owns the publication title, so do not duplicate it.
        title = publication.get("title", {}).get("ru")
        if title and re.match(r"^#\s+" + re.escape(title) + r"\s*\n", rendered):
            rendered = re.sub(r"^#\s+" + re.escape(title) + r"\s*\n", "", rendered, count=1).lstrip()
        rendered = header.rstrip() + "\n\n" + rendered.lstrip()
    rendered = rendered.rstrip() + "\n"
    return rendered, [sources[sid] for sid in ordered], mapping


def _bibtex(sources: list[dict]) -> str:
    from .publishing import bibliography_bibtex
    return bibliography_bibtex(sources)


def finalize(run: Path, *, formats: tuple[str, ...] = ()) -> dict:
    current = status(run)
    if any(item["status"] != "complete" for item in current):
        raise ContractError("Финальная сборка требует всех семи актуальных этапов")
    validation = validate(run)
    if not validation["ok"]:
        raise ContractError(json.dumps(validation["stages"], ensure_ascii=False, indent=2))
    if (run / "release").is_symlink():
        raise ContractError("Каталог итогового пакета не должен быть симлинком")
    target = safe_path(run, "release")
    if target.exists() and not target.is_dir():
        raise ContractError("release уже существует и не является каталогом")
    output = Path(tempfile.mkdtemp(prefix="release-build-", dir=safe_path(run, ".workflow")))
    try:
        manifest = _build_release(run, output, formats)
        # Build in isolation: an earlier DOCX/raw report can never silently join
        # a later release. Preserve the preceding generated packet as history.
        previous = None
        if target.exists():
            history = safe_path(run, ".workflow/release-history")
            history.mkdir(exist_ok=True)
            previous = history / output.name.replace("release-build-", "release-")
            target.rename(previous)
        try:
            output.rename(target)
        except OSError:
            if previous is not None:
                previous.rename(target)
            raise
        write_json(safe_path(run, "release-manifest.json"), manifest)
        return {"status": "released", "directory": str(target), **manifest}
    finally:
        if output.exists():
            shutil.rmtree(output)


def _build_release(run: Path, output: Path, formats: tuple[str, ...]) -> dict:
    rendered, sources, mapping = _render_manuscript(run)
    write_text(output / "article.md", rendered)
    write_text(output / "manuscript-audit.md", (run / "manuscript.md").read_text(encoding="utf-8"))
    write_text(output / "bibliography.bib", _bibtex(sources))
    write_json(output / "citation-map.json", mapping)
    for name in ("final-review.md", "final-review.json", "domain-review.json", "methods-review.json", "quality.json", "editorial-review.json", "source-audit.json"):
        write_text(safe_path(output, "reports/" + name), (run / "reports" / name).read_text(encoding="utf-8"))
    quality_report = read_json(run / "reports/quality.json")
    for item in quality_report["raw_artifacts"]:
        origin = safe_path(run / "reports", item["path"])
        write_text(safe_path(output, "reports/" + item["path"]), origin.read_bytes().decode("utf-8"))
    for name in ("sources.jsonl", "evidence.jsonl", "glossary.json", "school-map.md", "style-profile.json", "journal-profile.json", "revision-log.md"):
        write_text(safe_path(output, name), (run / name).read_text(encoding="utf-8"))
    final = read_json(run / "reports/final-review.json")
    brief = read_json(run / "brief.json")
    ceiling = readiness_requirements(brief)
    if not ceiling["full_readiness_eligible"]:
        warning = "> ПРОВЕРЕННАЯ РУКОПИСЬ С ОГРАНИЧЕНИЯМИ. Готовность к подаче не подтверждена. " + "; ".join(final["limitations"]) + "\n\n"
        rendered = warning + rendered
        write_text(output / "article.md", rendered)
    exports = {"files": [], "warnings": []}
    if formats:
        from .publishing import export_formats
        exports = export_formats(rendered, output, formats=formats)
        if final["ready_for_submission"] and exports.get("warnings"):
            raise ContractError("Форматный экспорт содержит предупреждения; перед заявлением готовности к подаче устраните их и повторите проверку формата: " + "; ".join(exports["warnings"]))
    manifest = {
        "created_at": now(), "run_id": brief["run_id"],
        "policy_sha256": policy_hash(), "manuscript_sha256": sha256(run / "manuscript.md"),
        "scientific_ready": final["scientific_ready"], "ready_for_submission": final["ready_for_submission"],
        "disposition": "prepared_for_submission" if final["ready_for_submission"] else "reviewed_manuscript",
        "contract_version": workflow().get("contract_version", workflow()["version"]),
        **provenance_hashes(),
        "execution_profile": ceiling["execution_profile"], "review_mode": ceiling["review_mode"],
        "independent_peer_review": ceiling["independent_peer_review"],
        "limitation_codes": ceiling["required_limitation_codes"], "limitations": final["limitations"],
        "citation_format": brief.get("publication", {}).get("citation_style", "gost-r-7.0.5-2008"),
        "publication_metadata": brief.get("publication", {}), "format_exports": exports,
        "stage_execution": _state(run)["completed"],
        "automated_checks_are_not_peer_review": True,
        "reproducibility": "Full source texts, author samples and data stay in the run; this release is a manuscript and review package.",
        "run_dependencies_not_bundled": {name: sha256(safe_path(run, name)) for name in linked_artifacts(run) if safe_path(run, name).is_file()},
        "files": {str(path.relative_to(output)): sha256(path) for path in sorted(output.rglob("*")) if path.is_file() and path.name != "release-manifest.json"},
    }
    write_json(output / "release-manifest.json", manifest)
    return manifest


def export_draft(run: Path, *, formats: tuple[str, ...] = ()) -> dict:
    output = safe_path(run, "draft-export")
    output.mkdir(exist_ok=True)
    text = safe_path(run, "manuscript.md").read_text(encoding="utf-8")
    text = "> НЕЗАВЕРШЁННАЯ РУКОПИСЬ. Научная готовность и пригодность к подаче не подтверждены.\n\n" + text
    write_text(safe_path(output, "DRAFT.md"), text)
    exports = {"files": [], "warnings": []}
    if formats:
        from .publishing import export_formats
        exports = export_formats(text, output, formats=formats)
    report = {"status": "draft", "scientific_ready": False, "ready_for_submission": False, "independent_peer_review": False, "stages": status(run), "validation": validate(run), "format_exports": exports}
    write_json(safe_path(output, "readiness.json"), report)
    return {"directory": str(output), **report}
