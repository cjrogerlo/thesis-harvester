# Storage proposal: what is useful and what still needs measurement

The useful design is to separate an immutable PDF archive from a much smaller,
versioned working corpus (Markdown, source/rights metadata, retrieval chunks and
optional figures). Profile first. Do not recompress originals to make a budget
spreadsheet balance.

The proposed 2–8% outer-compression savings, 10–30% deduplication savings,
30 TB → 8–15 TB archive, 40–60 GB working layer, and fixed Parquet compression
multipliers are **unmeasured hypotheses for this corpus**, not engineering
acceptance criteria. PDF text/scan ratios, figure retention and duplicated
serializations change these numbers substantially.

[Borg documents content-defined chunk deduplication](https://borgbackup.readthedocs.io/en/stable/).
It matches byte chunks. Semantic overlap between a chapter PDF and a book PDF
is not evidence that their encoded streams contain matching chunks. Estimate
CDC savings with a representative backup pilot before buying storage around it.
Exact SHA-256 duplicate bytes can be measured independently; they are logical
bytes, not necessarily physical reclaimable bytes on hardlinked/CoW storage.

`/Font` and selectable text cannot establish born-digital origin: scanned pages
can contain an OCR text layer. The profiler therefore reports text-density and
image candidates. DPI uses image pixel dimensions and the PDF image placement
matrix; inline images and nested form transforms are outside current coverage.
Sampling three pages cannot certify all pages in a long thesis.

`--optimize 3` should not be described as automatically enabling lossy JBIG2.
[Historical OCRmyPDF documentation](https://ocrmypdf.readthedocs.io/en/v9.1.0/optimizer.html)
distinguishes aggressive/lossy image optimization from the separate
`--jbig2-lossy` opt-in. [Current upstream JBIG2 documentation](https://github.com/ocrmypdf/OCRmyPDF/blob/main/docs/jbig2.md)
describes lossless JBIG2 and removal of the former lossy mode. Check the exact
installed version. This pipeline uses `--optimize 0` on temporary OCR copies
and performs no archive recompression or downsampling.

Private storage and private GitHub publication do not modify paper licence
metadata. The metadata/licence filter remains available for a later training
policy. Unknown licence evidence stays unknown.
