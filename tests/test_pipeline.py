import contextlib, gzip, importlib.util, io, json, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
from pdf_text_pipeline.cli import main
from pdf_text_pipeline.export import export_corpus, duplicate_candidates
from pdf_text_pipeline.llm import prepare, prepare_structured, split_text
from pdf_text_pipeline.pipeline import Config, Store, command, extract, parse_tei, sparse_pages, valid_bundle


def make_pdf(path, pages):
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>', b'', b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>']
    kids = []
    for text in pages:
        n = len(objects) + 1
        kids.append(f'{n} 0 R')
        objects.append(f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> /Contents {n+1} 0 R >>'.encode())
        lines = [x.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)') for x in text.split('\n')]
        stream = ('BT /F1 12 Tf 50 740 Td 16 TL ' + ' T* '.join(f'({x}) Tj' for x in lines) + ' ET').encode()
        objects.append(b'<< /Length ' + str(len(stream)).encode() + b' >>\nstream\n' + stream + b'\nendstream')
    objects[1] = f'<< /Type /Pages /Count {len(pages)} /Kids [{" ".join(kids)}] >>'.encode()
    data, offsets = b'%PDF-1.4\n', [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(data)); data += f'{i} 0 obj\n'.encode() + obj + b'\nendobj\n'
    xref = len(data)
    data += f'xref\n0 {len(offsets)}\n0000000000 65535 f \n'.encode()
    data += b''.join(f'{n:010d} 00000 n \n'.encode() for n in offsets[1:])
    data += f'trailer << /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode()
    path.write_bytes(data)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.pdf = self.root / 'paper.pdf'
        make_pdf(self.pdf, ['1 Introduction\nUseful scientific content for a searchable corpus.', '2 Results\nResults remain grounded in the original PDF page.'])
        self.store = Store(self.root / 'out'); self.config = Config(backend='pypdf', min_chars=5)
    def tearDown(self):
        self.store.db.close(); self.temp.cleanup()
    def process(self, config=None):
        return self.store.process(self.pdf, config or self.config)
    def directory(self, r):
        return self.store.directory(r['document_id'], r['profile'])
    def test_real_extraction_and_anchors(self):
        r = self.process(); self.assertEqual(r['status'], 'ready'); self.assertEqual(r['page_count'], 2)
        d = self.directory(r)
        with gzip.open(d / 'llm_chunks.jsonl.gz', 'rt') as f: rows = [json.loads(x) for x in f]
        self.assertEqual({x['metadata']['page_start'] for x in rows}, {1, 2})
        self.assertTrue(all(x['metadata']['paper']['license'] == 'unknown' for x in rows))
        self.assertTrue(valid_bundle(d))
        with gzip.open(d / 'document.md.gz', 'rt') as f: self.assertIn('## 1 Introduction', f.read())
        with gzip.open(d / 'metadata.jsonl.gz', 'rt') as f: self.assertEqual(len(list(f)), 1)
    @unittest.skipUnless(shutil.which('pdftotext'), 'Poppler not installed')
    def test_poppler(self):
        self.assertEqual(len(extract(self.pdf, 'pdftotext')[0]), 2)
    def test_blank_pages_and_upload_gate(self):
        make_pdf(self.pdf, ['Text on first page is sufficiently long.', '', '', 'Text on last page is sufficiently long.'])
        r = self.process(); self.assertEqual(r['low_text_pages'], [2,3]); self.assertEqual(r['page_count'], 4)
        self.assertEqual(r['status'], 'needs_ocr')
        with self.assertRaises(ValueError): self.store.upload(r['document_id'], r['profile'], 'fake:corpus')
    def test_resume_and_ledger_repair(self):
        r = self.process(); self.store.db.execute('DELETE FROM jobs'); self.store.db.commit()
        with patch('pdf_text_pipeline.pipeline.extract', side_effect=AssertionError('reextracted')): again = self.process()
        self.assertTrue(again['cached']); self.assertEqual(self.store.db.execute('SELECT count(*) FROM jobs').fetchone()[0], 1)
    def test_corruption_recovers(self):
        r = self.process(); (self.directory(r) / 'document.md.gz').write_bytes(b'broken')
        self.assertFalse(valid_bundle(self.directory(r))); self.assertFalse(self.process()['cached'])
    def test_metadata_invalidates(self):
        a = self.process()
        Path(str(self.pdf)+'.metadata.json').write_text(json.dumps({'license':'cc-by', 'source_url':'https://example.org/1', 'language':'en'}))
        b = self.process(); self.assertNotEqual(a['profile'], b['profile']); self.assertEqual(a['document_id'], b['document_id'])
        self.assertEqual(b['metadata']['license'], 'cc-by')
    def test_upload_failure_retry_without_source(self):
        r = self.process()
        with patch('pdf_text_pipeline.pipeline.command', side_effect=RuntimeError('network')):
            with self.assertRaises(RuntimeError): self.store.upload(r['document_id'], r['profile'], 'fake:corpus')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM uploads').fetchone()[0], 0)
        self.pdf.unlink()
        with patch('pdf_text_pipeline.pipeline.command', return_value=b'') as call:
            self.assertEqual(list(self.store.upload_pending('fake:corpus','rclone',10))[0]['upload'], 'uploaded')
            self.assertTrue(str(call.call_args.args[0][2]).endswith('manifest.json'))
            count = call.call_count
            self.assertEqual(list(self.store.upload_pending('fake:corpus','rclone',10))[0]['upload'], 'cached_receipt')
            self.assertEqual(count, call.call_count)
    def test_ocr_sparse_pages(self):
        make_pdf(self.pdf, ['Text on first page is sufficiently long.', ''])
        import pdf_text_pipeline.pipeline as m
        real = m.command
        def fake(args, timeout):
            if args[0] == 'ocrmypdf':
                self.assertEqual(args[args.index('--pages')+1], '2')
                self.assertEqual(args[args.index('--optimize')+1], '0')
                make_pdf(Path(args[-1]), ['Text on first page is sufficiently long.', 'Recovered scanned page content.']); return b''
            return real(args, timeout)
        with patch('pdf_text_pipeline.pipeline.shutil.which', return_value='/fake/ocrmypdf'), patch('pdf_text_pipeline.pipeline.command', side_effect=fake):
            r = self.process(Config(backend='pypdf',ocr='auto',min_chars=5))
        self.assertEqual(r['status'], 'ready'); self.assertTrue(r['ocr_applied'])
    def test_source_change_rejected(self):
        original = shutil.copyfile
        def mutate(src,dst):
            original(src,dst); Path(src).write_bytes(Path(src).read_bytes()+b'\nchanged')
        with patch('pdf_text_pipeline.pipeline.shutil.copyfile', side_effect=mutate):
            with self.assertRaisesRegex(RuntimeError,'changed'): self.process()
    def test_timeout(self):
        with self.assertRaises(subprocess.TimeoutExpired): command([sys.executable,'-c','import time; time.sleep(5)'],0.05)
    def test_lock(self):
        other = Store(self.root/'out')
        try:
            with self.store.lock():
                with self.assertRaises(RuntimeError):
                    with other.lock(): pass
        finally: other.db.close()
    def test_malformed(self):
        self.pdf.write_bytes(b'not a pdf')
        with self.assertRaises(RuntimeError): self.process()
        self.assertEqual(self.store.db.execute('SELECT status FROM jobs').fetchone()[0], 'error')
    def test_license_filter(self):
        self.process(); self.assertEqual(export_corpus(self.store,self.root/'export',licenses=['cc-by'])['rows'],0)
    @unittest.skipUnless(importlib.util.find_spec('pyarrow'), 'optional pyarrow absent')
    def test_parquet(self):
        import pyarrow.parquet as pq
        self.process(); r = export_corpus(self.store,self.root/'export',format='parquet',kind='chunks',shard_rows=1)
        self.assertGreater(r['rows'],0)
        for path in (self.root/'export').glob('*.parquet'):
            row = pq.read_table(path,columns=['license','text','page_start']).to_pylist()[0]
            self.assertEqual(row['license'],'unknown'); self.assertTrue(row['text'])
    def test_near_duplicates(self):
        self.process(); other=self.root/'copy.pdf'; other.write_bytes(self.pdf.read_bytes()+b'\n% different bytes')
        self.store.process(other,self.config)
        self.assertEqual(duplicate_candidates(self.store,self.root/'duplicates.jsonl')['candidate_pairs'],1)
        self.assertTrue(json.loads((self.root/'duplicates.jsonl').read_text())['normalized_exact'])
    def test_cli(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(['run',str(self.pdf),'--output',str(self.root/'cli'),'--settle-seconds','0','--backend','pypdf']),0)
            self.assertEqual(main(['run',str(self.root),'--output',str(self.root/'nested')]),1)


class StructureTests(unittest.TestCase):
    def test_cleanup(self):
        pages=[f'Journal Example\n{i}\n1 Introduction\nStudy content line\ncontinues here {i}.\nJournal Footer' for i in range(3)]
        md, rows, removed, roles=prepare(pages,'abc','profile',Config())
        self.assertNotIn('Journal Example',md); self.assertIn('Study content line continues here',md)
        self.assertIn('1 Introduction',md); self.assertTrue(removed); self.assertIn('body',roles)
        for r in rows:
            for s in r['metadata']['source_spans']: self.assertTrue(pages[s['page']-1][s['char_start']:s['char_end']])
    def test_structured(self):
        texts=[('section_header','# Results'),('formula','$$\nx^2+y^2=z^2\n$$'),('table','<table><tr><td colspan="2">Merged</td></tr></table>'),('caption','Figure 1: caption'),('section_header','# References'),('text','Author Paper 2026')]
        blocks=[dict(text=t,label=l,pages=[1],coords=[],item_ref=f'#/texts/{i}') for i,(l,t) in enumerate(texts)]
        md, rows, _, roles=prepare_structured(blocks,'abc','profile',Config())
        self.assertIn('$$\nx^2',md); self.assertIn('colspan="2"',md); self.assertIn('Figure 1',md)
        self.assertNotIn('Author Paper',roles['body']); self.assertEqual(rows[-1]['metadata']['content_role'],'references')
    def test_unicode_token_cap(self):
        class ByteEncoding:
            def encode(self,text,**kw): return list(text.encode())
        chunks=list(split_text('中文內容和數學公式 ∑x² '*30,200,20,ByteEncoding(),40))
        self.assertTrue(all(len(x.encode())<=40 and '�' not in x for x in chunks)); self.assertTrue(chunks[-1].endswith('∑x² '))
    def test_tei(self):
        xml='<TEI xmlns="http://www.tei-c.org/ns/1.0"><text><body><div><head>Methods</head><div><head>Model</head><p coords="2,1,2,3,4;3,1,2,3,4">Model.</p></div></div></body><back><listBibl><biblStruct xml:id="b1"><idno type="DOI">10.1234/test</idno></biblStruct></listBibl></back></text></TEI>'
        r=parse_tei(xml); self.assertEqual(r['paragraphs'][0]['section_path'],['Methods','Model']); self.assertEqual(r['paragraphs'][0]['coords'][1]['page'],3)
        self.assertEqual(r['references'][0]['doi'],'10.1234/test')
        with self.assertRaises(ValueError): parse_tei('<html>error</html>')
    def test_xml_entity(self):
        with self.assertRaises(Exception): parse_tei('<!DOCTYPE foo [<!ENTITY x SYSTEM "file:///etc/passwd">]><TEI xmlns="http://www.tei-c.org/ns/1.0">&x;</TEI>')
    def test_page_heuristic(self): self.assertEqual(sparse_pages(['text '*100,'1','enough text on page'],10),[2])

if __name__=='__main__': unittest.main()

class ProfilingTests(unittest.TestCase):
    def test_profile_counts_and_hash_duplicates(self):
        from pdf_text_pipeline.profile import profile
        with tempfile.TemporaryDirectory() as t:
            root=Path(t); inputs=root/'pdfs'; inputs.mkdir()
            make_pdf(inputs/'a.pdf',['This sufficiently long scientific document contains searchable text and reliable page provenance.'])
            shutil.copyfile(inputs/'a.pdf',inputs/'b.pdf')
            r=profile(inputs,root/'profile',sample=2,hash_all=True)
            self.assertEqual(r['pdf_count'],2); self.assertEqual(r['duplicate_extra_files'],1)
            self.assertEqual(r['hash_coverage_files'],2); self.assertEqual(r['sample_categories'][0]['category'],'text_present')
    def test_sample_budget_follows_bytes_not_files(self):
        from pdf_text_pipeline.profile import allocate
        counts={'<1 MiB':10000,'>=1024 MiB':20}
        byte_totals={'<1 MiB':5_000_000_000,'>=1024 MiB':30_000_000_000}
        plan=allocate(counts,byte_totals,budget=60,min_bin_sample=5)
        self.assertGreater(plan['>=1024 MiB'],plan['<1 MiB'])
        self.assertLessEqual(plan['>=1024 MiB'],counts['>=1024 MiB'])
    def test_stratified_estimate_uses_census_bin_bytes(self):
        from pdf_text_pipeline.profile import stratified_estimates
        counts={'<1 MiB':10000,'>=1024 MiB':20}
        byte_totals={'<1 MiB':5_000_000_000,'>=1024 MiB':30_000_000_000}
        per_bin={'<1 MiB':[('text_present',500_000)]*8,
                 '>=1024 MiB':[('low_text_image_candidate',1_500_000_000)]*20}
        est={e['category']:e for e in stratified_estimates(per_bin,byte_totals,counts)}
        # the heavy bin is censused: its contribution is exact and carries no interval
        self.assertEqual(est['low_text_image_candidate']['estimated_bytes'],30_000_000_000)
        self.assertEqual(est['low_text_image_candidate']['ci95_bytes'],0)
        self.assertEqual(est['text_present']['estimated_bytes'],5_000_000_000)
    def test_duplicates_report_not_measured_without_hashing(self):
        from pdf_text_pipeline.profile import profile
        with tempfile.TemporaryDirectory() as t:
            root=Path(t); inputs=root/'pdfs'; inputs.mkdir()
            make_pdf(inputs/'a.pdf',['This sufficiently long scientific document contains searchable text and reliable page provenance.'])
            r=profile(inputs,root/'profile',sample=2)
            self.assertFalse(r['duplicates_measured']); self.assertIsNone(r['duplicate_extra_files'])
            self.assertIn('not measured',(root/'profile'/'profile.md').read_text())
    def test_page_plan_grows_with_length(self):
        from pdf_text_pipeline.profile import page_plan
        self.assertEqual(page_plan(10,3),3); self.assertGreater(page_plan(300,3),3)
        self.assertLessEqual(page_plan(5000,3),16)
    def test_attested_dehyphenation(self):
        md,_,_,_=prepare(['Learning supports learn-\ning in this document.'],'abc','p',Config())
        self.assertIn('supports learning',md)
    def test_structured_short_blocks_are_packed(self):
        from pdf_text_pipeline.llm import pack_chunks
        blocks=[dict(text='Example paragraph content.',label='text',pages=[1],coords=[],item_ref=f'#/texts/{i}') for i in range(3)]
        _,rows,_,_=prepare_structured(blocks,'a','p',Config())
        packed=pack_chunks(rows,Config()); self.assertEqual(len(packed),1); self.assertEqual(len(packed[0]['metadata']['source_items']),3)
    @unittest.skipUnless(importlib.util.find_spec('tiktoken'),'optional tokenizer absent')
    def test_real_tokenizer(self):
        import tiktoken
        enc=tiktoken.get_encoding('cl100k_base')
        text='English 中文 LaTeX: \\int_0^1 x dx. <|endoftext|> '*100
        parts=list(split_text(text,6000,200,enc,1000))
        self.assertTrue(all(len(enc.encode(x,disallowed_special=()))<=1000 for x in parts))

class AdapterTests(unittest.TestCase):
    def test_docling_bundle_contract(self):
        import pdf_text_pipeline.pipeline as m
        with tempfile.TemporaryDirectory() as t:
            root=Path(t); pdf=root/'input.pdf'; make_pdf(pdf,['Raw text source for a structured conversion fixture.'])
            real=m.command
            def fake(args,timeout):
                if str(args[1]).endswith('docling_worker.py'):
                    out=Path(args[-1]);out.mkdir()
                    blocks=[dict(text='# Results',label='section_header',pages=[1],coords=[],item_ref='#/texts/0'),dict(text='$$\nx^2 + y^2 = z^2\n$$\nMeaningful scientific formula explanation.',label='formula',pages=[1],coords=[],item_ref='#/texts/1')]
                    (out/'result.json').write_text(json.dumps(dict(blocks=blocks,assets=[],page_count=1)))
                    (out/'docling.json').write_text('{}');return b''
                return real(args,timeout)
            store=Store(root/'out')
            try:
                with patch('pdf_text_pipeline.pipeline.command',side_effect=fake): r=store.process(pdf,Config(backend='docling',min_chars=5))
                self.assertEqual(r['quality_tier'],'structured_unreviewed')
                self.assertIn('docling.json.gz',r['files'])
                with gzip.open(store.directory(r['document_id'],r['profile'])/'document.md.gz','rt') as f:self.assertIn('$$',f.read())
            finally: store.db.close()
    def test_image_effective_dpi(self):
        from pypdf import PdfWriter
        from pypdf.generic import DictionaryObject,NameObject,NumberObject,DecodedStreamObject
        from pdf_text_pipeline.profile import inspect_pdf
        with tempfile.TemporaryDirectory() as t:
            path=Path(t)/'scan.pdf'; writer=PdfWriter();page=writer.add_blank_page(width=612,height=792)
            im=DecodedStreamObject();im.set_data(bytes(600*600))
            im.update({NameObject('/Type'):NameObject('/XObject'),NameObject('/Subtype'):NameObject('/Image'),NameObject('/Width'):NumberObject(600),NameObject('/Height'):NumberObject(600),NameObject('/BitsPerComponent'):NumberObject(8),NameObject('/ColorSpace'):NameObject('/DeviceGray')})
            page[NameObject('/Resources')]=DictionaryObject({NameObject('/XObject'):DictionaryObject({NameObject('/Im0'):writer._add_object(im)})})
            content=DecodedStreamObject();content.set_data(b'q 144 0 0 144 0 0 cm /Im0 Do Q')
            page[NameObject('/Contents')]=writer._add_object(content)
            writer.write(path)
            r=inspect_pdf(path);self.assertEqual(r['category'],'low_text_image_candidate')
            self.assertEqual(r['sampled_pages'][0]['direct_images'][0]['effective_dpi_x'],300)
