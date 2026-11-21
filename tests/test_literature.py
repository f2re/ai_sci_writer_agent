"""Metadata ingestion tests use declared fixtures, not real scholarly evidence."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sci_writer.contracts import ContractError, read_json, write_text
from sci_writer.literature import (
    import_bibliography, normalize_work, normalize_openalex_work,
    openalex_search, parse_bibtex, parse_ris, search_openalex_into_run,
)

BIB = r'''@string{journal = "Тестовый журнал"}
@article{fixture,
 title = {Тест {сложного} названия},
 author = {Иванов, И. И. and {Исследовательский центр and группа}},
 journal = journal, year = 2020, volume = {12}, number = {3},
 pages = {45--57}, doi = {10.0000/test-fixture}, edn = {ABCDEF}
}
@book{bookfixture, title={Демонстрационная монография}, author={Тестов, Т. Т.},
 year={2000}, address={Москва}, publisher={Тестовое издательство}, edition={2-е изд.}}
'''
RIS = '''TY  - JOUR
TI  - Демонстрационная статья
AU  - Тестов, Т. Т.
PY  - 2020/01/01
JO  - Тестовый журнал
VL  - 12
IS  - 3
SP  - 45
EP  - 57
DO  - 10.0000/test-fixture
AB  - Первая строка
  продолжение аннотации
ER  -
'''


class LiteratureTests(unittest.TestCase):
    def test_bibtex_nested_braces_strings_and_corporate_author(self):
        rows = parse_bibtex(BIB)
        self.assertEqual(len(rows), 2)
        row = rows[0]
        self.assertEqual(row['authors'], ['Иванов, И. И.', 'Исследовательский центр and группа'])
        self.assertEqual(row['journal'], 'Тестовый журнал')
        self.assertEqual(row['year'], 2020)
        self.assertEqual(row['pages'], '45--57')
        self.assertEqual(row['edn'], 'ABCDEF')
        self.assertEqual(rows[1]['city'], 'Москва')
        self.assertFalse(row['metadata_verified'])
        self.assertEqual(row['access'], 'metadata_only')
        self.assertEqual(parse_bibtex(BIB)[0]['candidate_id'], row['candidate_id'])

    def test_bibtex_rejects_incomplete_or_ambiguous_records(self):
        for content in ('@article{x,title={broken}', '@article{x,title=unknown}', '@article{x,title={a},title={b}}', '@book{x,crossref={y}}'):
            with self.subTest(content=content), self.assertRaises(ContractError):
                parse_bibtex(content)

    def test_ris_preserves_continuation_and_bibliographic_fields(self):
        row = parse_ris(RIS)[0]
        self.assertEqual(row['abstract'], 'Первая строка продолжение аннотации')
        self.assertEqual(row['pages'], '45–57')
        self.assertEqual(row['journal'], 'Тестовый журнал')
        self.assertFalse(row['metadata_verified'])
        self.assertEqual(row['candidate_id'], parse_bibtex(BIB)[0]['candidate_id'])

    def test_ris_rejects_unfinished_record(self):
        with self.assertRaises(ContractError):
            parse_ris(RIS.replace('ER  -', ''))

    def test_import_keeps_sources_untouched_and_preserves_raw_input(self):
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary)
            write_text(run/'brief.json', '{}')
            write_text(run/'search-log.jsonl', '')
            write_text(run/'sources.jsonl', '{"untouched":true}\n')
            path = run/'input.bib'; write_text(path, BIB)
            result = import_bibliography(run, path)
            record = read_json(Path(result['candidate_file']))
            self.assertEqual(record['returned'], 2)
            self.assertEqual(record['raw_import'], BIB)
            self.assertEqual((run/'sources.jsonl').read_text(), '{"untouched":true}\n')
            self.assertTrue(all(item['status']=='candidate' for item in record['candidates']))

    def test_openalex_restores_abstract_and_retains_metadata_only(self):
        row = normalize_openalex_work({'id':'https://openalex.org/W1','display_name':'Test', 'type':'article','publication_year':2020,
            'authorships':[{'author':{'display_name':'Fixture'}}],
            'primary_location':{'source':{'display_name':'Journal'},'landing_page_url':'https://example.org/fixture'},
            'biblio':{'volume':'2','issue':'1','first_page':'3','last_page':'9'},
            'best_oa_location':{'pdf_url':'https://example.org/not-fetched.pdf'},
            'abstract_inverted_index':{'A':[0,2],'test':[1]}}, 1)
        self.assertEqual(row['abstract'],'A test A')
        self.assertEqual(row['access'],'metadata_only')
        self.assertEqual(row['pages'],'3–9')
        self.assertIn('pdf_url',row)

    def test_openalex_auth_is_header_only_and_raw_saved(self):
        payload = {'meta':{'count':1},'results':[{'id':'https://openalex.org/W1','title':'Fixture','type':'article'}]}
        with patch('sci_writer.literature._fetch_json',return_value=(payload,b'fixture')) as fetch:
            result = openalex_search('русская школа',api_key='secret-fixture-key',language='ru')
        self.assertNotIn('secret-fixture-key',json.dumps(result))
        self.assertNotIn('secret-fixture-key',fetch.call_args.args[0])
        self.assertEqual(fetch.call_args.kwargs['headers'],{'Authorization':'Bearer secret-fixture-key'})
        self.assertIn('language%3Aru',result['request_url'])
        self.assertEqual(result['returned'],1)

    def test_openalex_malformed_response_and_index_fail_explicitly(self):
        with patch('sci_writer.literature._fetch_json',return_value=({'results':'bad'},b'bad')):
            with self.assertRaises(ContractError): openalex_search('query')
        with self.assertRaises(ContractError):
            normalize_openalex_work({'abstract_inverted_index':{'bad':[10**9]}},1)

    def test_crossref_preserves_expanded_source_fields(self):
        row=normalize_work({'title':['Fixture'],'type':'journal-article','author':[{'family':'Fixture'}],
            'published':{'date-parts':[[2020]]},'DOI':'10.0000/fixture','container-title':['Journal'],'volume':'9','issue':'2','page':'10-20','publisher':'Publisher'},1)
        self.assertEqual((row['journal'],row['volume'],row['issue'],row['pages'],row['publisher']),('Journal','9','2','10-20','Publisher'))

    def test_failed_query_does_not_create_successful_search_log(self):
        with tempfile.TemporaryDirectory() as temporary:
            run=Path(temporary);write_text(run/'brief.json','{}');write_text(run/'search-log.jsonl','')
            with patch('sci_writer.literature.openalex_search',side_effect=ContractError('unavailable')):
                with self.assertRaises(ContractError): search_openalex_into_run(run,'query')
            self.assertEqual((run/'search-log.jsonl').read_text(),'')
            self.assertFalse((run/'search').exists())


if __name__=='__main__': unittest.main()
