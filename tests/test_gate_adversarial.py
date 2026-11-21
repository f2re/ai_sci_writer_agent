"""Adversarial regression cases for publication gates, using synthetic fixtures.

No fixture in this module claims a real scientific or external service result.
"""

from pathlib import Path
import tempfile
import unittest

from sci_writer import engine
from sci_writer.contracts import (
    ContractError, manuscript_errors, quality_context, read_json,
    sha256, validate_results, write_json,
)
from sci_writer.quality import run_quality


WORKSPACE = Path(__file__).resolve().parents[1]


class GateAdversarial(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="gate-fixture-", dir=WORKSPACE)
        self.root = Path(self.temporary.name)
        self.run = self.root / "run"
        engine.init_run(self.run, "Проверка защитных условий публикации")
        self.manuscript = (
            "Исследован временной ряд температуры. Доступные наблюдения использованы "
            "для оценки изменчивости и описания ограничений исследования."
        )
        (self.run / "manuscript.md").write_text(self.manuscript, encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    def test_claimed_service_success_cannot_override_raw_not_run(self):
        report = run_quality(self.run / "manuscript.md", output_dir=self.run / "reports")
        report["context_sha256"] = quality_context(self.run)
        # Simulate an agent accidentally completing the summary without ever
        # running the service. The raw not_run record remains untouched.
        report["required_complete"] = True
        report["execution_status"] = "completed"
        report["tools"]["languagetool"]["status"] = "completed"
        report["tools"]["languagetool"]["execution_status"] = "completed"
        write_json(self.run / "reports/quality.json", report)
        write_json(self.run / "reports/editorial-review.json", {
            "manuscript_sha256": sha256(self.run / "manuscript.md"),
            "quality_report_sha256": sha256(self.run / "reports/quality.json"),
            "editor_id": "fixture-editor", "decisions": [],
        })
        errors = engine._quality_errors(self.run, read_json(self.run / "brief.json"))
        self.assertTrue(errors, "A not_run raw execution must not pass as completed")

    def test_invalid_result_row_is_reported_without_unhandled_type_errors(self):
        write_json(self.run / "analysis/results.json", {"results": [None]})
        try:
            _, errors = validate_results(self.run, False)
        except ContractError:
            return
        self.assertTrue(errors)

    def test_empirical_result_needs_actual_execution_provenance(self):
        data = self.run / "analysis/data.csv"
        data.write_text("x\n1\n2\n", encoding="utf-8")
        write_json(self.run / "analysis/data-manifest.json", {
            "datasets": [{"id": "D001", "kind": "empirical", "files": [
                {"path": "analysis/data.csv", "sha256": sha256(data)}
            ]}]
        })
        write_json(self.run / "analysis/results.json", {"results": [{
            "id": "R001", "value": 123, "unit": "K", "kind": "empirical",
            "dataset_ids": ["D001"], "command": "", "executed_at": "",
            "log_path": "", "log_sha256": "",
            "analysis_plan_sha256": sha256(self.run / "analysis/analysis-plan.md"),
        }]})
        _, errors = validate_results(self.run, True)
        self.assertTrue(errors, "Present-but-empty execution fields must not establish an actual result")

    def test_unclosed_evidence_marker_is_rejected_even_with_valid_marker(self):
        text = self.manuscript + " [SRC:C001]. Незавершённая ссылка [SRC:C999"
        (self.run / "manuscript.md").write_text(text, encoding="utf-8")
        errors = manuscript_errors(self.run, {"C001": {"kind": "source", "verification": "verified"}}, {})
        self.assertTrue(errors, "Malformed evidence markers must not leak into release output")

    def test_export_cannot_write_through_a_directory_symlink(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.run / "draft-export").symlink_to(outside, target_is_directory=True)
        try:
            engine.export_draft(self.run)
        except ContractError:
            pass
        self.assertFalse((outside / "DRAFT.md").exists(), "The export directory must be contained within the run")


if __name__ == "__main__":
    unittest.main()
