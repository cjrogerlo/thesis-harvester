# Thesis Harvester

Research corpus codebase: metadata harvest → PDF profiling → structured Markdown → training documents / RAG chunks → optional Drive upload.

**The code does not confer reuse rights.** Every paper and RAG chunk carries its source metadata and `license`; absent evidence stays `unknown`. Private storage does not change these fields. No PDF compression, deletion, live crawl, cloud upload, or GitHub publication happens during installation or tests.

## Install

Python 3.10+ on macOS/Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[tokens,parquet]'
```

For the structured conversion backend:

```bash
python -m pip install -e '.[structured,parquet]'
```

Docling runs locally and downloads its model weights on first use. It enables OCR, table structure and formula enrichment; formula/table accuracy still requires a representative human review. It is optional and is not installed by the lightweight package. Poppler (`pdftotext`) is optional for fast extraction; `pypdf` is the fallback. The separate OCR path requires `ocrmypdf`, its OCR engine and requested language packs. Drive upload requires an independently configured `rclone`.

## The two outputs

| Use | Files | Contract |
|---|---|---|
| Training / human review | `document.md.gz`, `metadata.jsonl.gz` | Full Markdown; exactly one metadata row per paper version. Role files separate body, abstract, acknowledgements, references and unclassified front matter when identifiable. |
| Retrieval | `llm_chunks.jsonl.gz` | `id`, `text`, `metadata`: section path, page anchor, paper metadata, quality flags, tokenizer and text token count. |
| Evidence / reprocessing | `fulltext.txt.gz`, `records.jsonl.gz`, `cleanup.jsonl.gz` | Original extracted pages, raw-page character spans or Docling item coordinates, and a record of removed margins. |
| Structured source | `docling.json.gz`, figure PNGs; optional `structure.tei.xml.gz` | Docling item graph and separately stored figures; GROBID TEI for bibliographic structure. |
| Integrity | `manifest.json`, local `state.sqlite` | Content checksums, extraction configuration, status, source aliases and upload receipts. |

The Docling backend preserves reading order from its document model, serializes formulas as LaTeX and tables as HTML (including merged cells), and retains captions with separate figure assets. These are **model extractions, not verified transcriptions**. The fast backend only cleans existing text: it cannot reconstruct broken columns, formulas or tables; its quality tier is `fast_text_unreviewed`.

Default structured chunks use `cl100k_base` with a **1,000-token maximum for the text field**. Adjacent short blocks are packed within the same page and section; short pages, section tails and isolated elements may remain below 500 tokens. Metadata/context added by a RAG application needs an additional prompt budget. Oversized table/formula fragments carry a flag linking them back to full Markdown. Set the tokenizer to match the model being trained or queried; `cl100k_base` is not universal.

## First measure the PDFs

```bash
python profile_pdfs.py /Volumes/Archive/PDFs --output output/profile --sample 100
# Optional full-byte hash pass, potentially hours/days on a large archive:
python profile_pdfs.py /Volumes/Archive/PDFs --output output/profile --sample 100 --hash-all

pdf-corpus audit /Volumes/Archive/PDFs --sample 100 --report output/text-audit.json
```

The profiler counts logical bytes across the entire selected tree and reports size bins, largest 20 files, SHA-256 duplicates when hashed, sampled text density and directly placed image DPI. Hashes resume using size/mtime/ctime fingerprints. It does not follow directory symlinks, infer physical disk savings, or classify an entire PDF from `/Font` alone. Reading a cloud placeholder may cause its normal download: point it at a local/archive directory when that is undesirable.

Text audit measures extraction time and compressed text size on a seeded uniform sample. Sparse text is an **OCR candidate**, including blank/figure pages; text presence can also come from earlier OCR. Neither command projects an unmeasured 30 TB corpus down to a claimed size.

## Source and licence metadata

Place `paper.pdf.metadata.json` next to `paper.pdf`, using the schema in [docs/metadata.example.json](docs/metadata.example.json). A harvester's record can be used as a sidecar: `repo`, `oai_identifier`, `landing_url`, `licence_class`, `rights` and `rights_uri` are recognized. Associate the record with the PDF explicitly; the code does not guess by title or filename.

Language is preserved from source metadata; missing language is `und`, not a guessed English label. Missing licence is `unknown`; `open` is not substituted for a licence. A changed metadata sidecar produces a new cache profile, so stale rights metadata is not silently reused.

## Bind existing downloads and search locally

`link` joins a stable PDF snapshot to an Apollo download manifest (UUID, filename, size and optional SHA-256). `run --bindings` carries its source/licence evidence into every document and chunk. `index` and `search` provide local BM25 retrieval with physical PDF page citations. A bounded Docling review command compares selected pages against original page images without declaring a whole paper ready.

See [docs/RESEARCH_WORKFLOW.md](docs/RESEARCH_WORKFLOW.md) for the complete commands and [docs/PILOT_20260912.md](docs/PILOT_20260912.md) for the real-paper pilot and observed quality limits.

## Convert

```bash
# Production-format path; start with a bounded pilot and inspect the Markdown:
pdf-corpus run /Volumes/Archive/PDFs --output output/corpus \
  --backend docling --limit 10 --chunk-tokens 1000

# Lightweight text baseline / OCR queue:
pdf-corpus run /Volumes/Archive/PDFs --output output/fast \
  --backend auto --ocr defer --limit 100

# Dedicated OCR worker, preserving source PDFs:
pdf-corpus run /Volumes/Archive/PDFs --output output/ocr \
  --backend pypdf --ocr auto --language eng --limit 10

# Optional bibliographic structure from your GROBID service:
pdf-corpus run /Volumes/Archive/PDFs --output output/grobid \
  --grobid-url http://localhost:8070
```

`extract_text.py` accepts the same commands without installing the console entry point.

The OCR path snapshots PDFs and OCRs only sparse candidate pages with `--optimize 0`; it never overwrites originals. A page still below the text threshold remains `needs_review`. Blank pages can cause conservative false positives. No file with `needs_ocr`, `needs_review` or `error` is automatically uploaded/exported as ready. `ready` means mechanically complete, not approved for training or human-verified.

For Docling, a fresh pypdf/Poppler extraction is retained as raw evidence. GROBID is optional and receives a PDF only when its URL is supplied; external URLs transmit the PDF to that service. Crossref/OpenAlex enrichment, citation graph construction, Marker/MinerU adapters and hosted model inference are not implemented.

## Work alongside downloads

```bash
pdf-corpus run /Volumes/Archive/PDFs --output output/fast --watch --interval 30
```

Only finalized `.pdf` filenames older than 60 seconds are considered. Configure downloaders to write `.part` and atomically rename to `.pdf` when complete. Size/mtime/ctime is checked around the snapshot, but inactivity alone cannot prove a download is complete. The input directory must be outside the output directory. One process owns each output directory through a file lock; use separate outputs for a fast worker and a slow Docling/OCR worker. No modification to existing running harvesters is needed.

Reruns verify artifact hashes and skip valid bundles. `--rehash` reads original PDFs again; `--profile-tag` should change after an external model/tool upgrade. The current implementation processes one document at a time; it is not a distributed million-document scheduler. Rerun errors with the same command; a missing PDF after staging eviction cannot be extracted until made available again.

## Export / deduplication

```bash
pdf-corpus export --output output/corpus --destination output/train-v1 \
  --kind documents --format parquet --role body --license cc-by
pdf-corpus export --output output/corpus --destination output/rag-v1 \
  --kind chunks --format parquet
pdf-corpus dedup --output output/corpus --report output/duplicate-candidates.jsonl
```

Exports use stable Arrow column types and Zstandard compression, capped at 1,000 rows or approximately 32 MiB of text/metadata per shard (a single unusually large row can exceed that). JSONL remains available with `--format jsonl`. `license`, `language`, `source_url`, `quality_tier`, page numbers and section paths are individually queryable columns. Detailed metadata is retained as JSON. Figure assets remain in the per-paper bundles; copying a Parquet shard alone does not copy those images.

The licence filter performs an exact metadata match, not legal adjudication. It can be repeated. No filter exports all ready records, including `unknown`, for inspection. Multiple ready versions of the same PDF require an explicit `--profile` selection; use a separate output directory for each corpus release to simplify version selection.

Exact PDF identity uses SHA-256; lexical near-duplicate candidates use normalized text SHA-256 and 64-bit SimHash with a four-band index and Hamming distance ≤3. Candidates are reported, never deleted. This is not semantic deduplication, paragraph-template removal, CDC archival deduplication or a training acceptance gate. Near-duplicate reports default to a 100,000-pair cap and report truncation.

## Optional upload

```bash
pdf-corpus upload --output output/corpus --remote 'gdrive:Thesis Text'
```

Only explicit `upload` or `run --remote` invokes rclone. Only ready, hash-verified text/structure/figure bundles are sent; PDFs and the local SQLite database stay local. The manifest is copied last and a receipt is committed only after all copy commands succeed. Retry works after source PDFs leave staging. A cached receipt records a past successful upload; it does not prove that a remote file still exists. Remote ACLs are managed by your rclone/Drive destination. Gzip is used for per-document bundles; Parquet uses Zstandard. Corpus compression ratios must be measured.

## Existing metadata collectors

The top-level `repo_harvest.py`, `caltech_thesis_harvest.py`, `build_repo_catalog.py` and `repos.json` are source snapshots copied from the existing workspace on 2026-09-12; the active originals were not changed. These collectors keep their original behaviors and are not certified by the new extraction tests. Live endpoint status has not been re-probed. `repos.json` is a registry, not a statement that every endpoint is currently reachable.

## Tests and maturity

```bash
python -m unittest discover -s tests -v
```

See [docs/VALIDATION.md](docs/VALIDATION.md) for exactly what was executed. Tests cover real selectable-text PDFs, sparse pages, interrupted/failed work, Unicode/token budgets, roles, provenance, licence filtering, duplicate candidates and Parquet round trips. OCR/GROBID/Docling model results require additional end-to-end pilots on representative real scans, two-column papers, equations and tables before training acceptance.

The optional `Publish to GitHub.command` creates a **public code repository** only when you run it. PDFs, generated corpora, credentials and caches are excluded from version control. Repository visibility does not grant reuse rights to papers.
