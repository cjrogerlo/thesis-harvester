"""Optional heavy conversion isolated in a subprocess. No hosted inference APIs."""
import json
from pathlib import Path
import sys

# Direct subprocess execution must not shadow stdlib `profile` with our sibling.
if __package__ in (None, ""):
    sys.path = [p for p in sys.path if Path(p).resolve() != Path(__file__).parent.resolve()]


def convert(source, output, page_range=None):
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling_core.types.doc import PictureItem, TableItem
    options = PdfPipelineOptions()
    options.do_ocr = True
    options.do_table_structure = True
    options.do_formula_enrichment = True
    options.generate_picture_images = True
    options.images_scale = 2.0
    converter = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)})
    result = converter.convert(source, **({"page_range": page_range} if page_range else {}))
    if str(result.status.value) != "success":
        raise RuntimeError(f"Docling conversion status: {result.status}")
    doc = result.document
    output.mkdir(parents=True, exist_ok=True)
    assets, blocks = [], []
    for item, _ in doc.iterate_items():
        if not hasattr(item, "prov"):
            continue
        label = str(item.label.value)
        pages = sorted({p.page_no for p in item.prov})
        coords = [{"page": p.page_no, "bbox": p.bbox.model_dump(mode="json")} for p in item.prov]
        if isinstance(item, TableItem):
            text = item.export_to_html(doc=doc)  # preserve merged cells
        elif isinstance(item, PictureItem):
            picture = item.get_image(doc)
            if picture is None:
                text = "<!-- figure image unavailable -->"
            else:
                name = f"figure-{len(assets):05d}.png"
                picture.save(output / name)
                assets.append(name)
                text = f"![Figure]({name})"
            caption = item.caption_text(doc)
            if caption:
                text += "\n\n" + caption
        else:
            text = getattr(item, "text", "")
            if label == "formula":
                text = "$$\n" + text + "\n$$"
            elif label in ("section_header", "title"):
                text = "#" * max(1, min(6, getattr(item, "level", 1))) + " " + text
            elif label == "list_item":
                text = "- " + text
        if text:
            blocks.append({"text": text, "label": label, "pages": pages,
                           "coords": coords, "item_ref": item.self_ref})
    (output / "docling.json").write_text(json.dumps(doc.export_to_dict(), ensure_ascii=False))
    (output / "result.json").write_text(json.dumps({"blocks": blocks, "assets": assets,
                                                     "page_count": len(doc.pages)}, ensure_ascii=False))


if __name__ == "__main__":
    pages = tuple(map(int, sys.argv[3].split(":"))) if len(sys.argv) > 3 else None
    convert(Path(sys.argv[1]), Path(sys.argv[2]), pages)
