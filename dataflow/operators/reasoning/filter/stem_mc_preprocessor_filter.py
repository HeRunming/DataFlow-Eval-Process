"""
StemMCPreprocessorFilter
=======================
预处理多选题数据：
  - 从 question 字段提取核心命题（去除 wrapper prompt）
  - 将答案标签规范化为标准格式（A,B,C）
  - 修复 M1 格式（选项内嵌无分行）
  - 从 question 中提取/结构化选项为独立字段
  - 对所有数据打质量标签（preprocess_label），不删除任何记录

关键操作流程：
  1. 正则提取 wrapper：题目：{...} → 核心命题
  2. 答案格式统一：AB → A,B，处理各种分隔符
  3. M1 修复：选项 A B C D 内嵌 → 添加换行
  4. 选项提取：从命题中按行解析 A. / B. / C. 等格式
  5. 质量打标：ok / no_answer / no_question / few_options（保留全部数据）
"""

import re

from dataflow.utils.registry import OPERATOR_REGISTRY
from dataflow import get_logger
from dataflow.utils.storage import DataFlowStorage
from dataflow.core import OperatorABC


@OPERATOR_REGISTRY.register()
class StemMCPreprocessorFilter(OperatorABC):
    """
    Step 0: 预处理多选题数据。

    功能：
    - 从 `question` 字段提取核心命题，去除 wrapper prompt（题目：{...} 等）
    - 将答案标签规范化为 "A,B,C" 标准格式，支持多种输入格式：
      * AB, ABC, ABCD（无分隔）
      * A,B,C 或 A、B、C（已分隔）
      * A. 答案是... B. ... 等包含完整答案的形式
    - 修复 M1 格式（内嵌无分行选项），在选项前插入换行
    - 从命题中提取/结构化选项为独立字段（option_a, option_b, option_c, option_d）
    - 对所有数据打质量标签，不删除任何记录（供后续分析不同 label 的改写性能）

    输入字段：
      - question: 原始多选题题干（可能包含 wrapper 和选项）
      - text: 原始答案标签（多种格式）

    输出字段：
      - question_clean: 去除 wrapper 的核心命题
      - answer_label: 标准化后的答案（A,B,C 格式）
      - options_text: 提取的选项原文
      - option_a, option_b, option_c, option_d: 各选项内容
      - num_options: 提取到的选项总数
      - preprocess_label: 质量标签（ok / no_answer / no_question / few_options）
    """

    # Wrapper 提取正则
    _WRAPPER_PATTERNS = [
        re.compile(r"题目：\{(.+?)\}", re.DOTALL),       # 题目：{...}
        re.compile(r"题目：「(.+?)」", re.DOTALL),         # 题目：「...」
        re.compile(r"题目：【(.+?)】", re.DOTALL),         # 题目：【...】
        re.compile(r"题目：《(.+?)》", re.DOTALL),         # 题目：《...》
    ]
    _TOPIC_RE = re.compile(r"题目：(.+)", re.DOTALL)

    # M1 检测：选项内嵌无分行（形如 "A. xxx B. yyy C. zzz"，选项之间无 \n）
    _M1_INLINE_RE = re.compile(
        r"(?<!\n)\s+([ABCD])[.、．\s]",
    )

    # 选项提取正则（从多行命题中提取 A/B/C/D）
    # 支持分隔符：. 、 ． : (冒号) 及空白符
    _OPTION_LINE_RE = re.compile(r"^([ABCD])[.、．:\s]\s*(.+)$", re.MULTILINE)


    def __init__(self):
        super().__init__()
        self.logger = get_logger()

    @staticmethod
    def get_desc(lang: str = "zh") -> str:
        if lang == "zh":
            return (
                "预处理多选题数据算子。\n"
                "功能：\n"
                "- 从 question 字段提取核心命题（去除 wrapper prompt 如\"题目：{...}\"）\n"
                "- 将答案标签规范化为 A,B,C 标准格式（支持多种输入格式）\n"
                "- 修复 M1 格式（选项内嵌无分行）\n"
                "- 从命题中提取/结构化选项为独立字段\n"
                "- 对所有数据打质量标签（preprocess_label: ok/no_answer/no_question/few_options），不删除任何记录\n\n"
                "输出字段：question_clean（核心命题），answer_label（标准化答案标签），"
                "option_a/b/c/d（各选项内容），num_options（选项总数），preprocess_label（质量标签）"
            )
        return (
            "Preprocessor for multiple-choice STEM questions.\n"
            "- Extracts core propositions from wrapper prompts\n"
            "- Normalizes answer labels to A,B,C format\n"
            "- Fixes M1 format (inline options without line breaks)\n"
            "- Structures options into separate fields\n"
            "- Filters records with empty answers or propositions"
        )

    @classmethod
    def _extract_core_proposition(cls, question: str) -> str:
        """从可能含 wrapper 的 question 字段中提取核心命题。"""
        if not isinstance(question, str):
            return ""

        # 尝试各个 wrapper 格式
        for pat in cls._WRAPPER_PATTERNS:
            m = pat.search(question)
            if m:
                return m.group(1).strip()

        # 退化提取：直接取"题目："之后的内容
        m = cls._TOPIC_RE.search(question)
        if m:
            return m.group(1).strip()

        return question.strip()

    @classmethod
    def _normalize_answer(cls, raw: str) -> str:
        """
        将各种形式的多选答案规范化为 'A,B,C' 标准格式。

        支持：
        - AB, ABC, ABCD (无分隔)
        - A,B,C 或 A、B、C (各种分隔)
        - "答案：A" / "答案是C" / "Answer: B" 等含提示词格式

        只从答案文本的首行或前 30 字符中提取，避免扫描全文把
        说明性文字中的 A-D 字母错误纳入（如 "the answer is C,
        because A and B are also ..."）。
        """
        if not isinstance(raw, str):
            return ""

        raw = raw.strip()
        if not raw:
            return ""

        # 优先匹配明确答案声明，按优先级尝试各种格式
        _EXPLICIT_PATTERNS = [
            # 中文：答案[：:是为]? A,B  或  答案为A和B
            re.compile(r"答案[：:是为]?\s*([ABCD][,，、和\s]*(?:[ABCD][,，、和\s]*)*)"),
            # 英文冒号/空格：Answer: B / answer B
            re.compile(r"[Aa]nswer\s*[:\s]\s*([ABCD][,，、和\s]*(?:[ABCD][,，、和\s]*)*)"),
            # 英文 "is"：The answer is C / answer is A and C / answer is A,B
            re.compile(r"[Aa]nswer\s+is\s+([ABCD](?:[,，、和\s]+(?:and\s+)?[ABCD])*)"),
        ]
        for pat in _EXPLICIT_PATTERNS:
            m = pat.search(raw)
            if m:
                # Use word-boundary match to avoid false positives from words like "and"
                candidates = re.findall(r"\b([ABCD])\b", m.group(1).upper())
                if candidates:
                    return ",".join(sorted(set(candidates)))

        # 只取第一行（或前 30 字符）做提取，防止扫全文
        first_line = raw.splitlines()[0].strip()
        search_text = first_line[:30] if len(first_line) > 30 else first_line

        candidates = re.findall(r"[ABCD]", search_text.upper())
        if candidates:
            return ",".join(sorted(set(candidates)))

        return ""

    @classmethod
    def _fix_m1_format(cls, question: str) -> str:
        """
        修复 M1 格式（选项内嵌无分行）。
        检测到 A. B. C. D. 但无换行，则在选项前插入 \n。
        """
        if not isinstance(question, str):
            return question

        # 检测：有 A. / A: / B. / B: 等但没有换行
        has_a = re.search(r"\bA[.、．:]", question)
        has_b = re.search(r"\bB[.、．:]", question)
        if not (has_a and has_b):
            return question  # 至少需要 A 和 B 才能判定为多选

        # 检测是否已有换行（\nA 或 \nB 等）
        if "\nA" in question or "\nB" in question:
            return question  # 已经是多行格式

        # M1 修复：在 A/B/C/D 前插入换行（支持 . 、 ． : 等分隔符）
        fixed = re.sub(r"\s+([ABCD])[.、．:]\s*", r"\n\1. ", question)
        return fixed.strip()

    @classmethod
    def _extract_options(cls, question: str) -> dict:
        """
        从命题中提取 A/B/C/D 选项。
        
        返回：{
            "option_a": "选项内容" or None,
            "option_b": "选项内容" or None,
            ...
            "num_options": 3,  # 找到的选项总数
            "options_text": "A. xxx\nB. yyy\n..."  # 原始提取文本
        }
        """
        options = {}
        options_text_lines = []

        matches = cls._OPTION_LINE_RE.findall(question)
        if not matches:
            return {
                "option_a": None,
                "option_b": None,
                "option_c": None,
                "option_d": None,
                "num_options": 0,
                "options_text": "",
            }

        for letter, content in matches:
            options[f"option_{letter.lower()}"] = content.strip()
            options_text_lines.append(f"{letter}. {content.strip()}")

        # 填充缺失选项
        for letter in "ABCD":
            key = f"option_{letter.lower()}"
            if key not in options:
                options[key] = None

        options["num_options"] = len(matches)
        options["options_text"] = "\n".join(options_text_lines)

        return options

    def run(
        self,
        storage: DataFlowStorage,
        input_question_key: str = "question",
        input_answer_key: str = "text",
        output_question_key: str = "question_clean",
        output_answer_key: str = "answer_label",
        output_options_text_key: str = "options_text",
        output_num_options_key: str = "num_options",
        min_required_options: int = 2,
    ) -> list[str]:
        """
        执行多选题预处理。

        Args:
            storage: DataFlowStorage 实例
            input_question_key: 输入的题干字段名（默认 "question"）
            input_answer_key: 输入的答案字段名（默认 "text"）
            output_question_key: 输出的核心命题字段名（默认 "question_clean"）
            output_answer_key: 输出的标准化答案字段名（默认 "answer_label"）
            output_options_text_key: 输出的选项原文字段名（默认 "options_text"）
            output_num_options_key: 输出的选项总数字段名（默认 "num_options"）
            min_required_options: 最少需要的选项数（默认 2）

        Returns:
            输出字段列表
        """
        df = storage.read("dataframe")
        self.logger.info(f"[StemMCPreprocessorFilter] 读取到 {len(df)} 条数据")

        # Step 1: 提取核心命题
        df[output_question_key] = df[input_question_key].apply(
            self._extract_core_proposition
        )

        # Step 2: 修复 M1 格式
        df[input_question_key] = df[output_question_key].apply(self._fix_m1_format)

        # Step 3: 规范化答案
        df[output_answer_key] = df[input_answer_key].apply(self._normalize_answer)

        # Step 4: 提取选项
        options_data = df[output_question_key].apply(self._extract_options)
        for key in ["option_a", "option_b", "option_c", "option_d", "num_options", "options_text"]:
            if key == "options_text":
                df[output_options_text_key] = options_data.apply(lambda x: x[key])
            elif key == "num_options":
                df[output_num_options_key] = options_data.apply(lambda x: x[key])
            else:
                df[key] = options_data.apply(lambda x: x[key])

        # Step 5: 质量打标（不过滤，仅标记问题类型供后续分析）
        def _assign_label(row) -> str:
            if not isinstance(row[output_question_key], str) or not row[output_question_key].strip():
                return "no_question"
            if not isinstance(row[output_answer_key], str) or not row[output_answer_key].strip():
                return "no_answer"
            if row[output_num_options_key] < min_required_options:
                return "few_options"
            return "ok"

        df["preprocess_label"] = df.apply(_assign_label, axis=1)

        label_counts = df["preprocess_label"].value_counts().to_dict()
        self.logger.info(
            f"[StemMCPreprocessorFilter] 全部 {len(df)} 条数据保留，质量标签分布：{label_counts}"
        )

        storage.write(df)
        return [
            output_question_key,
            output_answer_key,
            output_options_text_key,
            output_num_options_key,
            "option_a",
            "option_b",
            "option_c",
            "option_d",
            "preprocess_label",
        ]
