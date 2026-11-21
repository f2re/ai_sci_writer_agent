"""Independent regressions for workspace isolation and review-mode honesty.

All fixtures are artificial software artifacts, never claimed scientific work.
"""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_profiles as profiles
from sci_writer import engine
from sci_writer.contracts import ContractError, read_json, sha256, write_json, write_text
from sci_writer.mcp_server import WorkflowService


class WorkspaceBoundaryRegression(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.service = WorkflowService(self.root / "runs")
        self.service.init_run("fixture", "Проверка изоляции программных артефактов",
                              execution_profile="offline_sandbox", review_mode="single_session_self_audit")
        self.run = self.root / "runs/fixture"

    def tearDown(self):
        self.temp.cleanup()

    def test_external_workflow_symlink_cannot_be_read_or_locked(self):
        state = (self.run / ".workflow/state.json").read_bytes()
        outside = self.root / "outside-workflow"
        (self.run / ".workflow").rename(outside)
        (self.run / ".workflow").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ContractError):
            self.service.status("fixture")
        with self.assertRaises(ContractError):
            with engine.state_lock(self.run):
                self.fail("External workflow directory must not be locked")
        self.assertFalse((outside / "lock").exists())
        self.assertEqual((outside / "state.json").read_bytes(), state)

    def test_quality_rejects_external_manuscript_before_processing(self):
        outside = self.root / "outside-manuscript.md"
        outside.write_text("Программный маркер вне каталога запуска. TODO", encoding="utf-8")
        (self.run / "manuscript.md").unlink()
        (self.run / "manuscript.md").symlink_to(outside)
        with self.assertRaises(ContractError):
            self.service.run_quality("fixture")
        self.assertFalse((self.run / "reports/quality.json").exists())

    def test_plan_freeze_cannot_return_an_external_record(self):
        plan = self.run / "analysis/analysis-plan.md"
        write_text(plan, "# Программный план\n\nЭтот пример проверяет только границы доступа к артефактам запуска.\n")
        outside = self.root / "outside-freeze.json"
        write_json(outside, {"analysis_plan_sha256": sha256(plan), "frozen_at": "2026-11-21T12:00:00+03:00",
                             "private_fixture": "must not be returned through freeze_plan"})
        (self.run / "analysis/plan-freeze.json").symlink_to(outside)
        with self.assertRaises(ContractError):
            self.service.freeze_plan("fixture")

    def test_draft_export_cannot_copy_external_manuscript_before_failing(self):
        outside = self.root / "outside-manuscript.md"
        outside.write_text("Программный маркер вне каталога запуска.", encoding="utf-8")
        (self.run / "manuscript.md").unlink()
        (self.run / "manuscript.md").symlink_to(outside)
        with self.assertRaises(ContractError):
            self.service.export_draft("fixture")
        self.assertFalse((self.run / "draft-export/DRAFT.md").exists())

    def test_remote_consent_requires_literal_true_not_truthy_json(self):
        for claimed_consent in ("false", "true", 1, [True]):
            with self.subTest(claimed_consent=claimed_consent):
                brief = read_json(self.run / "brief.json")
                brief["external_processing_authorized"] = claimed_consent
                write_json(self.run / "brief.json", brief)
                with patch("sci_writer.cli.run_quality", side_effect=AssertionError("No processing before explicit consent")):
                    with self.assertRaises(ContractError):
                        self.service.run_quality("fixture", languagetool_url="https://example.org/v2/check", allow_remote=True)


class ReviewHonestyRegression(unittest.TestCase):
    def test_independent_report_cannot_explicitly_deny_its_independence(self):
        case = profiles.ExecutionProfiles()
        case.setUp()
        try:
            case.prepare_offline(independent=True)
            case.finish_review(independent=True)
            path = case.run / "reports/domain-review.json"
            report = read_json(path)
            report["independent_peer_review"] = False
            write_json(path, report)
            self.assertTrue(engine.validate_stage(case.run, "06_review"))
        finally:
            case.tearDown()


if __name__ == "__main__":
    unittest.main()
