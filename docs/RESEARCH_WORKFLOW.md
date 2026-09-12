# From existing downloads to local research evidence

The commands below keep paper content local. A GitHub code repository is separate from a corpus release.

## 1. Bind a stable PDF snapshot to its manifest

```bash
pdf-corpus link --manifest /path/to/manifest.jsonl \
  --pdf-root /path/to/stable-pdfs --database output/bindings.sqlite --hash-files
```

Apollo records are joined by document UUID, original or recorded stored filename, and exact recorded download size. A match by basename alone is never used. Hashing is optional but recommended for release snapshots. Missing/ambiguous/size-mismatched PDFs remain unbound; inspect the adjacent `.report.json`. Generic records can provide `pdf_path` relative to the PDF root and a `metadata` object, e.g. `{"pdf_path":"paper.pdf","metadata":{"title":"A study","license":"unknown","source_url":"https://example.org/item"}}`.

The binding stores the manifest hash/line, rights evidence, source URL and file fingerprint. A moved or changed PDF requires a new binding snapshot. Run against stable local copies when a downloader/uploader may evict files. This is a snapshot join, not a live manifest subscription. Explicit `--bindings` refuses an unbound file rather than extracting it with guessed metadata.

## 2. Produce an inexpensive baseline

```bash
pdf-corpus run /path/to/stable-pdfs --output output/fast \
  --bindings output/bindings.sqlite --backend pypdf --tokenizer cl100k_base
```

Every document and chunk receives the bound paper metadata. Missing licence evidence stays unknown; open access alone is not treated as a licence. Metadata changes produce new extraction profiles.

## 3. Search with paper and page citations

```bash
pdf-corpus index --output output/fast --database output/research.sqlite
pdf-corpus search 'FAMIN T cell priming' --database output/research.sqlite \
  --limit 5 --role body --report output/evidence.md
```

The SQLite FTS5 index checks bundle hashes and defaults to mechanically ready documents. Explicit `--include-needs-review` also indexes readable, anchored chunks from `needs_ocr`/`needs_review` documents; flagged low-text pages are excluded and processing status remains visible. This does not change export/upload eligibility. `--license` and `--role` filter exact metadata values.

Results contain verbatim source chunks, highlighted excerpts, document/chunk IDs, metadata, physical PDF page numbers and a page link when the download URL is recorded. Physical page numbers can differ from printed page labels. The local original PDF path may cease to exist after staging eviction. Preserve source archives separately.

Retrieval uses lexical BM25 with literal OR query terms, English/Unicode word tokenization and Chinese bigrams. It returns at most one chunk per document/page range from a bounded candidate pool. It is not semantic retrieval or a generated answer. Exact wording works best; broad topic queries may surface cover pages or references. `--role body` helps only where the extraction correctly recognized roles. Search relevance, section detection, and transcription accuracy still need evaluation.

Index filenames are immutable snapshots: use a new filename to rebuild. Multiple extraction versions for one PDF require `--profile`, or separate output directories for each release. Per-paper metadata hashes mean profiles may differ between papers; separate release directories are the simpler default. Failed builds can leave a `.tmp` diagnostic file; select a new output name when retrying.

## 4. Review selected complex pages before scaling

```bash
# Use a Python environment installed with .[structured]. Weights download on first use.
PYTHONPATH=src python -m pdf_text_pipeline.review /path/to/paper.pdf \
  --start 42 --end 43 --output output/review-table --render --timeout 900
```

This bounded worker produces source SHA-256, a conversion log, raw-text baseline, original page images, Docling item JSON, figure assets and Markdown with physical page anchors. Partial reviews stay outside the corpus ledger and never mark a full document ready. Review receipts say `needs_human_review`, not training approved. Compare row/column associations, negative signs, exponents, symbols, caption placement, and missing text against the images. A successful model call alone is not quality acceptance.

For the complete structured corpus, use `pdf-corpus run ... --backend docling --bindings ...` after validating representative scanned, two-column, table and equation cases. A four-document convenience pilot is not representative of an entire archive.
