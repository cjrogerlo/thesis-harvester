"""Local FTS5/BM25 retrieval with immutable source and page citations. No model API."""
import gzip
import json
from pathlib import Path
import re
import sqlite3
import time

from .pipeline import valid_bundle, sha256


def query_terms(text, limit=64):
    # Strip FTS operators; user input is always data. Split Chinese runs into bigrams.
    terms = []
    for token in re.findall(r'[\u3400-\u9fff]+|[^\W_]+', text.casefold(), re.UNICODE):
        if re.fullmatch(r'[\u3400-\u9fff]+', token) and len(token)>1:
            terms.extend(token[i:i+2] for i in range(len(token)-1))
        else:
            terms.append(token)
    unique = list(dict.fromkeys(terms))
    return unique if limit is None else unique[:limit]


def build_index(store, database, include_needs_review=False, profile=None):
    """Build a new atomic index. A corpus release must choose one version per PDF."""
    database = Path(database).resolve()
    if database.exists():
        raise ValueError('Index exists; use a new filename for a new corpus snapshot')
    database.parent.mkdir(parents=True, exist_ok=True)
    temp = database.with_name(database.name + '.tmp')
    if temp.exists():
        raise ValueError('Unfinished index exists: ' + str(temp))
    conn = sqlite3.connect(temp)
    conn.executescript('CREATE VIRTUAL TABLE search USING fts5(title, body, terms, tokenize="unicode61");'
                       'CREATE TABLE chunks(rowid INTEGER PRIMARY KEY, chunk_id TEXT UNIQUE, document_id TEXT, profile TEXT, page_start INTEGER, page_end INTEGER, metadata TEXT, original_path TEXT);'
                       'CREATE TABLE indexed_documents(document_id TEXT PRIMARY KEY,profile TEXT,manifest_sha256 TEXT);'
                       'CREATE TABLE receipt(json TEXT);')
    statuses = ['ready'] + (['needs_ocr','needs_review'] if include_needs_review else [])
    sql = 'SELECT document_id,profile,status FROM jobs WHERE status IN ('+','.join('?'*len(statuses))+')'
    params = list(statuses)
    if profile:
        sql += ' AND profile=?'; params.append(profile)
    documents = chunks = excluded_low_text = 0
    try:
        with conn:
            for job in store.db.execute(sql+' ORDER BY document_id,profile', params):
                directory = store.directory(job['document_id'], job['profile'])
                manifest = valid_bundle(directory)
                if not manifest:
                    raise ValueError('Invalid bundle: '+str(directory))
                conn.execute('INSERT INTO indexed_documents VALUES (?,?,?)',
                             (job['document_id'],job['profile'],sha256(directory/'manifest.json')))
                source = store.db.execute('SELECT path FROM sources WHERE document_id=? ORDER BY path LIMIT 1',(job['document_id'],)).fetchone()
                original = source['path'] if source else None
                documents += 1
                with gzip.open(directory/'llm_chunks.jsonl.gz','rt',encoding='utf-8') as stream:
                    for line in stream:
                        row=json.loads(line); meta=row['metadata']; page=meta.get('page_start')
                        if not page or 'low_text_page' in meta.get('quality_flags',[]) or not row['text'].strip():
                            excluded_low_text += 1; continue
                        title=str(meta.get('paper',{}).get('title') or 'Untitled document')
                        # Keep original text intact; synthetic CJK tokens only live in the index.
                        terms=' '.join(query_terms(row['text'], limit=None)) if re.search(r'[\u3400-\u9fff]',row['text']) else ''
                        cur=conn.execute('INSERT INTO search(title,body,terms) VALUES (?,?,?)',(title,row['text'],terms))
                        conn.execute('INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?)',
                                     (cur.lastrowid,row['id'],job['document_id'],job['profile'],page,meta.get('page_end') or page,json.dumps(meta,ensure_ascii=False),original))
                        chunks += 1
            receipt={'documents':documents,'chunks':chunks,'excluded_low_text_or_unanchored':excluded_low_text,
                     'include_needs_review':include_needs_review,'profile':profile,'algorithm':'SQLite FTS5 BM25',
                     'created_at_unix':time.time(),'generative_model':False}
            conn.execute('INSERT INTO receipt VALUES (?)',(json.dumps(receipt),))
    except sqlite3.IntegrityError as exc:
        raise ValueError('Duplicate document/chunk version; select --profile or use one release per corpus directory') from exc
    finally:
        conn.close()
    temp.replace(database)
    return receipt


def search(database, query, limit=5, license=None, role=None):
    if not 1 <= limit <= 100:
        raise ValueError('limit must be between 1 and 100')
    terms=query_terms(query)
    if not terms:
        return {'query':query,'results':[],'method':'lexical BM25','note':'No searchable terms'}
    expression=' OR '.join('"'+t.replace('"','""')+'"' for t in terms)
    expression='{body terms} : (' + expression + ')'
    uri=Path(database).resolve().as_uri()+'?mode=ro'
    with sqlite3.connect(uri,uri=True) as db:
        db.row_factory=sqlite3.Row
        sql='SELECT chunks.*,search.body,snippet(search,1,"[", "]"," … ",40) AS excerpt,bm25(search,2.0,1.0,0.2) AS score FROM search JOIN chunks ON chunks.rowid=search.rowid WHERE search MATCH ?'
        params=[expression]
        if license:
            sql += " AND json_extract(chunks.metadata,'$.paper.license')=?";params.append(license)
        if role:
            sql += " AND json_extract(chunks.metadata,'$.content_role')=?";params.append(role)
        sql += ' ORDER BY score,chunks.rowid LIMIT ?';params.append(min(limit * 20, 2000))
        results=[]
        seen_pages=set()
        for row in db.execute(sql,params):
            page_key=(row['document_id'],row['page_start'],row['page_end'])
            if page_key in seen_pages:
                continue
            seen_pages.add(page_key)
            meta=json.loads(row['metadata']);paper=meta.get('paper',{});evidence=paper.get('binding_evidence') or {}
            pdf_url=evidence.get('content_url')
            citation={'document_id':row['document_id'],'chunk_id':row['chunk_id'],'profile':row['profile'],
                      'title':paper.get('title'),'authors':paper.get('authors',[]),'doi':paper.get('doi'),
                      'page_start':row['page_start'],'page_end':row['page_end'],'section_path':meta.get('section_path',[]),
                      'source_url':paper.get('source_url'),'pdf_page_url':pdf_url+'#page='+str(row['page_start']) if pdf_url else None,
                      'original_pdf':row['original_path'],'anchor':meta.get('anchor'),
                      'license':paper.get('license','unknown'),'quality_tier':meta.get('quality_tier'),
                      'processing_status':meta.get('status'),'quality_flags':meta.get('quality_flags',[])}
            results.append({'rank':len(results)+1,'score':row['score'],'excerpt':row['excerpt'],
                            'text':row['body'],'citation':citation})
            if len(results) >= limit:
                break
    return {'query':query,'method':'lexical BM25','results':results,
            'note':'Retrieved source excerpts, not a generated or fact-checked answer. PDF page numbers are physical page indices.'}


def render_results(result):
    lines=['# Research evidence: '+result['query'],'',result['note'],'']
    for hit in result['results']:
        c=hit['citation'];title=c['title'] or c['document_id']
        lines += [f"## [{hit['rank']}] {title} — PDF pp. {c['page_start']}–{c['page_end']}",'',hit['excerpt'],'']
        if c['pdf_page_url']: lines += [f"[Open PDF at page {c['page_start']}]({c['pdf_page_url']})",'']
        lines += [f"Licence: {c['license']}; extraction: {c['quality_tier']}; status: {c['processing_status']}.",'']
    return '\n'.join(lines)+'\n'
