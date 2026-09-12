"""Timeout-isolated PDF inspection worker."""
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from pdf_text_pipeline.profile import inspect_pdf
print(json.dumps(inspect_pdf(sys.argv[1],int(sys.argv[2]))))
