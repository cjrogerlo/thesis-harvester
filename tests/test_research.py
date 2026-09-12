import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from pdf_text_pipeline.bindings import link_manifest, bound_metadata, license_from_evidence
from pdf_text_pipeline.pipeline import Store, Config
from pdf_text_pipeline.retrieval import build_index, search, query_terms, render_results
from test_pipeline import make_pdf


class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.inputs = self.root / 'pdfs'
        self.inputs.mkdir()
        self.pdf = self.inputs / 'paper.pdf'
        make_pdf(self.pdf, ['1 Introduction\nFAMIN controls dendritic cells and T cell priming.',
                            '2 Results\nPurine metabolism supports biological experiments.'])
        self.store = Store(self.root / 'out')

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def bind(self, rows, name='bindings.sqlite'):
        manifest = self.root / 'manifest.jsonl'
        manifest.write_text(''.join(json.dumps(r)+'\n' for r in rows))
        db = self.root / name
        return db, link_manifest(manifest, self.inputs, db, hash_files=True)

    def process(self, **metadata):
        return self.store.process(self.pdf, Config(backend='pypdf', min_chars=5), supplied_metadata=metadata)

    def test_apollo_sanitized_path_and_hash(self):
        folder = self.inputs / 'uuid-1'; folder.mkdir()
        pdf = folder / 'Paper_ HJ.pdf'; self.pdf.rename(pdf)
        db, report = self.bind([dict(uuid='uuid-1', title='Evidence title', rights=['All rights reserved'],
            downloaded_files=[dict(name='Paper, HJ.pdf', path='/old/uuid-1/Paper_ HJ.pdf',
                download_status='downloaded', sizeBytes=pdf.stat().st_size, content_url='https://example.org/p.pdf')])])
        self.assertEqual(report['matched'], 1)
        meta = bound_metadata(db, pdf)
        self.assertEqual(meta['license'], 'all-rights-reserved')
        self.assertEqual(meta['binding_evidence']['manifest_line'], 1)
        pdf.write_bytes(pdf.read_bytes()+b' changed')
        with self.assertRaisesRegex(ValueError, 'changed'): bound_metadata(db, pdf)

    def test_size_and_traversal_fail_closed(self):
        db, report = self.bind([{'pdf_path':'paper.pdf','size_bytes':1}, {'pdf_path':'../paper.pdf'}])
        self.assertEqual(report['matched'], 0)
        self.assertEqual(len(report['errors']), 2)
        with self.assertRaisesRegex(ValueError, 'No verified'): bound_metadata(db, self.pdf)

    def test_generic_binding_and_unknown_rights(self):
        db, report = self.bind([{'pdf_path':'paper.pdf','metadata':{'title':'My title','license':'cc-by','rights':['Explicit rights']}}])
        self.assertEqual(report['matched'], 1)
        meta = bound_metadata(db, self.pdf)
        self.assertEqual(meta['title'], 'My title')
        self.assertEqual(meta['rights'], ['Explicit rights'])
        self.assertEqual(license_from_evidence('Open Access', []), 'unknown')
        self.assertEqual(license_from_evidence([], ['https://creativecommons.org/licenses/by-nc/4.0/']), 'cc-by-nc')

    def test_citations_filters_and_literal_query(self):
        self.process(title='Immunology study', license='cc-by', source_url='https://example.org/item',
                     binding_evidence={'content_url':'https://example.org/p.pdf'})
        db = self.root / 'search.sqlite'; report = build_index(self.store, db)
        self.assertEqual(report['documents'], 1)
        result = search(db, 'FAMIN " OR NEAR(foo)')
        citation = result['results'][0]['citation']
        self.assertEqual(citation['page_start'], 1)
        self.assertEqual(citation['title'], 'Immunology study')
        self.assertEqual(citation['pdf_page_url'], 'https://example.org/p.pdf#page=1')
        self.assertIn('FAMIN', result['results'][0]['text'])
        self.assertIn('PDF pp. 1', render_results(result))
        self.assertEqual(search(db, 'FAMIN', license='cc0')['results'], [])
        self.assertEqual(search(db, 'neverfoundword')['results'], [])
        self.assertEqual(search(db, 'Immunology')['results'], [])  # Title alone cannot masquerade as paragraph evidence.
        self.assertEqual(search(db, '!!!')['results'], [])
        self.assertTrue(search(db, 'FAMIN', role='body')['results'])

    def test_review_gate_and_sparse_pages(self):
        make_pdf(self.pdf, ['Scientific evidence explaining FAMIN and dendritic cell experiments.', '', 'Another sufficiently long experimental passage.'])
        result = self.process(title='Study')
        self.assertEqual(result['status'], 'needs_ocr')
        self.assertEqual(build_index(self.store, self.root/'default.sqlite')['documents'], 0)
        db = self.root/'review.sqlite'
        self.assertEqual(build_index(self.store, db, include_needs_review=True)['documents'], 1)
        with sqlite3.connect(db) as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM chunks WHERE page_start=2').fetchone()[0], 0)
        self.assertEqual(search(db, 'FAMIN')['results'][0]['citation']['processing_status'], 'needs_ocr')

    def test_duplicate_versions_and_corrupt_bundles_rejected(self):
        self.process(title='First title')
        result = self.process(title='Revised title')
        db = self.root/'duplicate.sqlite'
        with self.assertRaisesRegex(ValueError, 'Duplicate'): build_index(self.store, db)
        self.assertFalse(db.exists())
        (self.store.directory(result['document_id'],result['profile'])/'document.md.gz').write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError, 'Invalid bundle'):
            build_index(self.store, self.root/'corrupt.sqlite', profile=result['profile'])

    def test_read_only_missing_index_and_query_bounds(self):
        db = self.root/'missing.sqlite'
        with self.assertRaises(sqlite3.OperationalError): search(db,'evidence')
        self.assertFalse(db.exists())
        with self.assertRaises(ValueError): search(db,'evidence',limit=101)
        terms = query_terms(' '.join('word'+str(i) for i in range(80))+' 中文檢索', limit=None)
        self.assertIn('檢索', terms)
        self.assertEqual(len(query_terms(' '.join(terms))), 64)

    def test_worker_does_not_shadow_stdlib_profile(self):
        # Exercise the real direct-execution path without importing heavy Docling models.
        worker = Path(__file__).parents[1]/'src/pdf_text_pipeline/docling_worker.py'
        code = "import runpy,sys; sys.path.insert(0,sys.argv[1]); runpy.run_path(sys.argv[2],run_name='probe'); import cProfile"
        result = subprocess.run([sys.executable,'-c',code,str(worker.parent),str(worker)], capture_output=True,text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

class ReviewTests(unittest.TestCase):
    def test_partial_review_preserves_original_page_numbers(self):
        from pdf_text_pipeline.review import summarize
        with tempfile.TemporaryDirectory() as t:
            root=Path(t); pdf=root/'p.pdf'
            make_pdf(pdf,['Page one original evidence.','Page two original evidence.'])
            blocks=[{'text':'$$x^2$$','pages':[2],'label':'formula'}]
            (root/'result.json').write_text(json.dumps({'blocks':blocks,'assets':[],'page_count':1}))
            report=summarize(root,pdf,2,2)
            self.assertEqual(report['block_counts'],{'formula':1})
            self.assertIn('PDF page 2',(root/'review.md').read_text())
            self.assertIn('Page two',(root/'baseline.txt').read_text())
            with self.assertRaisesRegex(ValueError,'outside requested'): summarize(root,pdf,1,1)
