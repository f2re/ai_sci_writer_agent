"""Execution ceilings preserve integrity while allowing honest limited releases.

All literature and review fixtures are explicitly artificial software examples.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_workflow as fixtures
from sci_writer import engine
from sci_writer.cli import hashes, quality_run
from sci_writer.contracts import ContractError, REVIEW_DIMENSIONS, read_json, write_json, write_text


class ExecutionProfiles(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.WorkflowContracts()
        self.case.setUp()
        self.run = self.case.run

    def tearDown(self):
        self.case.tearDown()

    def prepare_offline(self, independent=False):
        brief = read_json(self.run / "brief.json")
        brief["execution_profile"] = "offline_sandbox"
        brief["review_mode"] = "independent_sessions" if independent else "single_session_self_audit"
        write_json(self.run / "brief.json", brief)
        self.case.prepare()
        report = quality_run(self.run)
        digest = hashes(self.run)
        write_json(self.run / "reports/editorial-review.json", {
            "editor_id": "writer-session-01" if not independent else "editor-fixture",
            "manuscript_sha256": digest["manuscript_sha256"], "quality_report_sha256": digest["quality_report_sha256"],
            "decisions": [{"finding_id": item["id"], "disposition": "accepted", "reason": "Формулировка сохранена в искусственном программном примере."} for item in report["findings"]],
        })
        engine.complete(self.run, "05_quality", "writer-session-01")
        return report

    def finish_review(self, independent=False):
        digest = hashes(self.run)
        for kind in ("domain", "methods"):
            write_json(self.run / f"reports/{kind}-review.json", {
                "manuscript_sha256": digest["manuscript_sha256"], "review_context_sha256": digest["review_context_sha256"],
                "reviewer_id": kind + "-fixture" if independent else "writer-session-01", "reviewer_role": kind + "-reviewer",
                "review_mode": "independent_sessions" if independent else "single_session_self_audit",
                "independent_of_author": independent, "decision": "accept", "issues": [],
            })
        engine.complete(self.run, "06_review", "coordinator-fixture")
        digest = hashes(self.run)
        scores = {key: {"score": None if key in ("author_style", "journal_fit") else 3,
                        "applicable": key not in ("author_style", "journal_fit"),
                        "reason": "Искусственный пример для проверки контрактов; не научная экспертиза."} for key in REVIEW_DIMENSIONS}
        brief = read_json(self.run / "brief.json")
        final = {
            "manuscript_sha256": digest["manuscript_sha256"], "review_context_sha256": digest["chief_context_sha256"],
            "reviewer_id": "chief-fixture" if independent else "writer-session-01", "reviewer_role": "chief-reviewer",
            "review_mode": "independent_sessions" if independent else "single_session_self_audit",
            "independent_of_author": independent, "independent_peer_review": independent,
            "decision": "accept", "issues": [], "scientific_ready": False, "ready_for_submission": False,
            "scores": scores, "limitation_codes": engine.readiness_requirements(brief)["required_limitation_codes"],
            "limitations": ["Внешняя проверка не запускалась: автономный программный тест."] + ([] if independent else ["Независимых рецензентов нет; выполнена только самопроверка."]),
        }
        write_json(self.run / "reports/final-review.json", final)
        write_text(self.run / "reports/final-review.md", "# Ограниченный программный пример\n\nВнешняя проверка не выполнялась; реальные научные результаты отсутствуют.\n")
        engine.complete(self.run, "07_release", final["reviewer_id"])

    def test_offline_self_audit_releases_with_non_upgradeable_limits(self):
        report = self.prepare_offline()
        self.assertFalse(report["required_complete"])
        self.assertTrue(report["draft_quality_complete"])
        self.assertEqual(report["tools"]["languagetool"]["status"], "skipped_no_runtime")
        self.finish_review()
        result = engine.finalize(self.run)
        self.assertEqual(result["disposition"], "reviewed_manuscript")
        self.assertFalse(result["scientific_ready"])
        self.assertFalse(result["ready_for_submission"])
        self.assertFalse(result["independent_peer_review"])
        self.assertEqual(set(result["limitation_codes"]), {"offline_quality", "self_audit"})
        self.assertIn("С ОГРАНИЧЕНИЯМИ", (self.run / "release/article.md").read_text())
        final = read_json(self.run / "reports/final-review.json")
        final.update(scientific_ready=True, ready_for_submission=True)
        write_json(self.run / "reports/final-review.json", final)
        self.assertTrue(engine.validate_stage(self.run, "07_release"))
        with self.assertRaises(ContractError):
            engine.finalize(self.run)

    def test_offline_independent_reviews_preserve_independence_but_not_submission(self):
        self.prepare_offline(independent=True)
        self.finish_review(independent=True)
        result = engine.finalize(self.run)
        self.assertTrue(result["independent_peer_review"])
        self.assertFalse(result["ready_for_submission"])
        self.assertEqual(result["limitation_codes"], ["offline_quality"])

    def test_strict_profile_still_requires_actual_external_check(self):
        self.case.prepare()
        report = quality_run(self.run)
        digest = hashes(self.run)
        write_json(self.run / "reports/editorial-review.json", {"editor_id": "fixture-editor", "decisions": [],
            "manuscript_sha256": digest["manuscript_sha256"], "quality_report_sha256": digest["quality_report_sha256"]})
        self.assertFalse(report["required_complete"])
        with self.assertRaises(ContractError):
            engine.complete(self.run, "05_quality", "fixture-editor")

    def test_self_audit_cannot_claim_independence_or_hide_rejection(self):
        self.prepare_offline()
        self.finish_review()
        path = self.run / "reports/domain-review.json"
        report = read_json(path)
        report.update(independent_of_author=True, decision="reject")
        write_json(path, report)
        errors = engine.validate_stage(self.run, "06_review")
        self.assertTrue(any("самопровер" in value.lower() for value in errors))
        self.assertTrue(any("reject" in value for value in errors))

    def test_switching_to_strict_invalidates_previous_stage_completion(self):
        self.prepare_offline()
        self.finish_review()
        brief = read_json(self.run / "brief.json")
        brief["execution_profile"] = "strict_local"
        write_json(self.run / "brief.json", brief)
        self.assertTrue(all(item["status"] == "stale" for item in engine.status(self.run)))

    def test_new_brief_has_no_unrequested_acronym_assumption(self):
        self.assertEqual(read_json(self.run / "brief.json")["assumptions"], [])

    def test_repeated_release_does_not_reuse_stale_formats_or_reports(self):
        self.prepare_offline()
        self.finish_review()
        first = engine.finalize(self.run, formats=("tex",))
        self.assertIn("article.tex", first["files"])
        write_text(self.run / "release/obsolete-report.txt", "Old generated fixture report")
        second = engine.finalize(self.run)
        self.assertNotIn("article.tex", second["files"])
        self.assertFalse((self.run / "release/article.tex").exists())
        self.assertFalse((self.run / "release/obsolete-report.txt").exists())
        self.assertTrue(list((self.run / ".workflow/release-history").glob("*/article.tex")))

    def test_failed_format_export_preserves_previous_release_and_cleans_build(self):
        self.prepare_offline()
        self.finish_review()
        engine.finalize(self.run)
        previous = (self.run / "release/release-manifest.json").read_bytes()
        with patch("sci_writer.publishing.export_formats", side_effect=ContractError("intentional fixture exporter failure")):
            with self.assertRaises(ContractError):
                engine.finalize(self.run, formats=("docx",))
        self.assertEqual(previous, (self.run / "release/release-manifest.json").read_bytes())
        self.assertFalse(list((self.run / ".workflow").glob("release-build-*")))

    def test_erased_result_marker_does_not_leave_space_or_empty_bibliography(self):
        self.case.prepare()
        write_text(self.run / "manuscript.md", "# Искусственный пример\n\nЗначение в программном тесте — 4,2 м/с [EXP:R001].\n")
        rendered, sources, mapping = engine._render_manuscript(self.run)
        self.assertIn("4,2 м/с.", rendered)
        self.assertNotIn("## Литература", rendered)
        self.assertFalse(sources)
        self.assertEqual(mapping["[EXP:R001]"]["result_id"], "R001")


class ContractFingerprint(unittest.TestCase):
    def test_editorial_changes_preserve_policy_but_contract_change_invalidates(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for directory in ("schemas", "config", "agents", "skills/example", "src/sci_writer"):
                (root / directory).mkdir(parents=True)
            write_json(root / "config/workflow.json", {"version": "2.0.0", "contract_version": "2.0", "stages": []})
            write_json(root / "schemas/test.json", {"type": "object"})
            write_text(root / "AGENTS.md", "Policy wording")
            write_text(root / "skills/example/SKILL.md", "Prompt wording")
            with patch.object(engine, "ROOT", root):
                first = engine.policy_hash()
                provenance = engine.provenance_hashes()
                write_text(root / "AGENTS.md", "Policy wording, typo corrected")
                write_text(root / "skills/example/SKILL.md", "Prompt wording, clearer prose")
                self.assertEqual(first, engine.policy_hash())
                self.assertNotEqual(provenance["instructions_sha256"], engine.provenance_hashes()["instructions_sha256"])
                write_json(root / "config/workflow.json", {"contract_version": "2.1"})
                self.assertNotEqual(first, engine.policy_hash())


if __name__ == "__main__":
    unittest.main()
