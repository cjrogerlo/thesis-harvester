"""Explicit source/licence metadata and auditable lightweight quality signals."""
import hashlib
import json
from pathlib import Path
import re
import unicodedata


def read_sidecar(source):
    path = Path(str(source) + ".metadata.json")
    row = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if not isinstance(row, dict):
        raise ValueError("Metadata sidecar must be a JSON object")
    # Do not infer permission from open access, institution or the PDF's contents.
    return {"source_file": Path(source).name, "source_url": row.get("source_url") or row.get("landing_url"),
            "repository": row.get("repository") or row.get("repo"),
            "source_identifier": row.get("source_identifier") or row.get("oai_identifier"),
            "title": row.get("title"), "authors": row.get("authors", []), "doi": row.get("doi"),
            "year": row.get("year"), "language": row.get("language") or "und",
            "language_method": "source_metadata" if row.get("language") else "unknown",
            "license": row.get("license") or row.get("licence_class") or "unknown",
            "license_url": row.get("license_url"), "rights": row.get("rights", []),
            "rights_uri": row.get("rights_uri", []),
            "license_evidence": row.get("license_evidence"),
            "metadata_sha256": hashlib.sha256(json.dumps(row, sort_keys=True, ensure_ascii=False).encode()).hexdigest()}


def content_role(section_path):
    title = " ".join(section_path).casefold()
    if re.search(r"reference|bibliograph|參考文獻", title):
        return "references"
    if re.search(r"acknowledg|dedication|致謝", title):
        return "acknowledgements"
    if re.search(r"abstract|摘要", title):
        return "abstract"
    if re.search(r"appendix|appendices|附錄", title):
        return "appendix"
    return "body" if section_path else "front_or_unclassified"


def text_signatures(text):
    normalized = " ".join(unicodedata.normalize("NFC", text).casefold().split())
    words = re.findall(r"\w+", normalized)
    # 64-bit SimHash: candidate discovery only, never automatic deletion.
    scores = [0] * 64
    for i in range(max(0, len(words) - 2)):
        h = int.from_bytes(hashlib.blake2b(" ".join(words[i:i+3]).encode(), digest_size=8).digest(), "big")
        for bit in range(64):
            scores[bit] += 1 if h & (1 << bit) else -1
    signature = sum((1 << bit) for bit, score in enumerate(scores) if score > 0)
    return {"normalized_text_sha256": hashlib.sha256(normalized.encode()).hexdigest(),
            "simhash64": f"{signature:016x}" if len(words) >= 3 else None}
