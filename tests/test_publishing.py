"""Export tests are synthetic layout fixtures, never real publication metadata."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sci_writer.contracts import ContractError, schema_errors
from sci_writer.literature import parse_bibtex
from sci_writer.publishing import bibliography_bibtex, export_formats, format_reference, publication_errors, publication_header


class PublishingTests(unittest.TestCase):
    def test_bibliography_has_journal_volume_issue_pages_edn(self):
        source={'id':'S001','title':'Тестовое название','authors':['Тестов Т. Т.'],'year':2020,'type':'article',
                'journal':'Тестовый журнал','volume':'12','issue':'2','pages':'10–20','doi':'10.0000/test',
                'edn':'ABCDEF','url':'https://doi.org/10.0000/test'}
        ref=format_reference(source)
        for expected in ('Тестовый журнал','Т. 12','№ 2','С. 10–20','DOI: 10.0000/test','EDN: ABCDEF'):
            self.assertIn(expected,ref)
        self.assertNotIn('URL:',ref)
        bib=bibliography_bibtex([source])
        parsed=parse_bibtex(bib)[0]
        self.assertEqual(parsed['journal'],source['journal'])
        self.assertEqual(parsed['issue'],'2')
        self.assertEqual(parsed['edn'],'ABCDEF')

    def test_bibtex_protects_corporate_and_and_tex_specials(self):
        source={'id':'S001','type':'article','title':'Test 10% & $value',
                'authors':['Research and Analysis Group','Fixture, A.'],'year':2020,'url':'https://example.org'}
        parsed=parse_bibtex(bibliography_bibtex([source]))[0]
        self.assertEqual(parsed['authors'],source['authors'])
        self.assertEqual(parsed['title'],source['title'])

    def test_book_uses_imprint_edition_and_never_invents_doi(self):
        ref=format_reference({'title':'Тестовая монография','authors':['Фикстура'],'type':'book','year':1950,
            'city':'Ленинград','publisher':'Тестовый издатель','edition':'2-е изд.','page_count':320,'url':'https://example.org/book','accessed_at':'2026-11-21'})
        self.assertIn('Ленинград: Тестовый издатель, 1950',ref)
        self.assertIn('320 с.',ref)
        self.assertIn('21.11.2026',ref)
        self.assertNotIn('DOI',ref)
        self.assertNotIn('EDN',ref)

    def test_publication_header_is_bilingual_and_affiliations_checked(self):
        metadata={'title':{'ru':'Тестовое название','en':'Fixture title'},'abstract':{'ru':'Тестовый текст.','en':'Fixture abstract.'},
          'keywords':{'ru':['проверка'],'en':['fixture']},'authors':[{'name_ru':'Тестов Т. Т.','name_en':'Fixture T.','affiliation_ids':['1']}],
          'affiliations':[{'id':'1','name_ru':'Тестовая организация','name_en':'Fixture institution'}], 'udc':'001'}
        self.assertFalse(publication_errors(metadata))
        header=publication_header(metadata)
        self.assertIn('Аннотация',header);self.assertIn('Abstract',header)
        self.assertIn('Fixture institution',header)
        self.assertNotIn('EDN',header)
        metadata['authors'][0]['affiliation_ids']=['unknown']
        self.assertTrue(publication_errors(metadata))

    def test_required_journal_fields_are_explicit_not_universal(self):
        self.assertFalse(publication_errors({}))
        self.assertTrue(publication_errors({}, [None]))
        self.assertTrue(publication_errors({},['title.ru','authors']))
        self.assertFalse(publication_errors({'title':{'ru':'Тест'}},['title.ru']))

    def test_expanded_source_schema_accepts_fields_and_rejects_bad_shapes(self):
        source={'id':'S001','title':'Fixture title','authors':['Fixture'],'year':2020,'url':'https://example.org',
            'type':'article','access':'metadata_only','metadata_verified':False,'status':'candidate','tradition':'undetermined',
            'is_foundational':False,'journal':'Journal','volume':'2','pages':'3–4','edn':'ABCDEF'}
        self.assertFalse(schema_errors(source,'source'))
        source['journal']=[]
        self.assertTrue(schema_errors(source,'source'))

    def test_docx_contains_table_and_bilingual_text_without_fetching_images(self):
        try: from docx import Document
        except ImportError: self.skipTest('optional python-docx is not installed')
        with tempfile.TemporaryDirectory() as temporary:
            output=Path(temporary)
            result=export_formats('# Тестовая статья\n\n**Abstract.** Fixture.\n\n| Термин | Term |\n| --- | --- |\n| Ветер | Wind |\n\n![Схема](https://example.org/not-fetched.png)\n',output,['docx'])
            doc=Document(output/'article.docx')
            self.assertEqual(len(doc.tables),1)
            self.assertEqual(doc.tables[0].cell(1,1).text,'Wind')
            self.assertIn('Abstract.',' '.join(p.text for p in doc.paragraphs))
            self.assertTrue(any('images_not_loaded' in item for item in result['warnings']))
            self.assertFalse(result['layout_verified'])

    def test_tex_escapes_external_commands_and_reports_formula_scope(self):
        with tempfile.TemporaryDirectory() as temporary:
            result=export_formats('# Тест\n\nКоманда \\input{/etc/passwd} и $\\input{evil}$.\n',Path(temporary),['tex'])
            text=(Path(temporary)/'article.tex').read_text()
            self.assertNotIn('\\input{',text)
            self.assertIn('\\textbackslash{}input',text)
            self.assertTrue(result['warnings'])

    def test_safe_math_exports_as_latex_and_native_docx_when_pandoc_available(self):
        import shutil
        import zipfile
        with tempfile.TemporaryDirectory() as temporary:
            output=Path(temporary)
            result=export_formats(r"# Проверка" + "\n\n" + r"Формула $v=\frac{s}{t}$.",output,['tex'])
            self.assertFalse(result['warnings'])
            self.assertIn(r"\(v=\frac{s}{t}\)",(output/'article.tex').read_text())
            if shutil.which('pandoc'):
                try: import docx
                except ImportError: return
                result=export_formats(r"# Проверка" + "\n\n" + r"Формула $v=\frac{s}{t}$.",output,['docx'])
                self.assertFalse(result['warnings'])
                with zipfile.ZipFile(output/'article.docx') as archive:
                    self.assertIn(b'<m:oMath',archive.read('word/document.xml'))

    def test_tex_character_code_expansion_cannot_bypass_command_allowlist(self):
        with tempfile.TemporaryDirectory() as temporary:
            output=Path(temporary)
            result=export_formats(r"Формула $\^^69nput{/etc/passwd}$.",output,['tex'])
            text=(output/'article.tex').read_text()
            self.assertNotIn(r"\^^69nput",text)
            self.assertNotIn('^^',text)
            self.assertIn(r"\textbackslash{}\textasciicircum{}\textasciicircum{}69nput",text)
            self.assertTrue(any('tex_math_source_escaped' in item for item in result['warnings']))

    def test_export_rejects_unknown_format_and_symlink_escape(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);output=root/'out';output.mkdir()
            with self.assertRaises(ContractError): export_formats('text',output,['pdf'])
            (output/'article.tex').symlink_to(root/'outside.tex')
            with self.assertRaises(ContractError): export_formats('text',output,['tex'])
            self.assertFalse((root/'outside.tex').exists())


if __name__=='__main__':unittest.main()
