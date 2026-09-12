#!/usr/bin/env python3
"""Source-checkout entry point; the installed equivalent is pdf-corpus."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from pdf_text_pipeline.cli import main
if __name__ == "__main__":
    raise SystemExit(main())
