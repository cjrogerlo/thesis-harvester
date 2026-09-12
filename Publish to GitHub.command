#!/bin/zsh
# Run manually when ready to publish code; this file is not run by the pipeline.
set -euo pipefail
cd "$(dirname "$0")"
command -v gh >/dev/null || { echo 'Install GitHub CLI (gh) first.'; exit 1; }
gh auth status >/dev/null 2>&1 || { echo 'Run gh auth login yourself, then rerun this script.'; exit 1; }
if [[ ! -d .git ]]; then git init -b main; fi
if git remote get-url origin >/dev/null 2>&1; then
  echo 'An origin already exists. Review it and push manually; this helper creates new public code repos only.'
  exit 1
fi
# Explicit code allowlist; never stage the surrounding corpus or credential files.
git add -- README.md LICENSE .gitignore pyproject.toml .github src tests docs \
  extract_text.py profile_pdfs.py repo_harvest.py caltech_thesis_harvest.py \
  build_repo_catalog.py repos.json 'Publish to GitHub.command'
if ! git diff --cached --quiet; then git commit -m 'Add thesis corpus processing pipeline'; fi
gh repo create "${1:-thesis-harvester}" --public --source=. --remote=origin --push
