"""Streaming corpus export and bounded SimHash candidate discovery."""
import gzip
import json
from pathlib import Path
import sqlite3
import tempfile

from .pipeline import valid_bundle


def select_profile(store, profile):
    available = [r[0] for r in store.db.execute("SELECT DISTINCT profile FROM jobs WHERE status='ready'")]
    if profile:
        return profile
    if len(available) > 1:
        # Per-document metadata hashes produce different cache profiles. They are not
        # conversion versions; each PDF may still occur only once in a default export.
        duplicate = store.db.execute("SELECT document_id FROM jobs WHERE status='ready' GROUP BY document_id HAVING count(*)>1 LIMIT 1").fetchone()
        if duplicate:
            raise ValueError("Multiple ready versions of one PDF; use --profile to select a version")
    return None


def bundles(store, profile=None):
    profile = select_profile(store, profile)
    sql = "SELECT document_id,profile FROM jobs WHERE status='ready'"
    args = ()
    if profile:
        sql += " AND profile=?"
        args = (profile,)
    sql += " ORDER BY document_id,profile"
    for row in store.db.execute(sql, args):
        directory = store.directory(row["document_id"], row["profile"])
        manifest = valid_bundle(directory)
        if not manifest:
            raise ValueError(f"Invalid bundle: {directory}")
        yield directory, manifest


def corpus_rows(store, kind, licenses=(), profile=None, role=None):
    for directory, manifest in bundles(store, profile):
        paper = manifest.get("metadata", {})
        if licenses and paper.get("license", "unknown") not in licenses:
            continue
        common = {"document_id": manifest["document_id"], "profile": manifest["profile"],
                  "license": paper.get("license", "unknown"), "language": str(paper.get("language", "und")),
                  "source_url": paper.get("source_url"), "repository": paper.get("repository"),
                  "quality_tier": manifest["quality_tier"]}
        if kind == "documents":
            filename = f"{role}.md.gz" if role else "document.md.gz"
            if not (directory / filename).exists():
                continue
            with gzip.open(directory / filename, "rt", encoding="utf-8") as stream:
                markdown = stream.read()
            yield {**common, "id": manifest["document_id"] + ":" + manifest["profile"],
                   "text": markdown, "metadata_json": json.dumps(manifest, ensure_ascii=False),
                   "page_start": None, "page_end": None, "token_count": None,
                   "section_path": [], "content_role": role or "all"}
        else:
            with gzip.open(directory / "llm_chunks.jsonl.gz", "rt", encoding="utf-8") as stream:
                for line in stream:
                    row = json.loads(line)
                    meta = row["metadata"]
                    if role and meta["content_role"] != role:
                        continue
                    yield {**common, "id": row["id"], "text": row["text"],
                           "metadata_json": json.dumps(meta, ensure_ascii=False),
                           "page_start": meta.get("page_start"), "page_end": meta.get("page_end"),
                           "token_count": meta.get("token_count"), "section_path": meta["section_path"],
                           "content_role": meta["content_role"]}


def export_corpus(store, destination, kind="documents", format="jsonl", shard_rows=1000,
                  licenses=(), profile=None, role=None):
    destination = Path(destination)
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("Export destination must be empty; use a new directory for each export")
    if shard_rows <= 0:
        raise ValueError("shard_rows must be positive")
    destination.mkdir(parents=True, exist_ok=True)
    schema = None
    if format == "parquet":
        import pyarrow as pa
        import pyarrow.parquet as pq
        schema = pa.schema([(name, pa.string()) for name in (
            "document_id", "profile", "license", "language", "source_url", "repository",
            "quality_tier", "id", "text", "metadata_json", "content_role")]
            + [("page_start", pa.int32()), ("page_end", pa.int32()),
               ("token_count", pa.int32()), ("section_path", pa.list_(pa.string()))])
    shard, count, batch, batch_bytes = 0, 0, [], 0

    def flush():
        nonlocal shard, batch, batch_bytes
        suffix = "parquet" if format == "parquet" else "jsonl.gz"
        target = destination / f"{kind}-{shard:05d}.{suffix}"
        temp = target.with_suffix(target.suffix + ".tmp")
        if format == "parquet":
            pq.write_table(pa.Table.from_pylist(batch, schema=schema), temp, compression="zstd")
        else:
            with gzip.open(temp, "wt", encoding="utf-8") as stream:
                for row in batch:
                    stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        temp.replace(target)
        shard += 1
        batch, batch_bytes = [], 0

    for row in corpus_rows(store, kind, licenses, profile, role):
        batch.append(row)
        batch_bytes += len(row["text"].encode("utf-8")) + len(row["metadata_json"].encode("utf-8"))
        count += 1
        if len(batch) >= shard_rows or batch_bytes >= 32 * 1024 * 1024:
            flush()
    if batch:
        flush()
    receipt = {"rows": count, "shards": shard, "kind": kind, "format": format,
               "licenses": list(licenses), "profile": profile, "role": role,
               "note": "License values are source metadata, not a legal determination. Figure assets stay in the source bundles."}
    (destination / "export.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt


def duplicate_candidates(store, output, profile=None, max_pairs=100000):
    """4 x 16-bit band index retrieves all pairs with Hamming distance <=3.

    This is a lexical candidate heuristic, not a semantic duplication verdict.
    """
    count, truncated = 0, False
    with tempfile.TemporaryDirectory(dir=store.output, prefix=".dedup-") as temp:
        db = sqlite3.connect(Path(temp) / "bands.sqlite")
        db.executescript("CREATE TABLE signatures (id TEXT PRIMARY KEY, sig TEXT, hash TEXT);"
                         "CREATE TABLE bands (band INTEGER,value INTEGER,id TEXT);"
                         "CREATE INDEX band_lookup ON bands(band,value);")
        with open(output, "w", encoding="utf-8") as out:
            for _, manifest in bundles(store, profile):
                signature = manifest.get("simhash64")
                if signature is None:
                    continue
                value = int(signature, 16)
                seen = set()
                for band in range(4):
                    key = (value >> (16 * band)) & 65535
                    for old in db.execute("SELECT id FROM bands WHERE band=? AND value=?", (band, key)):
                        if old[0] in seen or old[0] == manifest["document_id"]:
                            continue
                        seen.add(old[0])
                        previous = db.execute("SELECT sig,hash FROM signatures WHERE id=?", (old[0],)).fetchone()
                        distance = (value ^ int(previous[0], 16)).bit_count()
                        if distance <= 3:
                            if count >= max_pairs:
                                truncated = True
                                break
                            out.write(json.dumps({"left": old[0], "right": manifest["document_id"],
                                                  "hamming_distance": distance,
                                                  "normalized_exact": previous[1] == manifest["normalized_text_sha256"],
                                                  "action": "review_candidate"}) + "\n")
                            count += 1
                    if truncated:
                        break
                if truncated:
                    break
                db.execute("INSERT INTO signatures VALUES (?,?,?)", (manifest["document_id"], signature, manifest["normalized_text_sha256"]))
                db.executemany("INSERT INTO bands VALUES (?,?,?)", [(b, (value >> (16*b)) & 65535, manifest["document_id"]) for b in range(4)])
                db.commit()
        db.close()
    return {"candidate_pairs": count, "truncated": truncated, "threshold": 3}
