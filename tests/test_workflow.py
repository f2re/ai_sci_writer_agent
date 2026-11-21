"""Contract fixtures exercise orchestration; none of their claims are research evidence."""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from sci_writer.cli import create_style_profile, hashes, quality_run
from sci_writer.contracts import (
    REVIEW_DIMENSIONS, ContractError, now, read_json, sha256,
    validate_evidence, validate_sources, write_json, write_text,
)
from sci_writer.engine import complete, finalize, init_run, status, validate, validate_stage


class LanguageFixture(BaseHTTPRequestHandler):
    """A test HTTP fixture, explicitly not the LanguageTool implementation."""

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        body = json.dumps({"software": {"name": "test-fixture", "version": "fixture-1"}, "language": {"code": "ru-RU"}, "warnings": {"incompleteResults": False}, "matches": []}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class WorkflowContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), LanguageFixture)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.run = Path(self.temporary.name) / "test-run"
        init_run(self.run, "Демонстрация программных контрактов, не научное исследование")

    def tearDown(self):
        self.temporary.cleanup()

    def prepare(self):
        run = self.run
        brief = read_json(run / "brief.json")
        brief["research_questions"] = ["Сохраняет ли тестовый конвейер договорённость о структуре артефактов?"]
        brief["author_style"]["samples_requested"] = True
        write_json(run / "brief.json", brief)
        create_style_profile(run)
        complete(run, "01_intake", "intake-test")
        source = {
            "id": "S001", "title": "Фиктивный источник исключительно для программного теста",
            "authors": ["Test Fixture"], "year": 2000, "doi": None,
            "url": "https://example.org/test-fixture", "type": "book", "access": "full_text",
            "metadata_verified": True, "status": "included", "tradition": "russian",
            "is_foundational": True, "verification_url": "https://example.org/test-fixture",
            "verified_at": now(),
        }
        write_text(run / "sources.jsonl", json.dumps(source, ensure_ascii=False) + "\n")
        write_text(run / "search-log.jsonl", json.dumps({"database": "test-fixture", "query": "synthetic contract example", "searched_at": now()}) + "\n")
        for name in ("school-map.md", "literature-review.md", "research-plan.md", "analysis/analysis-plan.md", "outline.md", "revision-log.md"):
            write_text(run / name, "# Программный тест\n\nЭтот документ является тестовым артефактом для проверки переходов состояния. Научное исследование не выполнялось.\n")
        write_json(run / "glossary.json", {"entries": [{"id": "T001", "preferred": "контракт", "definition": "Условная программная договорённость для теста.", "source_ids": ["S001"], "locators": {"S001": "fixture page 1"}, "allowed_variants": [], "english_equivalents": [], "relations": []}], "phrasebook": []})
        complete(run, "02_research", "research-test")
        claim = {"id": "C001", "kind": "source", "statement": "Тестовая запись иллюстрирует проверку связи источника и утверждения.", "verification": "verified", "source_ids": ["S001"], "locators": {"S001": "fixture page 1"}, "scope": "Только программный тест, не научное утверждение", "support_span": "full_text"}
        write_text(run / "evidence.jsonl", json.dumps(claim, ensure_ascii=False) + "\n")
        write_json(run / "analysis/results.json", {"results": [], "not_applicable_reason": "В обзорном тесте собственные измерения не выполняются."})
        digest = hashes(run)
        write_json(run / "reports/source-audit.json", {"status": "completed", "sources_sha256": digest["sources_sha256"], "evidence_sha256": digest["evidence_sha256"], "unverified_claim_ids": [], "unlinked_assertions": [], "domestic_foundation_assessment": {"status": "found", "reason": "В программном тесте используется явно фиктивный источник для проверки структуры."}})
        complete(run, "03_design", "analysis-test")
        write_text(run / "manuscript.md", "# Пример программного теста\n\nТестовая запись описывает проверку связи между утверждением и его источником [SRC:C001]. Условия этой проверки ограничены искусственным программным примером. Содержательные научные выводы из него не следуют.\n")
        complete(run, "04_draft", "writer-session-01")

    def check_quality(self):
        run = self.run
        report = quality_run(run, url=self.url)
        digest = hashes(run)
        write_json(run / "reports/editorial-review.json", {
            "manuscript_sha256": digest["manuscript_sha256"], "quality_report_sha256": digest["quality_report_sha256"], "editor_id": "editor-test",
            "decisions": [{"finding_id": f["id"], "disposition": "accepted", "reason": "Это намеренная формулировка в программном тесте; реальный текст оценивался бы отдельно."} for f in report["findings"]],
        })
        complete(run, "05_quality", "quality-test")

    def reviews(self):
        run = self.run
        digest = hashes(run)
        for kind in ("domain", "methods"):
            write_json(run / f"reports/{kind}-review.json", {
                "manuscript_sha256": digest["manuscript_sha256"], "review_context_sha256": digest["review_context_sha256"],
                "reviewer_id": f"{kind}-test", "reviewer_role": f"{kind}-reviewer",
                "independent_of_author": True, "decision": "accept", "issues": [],
            })
        complete(run, "06_review", "review-coordinator-test")
        digest = hashes(run)
        scores = {key: {"score": None if key in ("author_style", "journal_fit") else 3, "applicable": key not in ("author_style", "journal_fit"), "reason": "Условная оценка программного теста; научная экспертиза не выполнялась.", "locations": ["fixture"]} for key in REVIEW_DIMENSIONS}
        write_json(run / "reports/final-review.json", {
            "manuscript_sha256": digest["manuscript_sha256"], "review_context_sha256": digest["chief_context_sha256"],
            "reviewer_id": "chief-test", "reviewer_role": "chief-reviewer", "independent_of_author": True,
            "decision": "accept", "issues": [], "scientific_ready": True, "ready_for_submission": False,
            "scores": scores, "limitations": ["Only tests: no real peer review, no real sources."],
        })
        write_text(run / "reports/final-review.md", "# Тестовый итог\n\nПроверены только переходы программного конвейера; научная статья и реальная рецензия не создавались.\n")
        complete(run, "07_release", "chief-test")

    def test_full_workflow_exports_traceable_packet_and_raw_reports(self):
        self.prepare()
        self.check_quality()
        self.reviews()
        self.assertTrue(validate(self.run)["ok"])
        result = finalize(self.run)
        self.assertFalse(result["ready_for_submission"])
        article = (self.run / "release/article.md").read_text()
        self.assertIn("[1]", article)
        self.assertNotIn("[SRC:", article)
        self.assertIn("## Литература", article)
        for item in read_json(self.run / "release/reports/quality.json")["raw_artifacts"]:
            self.assertEqual(sha256(self.run / "release/reports" / item["path"]), item["sha256"])

    def test_revised_manuscript_invalidates_all_downstream_stages(self):
        self.prepare()
        self.check_quality()
        self.reviews()
        with (self.run / "manuscript.md").open("a") as handle:
            handle.write("\nДополнительное условие меняет проверяемую версию текста.\n")
        current = {item["id"]: item["status"] for item in status(self.run)}
        self.assertEqual(current["03_design"], "complete")
        self.assertEqual(current["04_draft"], "stale")
        self.assertEqual(current["07_release"], "stale")
        with self.assertRaises(ContractError):
            finalize(self.run)
        complete(self.run, "04_draft", "writer-session-01")
        self.assertTrue(validate_stage(self.run, "05_quality"))

    def test_source_change_invalidates_review_even_when_manuscript_is_identical(self):
        self.prepare()
        self.check_quality()
        self.reviews()
        original_text_hash = hashes(self.run)["manuscript_sha256"]
        with (self.run / "sources.jsonl").open("r+", encoding="utf-8") as handle:
            row = json.loads(handle.read())
            row["year"] = 2001
            handle.seek(0)
            handle.write(json.dumps(row) + "\n")
            handle.truncate()
        self.assertEqual(original_text_hash, hashes(self.run)["manuscript_sha256"])
        self.assertNotEqual(hashes(self.run)["review_context_sha256"], read_json(self.run / "reports/domain-review.json")["review_context_sha256"])
        self.assertEqual(status(self.run)[1]["status"], "stale")

    def test_metadata_only_cannot_support_a_verified_claim(self):
        self.prepare()
        sources, errors = validate_sources(self.run)
        self.assertFalse(errors)
        sources["S001"]["access"] = "metadata_only"
        _, errors = validate_evidence(self.run, sources)
        self.assertTrue(any("метаданные" in error for error in errors))

    def test_writer_cannot_approve_as_independent_reviewer(self):
        self.prepare()
        self.check_quality()
        self.reviews()
        report = read_json(self.run / "reports/domain-review.json")
        report["reviewer_id"] = "  WRITER-SESSION-01  "
        write_json(self.run / "reports/domain-review.json", report)
        self.assertTrue(any("независимый" in error for error in validate_stage(self.run, "06_review")))

    def test_unregistered_actual_writer_is_rejected(self):
        self.prepare()
        with self.assertRaisesRegex(ContractError, "зарегистрирован"):
            complete(self.run, "04_draft", "unregistered-session")

    def test_final_cannot_claim_author_similarity_without_samples(self):
        self.prepare()
        self.check_quality()
        self.reviews()
        report = read_json(self.run / "reports/final-review.json")
        report["scores"]["author_style"].update({"score": 4, "applicable": True})
        write_json(self.run / "reports/final-review.json", report)
        self.assertTrue(any("Без образцов" in error for error in validate_stage(self.run, "07_release")))

    def test_new_run_cannot_skip_directly_to_release(self):
        with self.assertRaises(ContractError):
            complete(self.run, "07_release", "chief-test")
        with self.assertRaises(ContractError):
            finalize(self.run)

    def test_inference_can_reference_own_result_without_a_fictitious_source_claim(self):
        self.prepare()
        row = {"id": "C002", "kind": "inference", "statement": "Вывод из собственного тестового вычисления.", "verification": "verified", "source_ids": [], "locators": {}, "scope": "Тестовая интерпретация программного результата.", "result_ids": ["R001"]}
        with (self.run / "evidence.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        # This tests the evidence reference edge only, not experiment acceptance.
        write_json(self.run / "analysis/results.json", {"results": [{"id": "R001"}]})
        sources, _ = validate_sources(self.run)
        _, errors = validate_evidence(self.run, sources)
        self.assertFalse(errors)
        row["result_ids"] = ["R999"]
        write_text(self.run / "evidence.jsonl", json.dumps(row) + "\n")
        _, errors = validate_evidence(self.run, sources)
        self.assertTrue(any("R999" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
