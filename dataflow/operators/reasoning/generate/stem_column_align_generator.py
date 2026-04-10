"""
StemColumnAlignGenerator
=========================
将改写后的 question_rewritten 字段重命名为 question，
并只保留与 open_ended 数据集对齐的字段列表。
"""

from dataflow.utils.registry import OPERATOR_REGISTRY
from dataflow import get_logger
from dataflow.utils.storage import DataFlowStorage
from dataflow.core import OperatorABC


@OPERATOR_REGISTRY.register()
class StemColumnAlignGenerator(OperatorABC):
    """
    字段对齐算子：将改写结果与 open_ended 数据集格式对齐。

    - 将 rewritten_key (默认 question_rewritten) 的值写入 question 字段
    - 只保留 open_ended 数据集的标准字段（存在的字段才保留）

    open_ended 数据集标准字段：
        answer_model, dataset_name, id, not_zh, ori,
        std_answer_model, text, title, question
    """

    _KEEP_COLS = [
        "answer_model", "dataset_name", "id", "not_zh",
        "ori", "std_answer_model", "text", "title", "question",
    ]

    def __init__(self):
        super().__init__()
        self.logger = get_logger()

    @staticmethod
    def get_desc(lang: str = "zh") -> str:
        if lang == "zh":
            return (
                "字段对齐算子。\n"
                "将 question_rewritten 字段重命名为 question，"
                "并只保留与 open_ended 数据集对齐的标准字段：\n"
                "answer_model, dataset_name, id, not_zh, ori, "
                "std_answer_model, text, title, question"
            )
        return (
            "Column alignment operator for STEM TF→OE pipeline.\n"
            "Renames rewritten question column to 'question' and keeps only open_ended-aligned fields."
        )

    def run(
        self,
        storage: DataFlowStorage,
        input_rewritten_key: str = "question_rewritten",
    ) -> list[str]:
        df = storage.read("dataframe")

        if input_rewritten_key in df.columns:
            df["question"] = df[input_rewritten_key]

        keep = [c for c in self._KEEP_COLS if c in df.columns]
        df = df[keep]

        self.logger.info(
            f"[StemColumnAlignGenerator] 最终输出 {len(df)} 条，字段：{keep}"
        )
        storage.write(df)
        return ["question"]
