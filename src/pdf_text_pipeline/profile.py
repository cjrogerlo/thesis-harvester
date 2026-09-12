"""Read-only PDF storage profiler. Hashing is explicit; no recompression/deletion.

Byte shares are estimated with a size-stratified sample, not a uniform one: the
question is where the bytes are, and a uniform sample over files answers where the
files are. The first pass is a census (every file's size is measured, so each size
bin's total bytes B_h is exact); the sample is then allocated across bins in
proportion to B_h, which minimises the variance of the stratified estimator

    B_c = sum_h B_h * p_c,h          p_c,h = sampled bytes in category c / sampled bytes

Within a bin the category share is a ratio of bytes rather than a count of files, so
the residual correlation between file size and category inside the bin is absorbed.
Bins small enough to be covered completely are censused, contributing zero variance.
"""
import argparse
from collections import Counter, defaultdict
import heapq
import json
import math
from pathlib import Path
import random
import sqlite3
import sys
import uuid

from .pipeline import command, fingerprint, pdf_paths, sha256

BOUNDARIES = [1, 5, 20, 100, 500, 1024]
BIN_LABELS = [*(f'<{m} MiB' for m in BOUNDARIES), '>=1024 MiB']
MIN_BIN_SAMPLE = 5          # below this a bin's share is reported but flagged insufficient
GENERATIONS_KEPT = 5        # older census rows are pruned so the db does not accumulate ghosts


def bin_label(size):
    return next((f'<{m} MiB' for m in BOUNDARIES if size < m*1024*1024), '>=1024 MiB')


def page_plan(count, max_pages, cap=16):
    """Three pages is fine for a 10-page paper and nearly blind on a 300-page thesis."""
    return max(1, min(cap, count, max(max_pages, math.ceil(count ** 0.5 / 2))))


def inspect_pdf(path, max_pages=3, min_chars=40):
    from pypdf import PdfReader
    reader = PdfReader(path)
    count = len(reader.pages)
    if count == 0:
        raise ValueError("PDF has no pages")
    planned = page_plan(count, max_pages)

    def examine(indices):
        out = []
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
            out.append({'page':index+1,'alnum_chars':chars,'low_text':chars<min_chars,
                        'has_font_resources':bool(resources.get('/Font')),'direct_images':images,
                        'form_xobjects':forms,
                        'dpi_coverage':'direct image placements only; inline images may be missed'})
        return out

    indices = sorted({round(i * (count-1) / max(1, planned-1)) for i in range(planned)})
    pages = examine(indices)
    extra = 0
    if pages and all(p['low_text'] for p in pages) and count > len(indices):
        # an all-low-text verdict is the expensive one to get wrong: confirm on offset pages
        offsets = sorted({min(count-1, max(0, i + max(1, count//(2*len(indices)))))
                          for i in indices} - set(indices))[:4]
        if offsets:
            pages += examine(offsets)
            extra = len(offsets)

    low = sum(p['low_text'] for p in pages)
    has_images = any(p['direct_images'] for p in pages)
    has_forms = any(p['form_xobjects'] for p in pages)
    if low == len(pages):
        # a scan wrapped in a Form XObject has no direct image placement; count it separately
        # instead of burying it in low_text_unknown
        category = ('low_text_image_candidate' if has_images else
                    'low_text_form_candidate' if has_forms else 'low_text_unknown')
    else:
        category = 'mixed_text_candidate' if low else 'text_present'
    return {'page_count':count,'pages_examined':len(pages),'extra_pages_examined':extra,
            'sampled_pages':pages,'category':category,
            'note':'Text presence does not prove born-digital: scanned PDFs can already contain OCR.'}


def allocate(counts, byte_totals, budget, min_bin_sample=MIN_BIN_SAMPLE):
    """Split the sample budget across size bins in proportion to each bin's bytes."""
    live = [b for b in BIN_LABELS if counts.get(b)]
    total_bytes = sum(byte_totals.get(b, 0) for b in live) or 1
    plan = {}
    for label in live:
        share = budget * byte_totals.get(label, 0) / total_bytes
        plan[label] = min(counts[label], max(min_bin_sample, round(share)))
    return plan


def stratified_estimates(per_bin, byte_totals, counts):
    """B_c = sum_h B_h * (sampled bytes in c / sampled bytes), with a stratified variance."""
    categories = {c for rows in per_bin.values() for c, _ in rows}
    out = []
    for category in sorted(categories):
        estimate = variance = 0.0
        for label, rows in per_bin.items():
            n = len(rows)
            sampled_bytes = sum(size for _, size in rows)
            if not n or not sampled_bytes:
                continue
            p = sum(size for c, size in rows if c == category) / sampled_bytes
            bin_bytes = byte_totals.get(label, 0)
            estimate += bin_bytes * p
            fpc = max(0.0, 1 - n / counts[label]) if counts.get(label) else 1.0
            variance += (bin_bytes ** 2) * p * (1 - p) / max(n - 1, 1) * fpc
        half = 1.96 * math.sqrt(variance)
        out.append({'category':category, 'estimated_bytes':round(estimate),
                    'estimated_share':round(estimate / (sum(byte_totals.values()) or 1), 4),
                    'ci95_bytes':round(half),
                    'ci95_share':round(half / (sum(byte_totals.values()) or 1), 4)})
    return sorted(out, key=lambda r: -r['estimated_bytes'])


def profile(root, output, sample=50, seed=42, hash_all=False, page_sample=3, timeout=120,
            min_bin_sample=MIN_BIN_SAMPLE, generations_kept=GENERATIONS_KEPT):
    output = Path(output); output.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(output/'profile.sqlite')
    db.execute('CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY,size INTEGER,fingerprint TEXT,sha256 TEXT,generation TEXT)')
    db.execute('CREATE TABLE IF NOT EXISTS generations(generation TEXT PRIMARY KEY,created_at TEXT)')
    generation = uuid.uuid4().hex
    db.execute('INSERT INTO generations VALUES (?,datetime("now"))',(generation,))
    rng, largest, bins, errors = random.Random(seed), [], Counter(), []
    reservoir, seen_in_bin = defaultdict(list), Counter()
    total, count = 0, 0

    # pass 1: census. Every size is measured, so bin byte totals are exact, and each bin
    # keeps its own reservoir (a uniform sample of that bin) for the second pass.
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
            label = bin_label(size)
            bins[(label,'files')] += 1; bins[(label,'bytes')] += size
            item = (size,str(path))
            if len(largest)<20: heapq.heappush(largest,item)
            elif item>largest[0]: heapq.heapreplace(largest,item)
            seen_in_bin[label] += 1
            pool = reservoir[label]
            if len(pool) < sample: pool.append((path,size))
            else:
                slot = rng.randrange(seen_in_bin[label])
                if slot < sample: pool[slot] = (path,size)
            if count % 1000 == 0: db.commit()
        except (OSError,ValueError) as exc:
            errors.append({'source':str(path),'error':str(exc)})
    db.commit()

    counts = {label: bins[(label,'files')] for label in BIN_LABELS}
    byte_totals = {label: bins[(label,'bytes')] for label in BIN_LABELS}
    plan = allocate(counts, byte_totals, sample, min_bin_sample)

    # pass 2: inspect the stratified draw
    results, per_bin = [], defaultdict(list)
    for label, quota in plan.items():
        pool = list(reservoir[label]); rng.shuffle(pool)
        for path, size in pool[:quota]:
            try:
                payload = command([sys.executable,Path(__file__).with_name('profile_worker.py'),path,str(page_sample)],timeout)
                row = {'source':str(path),'bytes':size,'size_bin':label,**json.loads(payload)}
                per_bin[label].append((row['category'], size))
            except Exception as exc:
                row = {'source':str(path),'bytes':size,'size_bin':label,'error':str(exc)}
            results.append(row)

    groups, group_bytes = Counter(), Counter()
    for r in results:
        if 'category' in r: groups[r['category']] += 1; group_bytes[r['category']] += r['bytes']
    strata = [{'bin':label,'files':counts[label],'bytes':byte_totals[label],
               'sampled_files':len(per_bin.get(label,[])),
               'censused':counts[label] > 0 and len(per_bin.get(label,[])) >= counts[label],
               'insufficient':0 < len(per_bin.get(label,[])) < min_bin_sample}
              for label in BIN_LABELS if counts[label]]

    duplicates = db.execute('SELECT count(*),sum(n-1),sum((n-1)*size) FROM (SELECT sha256,count(*) n,max(size) size FROM files WHERE generation=? AND sha256 IS NOT NULL GROUP BY sha256 HAVING count(*)>1)',(generation,)).fetchone()
    hashed = db.execute('SELECT count(*) FROM files WHERE generation=? AND sha256 IS NOT NULL',(generation,)).fetchone()[0]
    db.execute('DELETE FROM files WHERE generation NOT IN (SELECT generation FROM generations ORDER BY created_at DESC, rowid DESC LIMIT ?)',(generations_kept,))
    db.execute('DELETE FROM generations WHERE generation NOT IN (SELECT generation FROM generations ORDER BY created_at DESC, rowid DESC LIMIT ?)',(generations_kept,))
    db.commit()

    measured_duplicates = hashed > 0
    report={'pdf_count':count,'logical_pdf_bytes':total,'largest_20':[{'bytes':s,'source':p} for s,p in sorted(largest,reverse=True)],
            'size_histogram':[{'bin':label,'files':bins[(label,'files')],'bytes':bins[(label,'bytes')]} for label in BIN_LABELS],
            'hash_coverage_files':hashed,
            'exact_duplicate_groups':duplicates[0] if measured_duplicates else None,
            'duplicate_extra_files':(duplicates[1] or 0) if measured_duplicates else None,
            'duplicate_logical_bytes':(duplicates[2] or 0) if measured_duplicates else None,
            'duplicates_measured':measured_duplicates,
            'sample_size':len(results),'sample_seed':seed,'sample_allocation':plan,
            'strata':strata,
            'sample_categories':[{'category':k,'files':v,'sampled_bytes':group_bytes[k]} for k,v in groups.items()],
            'category_byte_estimates':stratified_estimates(per_bin, byte_totals, counts),
            'sample_documents':results,'scan_errors':errors,
            'limitations':['Category byte shares are stratified estimates with 95% intervals, not a census; bins marked insufficient carry too few samples to interpret.',
                           'Page sampling can miss scanned sections; /Font is not a born-digital test.',
                           'Effective DPI covers directly placed image XObjects only; Form XObject scans are counted as low_text_form_candidate, not measured for DPI.',
                           'Duplicate logical bytes are not physical reclaimable bytes (hardlinks/CoW may share storage).',
                           'No CDC savings or compression ratios are inferred.']}
    (output/'profile.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    dup_line = (f"Exact duplicate extra copies: {report['duplicate_extra_files']:,}; "
                f"logical duplicate bytes: {report['duplicate_logical_bytes']:,}."
                if measured_duplicates else
                'Exact duplicates: not measured (run with --hash-all).')
    lines=['# PDF storage profile','',f'PDFs: {count:,}; logical bytes: {total:,}; hashed: {hashed:,}.','',
           '| Size bin (upper bound) | Files | Bytes | Sampled | Note |','|---|---:|---:|---:|---|']
    for s in strata:
        note = 'censused' if s['censused'] else ('insufficient sample' if s['insufficient'] else '')
        lines.append(f"| {s['bin']} | {s['files']:,} | {s['bytes']:,} | {s['sampled_files']:,} | {note} |")
    if report['category_byte_estimates']:
        lines += ['','## Estimated byte share by category','','| Category | Estimated bytes | Share | 95% interval |','|---|---:|---:|---|']
        lines += [f"| {e['category']} | {e['estimated_bytes']:,} | {e['estimated_share']:.1%} | ±{e['ci95_share']:.1%} |"
                  for e in report['category_byte_estimates']]
    lines += ['',dup_line,'','## Limits',''] + ['- '+x for x in report['limitations']]
    (output/'profile.md').write_text('\n'.join(lines)+'\n')
    db.close()
    return report


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('input',type=Path); p.add_argument('--output',type=Path,required=True)
    p.add_argument('--sample',type=int,default=50,help='total inspection budget, allocated across size bins in proportion to their bytes')
    p.add_argument('--min-bin-sample',type=int,default=MIN_BIN_SAMPLE,help='floor per non-empty size bin; bins below it are flagged insufficient')
    p.add_argument('--seed',type=int,default=42)
    p.add_argument('--page-sample',type=int,default=3,help='minimum pages per document; grows with page count')
    p.add_argument('--timeout',type=int,default=120)
    p.add_argument('--generations-kept',type=int,default=GENERATIONS_KEPT,help='census generations retained in profile.sqlite')
    p.add_argument('--hash-all',action='store_true',help='Read all PDF bytes; reuse matching stat fingerprints on resume')
    a=p.parse_args(argv)
    if min(a.sample,a.page_sample,a.timeout,a.min_bin_sample,a.generations_kept)<=0:
        p.error('sample, min-bin-sample, page-sample, timeout and generations-kept must be positive')
    report=profile(a.input,a.output,a.sample,a.seed,a.hash_all,a.page_sample,a.timeout,a.min_bin_sample,a.generations_kept)
    print(json.dumps({k:report[k] for k in ('pdf_count','logical_pdf_bytes','hash_coverage_files','duplicate_extra_files','sample_size')}))
    return 0
