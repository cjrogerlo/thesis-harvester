"""Conservative Markdown cleanup with lossless raw-page provenance.

No language model rewrites, guesses missing equations, or follows document instructions.
"""
from collections import Counter
import re
import unicodedata

COMMON_HEADINGS = re.compile(
    r"^(abstract|introduction|background|related work|methods?|methodology|"
    r"materials and methods|results|discussion|conclusions?|references|bibliography|"
    r"acknowledg[e]?ments|appendix(?: [A-Z])?|摘要|緒論|結論|參考文獻)$", re.I)
NUMBERED_HEADING = re.compile(r"^(\d+(?:\.\d+){0,5})[.)]?\s+([A-Za-z\u3400-\u9fff].{1,100})$")
BULLET = re.compile(r"^(?:[-*•▪]|\d+[.)])\s+")
LIGATURES = str.maketrans({"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl"})


def normal(text):
    # NFC keeps mathematical compatibility characters distinct (unlike NFKC).
    return unicodedata.normalize("NFC", text).translate(LIGATURES).replace("\u00a0", " ")


def heading(line):
    line = line.strip()
    if COMMON_HEADINGS.fullmatch(line):
        return 1, line
    match = NUMBERED_HEADING.fullmatch(line)
    if match and len(line) <= 110 and not line.endswith((".", ";", ",")):
        return min(6, match[1].count(".") + 1), line
    return None


def margins(pages):
    """Only repeated edge lines, on at least 3 and 60% of pages, are removed."""
    counts = Counter()
    for page in pages:
        lines = [normal(x).strip() for x in page.splitlines() if x.strip()]
        counts.update(set(lines[:2] + lines[-2:]))
    minimum = max(3, (len(pages) * 3 + 4) // 5)
    return {line for line, count in counts.items()
            if count >= minimum and len(line) <= 160 and not heading(line)}


def page_blocks(page, repeated, vocabulary=None, page_number=None):
    """Return Markdown blocks with exact raw-page character spans."""
    raw_lines = page.splitlines(keepends=True)
    nonempty = [i for i, line in enumerate(raw_lines) if line.strip()]
    edge = set(nonempty[:2] + nonempty[-2:])
    blocks, removed, pending = [], [], []
    offset = 0

    def flush():
        if not pending:
            return
        lines = [x[0] for x in pending]
        # Preserve hard hyphens across wrapped lines; do not guess a word's spelling.
        joined = " ".join(lines)
        flags = ["line_end_hyphen_preserved"] if any(x.endswith("-") for x in lines[:-1]) else []
        if vocabulary:
            restored = re.sub(r"([A-Za-z]+)- ([a-z]+)", lambda m: m[1]+m[2] if (m[1]+m[2]).casefold() in vocabulary else m[0], joined)
            if restored != joined:
                flags.append("hyphen_join_attested_elsewhere_in_document")
                joined = restored
        blocks.append({"text": joined, "kind": "paragraph", "flags": flags,
                       "raw_start": pending[0][1], "raw_end": pending[-1][2]})
        pending.clear()

    for i, raw in enumerate(raw_lines):
        start, end = offset, offset + len(raw)
        offset = end
        line = normal(raw).strip()
        page_number_line = page_number is not None and line == str(page_number)
        if i in edge and (line in repeated or page_number_line):
            flush()
            removed.append({"raw_start": start, "raw_end": end, "reason": "page_number" if page_number_line else "repeated_margin", "text": line})
            continue
        if not line:
            flush()
            continue
        h = heading(line)
        layout = bool(re.search(r"\S {3,}\S|\t", raw))
        math = bool(re.search(r"[=∑∫√∂∇≤≥≠]", line))
        if h or layout or math or BULLET.match(line):
            flush()
            if h:
                kind, value, flags = "heading", "#" * (h[0] + 1) + " " + line, []
            elif layout or math:
                # Verbatim display is honest; this is NOT reconstructed LaTeX or a parsed table.
                kind, value = "verbatim", "```text\n" + raw.rstrip("\r\n") + "\n```"
                flags = ["layout_or_formula_requires_review"]
            else:
                kind, value, flags = "list", re.sub(r"^[•▪]", "-", line), []
            blocks.append({"text": value, "kind": kind, "flags": flags,
                           "raw_start": start, "raw_end": end, "heading": h})
        else:
            pending.append((line, start, end))
    flush()
    return blocks, removed


def split_text(text, max_chars, overlap, encoding=None, max_tokens=1000):
    """Split at character boundaries, prefer paragraphs; optional exact BPE budget."""
    if max_chars <= 0 or not 0 <= overlap < max_chars or max_tokens <= 0:
        raise ValueError("Invalid chunk size, overlap or token budget")
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        if encoding is not None and len(encoding.encode(text[start:end], disallowed_special=())) > max_tokens:
            low, high = start + 1, end
            while low < high:
                mid = (low + high + 1) // 2
                if len(encoding.encode(text[start:mid], disallowed_special=())) <= max_tokens:
                    low = mid
                else:
                    high = mid - 1
            end = low
            if len(encoding.encode(text[start:end], disallowed_special=())) > max_tokens:
                raise ValueError("Token budget is too small for a single Unicode character")
        if end < len(text):
            boundary = text.rfind("\n\n", start + (end - start) // 2, end)
            if boundary > start:
                end = boundary
        if encoding is not None:
            # BPE token counts need not be monotonic after a boundary changes.
            while end > start and len(encoding.encode(text[start:end], disallowed_special=())) > max_tokens:
                end -= 1
            if end == start:
                raise ValueError("Token budget cannot hold the next Unicode character")
        value = text[start:end]
        yield value
        if end == len(text):
            break
        # Avoid near-zero progress when a token budget makes the window shorter than overlap.
        start = end - min(overlap, (end - start) // 4)


def prepare(pages, doc_id, profile, config, structure=None):
    encoding = None
    if config.tokenizer:
        import tiktoken
        encoding = tiktoken.get_encoding(config.tokenizer)
    repeated = margins(pages)
    vocabulary = set(re.findall(r"\b[A-Za-z]{4,}\b", "\n".join(pages).casefold()))
    prepared, cleanup, records = [], [], []
    roles = {}
    from .metadata import content_role
    section_path = []
    # Exact heading strings from GROBID override heuristic hierarchy when text agrees.
    verified_heads = {normal(s["path"][-1]): s["path"] for s in structure["sections"] if s["path"]} if structure else {}
    for number, page in enumerate(pages, 1):
        blocks, removed = page_blocks(page, repeated, vocabulary, number)
        cleanup.extend({"page": number, **item} for item in removed)
        groups = []
        for block in blocks:
            raw_heading = normal(page[block["raw_start"]:block["raw_end"]]).strip()
            if raw_heading in verified_heads:
                section_path = verified_heads[raw_heading][:]
                block["text"] = "#" * min(len(section_path) + 1, 6) + " " + raw_heading
                block["kind"] = "heading"
                section_method = "grobid_title_match"
            elif block.get("heading"):
                level, title = block["heading"]
                section_path = section_path[:level - 1] + [title]
                section_method = "heading_heuristic"
            else:
                section_method = groups[-1]["section_method"] if groups else "continued_or_unknown"
            if not groups or block["kind"] == "heading":
                groups.append({"section_path": section_path[:], "section_method": section_method, "blocks": []})
            groups[-1]["blocks"].append(block)
        markdown = "\n\n".join(b["text"] for b in blocks)
        prepared.append(f'<a id="page-{number}"></a>\n\n<!-- PDF page {number} -->\n\n{markdown}')
        for group in groups:
            text = "\n\n".join(b["text"] for b in group["blocks"])
            role = content_role(group["section_path"])
            roles.setdefault(role, []).append(text)
            for chunk in split_text(text, config.chunk_chars, config.overlap, encoding, config.chunk_tokens):
                flags = sorted({flag for b in group["blocks"] for flag in b["flags"]})
                records.append({"id": f"{doc_id}:{profile}:llm:{len(records)}", "text": chunk,
                                "metadata": {"document_id": doc_id, "profile": profile,
                                             "page_start": number, "page_end": number,
                                             "anchor": f"sha256:{doc_id}#page={number}",
                                             "section_path": group["section_path"],
                                             "content_role": role,
                                             "section_method": group["section_method"],
                                             "source_spans": [{"page": number, "char_start": b["raw_start"],
                                                               "char_end": b["raw_end"]} for b in group["blocks"]],
                                             "span_scope": "section_on_page",
                                             "quality_flags": flags,
                                             "tokenizer": config.tokenizer,
                                             "token_count": len(encoding.encode(chunk, disallowed_special=())) if encoding else None}})
    title = structure["title"] if structure and structure["title"] else "PDF document"
    document_markdown = f"# {title}\n\n<!-- source sha256:{doc_id} -->\n\n" + "\n\n---\n\n".join(prepared) + "\n"
    return document_markdown, records, cleanup, {k: "\n\n".join(v) + "\n" for k, v in roles.items()}


def prepare_structured(blocks, doc_id, profile, config):
    """Serialize one Docling item graph into training Markdown and RAG chunks."""
    from .metadata import content_role
    encoding = None
    if config.tokenizer:
        import tiktoken
        encoding = tiktoken.get_encoding(config.tokenizer)
    markdown, chunks, cleanup = [], [], []
    roles = {}
    section_path = []
    last_page = None
    for block in blocks:
        text, label = block["text"], block["label"]
        if label in ("page_header", "page_footer"):
            cleanup.append({"item_ref": block["item_ref"], "pages": block["pages"],
                            "text": text, "reason": "docling_margin_label"})
            continue
        if label in ("title", "section_header"):
            match = re.match(r"^(#{1,6})\s+(.+)", text)
            if match:
                level, title = len(match[1]), match[2]
                section_path = section_path[:level - 1] + [title]
        page = min(block["pages"]) if block["pages"] else None
        if page is not None and page != last_page:
            markdown.append(f'<a id="page-{page}"></a>\n\n<!-- PDF page {page} -->')
            last_page = page
        markdown.append(text)
        role = content_role(section_path)
        roles.setdefault(role, []).append(text)
        parts = list(split_text(text, config.chunk_chars, config.overlap, encoding, config.chunk_tokens))
        for part in parts:
            flags = []
            if label in ("formula", "table"):
                flags.append("model_extracted_structure_unverified")
                if len(parts) > 1:
                    flags.append("structure_fragment_see_full_markdown")
            if page is None:
                flags.append("missing_page_provenance")
            chunks.append({"id": f"{doc_id}:{profile}:llm:{len(chunks)}", "text": part,
                           "metadata": {"document_id": doc_id, "profile": profile,
                                        "section_path": section_path[:], "section_method": "docling",
                                        "page_start": page, "page_end": max(block["pages"]) if block["pages"] else None,
                                        "anchor": f"sha256:{doc_id}#page={page}" if page else None,
                                        "source_item": block["item_ref"], "coords": block["coords"],
                                        "span_scope": "docling_item", "content_role": role,
                                        "quality_flags": flags, "tokenizer": config.tokenizer,
                                        "token_count": len(encoding.encode(part, disallowed_special=())) if encoding else None}})
    return "\n\n".join(markdown) + "\n", chunks, cleanup, {k: "\n\n".join(v) + "\n" for k, v in roles.items()}


def pack_chunks(records, config):
    """Pack adjacent short blocks while retaining page and section boundaries."""
    encoding = None
    if config.tokenizer:
        import tiktoken
        encoding = tiktoken.get_encoding(config.tokenizer)
    output = []
    for row in records:
        row['metadata']['source_items'] = [row['metadata']['source_item']] if row['metadata'].get('source_item') else []
        if output:
            old = output[-1]
            a, b = old['metadata'], row['metadata']
            same = all(a.get(k) == b.get(k) for k in ('page_start','page_end','section_path','content_role','section_method'))
            if set(a.get('source_items', [])) & set(b.get('source_items', [])):
                same = False  # Do not repack overlapping fragments of the same item.
            if a.get('source_spans') and a.get('source_spans') == b.get('source_spans'):
                same = False
            text = old['text'] + '\n\n' + row['text']
            token_count = len(encoding.encode(text,disallowed_special=())) if encoding else None
            if same and len(text) <= config.chunk_chars and (token_count is None or token_count <= config.chunk_tokens):
                old['text'] = text; a['token_count'] = token_count
                a['source_items'] += b['source_items']
                a['quality_flags'] = sorted(set(a['quality_flags'] + b['quality_flags']))
                a['coords'] = a.get('coords',[]) + b.get('coords',[])
                a['source_spans'] = a.get('source_spans',[]) + b.get('source_spans',[])
                a['span_scope'] = 'source_items_or_section_on_page'
                continue
        output.append(row)
    return output
