"""Read-only PDF storage profiler. Hashing is explicit; no recompression/deletion."""
import argparse
from collections import Counter
import heapq
import json
import math
from pathlib import Path
import random
import sqlite3
import sys

from .pipeline import command, fingerprint, pdf_paths, sha256


def inspect_pdf(path, max_pages=3, min_chars=40):
    from pypdf import PdfReader
    reader = PdfReader(path)
    count = len(reader.pages)
    if count == 0:
        raise ValueError("PDF has no pages")
    indices = sorted({round(i * (count-1) / max(1, max_pages-1)) for i in range(min(max_pages,count))})
    pages = []
    for index in indices:
        page = reader.pages[index]
        resources = page.get('/Resources', {})
        resources = resources.get_object() if hasattr(resources, 'get_object') else resources
        xobjects = resources.get('/XObject', {})
        xobjects = xobjects.get_object() if hasattr(xobjects, 'get_object') else xobjects
        images, forms = [], 0
        for value in xobjects.values():
            obj = value.get_object()
            forms += int(obj.get('/Subtype') == '/Form')
        def visitor(op, operands, cm, tm):
            if op != b'Do' or not operands:
                return
            ref = xobjects.get(operands[0])
            obj = ref.get_object() if ref is not None else {}
            if obj.get('/Subtype') != '/Image':
                return
            # PDF paints an image into a unit square transformed by the current matrix.
            unit = float(page.get('/UserUnit', 1))
            sx, sy = math.hypot(cm[0],cm[1])*unit, math.hypot(cm[2],cm[3])*unit
            width, height = int(obj.get('/Width',0)), int(obj.get('/Height',0))
            images.append({'pixel_width':width, 'pixel_height':height,
                           'effective_dpi_x':round(72*width/sx,1) if sx else None,
                           'effective_dpi_y':round(72*height/sy,1) if sy else None,
                           'filters':str(obj.get('/Filter',''))})
        text = page.extract_text(visitor_operand_before=visitor) or ''
        chars = sum(c.isalnum() for c in text)
        pages.append({'page':index+1,'alnum_chars':chars,'low_text':chars<min_chars,
                      'has_font_resources':bool(resources.get('/Font')),'direct_images':images,
                      'form_xobjects':forms,'dpi_coverage':'direct image placements only; nested forms and inline images may be missed'})
    low = sum(p['low_text'] for p in pages)
    has_images = any(p['direct_images'] for p in pages)
    category = ('low_text_image_candidate' if has_images else 'low_text_unknown') if low == len(pages) else ('mixed_text_candidate' if low else 'text_present')
    return {'page_count':count,'sampled_pages':pages,'category':category,
            'note':'Text presence does not prove born-digital: scanned PDFs can already contain OCR.'}


def profile(root, output, sample=50, seed=42, hash_all=False, page_sample=3, timeout=120):
    output = Path(output); output.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(output/'profile.sqlite')
    db.execute('CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY,size INTEGER,fingerprint TEXT,sha256 TEXT,generation TEXT)')
    import uuid
    generation = uuid.uuid4().hex
    rng, sampled, largest, bins, errors = random.Random(seed), [], [], Counter(), []
    total, count = 0, 0
    boundaries = [1,5,20,100,500,1024]
    for path in pdf_paths(root):
        try:
            size = path.stat().st_size; fp = fingerprint(path)
            old = db.execute('SELECT fingerprint,sha256 FROM files WHERE path=?',(str(path),)).fetchone()
            digest = old[1] if old and old[0] == fp else None
            if hash_all and digest is None:
                digest = sha256(path)
                if fingerprint(path) != fp: raise ValueError('Changed during hashing; retry later')
            db.execute('INSERT OR REPLACE INTO files VALUES (?,?,?,?,?)',(str(path),size,fp,digest,generation))
            count += 1; total += size
            label = next((f'<{m} MiB' for m in boundaries if size < m*1024*1024),'>=1024 MiB')
            bins[(label,'files')] += 1; bins[(label,'bytes')] += size
            item = (size,str(path))
            if len(largest)<20: heapq.heappush(largest,item)
            elif item>largest[0]: heapq.heapreplace(largest,item)
            if len(sampled)<sample: sampled.append(path)
            else:
                slot=rng.randrange(count)
                if slot<sample: sampled[slot]=path
            if count % 1000 == 0: db.commit()
        except (OSError,ValueError) as exc:
            errors.append({'source':str(path),'error':str(exc)})
    db.commit()
    results=[]
    for path in sampled:
        try:
            payload = command([sys.executable,Path(__file__).with_name('profile_worker.py'),path,str(page_sample)],timeout)
            results.append({'source':str(path),'bytes':path.stat().st_size,**json.loads(payload)})
        except Exception as exc: results.append({'source':str(path),'error':str(exc)})
    groups=Counter(); group_bytes=Counter()
    for r in results:
        if 'category' in r: groups[r['category']]+=1; group_bytes[r['category']]+=r['bytes']
    duplicates = db.execute('SELECT count(*),sum(n-1),sum((n-1)*size) FROM (SELECT sha256,count(*) n,max(size) size FROM files WHERE generation=? AND sha256 IS NOT NULL GROUP BY sha256 HAVING count(*)>1)',(generation,)).fetchone()
    hashed = db.execute('SELECT count(*) FROM files WHERE generation=? AND sha256 IS NOT NULL',(generation,)).fetchone()[0]
    report={'pdf_count':count,'logical_pdf_bytes':total,'largest_20':[{'bytes':s,'source':p} for s,p in sorted(largest,reverse=True)],
            'size_histogram':[{'bin':label,'files':bins[(label,'files')],'bytes':bins[(label,'bytes')]} for label in [*(f'<{m} MiB' for m in boundaries),'>=1024 MiB']],
            'hash_coverage_files':hashed,'exact_duplicate_groups':duplicates[0], 'duplicate_extra_files':duplicates[1] or 0,
            'duplicate_logical_bytes':duplicates[2] or 0,'sample_size':len(sampled),'sample_seed':seed,
            'sample_categories':[{'category':k,'files':v,'sample_bytes':group_bytes[k]} for k,v in groups.items()],
            'sample_documents':results,'scan_errors':errors,
            'limitations':['Byte shares describe the sampled PDFs, not the whole archive.','Page sampling can miss scanned sections; /Font is not a born-digital test.','Effective DPI covers directly placed image XObjects only.','Duplicate logical bytes are not physical reclaimable bytes (hardlinks/CoW may share storage).','No CDC savings or compression ratios are inferred.']}
    (output/'profile.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    lines=['# PDF storage profile','',f'PDFs: {count:,}; logical bytes: {total:,}; hashed: {hashed:,}.','', '| Size bin (upper bound) | Files | Bytes |','|---|---:|---:|']
    lines += [f"| {b['bin']} | {b['files']:,} | {b['bytes']:,} |" for b in report['size_histogram']]
    lines += ['',f"Exact duplicate extra copies: {report['duplicate_extra_files']:,}; logical duplicate bytes: {report['duplicate_logical_bytes']:,}.",'','## Limits',''] + ['- '+x for x in report['limitations']]
    (output/'profile.md').write_text('\n'.join(lines)+'\n')
    db.close()
    return report


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('input',type=Path); p.add_argument('--output',type=Path,required=True)
    p.add_argument('--sample',type=int,default=50); p.add_argument('--seed',type=int,default=42)
    p.add_argument('--page-sample',type=int,default=3); p.add_argument('--timeout',type=int,default=120)
    p.add_argument('--hash-all',action='store_true',help='Read all PDF bytes; reuse matching stat fingerprints on resume')
    a=p.parse_args(argv)
    if min(a.sample,a.page_sample,a.timeout)<=0: p.error('sample, page-sample and timeout must be positive')
    report=profile(a.input,a.output,a.sample,a.seed,a.hash_all,a.page_sample,a.timeout)
    print(json.dumps({k:report[k] for k in ('pdf_count','logical_pdf_bytes','hash_coverage_files','duplicate_extra_files','sample_size')}))
    return 0
