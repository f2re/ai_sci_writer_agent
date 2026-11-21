"""Optional MCP transport over the same deterministic workflow engine.

The server is local stdio only. The host owns model calls and consent; this
module never shells out, executes article code, or accepts arbitrary root paths.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from . import engine
from .cli import create_style_profile, hashes as run_hashes, quality_run
from .contracts import ContractError, ROOT, read_json, safe_path, sha256, write_text

MAX_ARTIFACT_BYTES = 4_000_000
TEXT_EXTENSIONS = {".md", ".txt", ".json", ".jsonl", ".csv", ".tsv", ".bib", ".ris", ".tex", ".py"}


class WorkflowService:
    """Workspace-bound calls usable by both MCP and direct integration tests."""

    def __init__(self, workspace: Path):
        self.workspace = workspace.resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)

    def run_path(self, run_id: str, *, initialized: bool = True) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}", run_id):
            raise ContractError("run_id: 1–96 ASCII букв/цифр, точек, дефисов или подчёркиваний; без пути")
        run = safe_path(self.workspace, run_id)
        if initialized and not safe_path(run, ".workflow/state.json").is_file():
            raise ContractError("Запуск не инициализирован")
        return run

    def init_run(self, run_id: str, topic: str, article_type: str = "review",
                 execution_profile: str = "strict_local", review_mode: str = "independent_sessions") -> dict:
        return engine.init_run(self.run_path(run_id, initialized=False), topic, article_type,
                               execution_profile=execution_profile, review_mode=review_mode)

    def next_task(self, run_id: str) -> dict:
        result = engine.next_task(self.run_path(run_id))
        # A non-shell host can read exactly the current prompts, not the archive.
        paths = list(dict.fromkeys(["AGENTS.md", engine.workflow()["coordinator"], *result.get("agents", [])]))
        result["instructions"] = {path: safe_path(ROOT, path).read_text(encoding="utf-8") for path in paths}
        for path in result.get("agents", []):
            if "/references/" in path:
                parent = path.split("/references/", 1)[0] + "/SKILL.md"
                result["instructions"][parent] = safe_path(ROOT, parent).read_text(encoding="utf-8")
        return result

    def validate_stage(self, run_id: str, stage: str | None = None) -> dict:
        return engine.validate(self.run_path(run_id), stage)

    def complete_stage(self, run_id: str, stage: str, actor: str) -> dict:
        return engine.complete(self.run_path(run_id), stage, actor)

    def status(self, run_id: str) -> dict:
        run = self.run_path(run_id)
        return {"stages": engine.status(run), "readiness_requirements": engine.readiness_requirements(read_json(run / "brief.json"))}

    def run_quality(self, run_id: str, languagetool_url: str | None = None,
                    allow_remote: bool = False, glavred_report: str | None = None) -> dict:
        run = self.run_path(run_id)
        imported = str(safe_path(run, glavred_report)) if glavred_report else None
        with engine.state_lock(run):
            return quality_run(run, url=languagetool_url, allow_remote=allow_remote, glavred_report=imported)

    def hashes(self, run_id: str) -> dict:
        return run_hashes(self.run_path(run_id))

    def style_profile(self, run_id: str) -> dict:
        run = self.run_path(run_id)
        with engine.state_lock(run):
            return create_style_profile(run)

    def freeze_plan(self, run_id: str) -> dict:
        run = self.run_path(run_id)
        with engine.state_lock(run):
            return engine.freeze_plan(run)

    @staticmethod
    def formats(formats: list[str] | None) -> tuple[str, ...]:
        values = tuple(dict.fromkeys(formats or []))
        if any(value not in ("docx", "tex") for value in values):
            raise ContractError("Дополнительные форматы: docx, tex; Markdown формируется всегда")
        return values

    def finalize(self, run_id: str, formats: list[str] | None = None) -> dict:
        run = self.run_path(run_id)
        with engine.state_lock(run):
            return engine.finalize(run, formats=self.formats(formats))

    def export_draft(self, run_id: str, formats: list[str] | None = None) -> dict:
        run = self.run_path(run_id)
        with engine.state_lock(run):
            return engine.export_draft(run, formats=self.formats(formats))

    def read_artifact(self, run_id: str, path: str) -> dict:
        run = self.run_path(run_id)
        target = safe_path(run, path)
        if target.suffix not in TEXT_EXTENSIONS or not target.is_file():
            raise ContractError("Ожидается существующий текстовый артефакт запуска")
        with target.open("rb") as handle:
            raw = handle.read(MAX_ARTIFACT_BYTES + 1)
        if len(raw) > MAX_ARTIFACT_BYTES:
            raise ContractError("Артефакт превышает лимит 4 МБ; разделите материал на части")
        return {"path": str(target.relative_to(run)), "sha256": sha256(target), "content": raw.decode("utf-8"), "bytes": len(raw)}

    def write_artifact(self, run_id: str, path: str, content: str, expected_sha256: str | None = None) -> dict:
        run = self.run_path(run_id)
        relative = Path(path)
        if relative.is_absolute() or any(part.startswith(".") for part in relative.parts):
            raise ContractError("Нельзя писать абсолютные, скрытые или служебные пути")
        if not relative.parts or relative.parts[0] in ("release", "draft-export", "release-manifest.json"):
            raise ContractError("Итоговые пакеты создаются только finalize/export_draft")
        target = safe_path(run, path)
        resolved_parts = target.relative_to(run).parts
        if any(part.startswith(".") for part in resolved_parts) or resolved_parts[0] in ("release", "draft-export", "release-manifest.json"):
            raise ContractError("Ссылка ведёт в служебный путь или итоговый пакет")
        if target.suffix not in TEXT_EXTENSIONS or len(content.encode("utf-8")) > MAX_ARTIFACT_BYTES:
            raise ContractError("Нужен текстовый артефакт размером не более 4 МБ")
        with engine.state_lock(run):
            actual = sha256(target) if target.is_file() else None
            if actual != expected_sha256:
                raise ContractError("Артефакт изменился: прочитайте текущий sha256 и повторите осознанное редактирование")
            write_text(target, content)
            return {"path": str(target.relative_to(run)), "sha256": sha256(target), "validation": "not_implied; call validate_stage and complete_stage"}


def build_server(workspace: Path):
    """Support the official SDK's stable v1 and v2 import paths explicitly."""
    try:
        try:
            from mcp.server import MCPServer
        except ImportError:
            from mcp.server.fastmcp import FastMCP as MCPServer
    except ImportError as exc:
        raise ContractError("MCP SDK не установлен: pip install -e '.[mcp]' из репозитория") from exc
    service = WorkflowService(workspace)
    server = MCPServer("AI Scientific Writer", instructions=(
        "Use next_task for current instructions. All runs stay inside the configured workspace. "
        "Never claim a skipped check or self-audit is independent acceptance. "
        "read_artifact/write_artifact edit artifacts; complete_stage alone advances the validated workflow."
    ))
    functions = (
        (service.init_run, "Create an honest, initially incomplete run; choose strict or offline execution explicitly."),
        (service.next_task, "Get the next stage with its current repository prompts and required artifacts."),
        (service.validate_stage, "Validate one stage or the whole run without changing completion state."),
        (service.complete_stage, "Complete a stage only when all evidence, service and review gates pass."),
        (service.run_quality, "Execute real local lint and configured checks; preserve skipped/failed statuses and consent."),
        (service.hashes, "Get actual artifact and review-context SHA-256 values; never invent them."),
        (service.finalize, "Assemble an accepted or explicitly limited reviewed manuscript; never submit it to a journal."),
        (service.status, "Inspect stage freshness and the readiness ceiling imposed by execution/review modes."),
        (service.read_artifact, "Read a text artifact and its hash within one run, with a 4 MB limit."),
        (service.write_artifact, "Create/edit a text artifact using the current expected hash; internal state/releases are protected."),
        (service.style_profile, "Measure supplied author samples; absence of samples remains explicit."),
        (service.freeze_plan, "Record a local plan freeze before confirmatory analysis; not external preregistration."),
        (service.export_draft, "Export an explicitly unfinished draft without claiming readiness."),
    )
    for function, description in functions:
        server.tool(name=function.__name__, description=description)(function)
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Workspace-bound scientific workflow MCP server (stdio)")
    parser.add_argument("--workspace", type=Path, required=True, help="Dedicated parent directory for manuscript runs")
    args = parser.parse_args(argv)
    try:
        build_server(args.workspace).run(transport="stdio")
        return 0
    except (ContractError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
