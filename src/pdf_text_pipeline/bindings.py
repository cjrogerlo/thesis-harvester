"""Bind downloaded PDFs to manifest metadata without editing the PDF tree."""
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from .pipeline import fingerprint, pdf_paths, sha256


def strings(value):
    if value is None:
        return []
    return [str(x) for x in value] if isinstance(value, list) else [str(value)]


def license_from_evidence(rights, uris):
    # Only recognized explicit evidence. Open access is not a licence.
    text = ' '.join(strings(rights) + strings(uris)).casefold()
    for suffix in ('by-nc-nd','by-nc-sa','by-nc','by-nd','by-sa','by'):
        if re.search(r'creativecommons\.org/licenses/' + suffix + r'/', text):
            return 'cc-' + suffix
    if 'creativecommons.org/publicdomain/zero/' in text:
        return 'cc0'
    if 'all rights reserved' in text or 'all-rights-reserved' in text:
        return 'all-rights-reserved'
    return 'unknown'


def link_manifest(manifest, pdf_root, output, hash_files=False):
    """Accept Apollo downloaded_files rows or generic rows with explicit pdf_path."""
    manifest, root, output = Path(manifest).resolve(), Path(pdf_root).resolve(), Path(output).resolve()
    if output.exists():
        raise ValueError('Binding output already exists; use a new filename for a new snapshot')
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_name(output.name + '.tmp')
    if temp.exists():
        raise ValueError('Unfinished binding snapshot exists: ' + str(temp))
    db = sqlite3.connect(temp)
    db.row_factory = sqlite3.Row
    db.executescript('CREATE TABLE bindings(path TEXT PRIMARY KEY, relative_path TEXT UNIQUE, fingerprint TEXT, size INTEGER, sha256 TEXT, metadata TEXT, line INTEGER);'
                     'CREATE TABLE summary(json TEXT);')
    inventory = {}
    for path in pdf_paths(root):
        relative = path.relative_to(root).as_posix()
        inventory[relative] = path
    digest = sha256(manifest)
    errors = []
    rows_seen = 0
    try:
        with manifest.open(encoding='utf-8') as stream:
            for line_no, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                    if not isinstance(record, dict):
                        raise ValueError('Manifest row must be an object')
                    rows_seen += 1
                    if 'downloaded_files' in record:
                        identifier = str(record.get('uuid', ''))
                        files = []
                        for f in record['downloaded_files']:
                            if f.get('download_status') != 'downloaded' or not identifier:
                                continue
                            options = {f"{identifier}/{f.get('name', '')}"}
                            recorded = Path(f.get('path') or '')
                            if recorded.parent.name == identifier:
                                options.add(f"{identifier}/{recorded.name}")
                            present = sorted(options & inventory.keys())
                            if len(present) > 1:
                                raise ValueError('Ambiguous raw/stored PDF filenames: ' + identifier)
                            if present:
                                files.append((f, present[0]))
                        method = 'apollo_uuid_name_size'
                    elif record.get('pdf_path'):
                        files = [(record, str(record['pdf_path']))]
                        method = 'explicit_relative_path'
                    else:
                        continue
                    for item, relative in files:
                        # Never follow absolute/moved paths from the manifest or match by basename alone.
                        rel = Path(relative)
                        if rel.is_absolute() or '..' in rel.parts:
                            raise ValueError('pdf_path must be relative and remain inside pdf_root')
                        path = inventory.get(rel.as_posix())
                        if path is None:
                            continue
                        before = fingerprint(path)
                        size = path.stat().st_size
                        expected = item.get('sizeBytes', item.get('size_bytes'))
                        if expected is not None and size != int(expected):
                            raise ValueError(f'Size mismatch: {relative}')
                        if method == 'apollo_uuid_name_size' and expected is None:
                            raise ValueError(f'Missing download size: {relative}')
                        file_hash = sha256(path) if hash_files else None
                        if fingerprint(path) != before:
                            raise ValueError(f'PDF changed while binding: {relative}')
                        rights, uris = record.get('rights', []), record.get('rights_uri', [])
                        meta = dict(record.get('metadata', {})) if method == 'explicit_relative_path' else {}
                        meta.update({
                            'source_url': record.get('source_url') or record.get('uri') or record.get('landing_url') or meta.get('source_url'),
                            'repository': record.get('repository') or record.get('repo') or ('cambridge-apollo' if method.startswith('apollo') else meta.get('repository')),
                            'source_identifier': record.get('uuid') or record.get('oai_identifier') or meta.get('source_identifier'),
                            'title': record.get('title') or meta.get('title'),
                            'authors': record.get('authors') or meta.get('authors', []),
                            'doi': record.get('doi') or meta.get('doi'),
                            'year': str(record.get('year') or record.get('submitted') or meta.get('year') or '')[:4] or None,
                            'language': record.get('language') or meta.get('language') or 'und',
                            'license': record.get('license') or record.get('licence_class') or meta.get('license') or license_from_evidence(rights, uris),
                            'rights': strings(rights) or meta.get('rights', []),
                            'rights_uri': strings(uris) or meta.get('rights_uri', []),
                            'license_evidence': {'rights': strings(rights), 'rights_uri': strings(uris)},
                            'binding_evidence': {'manifest_sha256': digest, 'manifest_line': line_no,
                                                 'method': method, 'pdf_relative_path': relative,
                                                 'expected_bytes': expected, 'content_url': item.get('content_url'),
                                                 'download_status': item.get('download_status'),
                                                 'download_name': item.get('name'), 'recorded_download_path': item.get('path'),
                                                 'access_status': record.get('access_status'),
                                                 'cam_restriction': record.get('cam_restriction')},
                        })
                        db.execute('INSERT OR REPLACE INTO bindings VALUES (?,?,?,?,?,?,?)',
                                   (str(path), relative, before, size, file_hash, json.dumps(meta, ensure_ascii=False), line_no))
                except (ValueError, TypeError, KeyError, OSError) as exc:
                    errors.append({'line': line_no, 'error': str(exc)})
        matched = db.execute('SELECT count(*) FROM bindings').fetchone()[0]
        summary = {'manifest_sha256': digest, 'manifest_rows': rows_seen, 'local_pdfs': len(inventory),
                   'matched': matched, 'unmatched': len(inventory)-matched, 'errors': errors, 'hashed': hash_files,
                   'pdf_root': str(root)}
        db.execute('INSERT INTO summary VALUES (?)', (json.dumps(summary),))
        db.commit()
    finally:
        db.close()
    temp.replace(output)
    output.with_suffix('.report.json').write_text(json.dumps(summary, indent=2, ensure_ascii=False)+'\n')
    return summary


def bound_metadata(bindings, source):
    path = Path(source).resolve()
    uri = Path(bindings).resolve().as_uri() + '?mode=ro'
    with sqlite3.connect(uri, uri=True) as db:
        db.row_factory = sqlite3.Row
        row = db.execute('SELECT * FROM bindings WHERE path=?', (str(path),)).fetchone()
    if row is None:
        raise ValueError('No verified metadata binding for ' + str(path))
    if fingerprint(path) != row['fingerprint']:
        raise ValueError('PDF changed since binding; create a new binding snapshot')
    if row['sha256'] and sha256(path) != row['sha256']:
        raise ValueError('PDF hash no longer matches metadata binding')
    return json.loads(row['metadata'])
