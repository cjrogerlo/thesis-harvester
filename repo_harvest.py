#!/usr/bin/env python3
"""Harvest thesis/dissertation METADATA from university repositories via OAI-PMH.

Same spirit as apollo_thesis_crawler.py, but repository-agnostic: it only reads
public OAI-PMH interfaces, never logs in, and (in this phase) downloads no files.

Sub-commands
  probe    find which candidate endpoint answers for each repository, list its
           metadata formats and its thesis-looking sets  ->  _discovery/probe.json
  harvest  stream ListRecords into <repo>/records.jsonl (+ raw XML, gzipped),
           resumable via <repo>/state.json
  stats    print per-repository progress

Output layout (default ~/superintelligence/repos_metadata):
  _discovery/probe.json          probe results
  <repo_id>/records.jsonl        one normalised record per line
  <repo_id>/raw_records.xml.gz   the original <record> elements that were kept
  <repo_id>/state.json           resume state (set, resumptionToken, counters)
"""

from __future__ import annotations

import argparse
import gzip
import html
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

OAI_NS = "http://www.openarchives.org/OAI/2.0/"
USER_AGENT = "superintelligence-metadata-harvester/1.0 (OAI-PMH; metadata only)"
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
              "(KHTML, like Gecko) Version/17.4 Safari/605.1.15")
UA_POOL = [USER_AGENT, BROWSER_UA]
# common OAI-PMH paths, tried against the repository host when the listed candidates all fail
COMMON_OAI_PATHS = ["/oai/request", "/server/oai/request", "/oai2d", "/oai", "/cgi/oai2",
                    "/dspace-oai/request", "/ws/oai", "/oai/openaire", "/oai/driver",
                    "/do/oai/", "/api/oai-pmh", "/oai2", "/OAI-PMH", "/oai/oai2"]
BAD_XML_BYTES = re.compile(rb"[\x00-\x08\x0b\x0c\x0e-\x1f]")
BARE_AMP = re.compile(rb"&(?!#?\w+;)")
PREFIX_PREFERENCE = ["dim", "oai_etdms", "etdms", "oai_qdc", "qdc", "oai_dc"]
THESIS_SET_RE = re.compile(r"thes|dissert|etd|doctor|phd|promot|論文|学位", re.I)
THESIS_TYPE_RE = re.compile(r"thes|dissert|doctoral|ph\.?d|masters?|学位論文|etd", re.I)
DOCTORAL_RE = re.compile(r"doctor|ph\.?\s?d|dr\.?rer|dissertation|habilitation|博士|doktor", re.I)
MASTER_RE = re.compile(r"master|m\.?sc|m\.?phil|m\.?a\b|magister|licentiate|碩士", re.I)
BACHELOR_RE = re.compile(r"bachelor|undergraduate|diplomarbeit|學士", re.I)


def classify_level(qualification: str, types: list[str], sets: list[str]) -> str:
    """doctoral / master / bachelor / unspecified — the field the PDF phase filters on."""
    blob = " ".join([qualification] + list(types) + list(sets))
    if DOCTORAL_RE.search(blob):
        return "doctoral"
    if MASTER_RE.search(blob):
        return "master"
    if BACHELOR_RE.search(blob):
        return "bachelor"
    return "unspecified"


OPEN_RE = re.compile(r"open.?access|openaccess|creative ?commons|cc[ -]?by|public domain|no restriction", re.I)
CLOSED_RE = re.compile(r"embargo|restricted|closed access|controlled.?access|metadata.?only|request a copy|not available", re.I)

# licence classes matter for downstream reuse: CC licences are reusable, "In Copyright" is not.
LICENCE_RULES = [
    ("cc0/public-domain", re.compile(r"creativecommons\.org/publicdomain|\bcc0\b|public domain", re.I)),
    ("cc-by-nc-nd", re.compile(r"licenses/by-nc-nd|cc[ -]?by[ -]?nc[ -]?nd", re.I)),
    ("cc-by-nc-sa", re.compile(r"licenses/by-nc-sa|cc[ -]?by[ -]?nc[ -]?sa", re.I)),
    ("cc-by-nc", re.compile(r"licenses/by-nc|cc[ -]?by[ -]?nc", re.I)),
    ("cc-by-nd", re.compile(r"licenses/by-nd|cc[ -]?by[ -]?nd", re.I)),
    ("cc-by-sa", re.compile(r"licenses/by-sa|cc[ -]?by[ -]?sa", re.I)),
    ("cc-by", re.compile(r"licenses/by/|cc[ -]?by\b", re.I)),
    ("in-copyright", re.compile(r"rightsstatements\.org|in ?copyright|all rights reserved|\u00a9", re.I)),
    ("open-unspecified", OPEN_RE),
]


def classify_rights(rights: list[str], rights_uri: list[str], types: list[str]) -> tuple[str, str]:
    blob = " ".join(rights + rights_uri + types)
    access = "restricted_or_embargo" if CLOSED_RE.search(blob) else ("open" if OPEN_RE.search(blob) else "unknown")
    licence = next((name for name, rx in LICENCE_RULES if rx.search(blob)), "unknown")
    return access, licence


def log(msg: str) -> None:
    print(f"{datetime.now().strftime('%H:%M:%S')} {msg}", flush=True)


# ---------------------------------------------------------------- HTTP


class HttpError(RuntimeError):
    pass


class Http:
    def __init__(self, timeout: float = 90.0, retries: int = 4, delay: float = 1.0):
        self.timeout, self.retries, self.delay = timeout, retries, delay
        self.last = 0.0
        self.ua_i = 0  # sticky: once a host needs the browser UA, keep using it
        self.ctx = ssl.create_default_context()

    def get(self, url: str, params: dict | None = None) -> bytes:
        if params:
            sep = "&" if urllib.parse.urlsplit(url).query else "?"
            url = url + sep + urllib.parse.urlencode(params)
        attempt = 0
        ua_i = self.ua_i
        ua_switched = 0
        while True:
            gap = time.time() - self.last
            if gap < self.delay:
                time.sleep(self.delay - gap)
            req = urllib.request.Request(url, headers={"User-Agent": UA_POOL[ua_i], "Accept": "text/xml,*/*"})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout, context=self.ctx) as resp:
                    self.last = time.time()
                    return resp.read()
            except urllib.error.HTTPError as exc:
                self.last = time.time()
                if exc.code == 503:  # OAI flow control
                    wait = exc.headers.get("Retry-After")
                    try:
                        wait = float(wait)
                    except (TypeError, ValueError):
                        wait = 20.0
                    wait = min(max(wait, 5.0), 300.0)
                    log(f"    503 flow control, sleeping {wait:.0f}s")
                    time.sleep(wait)
                    attempt += 1
                    if attempt > self.retries + 4:
                        raise HttpError(f"503 loop on {url}") from exc
                    continue
                if exc.code in (403, 406, 429, 500, 405) and ua_switched == 0 and ua_i + 1 < len(UA_POOL):
                    ua_i += 1
                    self.ua_i = ua_i
                    ua_switched = 1
                    log(f"    HTTP {exc.code}: retrying with a browser user agent")
                    continue
                if exc.code in (403, 429):  # rate limited: wait it out rather than give up
                    attempt += 1
                    if attempt > self.retries + 2:
                        raise HttpError(f"HTTP {exc.code} (rate limited) on {url}") from exc
                    wait = min(600, 60 * attempt)
                    log(f"    HTTP {exc.code} (rate limited): waiting {wait}s before retry {attempt}")
                    time.sleep(wait)
                    continue
                if exc.code in (500, 502, 504) and attempt < self.retries:
                    attempt += 1
                    time.sleep(min(60, 2 ** attempt * 3))
                    continue
                raise HttpError(f"HTTP {exc.code} on {url}") from exc
            except Exception as exc:  # timeouts, DNS, TLS
                self.last = time.time()
                attempt += 1
                if attempt > self.retries:
                    raise HttpError(f"{type(exc).__name__}: {exc} on {url}") from exc
                time.sleep(min(60, 2 ** attempt * 3))


def parse_xml(data: bytes) -> ET.Element:
    """Some repositories emit control characters or bare ampersands; repair before giving up."""
    try:
        return ET.fromstring(data)
    except ET.ParseError:
        pass
    cleaned = BAD_XML_BYTES.sub(b" ", data)
    try:
        return ET.fromstring(cleaned)
    except ET.ParseError:
        pass
    return ET.fromstring(BARE_AMP.sub(b"&amp;", cleaned))


def oai(http: Http, base: str, **params) -> ET.Element:
    data = http.get(base, params)
    try:
        return parse_xml(data)
    except ET.ParseError as exc:
        raise HttpError(f"bad XML from {base}: {exc}") from exc


def oai_error(root: ET.Element) -> tuple[str, str] | None:
    err = root.find(f"{{{OAI_NS}}}error")
    if err is None:
        return None
    return (err.get("code") or "unknown", (err.text or "").strip())


# ---------------------------------------------------------------- parsing


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def clean(text: str) -> str:
    """Some repositories double-escape their metadata (&amp;amp;, &lt;i&gt;)."""
    out = html.unescape(html.unescape(text)).strip()
    return " ".join(out.split())


def flatten(elem: ET.Element) -> dict[str, list[str]]:
    """Collect every descendant's local tag name -> list of texts (any OAI format)."""
    out: dict[str, list[str]] = {}
    for node in elem.iter():
        text = clean(node.text or "")
        if not text:
            continue
        out.setdefault(local(node.tag).lower(), []).append(text)
    return out


def dim_fields(elem: ET.Element) -> dict[str, list[str]]:
    """DSpace Intermediate Metadata: <dim:field mdschema element qualifier>value</>."""
    out: dict[str, list[str]] = {}
    for node in elem.iter():
        if local(node.tag) != "field":
            continue
        text = clean(node.text or "")
        if not text:
            continue
        key = ".".join(x for x in (node.get("mdschema"), node.get("element"), node.get("qualifier")) if x)
        out.setdefault(key.lower(), []).append(text)
    return out


def first(d: dict[str, list[str]], *keys: str) -> str:
    for k in keys:
        v = d.get(k)
        if v:
            return v[0]
    return ""


def allof(d: dict[str, list[str]], *keys: str) -> list[str]:
    out: list[str] = []
    for k in keys:
        for v in d.get(k, []):
            if v not in out:
                out.append(v)
    return out


def normalise(repo_id: str, header: ET.Element, meta: ET.Element | None, prefix: str,
              thesis_sets: set[str]) -> dict:
    ident = (header.findtext(f"{{{OAI_NS}}}identifier") or "").strip()
    stamp = (header.findtext(f"{{{OAI_NS}}}datestamp") or "").strip()
    sets = [(s.text or "").strip() for s in header.findall(f"{{{OAI_NS}}}setSpec")]
    row = {
        "repo": repo_id, "oai_identifier": ident, "datestamp": stamp, "sets": sets,
        "prefix": prefix, "deleted": (header.get("status") == "deleted"),
    }
    if meta is None:
        row["is_thesis"] = False
        return row

    if prefix == "dim":
        f = dim_fields(meta)
        title = first(f, "dc.title")
        authors = allof(f, "dc.contributor.author", "dc.creator", "dc.contributor")
        advisors = allof(f, "dc.contributor.advisor", "dc.contributor.supervisor", "dc.contributor.other")
        date = first(f, "dc.date.issued", "dc.date.available", "dc.date.accessioned", "dc.date")
        types = allof(f, "dc.type")
        qualification = first(f, "dc.type.qualificationname", "dc.type.qualificationlevel", "thesis.degree.name")
        dept = allof(f, "dc.publisher.department", "thesis.degree.discipline", "dc.contributor.department")
        subjects = allof(f, "dc.subject", "dc.subject.lcsh", "dc.subject.other")
        abstract = first(f, "dc.description.abstract")
        rights = allof(f, "dc.rights", "dc.rights.holder", "dc.rights.license", "dc.rights.accessrights")
        rights_uri = allof(f, "dc.rights.uri")
        ids = allof(f, "dc.identifier.uri", "dc.identifier.doi", "dc.identifier", "dc.identifier.other")
        lang = first(f, "dc.language.iso", "dc.language")
        publisher = first(f, "dc.publisher")
    else:
        f = flatten(meta)
        title = first(f, "title")
        authors = allof(f, "creator", "author", "name")
        advisors = allof(f, "contributor", "advisor")
        date = first(f, "date", "issued", "dateaccepted")
        types = allof(f, "type")
        qualification = first(f, "name", "degree", "level")
        dept = allof(f, "discipline", "department", "grantor")
        subjects = allof(f, "subject", "topic")
        abstract = first(f, "abstract", "description")
        rights = allof(f, "rights", "accessrights", "license", "accessRights".lower())
        rights_uri = [r for r in rights if r.startswith("http")]
        ids = allof(f, "identifier", "uri", "doi")
        lang = first(f, "language")
        publisher = first(f, "publisher")

    blob = " ".join(types + [qualification] + sets)
    is_thesis = bool(THESIS_TYPE_RE.search(blob)) or bool(thesis_sets & set(sets)) or bool(qualification)
    access, licence = classify_rights(rights, rights_uri, types)

    row.update({
        "title": title, "authors": authors, "advisors": advisors, "date": date,
        "types": types, "qualification": qualification, "departments": dept,
        "subjects": subjects[:40], "abstract": abstract[:4000], "language": lang,
        "publisher": publisher, "rights": rights, "rights_uri": rights_uri,
        "access_guess": access, "licence_class": licence, "identifiers": ids[:12],
        "level": classify_level(str(qualification), types, sets),
        "landing_url": next((i for i in ids if i.startswith("http")), ""),
        "is_thesis": is_thesis,
    })
    return row


# ---------------------------------------------------------------- probe


def candidate_endpoints(repo: dict) -> list[str]:
    listed = list(repo.get("oai", []))
    hosts = []
    for url in listed:
        parts = urllib.parse.urlsplit(url)
        root = f"{parts.scheme}://{parts.netloc}"
        if root not in hosts:
            hosts.append(root)
    swept = [root + path for root in hosts for path in COMMON_OAI_PATHS]
    return listed + [u for u in swept if u not in listed]


def probe_repo(http: Http, repo: dict, max_set_pages: int = 25) -> dict:
    out = {"id": repo["id"], "name": repo["name"], "repo": repo.get("repo", ""),
           "country": repo.get("country", ""), "checked_at": datetime.now(timezone.utc).isoformat(),
           "ok": False, "attempts": []}
    host_conn_fail: dict[str, int] = {}
    for base in candidate_endpoints(repo):
        host = urllib.parse.urlsplit(base).netloc
        if host_conn_fail.get(host, 0) >= 2:
            continue
        attempt = {"endpoint": base}
        try:
            root = oai(http, base, verb="Identify")
            err = oai_error(root)
            if err:
                attempt["error"] = f"OAI error {err[0]}: {err[1]}"
                out["attempts"].append(attempt)
                continue
            ident = root.find(f"{{{OAI_NS}}}Identify")
            if ident is None:
                attempt["error"] = "no Identify element"
                out["attempts"].append(attempt)
                continue
            out["oai_base"] = base
            out["repository_name"] = ident.findtext(f"{{{OAI_NS}}}repositoryName", "")
            out["protocol"] = ident.findtext(f"{{{OAI_NS}}}protocolVersion", "")
            out["granularity"] = ident.findtext(f"{{{OAI_NS}}}granularity", "")
            out["earliest"] = ident.findtext(f"{{{OAI_NS}}}earliestDatestamp", "")
            attempt["ok"] = True
            out["attempts"].append(attempt)
        except HttpError as exc:
            attempt["error"] = str(exc)[:300]
            out["attempts"].append(attempt)
            if not str(exc).startswith("HTTP "):  # connection level, not a 404/403 answer
                host_conn_fail[host] = host_conn_fail.get(host, 0) + 1
            continue

        # formats
        try:
            root = oai(http, base, verb="ListMetadataFormats")
            out["formats"] = [n.text for n in root.iter(f"{{{OAI_NS}}}metadataPrefix") if n.text]
        except HttpError as exc:
            out["formats"] = []
            out["formats_error"] = str(exc)[:200]

        # sets (paginated)
        sets: list[dict] = []
        token = None
        try:
            for _ in range(max_set_pages):
                root = oai(http, base, verb="ListSets", **({"resumptionToken": token} if token else {}))
                if oai_error(root):
                    break
                for s in root.iter(f"{{{OAI_NS}}}set"):
                    spec = s.findtext(f"{{{OAI_NS}}}setSpec", "")
                    name = s.findtext(f"{{{OAI_NS}}}setName", "")
                    sets.append({"spec": spec, "name": name})
                rt = root.find(f".//{{{OAI_NS}}}resumptionToken")
                token = (rt.text or "").strip() if rt is not None and rt.text else None
                if not token:
                    break
        except HttpError as exc:
            out["sets_error"] = str(exc)[:200]
        out["set_count"] = len(sets)
        thesis_sets = [s for s in sets if THESIS_SET_RE.search(f"{s['spec']} {s['name']}")]
        out["thesis_sets"] = thesis_sets[:60]
        out["prefix_choice"] = next((p for p in PREFIX_PREFERENCE if p in (out.get("formats") or [])), "oai_dc")
        out["ok"] = True
        break
    return out


# ---------------------------------------------------------------- harvest


def load_state(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return {}


def save_state(path: Path, state: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1))
    tmp.replace(path)


def seen_identifiers(path: Path) -> set[str]:
    seen: set[str] = set()
    if not path.exists():
        return seen
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                seen.add(json.loads(line).get("oai_identifier", ""))
            except Exception:
                continue
    seen.discard("")
    return seen


def year_segments(probe: dict) -> list[tuple[str, str]]:
    """Fall back to harvesting one year at a time when resumption tokens keep expiring."""
    earliest = (probe.get("earliest") or "1900")[:4]
    try:
        first = max(1900, int(earliest))
    except ValueError:
        first = 1900
    last = datetime.now(timezone.utc).year
    return [(f"{y}-01-01", f"{y}-12-31") for y in range(first, last + 1)]


def split_segment(seg: tuple[str, str]) -> list[tuple[str, str]]:
    """Year -> months -> days, so a window whose tokens keep expiring is narrowed, not dropped."""
    start = datetime.strptime(seg[0], "%Y-%m-%d").date()
    end = datetime.strptime(seg[1], "%Y-%m-%d").date()
    span = (end - start).days
    if span <= 1:
        return []
    out: list[tuple[str, str]] = []
    if span > 45:                                    # months
        y, m = start.year, start.month
        while (y, m) <= (end.year, end.month):
            last = 31
            while True:
                try:
                    datetime(y, m, last)
                    break
                except ValueError:
                    last -= 1
            out.append((f"{y:04d}-{m:02d}-01", f"{y:04d}-{m:02d}-{last:02d}"))
            m += 1
            if m == 13:
                y, m = y + 1, 1
        return out
    day = start                                      # days
    while day <= end:
        out.append((day.isoformat(), day.isoformat()))
        day = date_plus(day, 1)
    return out


def date_plus(d, n):
    from datetime import timedelta
    return d + timedelta(days=n)


def harvest_repo(http: Http, probe: dict, outdir: Path, args) -> dict:
    repo_id = probe["id"]
    base = probe.get("oai_base")
    if not base:
        return {"id": repo_id, "skipped": "no working endpoint"}
    d = outdir / repo_id
    d.mkdir(parents=True, exist_ok=True)
    lock = d / "harvest.lock"
    if lock.exists() and time.time() - lock.stat().st_mtime < 600:
        log(f"  {repo_id}: another harvester holds the lock, skipping")
        return {"id": repo_id, "skipped": "locked"}
    lock.write_text(f"{os.getpid()} {datetime.now(timezone.utc).isoformat()}")
    records_path, raw_path, state_path = d / "records.jsonl", d / "raw_records.xml.gz", d / "state.json"
    state = load_state(state_path)
    if state.get("done") and not args.restart:
        log(f"  {repo_id}: already complete ({state.get('kept', 0)} kept)")
        return {"id": repo_id, **state}
    if args.restart:
        state = {}

    prefix = args.prefix or state.get("prefix") or probe.get("prefix_choice") or "oai_dc"
    thesis_specs = [s["spec"] for s in probe.get("thesis_sets", []) if s.get("spec")]
    if args.all_sets or not thesis_specs:
        targets = state.get("targets") or [None]
        mode = "whole-repository"
    else:
        targets = state.get("targets") or thesis_specs
        mode = f"{len(thesis_specs)} thesis sets"
    log(f"  {repo_id}: prefix={prefix}, {mode}")

    seen = seen_identifiers(records_path)
    done_targets = set(state.get("done_targets", []))
    total_seen = state.get("seen", 0)
    kept = state.get("kept", len(seen))
    started = time.time()
    http_errors = [0]
    thesis_set_names = set(thesis_specs)
    windows: dict[str, list] = {k: [tuple(v) if v else None for v in vs]
                                for k, vs in (state.get("windows") or {}).items()}

    def snapshot(**extra) -> dict:
        state.update({"seen": total_seen, "kept": kept, "prefix": prefix, "targets": targets,
                      "done_targets": sorted(done_targets), "base": base,
                      "windows": {k: [list(w) if w else None for w in v] for k, v in windows.items()},
                      "updated": datetime.now(timezone.utc).isoformat(), **extra})
        save_state(state_path, state)
        try:
            lock.write_text(f"{os.getpid()} {datetime.now(timezone.utc).isoformat()}")
        except OSError:
            pass
        return state

    for target in targets:
        key = target or "__ALL__"
        if key in done_targets:
            continue
        segments: list = windows.get(key) or [None]
        seg_i = int(state.get("segment_index", 0)) if state.get("current") == key else 0
        while seg_i < len(segments):
            seg = segments[seg_i]
            token = state.get("token") if (state.get("current") == key and
                                           state.get("segment_index", 0) == seg_i) else None
            params = {"verb": "ListRecords", "metadataPrefix": prefix}
            if target:
                params["set"] = target
            if seg:
                params["from"], params["until"] = seg
            page = 0
            restarts = 0
            while True:
                try:
                    root = oai(http, base, **({"verb": "ListRecords", "resumptionToken": token} if token else params))
                except HttpError as exc:
                    log(f"  {repo_id}: {exc}")
                    snapshot(current=key, token=token, segment_index=seg_i, error=str(exc)[:300])
                    http_errors[0] += 1
                    if http_errors[0] > 5:
                        log(f"  {repo_id}: too many HTTP errors, leaving the rest for the next pass")
                        return {"id": repo_id, **state}
                    break   # skip this set/window, keep going with the others
                err = oai_error(root)
                if err:
                    code = err[0]
                    if code == "noRecordsMatch":
                        break
                    if code == "badResumptionToken":
                        restarts += 1
                        if seg is None and restarts <= 1:
                            log(f"  {repo_id}: token expired, restarting {key} once")
                            token = None
                            continue
                        if seg is None:
                            segments = year_segments(probe)
                            windows[key] = segments
                            seg_i = -1
                            log(f"  {repo_id}: tokens keep expiring - switching {key} to year-by-year windows "
                                f"({segments[0][0][:4]}-{segments[-1][0][:4]})")
                            snapshot(current=key, token=None, segment_index=0)
                            break
                        if restarts <= 2:
                            token = None
                            continue
                        finer = split_segment(seg) if len(segments) < 6000 else []
                        if finer:
                            segments[seg_i:seg_i + 1] = finer
                            windows[key] = segments
                            seg_i -= 1               # the loop's += 1 lands on the first sub-window
                            log(f"  {repo_id}: narrowing {key} {seg[0]}..{seg[1]} into {len(finer)} windows")
                            snapshot(current=key, token=None, segment_index=max(0, seg_i + 1))
                            break
                        log(f"  {repo_id}: giving up on {key} {seg[0]}..{seg[1]} after repeated token expiry")
                        break
                    log(f"  {repo_id}: OAI error {code} ({err[1][:120]}) on {key}")
                    break

                page += 1
                batch_raw: list[str] = []
                with records_path.open("a", encoding="utf-8") as rec_fh:
                    for record in root.iter(f"{{{OAI_NS}}}record"):
                        header = record.find(f"{{{OAI_NS}}}header")
                        if header is None:
                            continue
                        ident = (header.findtext(f"{{{OAI_NS}}}identifier") or "").strip()
                        total_seen += 1
                        if ident in seen:
                            continue
                        meta_wrap = record.find(f"{{{OAI_NS}}}metadata")
                        meta = list(meta_wrap)[0] if meta_wrap is not None and len(meta_wrap) else None
                        row = normalise(repo_id, header, meta, prefix, thesis_set_names)
                        if not args.all_records and not row.get("is_thesis"):
                            continue
                        if args.level and row.get("level") not in (args.level, "unspecified"):
                            continue
                        seen.add(ident)
                        kept += 1
                        rec_fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                        if not args.no_raw:
                            batch_raw.append(ET.tostring(record, encoding="unicode"))
                if batch_raw:
                    with gzip.open(raw_path, "at", encoding="utf-8") as raw_fh:
                        raw_fh.write("\n".join(batch_raw) + "\n")

                rt = root.find(f".//{{{OAI_NS}}}resumptionToken")
                size = rt.get("completeListSize") if rt is not None else None
                token = (rt.text or "").strip() if rt is not None and rt.text else None
                label = key if seg is None else f"{key} {seg[0][:4]}"
                if page % 5 == 0 or not token:
                    log(f"  {repo_id}: {label} page {page}, seen {total_seen}, kept {kept}" + (f" / {size}" if size else ""))
                snapshot(current=key, token=token, segment_index=seg_i)

                if args.max_records and kept >= args.max_records:
                    log(f"  {repo_id}: hit --max-records {args.max_records}")
                    return {"id": repo_id, **state}
                if args.max_minutes and (time.time() - started) / 60 >= args.max_minutes:
                    log(f"  {repo_id}: hit --max-minutes {args.max_minutes}")
                    return {"id": repo_id, **state}
                if page > 30000:
                    log(f"  {repo_id}: page cap reached on {label}")
                    break
                if not token:
                    break
            seg_i += 1
        done_targets.add(key)
        snapshot(current=None, token=None, segment_index=0)

    snapshot(done=True, token=None, current=None, segment_index=0)
    log(f"  {repo_id}: DONE, kept {kept} of {total_seen} records")
    return {"id": repo_id, **state}


# ---------------------------------------------------------------- cli


def load_registry(path: Path, only: str | None) -> list[dict]:
    repos = json.loads(path.read_text())["repositories"]
    if only:
        wanted = {x.strip() for x in only.split(",") if x.strip()}
        repos = [r for r in repos if r["id"] in wanted]
    # work through the list by Nobel-laureate count, highest first
    repos.sort(key=lambda r: (-(r.get("nobel") or 0), r["id"]))
    return repos


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["probe", "harvest", "stats"])
    p.add_argument("--registry", default=str(Path(__file__).with_name("repos.json")))
    p.add_argument("--out", default=str(Path(__file__).with_name("repos_metadata")))
    p.add_argument("--repos", default=None, help="comma-separated repository ids")
    p.add_argument("--delay", type=float, default=1.0)
    p.add_argument("--timeout", type=float, default=90.0)
    p.add_argument("--retries", type=int, default=4)
    p.add_argument("--probe-timeout", type=float, default=20.0, help="per-request timeout while probing")
    p.add_argument("--probe-retries", type=int, default=0, help="retries while probing")
    p.add_argument("--prefix", default=None, help="force a metadata prefix")
    p.add_argument("--all-sets", action="store_true", help="harvest the whole repository, not just thesis sets")
    p.add_argument("--all-records", action="store_true", help="keep non-thesis records too")
    p.add_argument("--level", default=None, choices=["doctoral", "master", "bachelor"],
                   help="only keep records at this level (records whose level is unspecified are kept too, "
                        "since many repositories omit the degree)")
    p.add_argument("--no-raw", action="store_true", help="do not store raw XML")
    p.add_argument("--max-records", type=int, default=None, help="per repository")
    p.add_argument("--max-minutes", type=float, default=None, help="per repository")
    p.add_argument("--restart", action="store_true", help="ignore saved state")
    args = p.parse_args(argv)

    outdir = Path(args.out).expanduser()
    outdir.mkdir(parents=True, exist_ok=True)
    disc = outdir / "_discovery"
    disc.mkdir(exist_ok=True)
    probe_path = disc / "probe.json"
    http = Http(timeout=args.timeout, retries=args.retries, delay=args.delay)

    if args.command == "probe":
        http = Http(timeout=args.probe_timeout, retries=args.probe_retries, delay=args.delay)
        repos = load_registry(Path(args.registry), args.repos)
        results = {}
        if probe_path.exists():
            try:
                results = json.loads(probe_path.read_text())
            except Exception:
                results = {}
        for repo in repos:
            log(f"probing {repo['id']} ({repo['name']})")
            res = probe_repo(http, repo)
            results[repo["id"]] = res
            probe_path.write_text(json.dumps(results, ensure_ascii=False, indent=1))
            if res.get("ok"):
                log(f"  OK {res.get('repository_name','')[:60]} | formats={len(res.get('formats') or [])}"
                    f" | sets={res.get('set_count')} | thesis sets={len(res.get('thesis_sets') or [])}"
                    f" | prefix={res.get('prefix_choice')}")
            else:
                log("  no working OAI endpoint")
        ok = sum(1 for r in results.values() if r.get("ok"))
        log(f"probe finished: {ok}/{len(results)} repositories reachable -> {probe_path}")
        return 0

    if args.command == "stats":
        rows = []
        for d in sorted(outdir.iterdir()):
            if not d.is_dir() or d.name.startswith("_"):
                continue
            st = load_state(d / "state.json")
            recs = d / "records.jsonl"
            n = sum(1 for _ in recs.open(encoding="utf-8")) if recs.exists() else 0
            size = sum(f.stat().st_size for f in d.glob("*") if f.is_file()) / 1e6
            rows.append((d.name, n, st.get("seen", 0), "done" if st.get("done") else (st.get("current") or "-"), f"{size:.1f} MB"))
        w = max([len(r[0]) for r in rows] + [10])
        print(f"{'repository'.ljust(w)}  {'kept':>8} {'seen':>9}  status               size")
        for name, n, seen, status, size in rows:
            print(f"{name.ljust(w)}  {n:>8} {seen:>9}  {str(status)[:20].ljust(20)} {size}")
        print(f"total kept: {sum(r[1] for r in rows)}")
        return 0

    # harvest
    if not probe_path.exists():
        log("no probe.json yet - run `repo_harvest.py probe` first")
        return 2
    probes = json.loads(probe_path.read_text())
    repos = load_registry(Path(args.registry), args.repos)
    order = [probes[r["id"]] for r in repos if r["id"] in probes and probes[r["id"]].get("ok")]
    log(f"harvesting {len(order)} repositories -> {outdir}")
    for probe in order:
        log(f"[{probe['id']}] {probe.get('repository_name') or probe['name']}")
        try:
            harvest_repo(http, probe, outdir, args)
        except KeyboardInterrupt:
            log("interrupted")
            return 130
        except Exception as exc:  # never let one repository stop the run
            log(f"  {probe['id']}: unexpected {type(exc).__name__}: {exc}")
    log("harvest pass finished")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
