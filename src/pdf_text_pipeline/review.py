"""Bounded local Docling review; partial pages never enter the ready corpus."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
from pypdf import PdfReader
from .pipeline import sha256, fingerprint


def review(source, output, start, end, timeout=900, render=False):
    source, output = Path(source).resolve(), Path(output).resolve()
    if not 1 <= start <= end <= len(PdfReader(source).pages):
        raise ValueError('Invalid physical PDF page range')
    if output.exists():
        raise ValueError('Review output exists; choose a new directory')
    before, digest = fingerprint(source), sha256(source)
    output.mkdir(parents=True)
    receipt = {'source_sha256': digest, 'source_pdf': str(source), 'page_start': start,
               'page_end': end, 'status': 'running', 'partial_document': True,
               'training_approved': False}
    began = time.monotonic()
    try:
        worker = Path(__file__).with_name('docling_worker.py')
        with (output/'conversion.log').open('w') as log:
            subprocess.run([sys.executable, str(worker), str(source), str(output/'structured'), f'{start}:{end}'],
                           stdout=log, stderr=subprocess.STDOUT, check=True, timeout=timeout)
        if fingerprint(source) != before or sha256(source) != digest:
            raise ValueError('Source changed during review')
        receipt.update(summarize(output/'structured', source, start, end, render))
        receipt['status'] = 'needs_human_review'
    except Exception as exc:
        receipt.update(status='error', error=str(exc))
        raise
    finally:
        receipt['seconds'] = round(time.monotonic()-began, 3)
        (output/'review.json').write_text(json.dumps(receipt, indent=2)+'\n')
    return receipt


def summarize(structured, source, start, end, render=False):
    structured, source = Path(structured), Path(source)
    data = json.loads((structured/'result.json').read_text())
    from collections import Counter
    counts = Counter()
    reader = PdfReader(source)
    markdown, raw, emitted = [], [], set()
    for page in range(start, end+1):
        raw.append(f'<!-- PDF page {page} -->\n\n'+(reader.pages[page-1].extract_text() or ''))
        for index, block in enumerate(data['blocks']):
            if page not in block['pages'] or index in emitted:
                continue
            emitted.add(index)
            pages = sorted(set(block['pages']))
            anchor = str(pages[0]) if len(pages) == 1 else f'{pages[0]}–{pages[-1]}'
            markdown.append(f'<!-- PDF page {anchor} -->\n\n'+block['text'])
            counts[block['label']] += 1
        if render:
            import pypdfium2 as pdfium
            with pdfium.PdfDocument(source) as pdf:
                pdf[page-1].render(scale=1.5).to_pil().save(structured/f'original-page-{page}.png')
    unexpected = sorted({p for b in data['blocks'] for p in b['pages']} - set(range(start,end+1)))
    if unexpected:
        raise ValueError('Docling provenance outside requested range: '+str(unexpected))
    (structured/'review.md').write_text('\n\n'.join(markdown)+'\n')
    (structured/'baseline.txt').write_text('\n\n'.join(raw)+'\n')
    return {'requested_pages':end-start+1, 'reported_pages':data['page_count'],
            'block_counts':dict(counts), 'assets':len(data['assets'])}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source',type=Path)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--start',type=int,required=True)
    parser.add_argument('--end',type=int,required=True)
    parser.add_argument('--timeout',type=int,default=900)
    parser.add_argument('--render',action='store_true')
    args=parser.parse_args()
    print(json.dumps(review(args.source,args.output,args.start,args.end,args.timeout,args.render)))


if __name__ == '__main__': main()
