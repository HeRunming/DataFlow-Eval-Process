"""PDF table cleaning pipeline for RAG.

The pipeline uses MinerU for PDF layout/table extraction, then performs structure-aware
chunking so captions or short table titles located outside the table remain attached to
every table chunk. The final JSONL is ready to feed to an embedding/vector-store stage.
"""

import argparse
import json
import os
from pathlib import Path

from dataflow.operators.knowledge_cleaning import (
    FileOrURLToMarkdownConverterAPI,
    KBCTableContextChunkGenerator,
)
from dataflow.utils.storage import FileStorage


class KBCleaningPDFTableRAG_APIPipeline:
    def __init__(
        self,
        input_file: str,
        cache_path: str = "./.cache/api/pdf_table_rag",
        intermediate_dir: str = "./.cache/api/pdf_table_rag/mineru",
        mineru_backend: str = "vlm",
        mineru_api_key: str = None,
        max_table_rows: int = 12,
        max_text_chars: int = 3200,
    ):
        self.storage = FileStorage(
            first_entry_file_name=input_file,
            cache_path=cache_path,
            file_name_prefix="pdf_table_rag_step",
            cache_type="jsonl",
        )

        self.pdf_to_markdown = FileOrURLToMarkdownConverterAPI(
            intermediate_dir=intermediate_dir,
            mineru_backend=mineru_backend,
            api_key=mineru_api_key,
        )
        self.structure_chunker = KBCTableContextChunkGenerator(
            max_table_rows=max_table_rows,
            table_header_rows=1,
            max_text_chars=max_text_chars,
            include_non_table_text=True,
            inherit_table_title_across_pages=True,
        )

    def forward(self):
        self.pdf_to_markdown.run(
            storage=self.storage.step(),
            input_key="source",
            output_key="text_path",
        )
        return self.structure_chunker.run(
            storage=self.storage.step(),
            input_key="text_path",
            source_key="source",
            output_key="rag_chunk",
        )


def _single_pdf_input(pdf_path: str, cache_path: str) -> str:
    pdf_path = str(Path(pdf_path).expanduser().resolve())
    if not os.path.exists(pdf_path):
        raise FileNotFoundError(pdf_path)
    input_path = Path(cache_path) / "single_pdf_input.jsonl"
    input_path.parent.mkdir(parents=True, exist_ok=True)
    with input_path.open("w", encoding="utf-8") as f:
        f.write(json.dumps({"source": pdf_path}, ensure_ascii=False) + "\n")
    return str(input_path)


def main():
    parser = argparse.ArgumentParser(description="Clean PDF tables into RAG-ready chunks with external table-title context.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--pdf", help="A local PDF path.")
    source.add_argument("--input-jsonl", help="JSONL input containing a `source` column (local path or URL).")
    parser.add_argument("--cache-path", default="./.cache/api/pdf_table_rag")
    parser.add_argument("--intermediate-dir", default="./.cache/api/pdf_table_rag/mineru")
    parser.add_argument("--mineru-backend", default="vlm", choices=["vlm", "pipeline"])
    parser.add_argument("--max-table-rows", type=int, default=12)
    parser.add_argument("--max-text-chars", type=int, default=3200)
    args = parser.parse_args()

    input_file = args.input_jsonl or _single_pdf_input(args.pdf, args.cache_path)
    pipeline = KBCleaningPDFTableRAG_APIPipeline(
        input_file=input_file,
        cache_path=args.cache_path,
        intermediate_dir=args.intermediate_dir,
        mineru_backend=args.mineru_backend,
        max_table_rows=args.max_table_rows,
        max_text_chars=args.max_text_chars,
    )
    pipeline.forward()


if __name__ == "__main__":
    main()
