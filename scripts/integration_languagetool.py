"""Exercise a REAL self-hosted LanguageTool, using deliberately nonresearch test text."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from sci_writer.quality import run_quality, verify_raw_artifacts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    manuscript = args.output / "integration-input.md"
    text = "# Проверка подключения\n\nТемператруа воздуха измерена термометром. Он пишут статью.\n\nЭто намеренно ошибочный текст для проверки сервиса, а не научная рукопись.\n"
    manuscript.write_text(text, encoding="utf-8")
    report = run_quality(manuscript, output_dir=args.output, languagetool_url=args.url, author_style_mode="neutral_requested")
    tool = report["tools"]["languagetool"]
    errors = verify_raw_artifacts(report, args.output, text)
    success = report["required_complete"] and tool["status"] == "completed" and bool(tool["findings"]) and not errors
    summary = {
        "status": "passed" if success else "failed", "test_type": "real_service_integration",
        "service": "self-hosted LanguageTool", "version": tool.get("version"),
        "finding_count": len(tool["findings"]), "integrity_errors": errors,
        "manuscript_sha256": report["manuscript_sha256"],
        "scope": "Service connectivity and Russian diagnostics only; not a scientific article or an AI-origin detector test.",
    }
    (args.output / "integration-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())

