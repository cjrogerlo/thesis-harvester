# Validation receipt — 2026-09-12

Result: **28 tests discovered; 27 passed; 1 skipped** in the final local run.
The skipped test requires `pdftotext`, which is not installed on this host.
During the validation run, no live crawler, Drive upload, GitHub publication or archive recompression was run.

Command used in this development environment (Python 3.12 / Miniforge):

```bash
PYTHONPATH=src:.test-deps TIKTOKEN_CACHE_DIR=.test-cache \
  /Users/cjrogerlo/miniforge3/bin/python3 -m unittest discover -s tests -v
```

The optional tokenizer was installed into the ignored `.test-deps` directory;
its public vocabulary was cached in ignored `.test-cache`. Neither directory
belongs in the repository. Users should install the declared extras into a
virtual environment, then use the README test command.

Tested with actual libraries and synthetic PDF fixtures:

- pypdf 6.8.0 selectable-text extraction, page counts and preserved blank pages.
- Unicode-safe chunking with actual `cl100k_base` token counts, including literal special-token strings.
- SHA-256 identity, source snapshot race rejection, checksum corruption recovery and durable resume ledger.
- Repeated margins, attested hyphen repair, heading/role separation, raw-page spans and packed blocks.
- PyArrow 24.0.0 Parquet write/read with projection of licence, text and page columns.
- Licence filtering, metadata-change cache invalidation and near-duplicate text candidates.
- Read-only profiler inventory/hash counts and synthetic 300-DPI image placement.
- Timeout termination and output-directory lock exclusion.

Contract tests, not real model/service quality tests:

- OCRmyPDF command construction, sparse-page routing and successful OCR result ingestion use a mock OCR executable.
- GROBID TEI hierarchy, references and coordinates use a fixture; no live GROBID service was invoked.
- Docling bundle serialization uses a mocked worker result; HTML table spans, LaTeX, captions and role serialization use structured fixtures. Docling and its weights were not installed or executed.
- rclone upload failure/retry/manifest-last/receipt handling uses a mocked subprocess; no real remote ACL or object availability was checked.

Additional checks: Python compilation, CLI help and `zsh -n` for the publication
helper. The copied registry contains 75 entries. The four copied
collector/config files matched their source snapshots at the final check;
their live network behavior and current endpoint reachability were not tested.

Known boundaries:

- `ready` is mechanical completion. Both structured and fast output retain an unreviewed quality tier.
- Real two-column reading order, scan OCR, mathematical transcription, merged tables and captions require a held-out human-reviewed corpus pilot.
- Role classification is heuristic; front matter without recognizable headings may remain unclassified.
- Language is sourced from metadata or `und`; automatic language detection is not implemented.
- Near-duplicate candidates are lexical, not semantic or template-removal decisions.
- The worker is sequential and stores per-document bundles before optional Parquet export. Million-document throughput, process distribution, storage ratios and recovery under power loss have not been benchmarked.
- Directory locks and subprocess groups target macOS/Linux; native Windows is not supported.
