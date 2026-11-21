"""Regression tests for advisory RU editing and honest sandbox receipts.

HTTP fixtures exercise only the protocol; they do not claim a real service run.
"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator, FormatChecker

from sci_writer.quality import (
    analyze_text, check_english_abstract, manuscript_sha256, run_quality,
    verify_raw_artifacts,
)
import test_quality as fixtures
from test_quality import fake_server


GLOSSARY = {"entries": [
    {"id": "T001", "preferred": "турбулентность", "allowed_variants": [],
     "relations": [{"type": "not_equivalent", "target_id": "T002"}]},
    {"id": "T002", "preferred": "диффузия", "allowed_variants": [], "relations": []},
], "phrasebook": []}


class RussianEditorialV02(unittest.TestCase):
    def test_new_cliches_are_advisory_and_protected_context_is_respected(self):
        text = "Результат проливает свет на механизм. Осуществление оценки выполнено заранее."
        findings = analyze_text(text)["findings"]
        self.assertEqual({f["rule"] for f in findings}, {"translated_cliche", "split_predicate"})
        self.assertTrue(all(f["severity"] == "warning" for f in findings))
        protected = '«проливает свет»; $осуществление оценки$. Было оценено — вероятно, эффект обусловлен выборкой.'
        self.assertEqual(analyze_text(protected)["findings"], [])
        term = "Проведение анализа"
        self.assertEqual(analyze_text(term + " определено в глоссарии.", protected_terms=[term])["findings"], [])

    def test_noun_chain_is_explicitly_heuristic_and_can_be_a_protected_term(self):
        text = "Использован показатель качества результатов моделирования атмосферы."
        findings = analyze_text(text)["findings"]
        candidate = next(f for f in findings if f["rule"] == "genitive_chain_candidate")
        self.assertEqual(candidate["confidence"], "surface_heuristic")
        self.assertIn("Падежи не разбирались", candidate["message"])
        protected = analyze_text(text, protected_terms=[candidate["excerpt"]])
        self.assertNotIn("genitive_chain_candidate", [f["rule"] for f in protected["findings"]])
        self.assertEqual(analyze_text("Модель проверена. Результат зависит от выбранных условий.")["findings"], [])

    def test_typography_checks_do_not_ban_em_dashes_or_alter_formulas(self):
        text = 'Ошибка равна 2%, температура — 20°C. Метод - основной. Термин "бароклинность" определён ранее.'
        rules = [f["rule"] for f in analyze_text(text)["findings"]]
        self.assertEqual(rules.count("unit_spacing"), 2)
        self.assertIn("prose_dash", rules)
        self.assertIn("russian_quote_glyphs", rules)
        valid = 'Ошибка равна 2 %, температура — 20 °C. Угол равен 10°. Это — метод «оценки». $x=2м$; `5Гц`.'
        self.assertEqual(analyze_text(valid)["findings"], [])

    def test_not_equivalent_checks_explicit_equivalence_not_cooccurrence(self):
        for text in ("Турбулентность — это диффузия.", "Турбулентность и диффузия — одно и то же."):
            with self.subTest(text=text):
                findings = analyze_text(text, glossary=GLOSSARY)["findings"]
                self.assertEqual([f["rule"] for f in findings], ["not_equivalent_terms"])
                self.assertEqual(findings[0]["glossary_ids"], ["T001", "T002"])
        for text in (
            "Турбулентность и диффузия описывают разные явления.",
            "Неверно утверждать, что турбулентность — это диффузия.",
            "Турбулентность не эквивалентна диффузии.",
            "«Турбулентность — это диффузия» — ошибочное утверждение.",
        ):
            with self.subTest(text=text):
                self.assertNotIn("not_equivalent_terms", [f["rule"] for f in analyze_text(text, glossary=GLOSSARY)["findings"]])

    def test_english_scope_offsets_and_absence_are_truthful(self):
        self.assertEqual(check_english_abstract({}, "Рукопись.")["execution_status"], "not_supplied")
        english = "We measured the the temperature. Значение is provisional. TODO"
        report = check_english_abstract({"abstract": {"en": english}}, "Рукопись.")
        self.assertEqual(report["disposition"], "fail")
        self.assertEqual({f["rule"] for f in report["findings"]}, {"english_repeated_word", "cyrillic_in_english_abstract", "english_placeholder"})
        self.assertIn("no complete English grammar", report["scope"])
        for finding in report["findings"]:
            self.assertEqual(finding["offset_basis"], "publication.abstract.en")
            self.assertEqual(english[finding["offset"]:][:finding["length"]], finding["excerpt"])
        with self.assertRaises(ValueError):
            check_english_abstract({"abstract": {"en": ["not a string"]}}, "Рукопись.")


class SandboxReceipts(unittest.TestCase):
    def run_local(self, directory, **kwargs):
        root = Path(directory)
        text = "Температура измерена на выбранной высоте в течение суток."
        path = root / "paper.md"
        path.write_text(text, encoding="utf-8")
        report = run_quality(path, output_dir=root / "reports", **kwargs)
        return root, text, report

    def test_offline_receipt_never_claims_external_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("sci_writer.quality.build_opener", side_effect=AssertionError("offline must not call network")):
                root, text, report = self.run_local(directory, execution_profile="offline_sandbox")
            self.assertEqual(report["execution_status"], "partial")
            self.assertFalse(report["required_complete"])
            self.assertTrue(report["draft_quality_complete"])
            self.assertEqual(report["missing_required_tools"], ["languagetool"])
            for name in ("languagetool", "glavred"):
                self.assertEqual(report["tools"][name]["status"], "skipped_no_runtime")
                self.assertFalse(report["tools"][name]["request_attempted"])
                self.assertEqual(report["tools"][name]["disposition"], "not_assessed")
                self.assertTrue(report["tools"][name]["raw_report_sha256"])
            self.assertEqual(verify_raw_artifacts(report, root / "reports", text, execution_profile="offline_sandbox"), [])

    def test_strict_default_still_blocks_missing_service(self):
        with tempfile.TemporaryDirectory() as directory:
            root, text, report = self.run_local(directory)
            self.assertEqual(report["execution_profile"], "strict_local")
            self.assertEqual(report["tools"]["languagetool"]["status"], "not_run")
            self.assertFalse(report["draft_quality_complete"])
            self.assertEqual(verify_raw_artifacts(report, root / "reports", text), [])

    def test_attempted_service_failure_is_not_converted_to_skip(self):
        with tempfile.TemporaryDirectory() as directory:
            with fake_server(lambda payload, n: (503, b"unavailable", {})) as (url, requests):
                root, text, report = self.run_local(directory, execution_profile="offline_sandbox", languagetool_url=url)
            self.assertEqual(len(requests), 1)
            self.assertEqual(report["tools"]["languagetool"]["status"], "unavailable")
            self.assertFalse(report["draft_quality_complete"])
            self.assertEqual(verify_raw_artifacts(report, root / "reports", text), [])

    def test_skipped_summary_cannot_be_relabelled_completed(self):
        with tempfile.TemporaryDirectory() as directory:
            root, text, report = self.run_local(directory, execution_profile="offline_sandbox")
            report["tools"]["languagetool"].update(status="completed", execution_status="completed")
            report.update(required_complete=True, missing_required_tools=[])
            self.assertTrue(verify_raw_artifacts(report, root / "reports", text))
            fixtures.RawReportIntegrity.change_raw(report, root / "reports", "languagetool", lambda raw: raw.update(execution_status="completed"))
            self.assertTrue(verify_raw_artifacts(report, root / "reports", text))

    def test_skip_requires_an_untampered_receipt_and_current_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            root, text, report = self.run_local(directory, execution_profile="offline_sandbox")
            self.assertTrue(verify_raw_artifacts(report, root / "reports", text, execution_profile="strict_local"))
            fixtures.RawReportIntegrity.change_raw(report, root / "reports", "languagetool", lambda raw: raw.update(request_attempted=True))
            self.assertTrue(verify_raw_artifacts(report, root / "reports", text))

    def test_live_glossary_and_english_abstract_are_bound_and_recomputed(self):
        publication = {"abstract": {"en": "We measured the the temperature."}}
        with tempfile.TemporaryDirectory() as directory:
            root, text, report = self.run_local(directory, execution_profile="offline_sandbox", glossary=GLOSSARY, publication=publication)
            self.assertEqual(verify_raw_artifacts(report, root / "reports", text, glossary=GLOSSARY, publication=publication), [])
            self.assertTrue(verify_raw_artifacts(report, root / "reports", text, glossary={}, publication=publication))
            changed = {"abstract": {"en": "Different text."}}
            self.assertTrue(verify_raw_artifacts(report, root / "reports", text, publication=changed))
            fixtures.RawReportIntegrity.change_raw(report, root / "reports", "en_abstract_lint", lambda raw: raw.update(findings=[], disposition="pass"))
            report["tools"]["en_abstract_lint"].update(findings=[], disposition="pass")
            report["findings"] = [f for f in report["findings"] if f["tool"] != "en_abstract_lint"]
            self.assertTrue(verify_raw_artifacts(report, root / "reports", text))

    def test_external_schema_distinguishes_completion_from_nonexecution(self):
        path = Path(__file__).resolve().parents[1] / "schemas/external-check.schema.json"
        validator = Draft202012Validator(json.loads(path.read_text()), format_checker=FormatChecker())
        skipped = {
            "tool": "languagetool", "status": "skipped_no_runtime", "checked_at": "2026-11-21T12:00:00+03:00",
            "manuscript_sha256": manuscript_sha256("text"), "execution_profile": "offline_sandbox",
            "reason": "No endpoint supplied in explicitly selected offline profile.",
            "skip_reason": "no_endpoint_configured", "request_attempted": False,
        }
        self.assertEqual(list(validator.iter_errors(skipped)), [])
        for change in ({"status": "completed"}, {"execution_profile": "strict_local"}, {"raw_report": {"score": 10}}):
            with self.subTest(change=change):
                self.assertTrue(list(validator.iter_errors({**skipped, **change})))

    def test_raw_output_symlink_cannot_escape_reports_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "reports").mkdir()
            (root / "outside").mkdir()
            (root / "reports/raw").symlink_to(root / "outside", target_is_directory=True)
            with self.assertRaises(ValueError):
                self.run_local(directory, execution_profile="offline_sandbox")
            self.assertEqual(list((root / "outside").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
