import json
import os
import re
from html.parser import HTMLParser
from pathlib import Path

import pandas as pd

from dataflow import get_logger
from dataflow.core import OperatorABC
from dataflow.utils.registry import OPERATOR_REGISTRY
from dataflow.utils.storage import DataFlowStorage


AUX_TYPES = {
    "header", "footer", "page_number", "aside_text", "page_footnote",
    "page_header", "page_footer", "page_aside_text",
}
CAPTION_RE = re.compile(
    r"^(?:附件\s*[一二三四五六七八九十百零〇0-9]+|附表\s*\w*|"
    r"表\s*[\w一二三四五六七八九十百零〇.-]+|table\s*[\w.-]+)\s*[:：]?",
    re.I,
)
TABLEISH_RE = re.compile(r"(?:标准|明细|清单|一览表|统计表|对照表|汇总表|目录)$")


def _text(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return re.sub(r"\s+", " ", value).strip()
    if isinstance(value, (list, tuple)):
        return " ".join(filter(None, (_text(v) for v in value))).strip()
    if isinstance(value, dict):
        if "content" in value:
            return _text(value["content"])
        return " ".join(filter(None, (_text(v) for v in value.values()))).strip()
    return str(value).strip()


def _title_like(value, max_chars=140):
    value = _text(value)
    if not value or len(value) > max_chars:
        return False
    if CAPTION_RE.search(value) or TABLEISH_RE.search(value):
        return True
    return len(value) <= 60 and not value.endswith(("。", ".", "；", ";", "！", "!", "？", "?"))


def _bbox_gap(upper, lower):
    try:
        return float(lower[1]) - float(upper[3])
    except (TypeError, ValueError, IndexError):
        return None


class _TableParser(HTMLParser):
    """Read HTML table cells and keep rowspan/colspan information."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self.row = None
        self.cell = None
        self.rowspan = 1
        self.colspan = 1

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "tr":
            self.row = []
        elif tag in {"td", "th"} and self.row is not None:
            attrs = dict(attrs)
            try:
                self.rowspan = max(1, int(attrs.get("rowspan") or 1))
            except ValueError:
                self.rowspan = 1
            try:
                self.colspan = max(1, int(attrs.get("colspan") or 1))
            except ValueError:
                self.colspan = 1
            self.cell = []
        elif tag == "br" and self.cell is not None:
            self.cell.append(" ")

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in {"td", "th"} and self.row is not None and self.cell is not None:
            self.row.append((_text("".join(self.cell)), self.rowspan, self.colspan))
            self.cell = None
            self.rowspan = self.colspan = 1
        elif tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = None


def _expand_html_table(html):
    parser = _TableParser()
    try:
        parser.feed(html or "")
    except Exception:
        return []

    result = []
    active = {}  # column -> (text, future_rows)
    for raw_row in parser.rows:
        row = []
        col = 0

        def fill_active(c):
            while c in active:
                while len(row) <= c:
                    row.append("")
                row[c] = active[c][0]
                c += 1
            return c

        col = fill_active(col)
        new_spans = {}
        for value, rowspan, colspan in raw_row:
            col = fill_active(col)
            for offset in range(colspan):
                c = col + offset
                while len(row) <= c:
                    row.append("")
                row[c] = value
                if rowspan > 1:
                    new_spans[c] = (value, rowspan - 1)
            col = fill_active(col + colspan)

        for c, (value, _) in active.items():
            while len(row) <= c:
                row.append("")
            if not row[c]:
                row[c] = value
        result.append(row)

        active = {c: (value, left - 1) for c, (value, left) in active.items() if left > 1}
        active.update(new_spans)

    width = max((len(row) for row in result), default=0)
    return [row + [""] * (width - len(row)) for row in result]


def _markdown_rows(body):
    rows = []
    for line in (body or "").splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if cells and all(re.fullmatch(r":?-{3,}:?", c.replace(" ", "")) for c in cells):
            continue
        rows.append(cells)
    width = max((len(row) for row in rows), default=0)
    return [row + [""] * (width - len(row)) for row in rows]


def _table_rows(body):
    if "<table" in (body or "").lower():
        rows = _expand_html_table(body)
        if rows:
            return rows
    return _markdown_rows(body)


def _row_text(rows):
    return "\n".join(" | ".join(_text(cell) for cell in row).rstrip() for row in rows).strip()


def _signature(rows, header_rows):
    if not rows:
        return ""
    value = " || ".join(" | ".join(_text(c).lower() for c in row) for row in rows[:max(1, header_rows)])
    return re.sub(r"[^\w\u4e00-\u9fff]+", " ", value).strip()[:500]


def _split_rows(rows, max_rows, header_rows):
    if len(rows) <= max_rows:
        return [rows]
    header_rows = max(0, min(header_rows, len(rows), max_rows - 1))
    header = rows[:header_rows]
    payload = rows[header_rows:]
    size = max(1, max_rows - header_rows)
    return [header + payload[i:i + size] for i in range(0, len(payload), size)]


def _flatten_v2(data):
    blocks = []
    if not isinstance(data, list):
        return blocks
    for page_idx, page in enumerate(data):
        if not isinstance(page, list):
            continue
        for item in page:
            if not isinstance(item, dict):
                continue
            kind = item.get("type")
            content = item.get("content") or {}
            common = {"bbox": item.get("bbox"), "page_idx": page_idx}
            if kind == "title":
                blocks.append({"type": "text", "text": _text(content.get("title_content")),
                               "text_level": content.get("level", 1), **common})
            elif kind == "paragraph":
                blocks.append({"type": "text", "text": _text(content.get("paragraph_content")),
                               "text_level": 0, **common})
            elif kind == "table":
                blocks.append({"type": "table",
                               "table_body": content.get("html") or content.get("table_body") or "",
                               "table_caption": content.get("table_caption") or [],
                               "table_footnote": content.get("table_footnote") or [], **common})
    return blocks


@OPERATOR_REGISTRY.register()
class KBCTableContextChunkGenerator(OperatorABC):
    """Create RAG chunks without separating a table from its outside title/caption."""

    def __init__(self, max_table_rows=12, table_header_rows=1, max_text_chars=3200,
                 external_title_max_gap=220, include_non_table_text=True,
                 inherit_table_title_across_pages=True):
        super().__init__()
        self.max_table_rows = max(2, max_table_rows)
        self.table_header_rows = max(0, table_header_rows)
        self.max_text_chars = max(256, max_text_chars)
        self.external_title_max_gap = max(0, external_title_max_gap)
        self.include_non_table_text = include_non_table_text
        self.inherit_table_title_across_pages = inherit_table_title_across_pages
        self.logger = get_logger()

    @staticmethod
    def get_desc(lang="zh"):
        if lang == "zh":
            return ("面向 PDF/RAG 的结构化分块算子：优先读取 MinerU content_list.json，"
                    "把表内 caption、表外附件/标题、章节标题和表格绑定；长表分块时重复表头，"
                    "并保留页码、标题来源和表格行元数据。")
        return ("Structure-aware PDF/RAG chunker that binds MinerU table captions, nearby external "
                "titles and section headings to every table chunk, while repeating table headers.")

    def _find_content_list(self, text_path):
        folder = Path(text_path).resolve().parent
        v1 = sorted(p for p in folder.glob("*_content_list.json") if not p.name.endswith("_content_list_v2.json"))
        if v1:
            return v1[0]
        v2 = sorted(folder.glob("*_content_list_v2.json"))
        return v2[0] if v2 else None

    def _load_blocks(self, text_path):
        content_path = self._find_content_list(text_path)
        if content_path:
            with content_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            return (_flatten_v2(data) if content_path.name.endswith("_content_list_v2.json") else data,
                    str(content_path))

        self.logger.warning("No MinerU content_list.json found next to %s; using markdown fallback.", text_path)
        if not os.path.exists(text_path):
            raise FileNotFoundError(text_path)
        with open(text_path, "r", encoding="utf-8") as f:
            markdown = f.read()
        return [{"type": "text", "text": markdown, "text_level": 0, "page_idx": None}], None

    def _choose_title(self, block, recent_text, section, last_table, signature):
        caption = _text(block.get("table_caption"))
        if caption:
            return caption, "mineru_caption"

        page_idx = block.get("page_idx")
        for candidate in reversed(recent_text[-8:]):
            value = _text(candidate.get("text"))
            if not _title_like(value):
                continue
            candidate_page = candidate.get("page_idx")
            if page_idx is not None and candidate_page is not None and page_idx != candidate_page:
                continue
            gap = _bbox_gap(candidate.get("bbox"), block.get("bbox"))
            if gap is not None and (gap < -5 or gap > self.external_title_max_gap):
                continue
            return value, "external_text"

        if self.inherit_table_title_across_pages and last_table:
            prev_page = last_table.get("page_idx")
            adjacent = (page_idx is None or prev_page is None or
                        (isinstance(page_idx, int) and isinstance(prev_page, int) and 0 <= page_idx - prev_page <= 1))
            same_header = bool(signature and signature == last_table.get("signature"))
            if adjacent and (same_header or not section) and last_table.get("title"):
                return last_table["title"], "inherited"

        return (section, "section") if section else ("", "none")

    def _build_chunks(self, blocks, source, text_path, content_path):
        chunks = []
        sections = {}
        recent_text = []
        pending = []
        pending_chars = 0
        last_table = None

        def section_title():
            return " > ".join(sections[level] for level in sorted(sections) if sections[level])

        def flush_text():
            nonlocal pending, pending_chars
            if not self.include_non_table_text or not pending:
                pending, pending_chars = [], 0
                return
            value = "\n\n".join(text for text, _ in pending if text).strip()
            pages = [p for _, p in pending if isinstance(p, int)]
            if value:
                chunks.append({
                    "rag_chunk": value, "chunk_type": "text", "table_title": "",
                    "table_title_source": "none", "section_title": section_title(),
                    "page_start": min(pages) + 1 if pages else None,
                    "page_end": max(pages) + 1 if pages else None,
                    "table_rows": None, "source": source, "text_path": text_path,
                    "content_list_path": content_path,
                })
            pending, pending_chars = [], 0

        for block in blocks:
            if not isinstance(block, dict) or block.get("type") in AUX_TYPES:
                continue
            kind = block.get("type")
            if kind == "text":
                value = _text(block.get("text"))
                if not value:
                    continue
                try:
                    level = int(block.get("text_level") or 0)
                except (TypeError, ValueError):
                    level = 0
                if level > 0:
                    flush_text()
                    for old in [x for x in sections if x >= level]:
                        sections.pop(old, None)
                    sections[level] = value
                recent_text.append(block)
                recent_text = recent_text[-16:]
                if self.include_non_table_text:
                    if pending and pending_chars + len(value) > self.max_text_chars:
                        flush_text()
                    pending.append((value, block.get("page_idx")))
                    pending_chars += len(value)
                continue

            if kind == "table":
                flush_text()
                body = block.get("table_body") or block.get("html") or ""
                rows = _table_rows(body)
                if not rows and _text(body):
                    rows = [[_text(body)]]
                signature = _signature(rows, self.table_header_rows)
                section = section_title()
                title, title_source = self._choose_title(block, recent_text, section, last_table, signature)
                footnote = _text(block.get("table_footnote"))
                parts = _split_rows(rows, self.max_table_rows, self.table_header_rows) if rows else [[]]
                page_idx = block.get("page_idx")
                page = page_idx + 1 if isinstance(page_idx, int) else None
                for part_no, part in enumerate(parts, 1):
                    values = [x for x in (title, section if section != title else "", _row_text(part),
                                          f"备注：{footnote}" if footnote else "") if x]
                    chunks.append({
                        "rag_chunk": "\n\n".join(values), "chunk_type": "table",
                        "table_title": title, "table_title_source": title_source,
                        "section_title": section, "page_start": page, "page_end": page,
                        "table_rows": part, "table_part": part_no, "table_parts": len(parts),
                        "source": source, "text_path": text_path, "content_list_path": content_path,
                    })
                last_table = {"title": title, "signature": signature, "page_idx": page_idx}
                continue

            value = _text(block.get("text") or block.get("content") or block.get("list_items") or block.get("code_body"))
            if value and self.include_non_table_text:
                if pending and pending_chars + len(value) > self.max_text_chars:
                    flush_text()
                pending.append((value, block.get("page_idx")))
                pending_chars += len(value)

        flush_text()
        for index, chunk in enumerate(chunks):
            chunk["chunk_index"] = index
        return chunks

    def run(self, storage: DataFlowStorage, input_key="text_path", source_key="source", output_key="rag_chunk"):
        dataframe = storage.read("dataframe")
        if input_key not in dataframe.columns:
            raise ValueError(f"Missing required input column: {input_key}")
        if output_key in dataframe.columns:
            raise ValueError(f"Output column already exists and would be overwritten: {output_key}")

        records = []
        for row in dataframe.to_dict(orient="records"):
            text_path = row.get(input_key)
            if not text_path:
                continue
            blocks, content_path = self._load_blocks(text_path)
            source = _text(row.get(source_key)) if source_key in row else _text(text_path)
            for chunk in self._build_chunks(blocks, source, text_path, content_path):
                item = row.copy()
                item.update(chunk)
                if output_key != "rag_chunk":
                    item[output_key] = item.pop("rag_chunk")
                records.append(item)

        output = pd.DataFrame(records)
        output_file = storage.write(output)
        self.logger.info("Generated %s structure-aware RAG chunks. Saved to %s", len(output), output_file)
        return [output_key, "chunk_type", "table_title", "table_title_source", "section_title",
                "page_start", "page_end", "table_rows", "chunk_index"]
