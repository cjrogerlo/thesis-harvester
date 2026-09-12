"""Command line interface. No network writes unless --remote or upload is supplied."""
import argparse
from collections import Counter
import gzip
import json
from pathlib import Path
import sys
import time

from .pipeline import Config, Store, extract, pdf_paths, sample_paths, sparse_pages


def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


def positive(value):
    n = int(value)
    if n <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return n


def nonnegative(value):
    n = int(value)
    if n < 0:
        raise argparse.ArgumentTypeError("must be nonnegative")
    return n


def parser():
    p = argparse.ArgumentParser(description="PDF -> page-anchored JSONL, OCR queue, optional GROBID and Drive")
    subs = p.add_subparsers(dest="action", required=True)
    for action in ("audit", "run"):
        q = subs.add_parser(action)
        q.add_argument("input", type=Path)
        q.add_argument("--backend", choices=("auto", "pdftotext", "pypdf", "docling"), default="auto")
        q.add_argument("--timeout", type=positive, default=300)
        q.add_argument("--min-chars", type=positive, default=40)
        if action == "audit":
            q.add_argument("--sample", type=positive, default=50)
            q.add_argument("--seed", type=int, default=42)
            q.add_argument("--report", type=Path)
        else:
            q.add_argument("--output", type=Path, required=True)
            q.add_argument("--ocr", choices=("defer", "auto"), default="defer")
            q.add_argument("--language", default="eng")
            q.add_argument("--ocr-timeout", type=positive, default=1800)
            q.add_argument("--chunk-chars", type=positive, default=6000)
            q.add_argument("--overlap", type=nonnegative, default=200)
            q.add_argument("--grobid-url")
            q.add_argument("--tokenizer", help="Optional tiktoken encoding, e.g. cl100k_base; install .[tokens]")
            q.add_argument("--chunk-tokens", type=positive, default=1000)
            q.add_argument("--profile-tag", default="default", help="Change after upgrading external extractors/models")
            q.add_argument("--limit", type=positive, help="Maximum newly processed/error documents per pass")
            q.add_argument("--watch", action="store_true")
            q.add_argument("--interval", type=positive, default=30)
            q.add_argument("--settle-seconds", type=nonnegative, default=60)
            q.add_argument("--rehash", action="store_true", help="Hash PDFs again instead of trusting stat fingerprints")
            q.add_argument("--remote", help="Explicit rclone destination, e.g. gdrive:Thesis Text")
            q.add_argument("--rclone", default="rclone")
            q.add_argument("--upload-timeout", type=positive, default=600)
    q = subs.add_parser("upload", help="Retry local ready bundles even if source PDFs are no longer present")
    q.add_argument("--output", type=Path, required=True)
    q.add_argument("--remote", required=True)
    q.add_argument("--rclone", default="rclone")
    q.add_argument("--upload-timeout", type=positive, default=600)
    q = subs.add_parser("export", help="Stream ready corpus into sharded JSONL or Parquet")
    q.add_argument("--output", type=Path, required=True)
    q.add_argument("--destination", type=Path, required=True)
    q.add_argument("--kind", choices=("documents", "chunks"), default="documents")
    q.add_argument("--format", choices=("jsonl", "parquet"), default="jsonl")
    q.add_argument("--license", action="append", default=[])
    q.add_argument("--profile")
    q.add_argument("--role", choices=("body", "abstract", "references", "acknowledgements", "appendix", "front_or_unclassified"))
    q.add_argument("--shard-rows", type=positive, default=1000)
    q = subs.add_parser("dedup", help="Report lexical near-duplicate candidates; never delete")
    q.add_argument("--output", type=Path, required=True)
    q.add_argument("--report", type=Path, required=True)
    q.add_argument("--profile")
    q.add_argument("--max-pairs", type=positive, default=100000)
    q = subs.add_parser("status")
    q.add_argument("--output", type=Path, required=True)
    return p


def audit(args):
    paths, population = sample_paths(args.input, args.sample, args.seed)
    rows = []
    for path in paths:
        started = time.monotonic()
        try:
            pages, backend = extract(path, "auto" if args.backend == "docling" else args.backend, args.timeout)
            raw = "\f".join(pages).encode("utf-8")
            candidates = sparse_pages(pages, args.min_chars)
            row = {"source": str(path), "backend": backend, "pages": len(pages),
                   "ocr_candidate_pages": candidates, "text_bytes": len(raw),
                   "text_gzip_bytes": len(gzip.compress(raw, mtime=0)), "pdf_bytes": path.stat().st_size}
        except Exception as exc:
            row = {"source": str(path), "error": str(exc)}
        row["seconds"] = round(time.monotonic() - started, 3)
        rows.append(row)
    ok = [r for r in rows if "error" not in r]
    page_count = sum(r["pages"] for r in ok)
    report = {"discovered_pdfs": population, "sample_size": len(rows), "seed": args.seed,
              "successful": len(ok), "errors": len(rows) - len(ok),
              "candidate_document_fraction": sum(bool(r["ocr_candidate_pages"]) for r in ok) / len(ok) if ok else None,
              "candidate_page_fraction": sum(len(r["ocr_candidate_pages"]) for r in ok) / page_count if page_count else None,
              "mean_seconds": sum(r["seconds"] for r in ok) / len(ok) if ok else None,
              "mean_text_gzip_bytes": sum(r["text_gzip_bytes"] for r in ok) / len(ok) if ok else None,
              "note": "Uniform reservoir sample of discovered local PDFs. Sparse text is an OCR candidate heuristic; blank/figure pages are included. Sizes exclude JSONL/TEI.",
              "documents": rows}
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    emit(report)
    return int(not rows or len(ok) != len(rows))


def run(args):
    if args.overlap >= args.chunk_chars:
        raise ValueError("--overlap must be smaller than --chunk-chars")
    # Outputs nested in the watched tree would cause recursive ingestion of temporary PDFs.
    source, output = args.input.resolve(), args.output.resolve()
    if source.is_dir() and output.is_relative_to(source):
        raise ValueError("--output must be outside the input tree")
    if not source.exists():
        raise ValueError(f"Input does not exist: {source}")
    if args.backend == "docling" and not args.tokenizer:
        args.tokenizer = "cl100k_base"
    import importlib.util
    import shutil
    required = ["pypdf"]
    if args.backend == "docling":
        required.append("docling")
    if args.tokenizer:
        required.append("tiktoken")
    missing = [name for name in required if importlib.util.find_spec(name) is None]
    if missing:
        raise ValueError("Missing dependencies: " + ", ".join(missing) + "; install the matching extras")
    if args.backend == "pdftotext" and not shutil.which("pdftotext"):
        raise ValueError("pdftotext is not installed")
    config = Config(**{key: getattr(args, key) for key in Config.__dataclass_fields__ if hasattr(args, key)})
    store = Store(output)
    try:
        with store.lock():
            while True:
                counts = Counter()
                attempted = 0
                for path in pdf_paths(source):
                    try:
                        if time.time() - path.stat().st_mtime < args.settle_seconds:
                            counts["unsettled"] += 1
                            continue
                        result = store.process(path, config, args.rehash)
                        counts["cached" if result["cached"] else result["status"]] += 1
                        attempted += int(not result["cached"])
                        event = {"source": str(path), "document_id": result["document_id"],
                                 "status": result["status"], "profile": result["profile"], "cached": result["cached"],
                                 "low_text_pages": result["low_text_pages"]}
                        if args.remote and result["status"] == "ready":
                            event["upload"] = store.upload(result["document_id"], result["profile"],
                                                           args.remote, args.rclone, args.upload_timeout)
                        if not result["cached"] or args.remote:
                            emit(event)
                    except Exception as exc:
                        counts["error"] += 1
                        attempted += 1
                        emit({"source": str(path), "status": "error", "error": str(exc)})
                    if args.limit and attempted >= args.limit:
                        break
                emit({"type": "pass_summary", "counts": dict(counts), "profile": config.profile})
                if not args.watch:
                    return int(counts["error"] > 0)
                time.sleep(args.interval)
    finally:
        store.db.close()


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.action == "audit":
            return audit(args)
        if args.action == "run":
            return run(args)
        if not (args.output / "state.sqlite").is_file():
            raise ValueError("No existing state.sqlite in --output")
        store = Store(args.output)
        try:
            if args.action == "status":
                emit({"jobs": [dict(r) for r in store.db.execute(
                    "SELECT profile,status,count(*) AS count FROM jobs GROUP BY profile,status")],
                      "upload_receipts": store.db.execute("SELECT count(*) FROM uploads").fetchone()[0]})
                return 0
            if args.action in ("export", "dedup"):
                from .export import export_corpus, duplicate_candidates
                with store.lock():
                    if args.action == "export":
                        emit(export_corpus(store, args.destination, args.kind, args.format,
                                           args.shard_rows, args.license, args.profile, args.role))
                    else:
                        args.report.parent.mkdir(parents=True, exist_ok=True)
                        emit(duplicate_candidates(store, args.report, args.profile, args.max_pairs))
                return 0
            errors = 0
            with store.lock():
                for row in store.db.execute("SELECT document_id,profile FROM jobs WHERE status='ready'"):
                    try:
                        result = store.upload(row["document_id"], row["profile"], args.remote,
                                              args.rclone, args.upload_timeout)
                        emit({**dict(row), "upload": result})
                    except Exception as exc:
                        errors += 1
                        emit({**dict(row), "upload": "error", "error": str(exc)})
            return int(errors > 0)
        finally:
            store.db.close()
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
