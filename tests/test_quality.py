"""Meaningful stage-5 checks using stdlib unittest and a loopback fake server.

The HTTP server is a protocol fixture, not a real LanguageTool execution.
"""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs

from sci_writer.quality import (
    _chunks, analyze_text, check_languagetool, import_glavred_report,
    manuscript_sha256, mask_nonprose, profile_style, run_quality, sentence_spans,
    verify_raw_artifacts,
)


@contextmanager
def fake_server(callback):
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
            requests.append(payload)
            status, body, headers = callback(payload, len(requests))
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body if isinstance(body, bytes) else json.dumps(body).encode())
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v2/check", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def response(matches=(), **extra):
    return {"matches": list(matches), "software": {"name": "LanguageTool", "version": "fixture-6.8"}, "language": {"code": "ru"}, **extra}


class EditorialChecks(unittest.TestCase):
    def test_filters_preserve_offsets_and_do_not_edit_scientific_terms(self):
        text = '# Метод\nНа сегодняшний день измерен градиент [SRC:C001].\n\n«уникальный TODO».\n\n```\nлучший TODO\n```\n\n$уникальный$\n\nЛучший линейный несмещённый оцениватель применён к ряду.\n\n## Литература\nВпервые. TODO.\n'
        mask = mask_nonprose(text)
        self.assertEqual(len(mask), len(text))
        self.assertNotIn("SRC:C001", mask)
        self.assertEqual([i for i, c in enumerate(mask) if c == "\n"], [i for i, c in enumerate(text) if c == "\n"])
        findings = analyze_text(text, protected_terms=["Лучший линейный несмещённый оцениватель"])["findings"]
        self.assertEqual([f["rule"] for f in findings], ["filler_opening"])
        self.assertEqual(findings[0]["line"], 2)
        self.assertEqual(text[findings[0]["offset"]:][:findings[0]["length"]], "На сегодняшний день")

    def test_decimals_initials_and_common_abbreviations_are_not_sentences(self):
        text = 'А. С. Иванов показал результат 0.25, т. е. оценку параметра (рис. 2). Следующий опыт дал иной результат.'
        spans = sentence_spans(text)
        self.assertEqual(len(spans), 2)
        self.assertTrue(text[slice(*spans[1])].startswith("Следующий"))

    def test_placeholders_are_blocking_but_hedging_passive_and_dashes_are_not(self):
        scientific = 'Значение было оценено — вероятно, различие обусловлено выборкой. Возможно применение метода при указанных условиях.'
        self.assertEqual(analyze_text(scientific)["findings"], [])
        self.assertEqual(analyze_text(scientific + ' [НУЖЕН ИСТОЧНИК]')["disposition"], "fail")

    def test_repeated_openings_are_advisory(self):
        text = '\n\n'.join('В данной работе ' + tail for tail in (
            'изучен первый тип взаимодействия атмосферных процессов.',
            'проверен другой механизм взаимодействия выделенных масштабов.',
            'выполнен отдельный анализ чувствительности полученных оценок.',
        ))
        found = [f for f in analyze_text(text)["findings"] if f["rule"] == "repeated_opening"]
        self.assertEqual(len(found), 3)
        self.assertTrue(all(f["severity"] == "warning" for f in found))

    def test_author_profile_does_not_invent_a_baseline(self):
        empty = profile_style([])
        self.assertEqual(empty["status"], "not_supplied")
        self.assertIsNone(empty["sentence_words"]["median"])
        self.assertEqual(profile_style([], mode="neutral_requested")["status"], "neutral_requested")
        supplied = profile_style(["Метод проверен. Получена устойчивая оценка среднего значения."])
        self.assertEqual(supplied["status"], "provisional")
        self.assertEqual(supplied["sentence_words"]["count"], 2)
        self.assertLess(supplied["sentence_words"]["p10"], supplied["sentence_words"]["p90"])


class LanguageToolProtocol(unittest.TestCase):
    def test_no_url_remote_url_and_public_api_do_not_send_requests(self):
        with patch("sci_writer.quality.build_opener", side_effect=AssertionError("network must not run")):
            self.assertEqual(check_languagetool("Проверка текста.", url=None)["execution_status"], "not_run")
            self.assertEqual(check_languagetool("Проверка текста.", url="https://example.org/v2/check")["execution_status"], "not_run")
            self.assertEqual(check_languagetool("Проверка текста.", url="https://api.languagetool.org/v2/check", allow_remote=True)["execution_status"], "failed")

    def test_utf16_offsets_map_to_original_line_and_column(self):
        text = 'Пример 😀 ошибки.\nВторое предложение.'
        offset = len(text[:text.index("ошибки")].encode("utf-16-le")) // 2
        match = {"offset": offset, "length": 6, "message": "Проверьте слово.", "rule": {"id": "TEST", "issueType": "grammar"}}
        with fake_server(lambda p, n: (200, response([match]), {})) as (url, requests):
            report = check_languagetool(text, url=url)
        self.assertEqual(report["execution_status"], "completed")
        found = report["findings"][0]
        self.assertEqual(found["excerpt"], "ошибки")
        self.assertEqual(found["offset"], text.index("ошибки"))
        self.assertEqual(found["column"], text.index("ошибки") + 1)
        self.assertEqual(requests[0]["language"], ["ru"])

    def test_chunks_respect_utf16_budget_and_cover_the_whole_text(self):
        text = ('Русский текст 😀 содержит измерения 0.25 и оценки.\n' * 7)
        chunks = _chunks(text, 50)
        self.assertEqual(''.join(chunk for _, chunk in chunks), text)
        self.assertTrue(all(len(chunk.encode("utf-16-le")) // 2 <= 50 for _, chunk in chunks))
        with fake_server(lambda p, n: (200, response(), {})) as (url, requests):
            report = check_languagetool(text, url=url, max_chunk_chars=50)
        self.assertEqual(report["execution_status"], "completed")
        self.assertEqual(report["completed_chunks"], len(requests))
        self.assertGreater(len(requests), 1)

    def test_partial_failure_cannot_look_like_pass(self):
        def callback(payload, n):
            return (200, response(), {}) if n == 1 else (503, b"unavailable", {})
        with fake_server(callback) as (url, _):
            report = check_languagetool('Предложение содержит измеренные значения и другие слова. ' * 8, url=url, max_chunk_chars=60)
        self.assertEqual(report["execution_status"], "unavailable")
        self.assertEqual(report["completed_chunks"], 1)
        self.assertEqual(report["disposition"], "not_assessed")

    def test_malformed_json_incomplete_results_and_bad_offsets_fail(self):
        cases = [b"not json", {}, response(warnings={"incompleteResults": True}), response([{"offset": 9999, "length": 1, "message": "x", "rule": {"id": "BAD"}}])]
        for body in cases:
            with self.subTest(body=body):
                with fake_server(lambda p, n: (200, body, {})) as (url, _):
                    report = check_languagetool("Пример текста.", url=url)
                self.assertEqual(report["execution_status"], "failed")
                self.assertEqual(report["disposition"], "not_assessed")

    def test_redirect_is_not_followed(self):
        with fake_server(lambda p, n: (302, b"", {"Location": "https://example.org/v2/check"})) as (url, requests):
            report = check_languagetool("Пример текста.", url=url)
        self.assertEqual(report["execution_status"], "failed")
        self.assertEqual(len(requests), 1)

    def test_protected_spelling_only_does_not_hide_grammar(self):
        text = "Квазигеострофичность связана с масштабом движения."
        word = text.split()[0]
        base = {"offset": 0, "length": len(word), "message": "Проверьте термин."}
        matches = [{**base, "rule": {"id": "SPELL", "issueType": "misspelling"}}, {**base, "rule": {"id": "GRAMMAR", "issueType": "grammar"}}]
        with fake_server(lambda p, n: (200, response(matches), {})) as (url, _):
            report = check_languagetool(text, url=url, protected_terms=[word])
        self.assertEqual([f["rule"] for f in report["findings"]], ["GRAMMAR"])


class Reports(unittest.TestCase):
    def test_missing_required_service_and_crlf_hash_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "paper.md"
            original = "# Метод\r\n\r\nПолучена оценка температуры.\r\n".encode()
            source.write_bytes(original)
            report = run_quality(source, output_dir=root / "reports")
            self.assertFalse(report["required_complete"])
            self.assertIn("languagetool", report["missing_required_tools"])
            self.assertEqual(report["manuscript_sha256"], hashlib.sha256(original).hexdigest())
            self.assertTrue((root / "reports/quality.json").exists())
            for item in report["raw_artifacts"]:
                raw = (root / "reports" / item["path"]).read_bytes()
                self.assertEqual(hashlib.sha256(raw).hexdigest(), item["sha256"])

    def test_attested_import_rejects_stale_or_claimed_only_reports(self):
        text = "Исследован временной ряд температуры."
        valid = {"tool": "glavred", "version": "not_reported", "manuscript_sha256": manuscript_sha256(text), "status": "completed", "checked_at": "2026-11-21T12:00:00+03:00", "attestation": {"actual_run": True, "performed_by": "fixture operator", "method": "manual_ui", "statement": "Protocol fixture only; this does not claim an actual external execution."}, "raw_report": {"score": 8, "notes": ["Fixture, not a real Glavred run."]}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "external.json"
            path.write_text(json.dumps(valid), encoding="utf-8")
            self.assertEqual(import_glavred_report(path, text)["execution_status"], "completed")
            self.assertEqual(import_glavred_report(path, text + " Другой текст.")["execution_status"], "failed")
            del valid["attestation"]
            path.write_text(json.dumps(valid), encoding="utf-8")
            self.assertEqual(import_glavred_report(path, text)["execution_status"], "failed")


class RawReportIntegrity(unittest.TestCase):
    @staticmethod
    def change_raw(report, output_dir, tool, change):
        item = next(item for item in report["raw_artifacts"] if item["tool"] == tool)
        path = output_dir / item["path"]
        raw = json.loads(path.read_text(encoding="utf-8"))
        change(raw)
        payload = (json.dumps(raw, ensure_ascii=False, indent=2) + "\n").encode()
        path.write_bytes(payload)
        item["sha256"] = hashlib.sha256(payload).hexdigest()
        report["tools"][tool]["raw_report_sha256"] = item["sha256"]

    def test_not_run_receipt_cannot_be_completed_by_editing_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            text = "Измерена температура атмосферы на выбранной высоте."
            path = root / "paper.md"
            path.write_text(text, encoding="utf-8")
            report = run_quality(path, output_dir=root / "reports")
            self.assertEqual(verify_raw_artifacts(report, root / "reports", text), [])
            report["tools"]["languagetool"].update(status="completed", execution_status="completed")
            self.assertTrue(verify_raw_artifacts(report, root / "reports", text))

    def test_completed_provider_receipt_covers_exact_sanitized_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            text = ("Температура 😀 измерена на высоте. [SRC:C001] «цитата».\n" * 4)
            path = root / "paper.md"
            path.write_text(text, encoding="utf-8")
            with fake_server(lambda p, n: (200, response(), {})) as (url, _):
                report = run_quality(path, output_dir=root / "reports", languagetool_url=url, max_chunk_chars=50)
            self.assertEqual(verify_raw_artifacts(report, root / "reports", text), [])
            self.change_raw(report, root / "reports", "languagetool", lambda raw: raw["chunks"][0].update(checked_text_sha256="0" * 64))
            self.assertTrue(verify_raw_artifacts(report, root / "reports", text))

    def test_deleting_local_findings_from_all_reports_does_not_hide_rule(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            text = "На сегодняшний день выполнена оценка временного ряда."
            path = root / "paper.md"
            path.write_text(text, encoding="utf-8")
            report = run_quality(path, output_dir=root / "reports")
            self.assertTrue(report["findings"])
            self.change_raw(report, root / "reports", "ru_science_lint", lambda raw: raw.update(findings=[], disposition="pass"))
            report["tools"]["ru_science_lint"].update(findings=[], disposition="pass")
            report["findings"] = []
            self.assertTrue(verify_raw_artifacts(report, root / "reports", text))

    def test_global_findings_must_match_engine_findings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            text = "На сегодняшний день выполнена оценка временного ряда."
            path = root / "paper.md"
            path.write_text(text, encoding="utf-8")
            report = run_quality(path, output_dir=root / "reports")
            report["findings"] = []
            self.assertTrue(verify_raw_artifacts(report, root / "reports", text))


if __name__ == "__main__":
    unittest.main()
