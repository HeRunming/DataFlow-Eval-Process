"""
StemTrueFalsePreprocessorFilter
================================
预处理判断题数据：
  - 从 question 字段提取核心命题（去除 wrapper prompt）
  - 将答案标签规范化为 True / False
  - 过滤答案无法识别（Unknown）及命题为空的记录
"""

import re

from dataflow.utils.registry import OPERATOR_REGISTRY
from dataflow import get_logger
from dataflow.utils.storage import DataFlowStorage
from dataflow.core import OperatorABC


@OPERATOR_REGISTRY.register()
class StemTrueFalsePreprocessorFilter(OperatorABC):
    """
    Step 0: 预处理判断题数据。

    - 从 `question` 字段中提取核心命题，去除 wrapper prompt。
    - 将答案标签规范化为 "True" / "False"。
    - 过滤掉答案未能识别（Unknown）或命题为空的记录。
    - 输出新字段：output_question_key (默认 question_clean), output_answer_key (默认 answer_label)。
    """

    # 匹配中文 wrapper 格式：题目：{…}
    _WRAPPER_RE = re.compile(r"题目：[「{【](.+?)[」}】]", re.DOTALL)
    # 兜底：匹配任意花括号/书名号内容
    _BRACKET_RE = re.compile(r"[{「【《](.+?)[}」】》]", re.DOTALL)
    # 匹配 "True or False?" 后缀
    _EN_SUFFIX_RE = re.compile(r"\.\s*(?:True\s+or\s+False|True/False)\s*\??$", re.IGNORECASE)

    def __init__(self):
        super().__init__()
        self.logger = get_logger()

    @staticmethod
    def get_desc(lang: str = "zh") -> str:
        if lang == "zh":
            return (
                "预处理判断题数据算子。\n"
                "功能：\n"
                "- 从 question 字段提取核心命题（去除 wrapper prompt 如\"以下是关于...题目：{...}\"）\n"
                "- 将答案标签规范化为 True / False（支持中英文多种表达）\n"
                "- 过滤掉答案无法识别（Unknown）或命题为空的记录\n\n"
                "输出字段：question_clean（核心命题），answer_label（标准化答案标签）"
            )
        return (
            "Preprocessor for True/False STEM questions.\n"
            "- Extracts core propositions from wrapper prompts\n"
            "- Normalizes answer labels to True/False\n"
            "- Filters records with unrecognized answers or empty propositions"
        )

    @staticmethod
    def _normalize_answer(raw: str) -> str:
        """将各种形式的判断答案规范化为 'True' 或 'False'。"""
        if not isinstance(raw, str):
            return "Unknown"
        s = raw.strip().lower()
        first_line = s.split("\n")[0].strip()
        if first_line in ("正确", "true", "yes", "对", "是", "√", "correct"):
            return "True"
        if first_line in ("不正确", "false", "no", "错", "否", "✗", "incorrect", "wrong"):
            return "False"
        if re.search(r"\b(正确|true|correct)\b", first_line):
            return "True"
        if re.search(r"\b(不正确|false|incorrect|wrong)\b", first_line):
            return "False"
        return "Unknown"

    @classmethod
    def _extract_core_proposition(cls, question: str) -> str:
        """从可能含 wrapper 的 question 字段中提取核心命题。"""
        if not isinstance(question, str):
            return ""

        m = cls._WRAPPER_RE.search(question)
        if m:
            return m.group(1).strip()

        m = cls._BRACKET_RE.search(question)
        if m:
            core = m.group(1).strip()
            if len(core) < len(question) * 0.9:
                return core

        cleaned = cls._EN_SUFFIX_RE.sub("", question).strip()
        if cleaned != question.strip():
            return cleaned

        if "题目：" in question:
            idx = question.index("题目：") + len("题目：")
            return question[idx:].strip()

        return question.strip()

    def run(
        self,
        storage: DataFlowStorage,
        input_question_key: str = "question",
        input_answer_key: str = "text",
        output_question_key: str = "question_clean",
        output_answer_key: str = "answer_label",
    ) -> list[str]:
        df = storage.read("dataframe")
        self.logger.info(f"[StemTrueFalsePreprocessorFilter] 读取到 {len(df)} 条数据")

        df[output_question_key] = df[input_question_key].apply(
            self._extract_core_proposition
        )
        df[output_answer_key] = df[input_answer_key].apply(self._normalize_answer)

        before = len(df)
        df = df[df[output_answer_key] != "Unknown"]
        self.logger.info(
            f"[StemTrueFalsePreprocessorFilter] 答案规范化后保留 {len(df)}/{before} 条"
        )

        df = df[df[output_question_key].str.strip() != ""]
        self.logger.info(
            f"[StemTrueFalsePreprocessorFilter] 去空后保留 {len(df)} 条"
        )

        storage.write(df)
        return [output_question_key, output_answer_key]
