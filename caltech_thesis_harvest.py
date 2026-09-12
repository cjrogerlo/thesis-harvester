#!/usr/bin/env python3
"""Harvest CaltechTHESIS metadata from the static site (it has no OAI-PMH or API any more).

Route: https://thesis.caltech.edu/sitemap.xml lists every record; each record page carries the
EPrints <meta name="eprints.*"> and Highwire <meta name="citation_*"> tags, which is all the
metadata the old OAI interface used to give. robots.txt on that host is "User-agent: * / Allow: /"
(its AI-crawler blocks are commented out, with the note that CaltechTHESIS is open access).

Writes repos_metadata/caltech/records.jsonl in the same shape as repo_harvest.py, plus the raw
<head> of each page (gzipped) for provenance. Resumable: re-running skips ids already recorded.

    python3 caltech_thesis_harvest.py --limit 3 --verbose     # dry run, show what was parsed
    python3 caltech_thesis_harvest.py                         # full run
"""
from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from repo_harvest import Http, HttpError, clean, classify_rights, log  # noqa: E402

SITE = "https://thesis.caltech.edu"
SITEMAP = f"{SITE}/sitemap.xml"
RECORD_PATH = re.compile(r"^/(\d+)/?$")
LOC_RE = re.compile(rb"<loc>([^<]+)</loc>")
CC_URL = re.compile(r"https?://creativecommons\.org/(?:licenses|publicdomain)/[a-z0-9\-./]+", re.I)


class MetaParser(HTMLParser):
    """Collect <meta name=... content=...>, <link rel=... href=...>, <title> and .pdf links."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, list[str]] = {}
        self.links: list[str] = []
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            name = (a.get("name") or a.get("property") or "").strip().lower()
            content = clean(a.get("content", ""))
            if name and content:
                self.meta.setdefault(name, []).append(content)
        elif tag == "link" and a.get("href"):
            self.links.append(a["href"])
        elif tag == "a" and a.get("href"):
            self.links.append(a["href"])
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title and not self.title:
            self.title = clean(data)


def first(d: dict[str, list[str]], *keys: str) -> str:
    for k in keys:
        if d.get(k):
            return d[k][0]
    return ""


def allof(d: dict[str, list[str]], *keys: str) -> list[str]:
    out: list[str] = []
    for k in keys:
        for v in d.get(k, []):
            if v not in out:
                out.append(v)
    return out


LABELS = {
    "author": "authors", "authors": "authors", "year": "year", "degree": "degree",
    "advisor": "advisors", "advisors": "advisors", "committee members": "committee",
    "committee member": "committee", "option": "options", "options": "options",
    "major option": "options", "minor option": "minor_options", "doi": "doi",
    "research group": "groups", "group": "groups", "thesis availability": "availability",
    "defense date": "defense_date", "record number": "record_number", "keywords": "keywords",
    "orcid": "orcid", "awards": "awards", "funders": "funders", "related urls": "related_urls",
    "degree grantor": "grantor", "resolver id": "resolver", "identifier": "identifier",
}
SECTION_STOP = {"abstract", "files", "id number", "doi", "citation", "related items"}
TAG_RE = re.compile(r"(?s)<script.*?</script>|<style.*?</style>")


def body_lines(text: str) -> list[str]:
    body = text[text.find("<body"):] if "<body" in text else text
    stripped = TAG_RE.sub(" ", body)
    stripped = re.sub(r"<[^>]+>", "\n", stripped)
    import html as _html
    return [clean(x) for x in _html.unescape(stripped).split("\n") if clean(x)]


def parse_record(rec_id: str, url: str, body: bytes) -> dict:
    text = body.decode("utf-8", "replace")
    p = MetaParser()
    p.feed(text)
    m = p.meta
    lines = body_lines(text)

    fields: dict[str, list[str]] = {}
    for i, line in enumerate(lines):
        if not line.endswith(":"):
            continue
        key = LABELS.get(line[:-1].strip().lower())
        if not key or i + 1 >= len(lines):
            continue
        value = lines[i + 1]
        if value.endswith(":") or value.lower() in SECTION_STOP:
            continue
        multi = key in ("authors", "advisors", "committee", "options", "groups", "keywords")
        for part in ([v.strip() for v in value.split(";")] if multi else [value]):
            if part and part not in fields.get(key, []):
                fields.setdefault(key, []).append(part)

    title = clean(p.title.split(" — ")[0]) if p.title else ""
    if not title:
        title = first(m, "citation_title", "og:title")

    abstract = ""
    if "Abstract" in lines:
        i = lines.index("Abstract")
        chunk = []
        for line in lines[i + 1:]:
            if line.lower() in SECTION_STOP or line.endswith(":") and line[:-1].lower() in LABELS:
                break
            chunk.append(line)
        abstract = " ".join(chunk)

    pdfs = [u for u in (allof(m, "citation_pdf_url") + p.links) if ".pdf" in u.lower()]
    pdfs = [u if u.startswith("http") else SITE + ("" if u.startswith("/") else "/") + u for u in pdfs]
    cc = CC_URL.findall(" ".join(p.links))
    rights = fields.get("availability", [])
    access, licence = classify_rights(rights + cc, cc, ["Thesis"])
    doi = (fields.get("doi") or [first(m, "citation_doi")])[0] if (fields.get("doi") or m.get("citation_doi")) else ""
    ids = [x for x in [doi, f"https://resolver.caltech.edu/CaltechTHESIS:{rec_id}"] if x]

    return {
        "repo": "caltech", "oai_identifier": f"caltechthesis:{rec_id}",
        "datestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d"), "sets": [],
        "prefix": "html-page", "deleted": False,
        "title": title,
        "authors": fields.get("authors", []),
        "advisors": fields.get("advisors", []),
        "committee": fields.get("committee", []),
        "date": (fields.get("year") or [""])[0],
        "types": ["Thesis"],
        "qualification": (fields.get("degree") or [""])[0],
        "departments": fields.get("options", []) + fields.get("groups", []),
        "subjects": fields.get("keywords", [])[:40],
        "abstract": abstract[:4000],
        "language": "",
        "publisher": "California Institute of Technology",
        "rights": rights, "rights_uri": sorted(set(cc)),
        "access_guess": access, "licence_class": licence,
        "identifiers": ids[:12], "landing_url": url,
        "file_urls": sorted(set(pdfs))[:10],
        "defense_date": (fields.get("defense_date") or [""])[0],
        "is_thesis": True,
    }


def sitemap_ids(http: Http, cache: Path, refresh: bool) -> list[str]:
    if cache.exists() and not refresh:
        return cache.read_text().split()
    log(f"fetching {SITEMAP}")
    data = http.get(SITEMAP)
    ids: list[str] = []
    seen = set()
    for loc in LOC_RE.findall(data):
        url = loc.decode("utf-8", "replace")
        path = url.split("//", 1)[-1]
        path = path[path.find("/"):] if "/" in path else "/"
        m = RECORD_PATH.match(path)
        if m and m.group(1) not in seen:
            seen.add(m.group(1))
            ids.append(m.group(1))
    ids.sort(key=int)
    cache.write_text("\n".join(ids))
    log(f"sitemap lists {len(ids)} thesis records -> {cache}")
    return ids


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(Path(__file__).with_name("repos_metadata") / "caltech"))
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--limit", type=int, default=None, help="stop after this many new records")
    ap.add_argument("--verbose", action="store_true", help="print the first parsed record")
    ap.add_argument("--refresh-sitemap", action="store_true")
    args = ap.parse_args(argv)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    records_path, raw_path, state_path = out / "records.jsonl", out / "raw_pages.jsonl.gz", out / "state.json"
    http = Http(timeout=60.0, retries=4, delay=args.delay)

    done: set[str] = set()
    if records_path.exists():
        with records_path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    ident = json.loads(line).get("oai_identifier", "")
                except Exception:
                    continue
                if ident.startswith("caltechthesis:"):
                    done.add(ident.split(":", 1)[1])
    log(f"{len(done)} records already stored")

    ids = sitemap_ids(http, out / "sitemap_ids.txt", args.refresh_sitemap)
    todo = [i for i in ids if i not in done]
    log(f"{len(todo)} to fetch")

    shape = "{site}/{id}/"
    kept = len(done)
    errors = 0
    started = time.time()
    for n, rec_id in enumerate(todo, 1):
        url = shape.format(site=SITE, id=rec_id)
        try:
            body = http.get(url)
        except HttpError as exc:
            if n == 1 and "404" in str(exc):        # try the other static shape once
                shape = "{site}/{id}/index.html"
                url = shape.format(site=SITE, id=rec_id)
                try:
                    body = http.get(url)
                except HttpError as exc2:
                    log(f"  {rec_id}: {exc2}")
                    errors += 1
                    continue
            else:
                errors += 1
                if errors <= 20 or errors % 100 == 0:
                    log(f"  {rec_id}: {exc}")
                if errors > 200 and errors > n * 0.5:
                    log("too many errors, stopping")
                    break
                continue
        row = parse_record(rec_id, url, body)
        if not row["title"]:
            errors += 1
            continue
        with records_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        head = body[:body.find(b"</head>") + 7] if b"</head>" in body else body[:8000]
        with gzip.open(raw_path, "at", encoding="utf-8") as raw:
            raw.write(json.dumps({"id": rec_id, "url": url,
                                  "head": head.decode("utf-8", "replace")}, ensure_ascii=False) + "\n")
        kept += 1
        if args.verbose and n == 1:
            log("first record parsed:\n" + json.dumps(row, ensure_ascii=False, indent=1)[:1600])
        if n % 100 == 0:
            rate = n / max(1e-9, (time.time() - started) / 60)
            log(f"  {n}/{len(todo)} fetched, {kept} stored, {errors} errors, {rate:.0f}/min")
            state_path.write_text(json.dumps({"kept": kept, "fetched": n, "errors": errors,
                                              "total": len(ids), "url_shape": shape,
                                              "updated": datetime.now(timezone.utc).isoformat()}, indent=1))
        if args.limit and n >= args.limit:
            break
    state_path.write_text(json.dumps({"kept": kept, "errors": errors, "total": len(ids),
                                      "url_shape": shape, "done": kept + errors >= len(ids),
                                      "updated": datetime.now(timezone.utc).isoformat()}, indent=1))
    log(f"finished: {kept} records stored, {errors} errors")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
