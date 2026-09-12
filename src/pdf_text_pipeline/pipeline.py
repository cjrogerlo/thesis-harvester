"""Bounded, resumable extraction. Original PDFs are always read-only."""
from __future__ import annotations

import contextlib
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, replace

SCHEMA = 1
REVISION = "0.1.0"


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def fingerprint(path):
    stat = path.stat()
    return f"{stat.st_size}:{stat.st_mtime_ns}:{stat.st_ctime_ns}"


def command(args, timeout):
    # New process group: timeouts also terminate OCR child processes.
    proc = subprocess.Popen([str(a) for a in args], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, start_new_session=True)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except BaseException:
        import signal
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        raise
    if proc.returncode:
        raise RuntimeError(f"{Path(args[0]).name} exited {proc.returncode}: "
                           + stderr.decode("utf-8", "replace")[-1500:])
    return stdout


def extract(path, backend="auto", timeout=300):
    if backend == "auto":
        backend = "pdftotext" if shutil.which("pdftotext") else "pypdf"
    if backend == "pdftotext":
        raw = command(["pdftotext", "-enc", "UTF-8", path, "-"], timeout).decode("utf-8")
        pages = raw.split("\f")
        if pages and not pages[-1].strip():
            pages.pop()  # Remove delimiter tail, never interior blank pages.
    else:
        # Isolate the pure-Python fallback too, so one malformed PDF cannot stall a run.
        script = ("import json,sys; from pypdf import PdfReader; "
                  "r=PdfReader(sys.argv[1]); "
                  "print(json.dumps([p.extract_text() or '' for p in r.pages]))")
        pages = json.loads(command([sys.executable, "-c", script, path], timeout))
    if not pages:
        raise ValueError("PDF has no pages")
    return [p.replace("\r\n", "\n").replace("\x00", "") for p in pages], backend


def sparse_pages(pages, min_chars):
    # This is a text-density heuristic, NOT proof that a page is scanned.
    return [i for i, text in enumerate(pages, 1)
            if sum(c.isalnum() for c in text) < min_chars]


def spans(text, size, overlap):
    if size <= 0 or not 0 <= overlap < size:
        raise ValueError("require 0 <= overlap < chunk size")
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            boundary = text.rfind("\n\n", start + size // 2, end)
            if boundary > start:
                end = boundary + 2
        yield start, end, text[start:end]
        if end == len(text):
            break
        start = max(start + 1, end - overlap)


def parse_tei(xml):
    from defusedxml import ElementTree as ET
    ns = {"t": "http://www.tei-c.org/ns/1.0"}
    root = ET.fromstring(xml)
    if root.tag != "{http://www.tei-c.org/ns/1.0}TEI":
        raise ValueError("GROBID did not return TEI")

    def text(node):
        return "" if node is None else " ".join("".join(node.itertext()).split())

    def name(node):
        return " ".join(text(part) for part in node if text(part))

    def coordinates(node):
        result = []
        for group in node.get("coords", "").split(";"):
            if not group:
                continue
            try:
                p, x, y, w, h = group.split(",")
                result.append({"page": int(p), "x": float(x), "y": float(y),
                               "width": float(w), "height": float(h)})
            except (ValueError, TypeError):
                continue
        return result

    header = root.find("t:teiHeader", ns)
    title = text(header.find(".//t:titleStmt/t:title", ns)) if header is not None else ""
    authors = [] if header is None else [name(n) for n in header.findall(".//t:sourceDesc//t:author/t:persName", ns)]
    sections, paragraphs, references = [], [], []

    def walk(node, hierarchy):
        if node.tag.endswith("}div"):
            head = node.find("t:head", ns)
            if head is not None:
                hierarchy = hierarchy + [text(head)]
                sections.append({"path": hierarchy, "coords": coordinates(head)})
        if node.tag.endswith("}p"):
            paragraphs.append({"text": text(node), "section_path": hierarchy,
                               "coords": coordinates(node)})
            return
        for child in node:
            walk(child, hierarchy)

    body = root.find("t:text/t:body", ns)
    if body is not None:
        walk(body, [])
    for bibl in root.findall(".//t:listBibl/t:biblStruct", ns):
        references.append({"reference_id": bibl.get("{http://www.w3.org/XML/1998/namespace}id"),
                           "text": text(bibl), "title": text(bibl.find(".//t:title", ns)),
                           "doi": next((text(x) for x in bibl.findall(".//t:idno", ns)
                                        if x.get("type", "").lower() == "doi"), None),
                           "coords": coordinates(bibl)})
    return {"title": title, "authors": authors, "sections": sections,
            "paragraphs": paragraphs, "references": references}


def grobid(path, url, timeout):
    import requests
    data = [("consolidateHeader", "0"), ("consolidateCitations", "0")]
    data += [("teiCoordinates", tag) for tag in ("head", "p", "biblStruct")]
    for attempt in range(3):
        try:
            with open(path, "rb") as stream:
                response = requests.post(url.rstrip("/") + "/api/processFulltextDocument",
                                         files={"input": ("input.pdf", stream, "application/pdf")},
                                         data=data, timeout=(10, timeout))
            if response.status_code == 429 or response.status_code >= 500:
                raise requests.RequestException(f"GROBID HTTP {response.status_code}")
            response.raise_for_status()
            return response.content, parse_tei(response.content)
        except requests.RequestException:
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)


@dataclass(frozen=True)
class Config:
    backend: str = "auto"
    ocr: str = "defer"
    language: str = "eng"
    min_chars: int = 40
    chunk_chars: int = 6000
    overlap: int = 200
    timeout: int = 300
    ocr_timeout: int = 1800
    grobid_url: str | None = None
    profile_tag: str = "default"
    tokenizer: str | None = None
    chunk_tokens: int = 1000
    metadata_fingerprint: str = ""

    @property
    def profile(self):
        data = {"revision": REVISION, **asdict(self)}
        # auto backend must not silently reuse outputs made by another extractor.
        if self.backend == "auto":
            data["backend"] = "pdftotext" if shutil.which("pdftotext") else "pypdf"
        return hashlib.sha256(json_bytes(data)).hexdigest()[:16]


def write_gzip(path, blocks):
    with open(path, "wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", filename="", mtime=0) as stream:
            for block in blocks:
                stream.write(block)


def valid_bundle(directory):
    try:
        manifest = json.loads((directory / "manifest.json").read_text())
        files = manifest["files"]
        if not files or not {"records.jsonl.gz", "fulltext.txt.gz", "document.md.gz", "llm_chunks.jsonl.gz", "cleanup.jsonl.gz"} <= files.keys():
            return None
        for name, digest in files.items():
            if Path(name).name != name or sha256(directory / name) != digest:
                return None
        return manifest
    except (OSError, ValueError, KeyError, TypeError):
        return None


def build_bundle(snapshot, target, doc_id, config, source_metadata=None):
    started = time.monotonic()
    pages, backend = extract(snapshot, "auto" if config.backend == "docling" else config.backend, config.timeout)
    candidates = sparse_pages(pages, config.min_chars)
    initial_candidates = candidates[:]
    ocr_seconds = 0.0
    actual_pdf = snapshot
    structured = None
    asset_dir = None
    if config.backend == "docling":
        asset_dir = snapshot.parent / "docling"
        command([sys.executable, Path(__file__).with_name("docling_worker.py"), snapshot, asset_dir], config.ocr_timeout)
        structured = json.loads((asset_dir / "result.json").read_text())
        if structured["page_count"] != len(pages):
            raise ValueError("Docling page count disagrees with raw extraction")
        candidates = sparse_pages([" ".join(b["text"] for b in structured["blocks"] if n in b["pages"]) for n in range(1, len(pages) + 1)], config.min_chars)
        backend = "docling"
    if candidates and config.ocr == "auto" and not structured:
        if not shutil.which("ocrmypdf"):
            raise RuntimeError("OCR requested but ocrmypdf is not installed; use --ocr defer or install it")
        actual_pdf = snapshot.with_name("ocr.pdf")
        before = time.monotonic()
        command(["ocrmypdf", "--pages", ",".join(map(str, candidates)), "--force-ocr",
                 "--output-type", "pdf", "--optimize", "0", "--jobs", "1", "-l", config.language,
                 snapshot, actual_pdf], config.ocr_timeout)
        pages_after, backend = extract(actual_pdf, config.backend, config.timeout)
        if len(pages_after) != len(pages):
            raise ValueError("OCR changed the page count")
        pages = pages_after
        candidates = sparse_pages(pages, config.min_chars)
        ocr_seconds = time.monotonic() - before
    status = "needs_ocr" if candidates and config.ocr == "defer" and not structured else (
        "needs_review" if candidates else "ready")
    structure = None
    xml = None
    if config.grobid_url and status == "ready":
        xml, structure = grobid(actual_pdf, config.grobid_url, config.timeout)
    document = {"type": "document", "schema_version": SCHEMA, "document_id": doc_id,
                "profile": config.profile, "status": status, "page_count": len(pages),
                "backend": backend, "ocr_candidate_pages": initial_candidates,
                "low_text_pages": candidates, "ocr_applied": actual_pdf != snapshot,
                "docling_ocr_enabled": structured is not None,
                "structure_method": "grobid" if structure else "page_paragraph",
                "title": structure["title"] if structure else None,
                "authors": structure["authors"] if structure else [],
                "sections": structure["sections"] if structure else []}

    def records():
        yield json_bytes(document)
        for number, page in enumerate(pages, 1):
            yield json_bytes({"type": "page", "document_id": doc_id, "page": number,
                              "text": page, "low_text": number in candidates})
            for i, (start, end, chunk) in enumerate(spans(page, config.chunk_chars, config.overlap)):
                yield json_bytes({"type": "chunk", "document_id": doc_id,
                                  "chunk_id": f"{doc_id}:{config.profile}:p{number}:c{i}",
                                  "page_start": number, "page_end": number,
                                  "char_start": start, "char_end": end,
                                  "anchor": f"sha256:{doc_id}#page={number}",
                                  "section_path": [], "method": "page_paragraph", "text": chunk})
        if structure:
            for i, para in enumerate(structure["paragraphs"]):
                valid_coords = [c for c in para["coords"] if 1 <= c["page"] <= len(pages)]
                numbers = [c["page"] for c in valid_coords]
                for j, (start, end, chunk) in enumerate(spans(para["text"], config.chunk_chars, config.overlap)):
                    yield json_bytes({"type": "section_chunk", "document_id": doc_id,
                                      "chunk_id": f"{doc_id}:{config.profile}:s{i}:c{j}",
                                      "text": chunk, "section_path": para["section_path"],
                                      "char_start": start, "char_end": end,
                                      "page_start": min(numbers) if numbers else None,
                                      "page_end": max(numbers) if numbers else None,
                                      "anchor": f"sha256:{doc_id}#page={min(numbers)}" if numbers else None,
                                      "anchor_scope": "paragraph", "coords": valid_coords,
                                      "method": "grobid"})
            for ref in structure["references"]:
                yield json_bytes({"type": "reference", "document_id": doc_id, **ref})

    from .llm import prepare, prepare_structured, pack_chunks
    from .metadata import text_signatures
    if structured:
        markdown, llm_records, cleanup, roles = prepare_structured(structured["blocks"], doc_id, config.profile, config)
        document["structure_method"] = "docling"
    else:
        markdown, llm_records, cleanup, roles = prepare(pages, doc_id, config.profile, config, structure)
    llm_records = pack_chunks(llm_records, config)
    provenance = dict(source_metadata or {})
    if not provenance.get("title"):
        provenance["title"] = document["title"]
    if not provenance.get("authors"):
        provenance["authors"] = document["authors"]
    if provenance.get("title") and markdown.startswith("# PDF document\n"):
        markdown = markdown.replace("# PDF document\n", "# " + provenance["title"] + "\n", 1)
    document["metadata"] = provenance
    document["quality_tier"] = "structured_unreviewed" if structured else "fast_text_unreviewed"
    document.update(text_signatures("\n".join(b["text"] for b in structured["blocks"]) if structured else "\n".join(pages)))
    for row in llm_records:
        row["metadata"]["status"] = status
        row["metadata"]["paper"] = provenance
        row["metadata"]["quality_tier"] = document["quality_tier"]
        row["metadata"]["ocr_applied"] = actual_pdf != snapshot
        row["metadata"]["docling_ocr_enabled"] = structured is not None
        if row["metadata"]["page_start"] in candidates:
            row["metadata"]["quality_flags"].append("low_text_page")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".bundle-", dir=target.parent) as temp:
        staging = Path(temp)
        write_gzip(staging / "records.jsonl.gz", records())
        write_gzip(staging / "fulltext.txt.gz", (p.encode("utf-8") + b"\f" for p in pages))
        write_gzip(staging / "document.md.gz", [markdown.encode("utf-8")])
        write_gzip(staging / "llm_chunks.jsonl.gz", (json_bytes(r) for r in llm_records))
        write_gzip(staging / "cleanup.jsonl.gz", (json_bytes(r) for r in cleanup))
        names = ["records.jsonl.gz", "fulltext.txt.gz", "document.md.gz", "llm_chunks.jsonl.gz", "cleanup.jsonl.gz"]
        write_gzip(staging / "metadata.jsonl.gz", [json_bytes(document)])
        names.append("metadata.jsonl.gz")
        for role, content in roles.items():
            name = f"{role}.md.gz"
            write_gzip(staging / name, [content.encode("utf-8")])
            names.append(name)
        if asset_dir is not None:
            write_gzip(staging / "docling.json.gz", [(asset_dir / "docling.json").read_bytes()])
            names.append("docling.json.gz")
            for name in structured["assets"]:
                if Path(name).name != name:
                    raise ValueError("Invalid figure asset path")
                shutil.copyfile(asset_dir / name, staging / name)
                names.append(name)
        if xml is not None:
            write_gzip(staging / "structure.tei.xml.gz", [xml])
            names.append("structure.tei.xml.gz")
        manifest = {**document, "seconds": round(time.monotonic() - started, 3),
                    "ocr_seconds": round(ocr_seconds, 3), "pdf_bytes": snapshot.stat().st_size,
                    "text_bytes": sum(len(p.encode("utf-8")) for p in pages),
                    "compressed_bytes": sum((staging / n).stat().st_size for n in names),
                    "llm_chunk_count": len(llm_records), "removed_margin_lines": len(cleanup),
                    "files": {n: sha256(staging / n) for n in names}}
        # Commit marker last; readers verify every checksum before use/upload.
        target.mkdir(exist_ok=True)
        for n in names:
            os.replace(staging / n, target / n)
        (staging / "manifest.json").write_bytes(json_bytes(manifest))
        os.replace(staging / "manifest.json", target / "manifest.json")
    return manifest


class Store:
    def __init__(self, output):
        self.output = Path(output).resolve()
        self.output.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.output / "state.sqlite")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
          PRAGMA journal_mode=WAL;
          CREATE TABLE IF NOT EXISTS sources (
            path TEXT PRIMARY KEY, fingerprint TEXT, document_id TEXT);
          CREATE TABLE IF NOT EXISTS jobs (
            document_id TEXT, profile TEXT, status TEXT, error TEXT,
            PRIMARY KEY(document_id, profile));
          CREATE TABLE IF NOT EXISTS uploads (
            document_id TEXT, profile TEXT, remote TEXT, manifest_sha TEXT,
            uploaded_at REAL, PRIMARY KEY(document_id, profile, remote));
        """)

    def directory(self, doc_id, profile):
        return self.output / "documents" / doc_id[:2] / doc_id / profile

    @contextlib.contextmanager
    def lock(self):
        with open(self.output / ".lock", "a") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("Another process is using this output directory") from None
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def process(self, source, config, rehash=False, supplied_metadata=None):
        source = Path(source).resolve()
        from .metadata import read_sidecar
        metadata = read_sidecar(source, supplied_metadata)
        config = replace(config, metadata_fingerprint=metadata["metadata_sha256"])
        before = fingerprint(source)
        row = self.db.execute("SELECT * FROM sources WHERE path=?", (str(source),)).fetchone()
        doc_id = row["document_id"] if row and row["fingerprint"] == before and not rehash else None
        if doc_id:
            found = valid_bundle(self.directory(doc_id, config.profile))
            if found:
                self.db.execute("INSERT OR REPLACE INTO jobs VALUES (?,?,?,NULL)",
                                (doc_id, config.profile, found["status"]))
                self.db.commit()
                return {**found, "cached": True}
        with tempfile.TemporaryDirectory(prefix=".pdf-", dir=self.output) as temp:
            snapshot = Path(temp) / "input.pdf"
            shutil.copyfile(source, snapshot)
            if fingerprint(source) != before:
                raise RuntimeError("Source changed during copy; retry when download is complete")
            doc_id = sha256(snapshot)
            self.db.execute("INSERT OR REPLACE INTO sources VALUES (?,?,?)", (str(source), before, doc_id))
            self.db.commit()
            target = self.directory(doc_id, config.profile)
            found = valid_bundle(target)
            if found:
                self.db.execute("INSERT OR REPLACE INTO jobs VALUES (?,?,?,NULL)",
                                (doc_id, config.profile, found["status"]))
                self.db.commit()
                return {**found, "cached": True}
            try:
                manifest = build_bundle(snapshot, target, doc_id, config, metadata)
            except Exception as exc:
                self.db.execute("INSERT OR REPLACE INTO jobs VALUES (?,?,?,?)",
                                (doc_id, config.profile, "error", str(exc)))
                self.db.commit()
                raise
            self.db.execute("INSERT OR REPLACE INTO jobs VALUES (?,?,?,NULL)",
                            (doc_id, config.profile, manifest["status"]))
            self.db.commit()
            return {**manifest, "cached": False}

    def upload(self, doc_id, profile, remote, rclone="rclone", timeout=600):
        directory = self.directory(doc_id, profile)
        manifest = valid_bundle(directory)
        if not manifest or manifest["status"] != "ready":
            raise ValueError("Only complete, checksum-verified ready bundles can be uploaded")
        digest = sha256(directory / "manifest.json")
        destination = remote.rstrip("/")
        receipt = self.db.execute("SELECT manifest_sha FROM uploads WHERE document_id=? AND profile=? AND remote=?",
                                  (doc_id, profile, destination)).fetchone()
        if receipt and receipt["manifest_sha"] == digest:
            return "cached_receipt"
        prefix = f"{destination}/{doc_id[:2]}/{doc_id}/{profile}"
        for name in [*manifest["files"], "manifest.json"]:
            command([rclone, "copyto", directory / name, f"{prefix}/{name}", "--checksum",
                     "--retries", "3", "--low-level-retries", "3"], timeout)
        self.db.execute("INSERT OR REPLACE INTO uploads VALUES (?,?,?,?,?)",
                        (doc_id, profile, destination, digest, time.time()))
        self.db.commit()
        return "uploaded"

    def upload_pending(self, remote, rclone, timeout):
        # Separate retry path: works even after source PDFs leave the download staging area.
        for row in self.db.execute("SELECT document_id, profile FROM jobs WHERE status='ready'"):
            result = self.upload(row["document_id"], row["profile"], remote, rclone, timeout)
            yield {**dict(row), "upload": result}


def pdf_paths(root):
    root = Path(root).resolve()
    if root.is_file():
        if root.suffix.lower() != ".pdf":
            raise ValueError("Input file must have a .pdf suffix")
        yield root
        return
    if not root.is_dir():
        raise ValueError(f"Input does not exist: {root}")
    def traversal_error(exc):
        raise exc
    for directory, dirs, files in os.walk(root, followlinks=False, onerror=traversal_error):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in sorted(files):
            path = Path(directory) / name
            if path.suffix.lower() == ".pdf" and not path.is_symlink():
                yield path


def sample_paths(root, limit, seed):
    rng, sample = random.Random(seed), []
    seen = 0
    for seen, path in enumerate(pdf_paths(root), 1):
        if len(sample) < limit:
            sample.append(path)
        else:
            slot = rng.randrange(seen)
            if slot < limit:
                sample[slot] = path
    return sample, seen
