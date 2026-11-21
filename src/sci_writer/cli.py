"""CLI for a host-agent workflow. No hidden model calls, fake runs, or publication."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__
from .contracts import (
    ROOT, ContractError, publication_metadata, quality_context, read_json, review_context,
    safe_path, sha256, write_json,
)
from .engine import complete, export_draft, finalize, freeze_plan, init_run, next_task, readiness_requirements, status, validate
from .literature import import_bibliography, search_into_run, search_openalex_into_run
from .quality import profile_style, run_quality


def hashes(run: Path) -> dict:
    names = {
        "manuscript_sha256": "manuscript.md", "sources_sha256": "sources.jsonl",
        "evidence_sha256": "evidence.jsonl", "quality_report_sha256": "reports/quality.json",
        "analysis_plan_sha256": "analysis/analysis-plan.md",
    }
    result = {key: sha256(safe_path(run, value)) if safe_path(run, value).is_file() else None for key, value in names.items()}
    result.update({"quality_context_sha256": quality_context(run), "review_context_sha256": review_context(run), "chief_context_sha256": review_context(run, chief=True)})
    return result


def create_style_profile(run: Path) -> dict:
    brief = read_json(safe_path(run, "brief.json"))
    paths = [safe_path(run, p) for p in brief.get("author_style", {}).get("sample_paths", [])]
    samples = [p.read_bytes().decode("utf-8") for p in paths]
    mode = "author_samples" if samples else "neutral_requested"
    profile = profile_style(samples, mode=mode)
    profile["mode"] = mode
    profile["samples_requested"] = bool(brief.get("author_style", {}).get("samples_requested"))
    profile["sample_count"] = len(samples)
    profile["sample_hashes"] = [sha256(p) for p in paths]
    profile.setdefault("preferences", {})
    profile.setdefault("limitations", [])
    if isinstance(profile["limitations"], str):
        profile["limitations"] = [profile["limitations"]]
    profile["limitations"].append("Метрики описывают образцы; они не являются квотами синтаксиса и не доказывают авторство.")
    write_json(safe_path(run, "style-profile.json"), profile)
    return profile


def quality_run(run: Path, *, url: str | None = None, allow_remote: bool = False, glavred_report: str | None = None) -> dict:
    brief = read_json(safe_path(run, "brief.json"))
    publication = publication_metadata(brief)  # Fail before any provider call or report write.
    glossary = read_json(safe_path(run, "glossary.json"))
    profile = read_json(safe_path(run, "style-profile.json"))
    terms = []
    for entry in glossary.get("entries", []):
        terms.extend([entry.get("preferred", ""), *entry.get("allowed_variants", [])])
    samples = [safe_path(run, path).read_bytes().decode("utf-8") for path in brief.get("author_style", {}).get("sample_paths", [])]
    if allow_remote and brief.get("external_processing_authorized") is not True:
        raise ContractError("Для удалённой передачи текста нужно явное разрешение в brief.external_processing_authorized")
    mode = "author_samples" if samples else "neutral_requested"
    report = run_quality(
        safe_path(run, "manuscript.md"), output_dir=safe_path(run, "reports"), language=brief["language"],
        protected_terms=[t for t in terms if t], author_samples=samples, author_style_mode=mode,
        languagetool_url=url, allow_remote=allow_remote,
        glavred_report=Path(glavred_report) if glavred_report else None,
        required_tools=brief["quality"]["required_tools"],
        execution_profile=brief.get("execution_profile", "strict_local"),
        glossary=glossary, publication=publication,
    )
    report["context_sha256"] = quality_context(run)
    report["author_style_declared_mode"] = profile.get("mode")
    write_json(safe_path(run, "reports/quality.json"), report)
    return report


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Научные агенты: источники, обязательный этап 5 и независимые рецензии")
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Создать новый запуск; существующие файлы не перезаписывать")
    init.add_argument("run", type=Path)
    init.add_argument("--topic", required=True)
    init.add_argument("--article-type", choices=("review", "original", "theoretical", "protocol", "methods"), default="review")
    init.add_argument("--language", choices=("ru", "ru-RU"), default="ru")
    init.add_argument("--execution-profile", choices=("strict_local", "offline_sandbox"), default="strict_local")
    init.add_argument("--review-mode", choices=("independent_sessions", "single_session_self_audit"), default="independent_sessions")
    for name in ("status", "next", "hashes", "readiness", "style-profile", "freeze-plan", "finalize", "export-draft"):
        command = commands.add_parser(name)
        command.add_argument("run", type=Path)
        if name in ("finalize", "export-draft"):
            command.add_argument("--formats", default="", help="Дополнительные форматы через запятую: docx,tex; Markdown сохраняется всегда")
        if name == "finalize":
            command.add_argument("--bibliography-style", choices=("gost", "generic"), help="Стиль цифровой библиографии; смена согласованного стиля требует повторной проверки перед подачей")
    validation = commands.add_parser("validate")
    validation.add_argument("run", type=Path)
    validation.add_argument("--stage")
    completion = commands.add_parser("complete")
    completion.add_argument("run", type=Path)
    completion.add_argument("stage")
    completion.add_argument("--actor", required=True, help="Фактический ID сеанса/исполнителя")
    quality = commands.add_parser("quality", help="Реальные проверки; без URL сервис не считается выполненным")
    quality.add_argument("run", type=Path)
    quality.add_argument("--languagetool-url")
    quality.add_argument("--allow-remote", action="store_true")
    quality.add_argument("--glavred-report", help="JSON отчёта реально выполненной проверки")
    search = commands.add_parser("search-crossref", help="Поиск кандидатов по метаданным; не чтение статей")
    search.add_argument("run", type=Path)
    search.add_argument("--query", required=True)
    search.add_argument("--rows", type=int, default=10)
    search.add_argument("--mailto")
    openalex = commands.add_parser("search-openalex", help="OpenAlex: метаданные, аннотации и OA-ссылки кандидатов")
    openalex.add_argument("run", type=Path)
    openalex.add_argument("--query", required=True)
    openalex.add_argument("--rows", type=int, default=10)
    openalex.add_argument("--language", help="Необязательный язык метаданных, например ru")
    openalex.add_argument("--api-key-env", default="OPENALEX_API_KEY", help="Имя переменной окружения с ключом; ключ не печатается")
    importer = commands.add_parser("import-bib", help="Импорт выгрузки BibTeX/RIS как непроверенных кандидатов")
    importer.add_argument("run", type=Path)
    importer.add_argument("path", type=Path)
    importer.add_argument("--format", choices=("bib", "bibtex", "ris"))
    importer.add_argument("--encoding", default="utf-8-sig")
    commands.add_parser("doctor", help="Проверить комплектность репозитория без сетевых запросов")
    return root


def doctor() -> dict:
    from .engine import stages, workflow

    required = ["AGENTS.md", "README.md", workflow()["coordinator"], "config/workflow.json"]
    required.extend(path for stage in stages() for path in stage["agents"])
    required.extend("schemas/" + name + ".schema.json" for name in ("brief", "source", "evidence", "glossary", "results", "data-manifest", "review", "external-check", "publication"))
    missing = [name for name in required if not (ROOT / name).is_file()]
    return {"ok": not missing, "version": __version__, "repository": str(ROOT), "missing": missing, "network_checked": False, "llm_execution": "host_agent_required", "full_manuscript_languages": ["ru", "ru-RU"]}


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    code = 0
    try:
        command = args.command
        run = args.run.resolve() if hasattr(args, "run") else None
        if command == "init":
            result = init_run(run, args.topic, args.article_type, args.language, execution_profile=args.execution_profile, review_mode=args.review_mode)
        elif command == "status":
            result = {"stages": status(run)}
        elif command == "next":
            result = next_task(run)
        elif command == "validate":
            result = validate(run, args.stage)
            code = 0 if result["ok"] else 2
        elif command == "complete":
            result = complete(run, args.stage, args.actor)
        elif command == "quality":
            full = quality_run(run, url=args.languagetool_url, allow_remote=args.allow_remote, glavred_report=args.glavred_report)
            result = {k: full[k] for k in ("manuscript_sha256", "execution_status", "required_complete", "missing_required_tools", "has_blocking_findings", "warnings")}
            result.update({"report": str(run / "reports/quality.json"), "finding_count": len(full["findings"]), "editor_review": "required"})
            result.update({key: full.get(key) for key in ("execution_profile", "draft_quality_complete")})
            code = 0 if (full["required_complete"] or full.get("draft_quality_complete")) and not full["has_blocking_findings"] else 2
        elif command == "hashes":
            result = hashes(run)
        elif command == "readiness":
            result = readiness_requirements(read_json(run / "brief.json"))
        elif command == "style-profile":
            result = create_style_profile(run)
        elif command == "freeze-plan":
            result = freeze_plan(run)
        elif command == "search-crossref":
            result = search_into_run(run, args.query, rows=args.rows, mailto=args.mailto)
        elif command == "search-openalex":
            result = search_openalex_into_run(run, args.query, rows=args.rows, api_key=os.environ.get(args.api_key_env), language=args.language)
        elif command == "import-bib":
            result = import_bibliography(run, args.path, format=args.format, encoding=args.encoding)
        elif command == "finalize":
            result = finalize(run, formats=_export_formats(args.formats), bibliography_style=args.bibliography_style)
        elif command == "export-draft":
            result = export_draft(run, formats=_export_formats(args.formats))
        else:
            result = doctor()
            code = 0 if result["ok"] else 2
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return code
    except (ContractError, OSError, UnicodeError, ValueError, TypeError, KeyError, AttributeError) as exc:
        # Malformed external artifacts fail closed; a traceback never becomes a green gate.
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


def _export_formats(value: str) -> tuple[str, ...]:
    formats = tuple(dict.fromkeys(x.strip().lower() for x in value.split(",") if x.strip()))
    if any(x not in ("docx", "tex") for x in formats):
        raise ContractError("Дополнительные форматы: docx,tex; Markdown и финальная библиография создаются автоматически")
    return formats


if __name__ == "__main__":
    raise SystemExit(main())
