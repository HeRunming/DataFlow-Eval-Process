import json

import pandas as pd

from dataflow.operators.knowledge_cleaning.generate.kbc_table_context_chunk_generator import (
    KBCTableContextChunkGenerator,
    _expand_html_table,
)


class MemoryStorage:
    def __init__(self, dataframe):
        self.dataframe = dataframe
        self.written = None

    def read(self, output_type):
        assert output_type == "dataframe"
        return self.dataframe

    def write(self, data):
        self.written = data
        return "memory://output"


def test_expand_html_table_rowspan():
    rows = _expand_html_table(
        "<table><tr><th>规格</th><th>标准</th></tr>"
        "<tr><td rowspan='2'>A类</td><td>100</td></tr>"
        "<tr><td>200</td></tr></table>"
    )
    assert rows == [["规格", "标准"], ["A类", "100"], ["A类", "200"]]


def test_external_title_is_attached_and_repeated(tmp_path):
    md_path = tmp_path / "full.md"
    md_path.write_text("placeholder", encoding="utf-8")
    content_path = tmp_path / "sample_content_list.json"
    content_path.write_text(
        json.dumps(
            [
                {
                    "type": "text",
                    "text": "附件二：唐山、天津本地接待标准",
                    "text_level": 0,
                    "bbox": [40, 40, 500, 80],
                    "page_idx": 0,
                },
                {
                    "type": "table",
                    "table_caption": [],
                    "table_footnote": [],
                    "table_body": (
                        "<table><tr><th>规格</th><th>来宾范围</th></tr>"
                        "<tr><td>A+类接待</td><td>正厅级以上</td></tr>"
                        "<tr><td>A类接待</td><td>正处级以上</td></tr>"
                        "<tr><td>B类接待</td><td>副科级以上</td></tr></table>"
                    ),
                    "bbox": [40, 100, 950, 900],
                    "page_idx": 0,
                },
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    storage = MemoryStorage(pd.DataFrame([{"source": "sample.pdf", "text_path": str(md_path)}]))
    op = KBCTableContextChunkGenerator(max_table_rows=3, table_header_rows=1)
    op.run(storage=storage)

    tables = storage.written[storage.written["chunk_type"] == "table"]
    assert len(tables) == 2
    assert set(tables["table_title"]) == {"附件二：唐山、天津本地接待标准"}
    assert set(tables["table_title_source"]) == {"external_text"}
    assert all("规格 | 来宾范围" in text for text in tables["rag_chunk"])
    assert all("附件二：唐山、天津本地接待标准" in text for text in tables["rag_chunk"])
