#!/usr/bin/env python3
"""Merge every repos_metadata/<repo>/records.jsonl into one catalog (CSV + SQLite + stats).

Mirrors build_apollo_catalog.py, but across universities. Run after (or during) a harvest:
    python3 build_repo_catalog.py
"""
from __future__ import annotations

import csv
import html
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "repos_metadata"
OUT = SRC / "_catalog"
YEAR_RE = __import__("re").compile(r"(1[6-9]\d{2}|20[0-4]\d)")

import sys as _sys
_sys.path.insert(0, str(ROOT))
from repo_harvest import classify_level  # noqa: E402

COLUMNS = ["repo", "oai_identifier", "title", "authors", "advisors", "year", "date", "qualification",
           "types", "departments", "subjects", "language", "publisher", "access_guess", "licence_class", "level",
           "rights", "rights_uri", "landing_url", "datestamp", "sets", "prefix"]


def year_of(row: dict) -> str:
    m = YEAR_RE.search(str(row.get("date") or "")) or YEAR_RE.search(str(row.get("datestamp") or ""))
    return m.group(1) if m else ""


# the figshare OAI endpoint serves every figshare-hosted repository, not just CMU's KiltHub
REPO_LABEL = {"cmu": "figshare (multi-institution, incl. CMU KiltHub)",
              "kth": "DiVA (Swedish consortium, incl. KTH)",
              "berkeley": "eScholarship (University of California system)"}


def rows():
    for d in sorted(SRC.iterdir()):
        if not d.is_dir() or d.name.startswith("_"):
            continue
        f = d / "records.jsonl"
        if not f.exists():
            continue
        with f.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                r["year"] = year_of(r)
                r["source"] = REPO_LABEL.get(r.get("repo"), r.get("repo"))
                if not r.get("level"):   # rows harvested before the level field existed
                    r["level"] = classify_level(str(r.get("qualification") or ""),
                                                r.get("types") or [], r.get("sets") or [])
                yield r


def flat(r: dict) -> dict:
    out = {}
    for c in COLUMNS:
        v = r.get(c, "")
        raw = " | ".join(str(x) for x in v) if isinstance(v, list) else ("" if v is None else str(v))
        out[c] = " ".join(html.unescape(html.unescape(raw)).split())
    return out


def main() -> int:
    if not SRC.exists():
        print("no repos_metadata directory yet")
        return 1
    OUT.mkdir(parents=True, exist_ok=True)
    db_path = OUT / "catalog.sqlite"
    if db_path.exists():
        db_path.unlink()
    db = sqlite3.connect(db_path)
    db.execute(f"CREATE TABLE theses ({', '.join(c + ' TEXT' for c in COLUMNS)})")
    db.execute("CREATE TABLE people (repo TEXT, oai_identifier TEXT, role TEXT, name TEXT)")

    per_repo = Counter()
    lic = defaultdict(Counter)
    acc = defaultdict(Counter)
    decade = defaultdict(Counter)
    n = 0
    with (OUT / "catalog.csv").open("w", newline="", encoding="utf-8") as cf, \
         (OUT / "catalog.jsonl").open("w", encoding="utf-8") as jf:
        writer = csv.DictWriter(cf, fieldnames=COLUMNS)
        writer.writeheader()
        for r in rows():
            f = flat(r)
            writer.writerow(f)
            jf.write(json.dumps(r, ensure_ascii=False) + "\n")
            db.execute(f"INSERT INTO theses VALUES ({','.join('?' * len(COLUMNS))})", [f[c] for c in COLUMNS])
            for role, key in (("author", "authors"), ("advisor", "advisors")):
                for name in (r.get(key) or [])[:20]:
                    db.execute("INSERT INTO people VALUES (?,?,?,?)", (r.get("repo"), r.get("oai_identifier"), role, name))
            repo = r.get("repo", "?")
            per_repo[repo] += 1
            lic[repo][r.get("licence_class", "unknown")] += 1
            acc[repo][r.get("access_guess", "unknown")] += 1
            y = r.get("year")
            decade[repo][(y[:3] + "0s") if y else "unknown"] += 1
            n += 1
    db.execute("CREATE INDEX idx_repo ON theses(repo)")
    db.execute("CREATE INDEX idx_year ON theses(year)")
    db.execute("CREATE INDEX idx_people ON people(name)")
    db.commit()
    db.close()

    lines = [f"# University thesis metadata catalogue", "",
             f"_built {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} — {n:,} records_", "",
             "| repository | records | open | restricted/embargo | unknown access | CC-licensed | in-copyright |",
             "|---|---:|---:|---:|---:|---:|---:|"]
    for repo, count in per_repo.most_common():
        cc = sum(v for k, v in lic[repo].items() if k.startswith("cc"))
        lines.append(f"| {repo} | {count:,} | {acc[repo]['open']:,} | {acc[repo]['restricted_or_embargo']:,} | "
                     f"{acc[repo]['unknown']:,} | {cc:,} | {lic[repo]['in-copyright']:,} |")
    lines += ["", "## licence classes", ""]
    for repo in per_repo:
        top = ", ".join(f"{k} {v:,}" for k, v in lic[repo].most_common(8))
        lines.append(f"- **{repo}**: {top}")
    lines += ["", "## decades", ""]
    for repo in per_repo:
        top = ", ".join(f"{k} {v:,}" for k, v in sorted(decade[repo].items()))
        lines.append(f"- **{repo}**: {top}")
    (OUT / "stats.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{n:,} records -> {OUT}")
    print("\n".join(lines[:8]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
