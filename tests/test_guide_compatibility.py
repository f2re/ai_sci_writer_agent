"""Guide compatibility over artificial complete workflows, never real research."""
from __future__ import annotations

import contextlib
import copy
import io
import json
import unittest
from unittest.mock import patch

import test_workflow as fixtures
from sci_writer.cli import hashes, main, quality_run
from sci_writer.contracts import (
    ContractError, now, publication_metadata, quality_context, read_json, schema_errors,
    write_json,
)
from sci_writer.engine import complete, export_draft, finalize, status, validate_stage


class GuideCompatibility(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.WorkflowContracts.setUpClass()

    @classmethod
    def tearDownClass(cls):
        fixtures.WorkflowContracts.tearDownClass()

    def setUp(self):
        self.case = fixtures.WorkflowContracts()
        self.case.setUp()
        self.run = self.case.run

    def tearDown(self):
        self.case.tearDown()

    def prepare(self):
        self.case.prepare()
        self.case.check_quality()
        self.case.reviews()

    def reviewed_input_bytes(self):
        names = ("brief.json", ".workflow/state.json", "reports/quality.json", "reports/editorial-review.json",
                 "reports/domain-review.json", "reports/methods-review.json", "reports/final-review.json")
        return {name: (self.run / name).read_bytes() for name in names}

    def test_alias_only_populates_header_english_check_and_hashes(self):
        brief = read_json(self.run / "brief.json")
        metadata = {"title": {"ru": "Издательский блок тестового примера", "en": "Fixture publication metadata"},
                    "abstract": {"en": "This this fixture contains no real scientific results."},
                    "udc": "001"}
        # The default empty canonical block must not hide a nonempty guide alias.
        self.assertEqual(brief["publication"], {})
        brief["publication_meta"] = metadata
        write_json(self.run / "brief.json", brief)
        self.prepare()
        quality = read_json(self.run / "reports/quality.json")
        self.assertEqual(quality["tools"]["en_abstract_lint"]["execution_status"], "completed")
        self.assertTrue(any(item["rule"] == "english_repeated_word" for item in quality["findings"]))
        self.assertEqual(quality["context_sha256"], quality_context(self.run))
        self.assertFalse(validate_stage(self.run, "05_quality"))
        before = self.reviewed_input_bytes()
        result = finalize(self.run)
        article = (self.run / "release/article.md").read_text()
        self.assertIn("# Издательский блок тестового примера", article)
        self.assertIn("**Abstract.** This this fixture", article)
        self.assertIn("УДК: 001", article)
        self.assertEqual(result["publication_metadata"], metadata)
        self.assertEqual(before, self.reviewed_input_bytes())
        # Changing only the alias still invalidates the raw brief context and reviews.
        previous = hashes(self.run)
        brief = read_json(self.run / "brief.json")
        brief["publication_meta"]["title"]["ru"] = "Поздняя непроверенная подмена заглавия"
        write_json(self.run / "brief.json", brief)
        self.assertNotEqual(previous["quality_context_sha256"], hashes(self.run)["quality_context_sha256"])
        self.assertNotEqual(previous["review_context_sha256"], hashes(self.run)["review_context_sha256"])
        self.assertTrue(all(item["status"] == "stale" for item in status(self.run)))
        with self.assertRaises(ContractError):
            finalize(self.run)
        self.assertEqual((self.run / "release/article.md").read_text(), article)

    def test_alias_resolution_allows_equal_objects_and_rejects_wrong_shape(self):
        metadata = {"title": {"ru": "Тестовое название"}}
        self.assertEqual(publication_metadata({"publication_meta": metadata}), metadata)
        self.assertEqual(publication_metadata({"publication": metadata, "publication_meta": {}}), metadata)
        self.assertEqual(publication_metadata({"publication": metadata, "publication_meta": copy.deepcopy(metadata)}), metadata)
        brief = read_json(self.run / "brief.json")
        brief.update(publication_meta=metadata, research_questions=["Проверяется совместимость полей издательского блока?"])
        self.assertFalse(schema_errors(brief, "brief"))
        for value in (None, [], "title", {"title": "wrong shape"}):
            with self.subTest(value=value), self.assertRaises(ContractError):
                publication_metadata({"publication_meta": value})

    def test_conflicting_aliases_fail_before_any_provider_or_export(self):
        brief = read_json(self.run / "brief.json")
        brief.update(publication={"title": {"ru": "Первое название"}},
                     publication_meta={"title": {"ru": "Другое название"}},
                     research_questions=["Проверяется конфликт двух полей издательского блока?"])
        write_json(self.run / "brief.json", brief)
        with patch("sci_writer.cli.run_quality") as provider:
            with self.assertRaisesRegex(ContractError, "конфликтуют"):
                quality_run(self.run, url="http://127.0.0.1:9999")
            provider.assert_not_called()
        self.assertFalse((self.run / "reports/quality.json").exists())
        self.assertTrue(any("конфликтуют" in error for error in validate_stage(self.run, "01_intake")))
        with patch("sci_writer.publishing.export_formats") as exporter:
            with self.assertRaisesRegex(ContractError, "конфликтуют"):
                finalize(self.run, formats=("docx",))
            with self.assertRaisesRegex(ContractError, "конфликтуют"):
                export_draft(self.run, formats=("docx",))
            exporter.assert_not_called()
        self.assertFalse((self.run / "release").exists())
        self.assertFalse((self.run / "draft-export").exists())

    def test_finalize_cli_styles_render_and_record_provenance_without_rewriting_inputs(self):
        self.prepare()
        before = self.reviewed_input_bytes()
        for flag, canonical, separator in (("generic", "numeric", " 2000. URL:"),
                                           ("gost", "gost-r-7.0.5-2008", " — 2000. — URL:")):
            with self.subTest(flag=flag):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = main(["finalize", str(self.run), "--bibliography-style", flag])
                self.assertEqual(code, 0)
                result = json.loads(output.getvalue())
                self.assertEqual(result["citation_format"], canonical)
                self.assertEqual(result["bibliography_style"], {
                    "requested": flag, "configured": "gost-r-7.0.5-2008", "effective": canonical,
                    "override_applied": flag == "generic", "reference_overrides_priority": "per_source_reviewed_overrides"})
                self.assertIn(separator, (self.run / "release/article.md").read_text())
                self.assertFalse(result["ready_for_submission"])
                self.assertEqual(before, self.reviewed_input_bytes())

    def test_default_style_uses_reviewed_alias_configuration_and_reference_override_wins(self):
        brief = read_json(self.run / "brief.json")
        override = "Редакторская запись S001 по проверенным правилам тестового издания."
        brief["publication_meta"] = {"citation_style": "numeric", "reference_overrides": {"S001": override}}
        write_json(self.run / "brief.json", brief)
        self.prepare()
        default = finalize(self.run)
        self.assertEqual(default["citation_format"], "numeric")
        self.assertIsNone(default["bibliography_style"]["requested"])
        result = finalize(self.run, bibliography_style="gost")
        self.assertEqual(result["citation_format"], "gost-r-7.0.5-2008")
        self.assertIn("1. " + override, (self.run / "release/article.md").read_text())
        self.assertTrue(result["bibliography_style"]["override_applied"])

    def test_submission_ready_release_rejects_different_style_but_allows_matching_style(self):
        # Explicitly artificial journal/peer-review fixture, not a real readiness claim.
        journal = read_json(self.run / "journal-profile.json")
        journal.update(status="verified", name="Fixture journal", format_checked=True,
                       checked_at=now(), sources=["https://example.org/artificial-journal-policy"],
                       ai_policy="Artificial policy fixture; no real journal was assessed.")
        write_json(self.run / "journal-profile.json", journal)
        self.prepare()
        final = read_json(self.run / "reports/final-review.json")
        final["ready_for_submission"] = True
        write_json(self.run / "reports/final-review.json", final)
        complete(self.run, "07_release", "chief-test")
        self.assertFalse(validate_stage(self.run, "07_release"))
        before = self.reviewed_input_bytes()
        result = finalize(self.run, bibliography_style="gost")
        self.assertTrue(result["ready_for_submission"])
        previous_release = (self.run / "release/release-manifest.json").read_bytes()
        with patch("sci_writer.publishing.export_formats") as exporter:
            with self.assertRaisesRegex(ContractError, "повторите зависимые проверки"):
                finalize(self.run, bibliography_style="generic", formats=("docx",))
            exporter.assert_not_called()
        self.assertEqual(before, self.reviewed_input_bytes())
        self.assertEqual(previous_release, (self.run / "release/release-manifest.json").read_bytes())
        self.assertFalse(list((self.run / ".workflow").glob("release-build-*")))

    def test_style_argument_never_bypasses_unfinished_workflow(self):
        for style in ("gost", "generic"):
            with self.subTest(style=style), self.assertRaises(ContractError):
                finalize(self.run, bibliography_style=style)
        self.assertFalse((self.run / "release").exists())


if __name__ == "__main__":
    unittest.main()
