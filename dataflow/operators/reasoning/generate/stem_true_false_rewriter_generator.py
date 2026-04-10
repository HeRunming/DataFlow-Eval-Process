"""
StemTrueFalseRewriterGenerator
================================
调用 LLM 将判断题核心命题改写为开放式问答题。
自动检测中/英文，分别使用对应的 Few-Shot prompt。
"""

import re

from dataflow.utils.registry import OPERATOR_REGISTRY
from dataflow import get_logger
from dataflow.utils.storage import DataFlowStorage
from dataflow.core import OperatorABC, LLMServingABC
from dataflow.prompts.reasoning.stem import StemTrueFalseRewritePrompt


@OPERATOR_REGISTRY.register()
class StemTrueFalseRewriterGenerator(OperatorABC):
    """
    将判断题核心命题改写为开放式问答题。

    使用中英双语 Few-Shot prompt，自动根据输入文本语言选择模板。
    对 LLM 输出做后处理（去掉前缀、思维链标签等）。
    """

    def __init__(self, llm_serving: LLMServingABC = None):
        super().__init__()
        self.logger = get_logger()
        self.llm_serving = llm_serving
        self._prompt_builder = StemTrueFalseRewritePrompt()

    @staticmethod
    def get_desc(lang: str = "zh") -> str:
        if lang == "zh":
            return (
                "判断题改写算子（LLM）。\n"
                "将判断题的核心命题改写为中立的开放式问答题，不泄漏原始答案（True/False）。\n"
                "- 自动检测中英文，使用对应的 Few-Shot prompt\n"
                "- 对 LLM 输出做后处理，去除前缀、思维链标签\n\n"
                "输入字段：input_key（核心命题，默认 question_clean）\n"
                "输出字段：output_key（改写后问题，默认 question_rewritten）"
            )
        return (
            "True/False to open-ended question rewriter (LLM-based).\n"
            "Rewrites T/F propositions into neutral open-ended questions without answer leakage."
        )

    @staticmethod
    def _postprocess(raw: str) -> str:
        """去掉 LLM 可能输出的前缀/后缀噪音。"""
        if not isinstance(raw, str):
            return ""
        prefixes = ["改写后：", "改写后:", "Rewritten:", "Rewritten：", "Question:"]
        for p in prefixes:
            if raw.strip().startswith(p):
                raw = raw.strip()[len(p):].strip()
        raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
        return raw.strip()

    def run(
        self,
        storage: DataFlowStorage,
        input_key: str = "question_clean",
        output_key: str = "question_rewritten",
    ) -> list[str]:
        df = storage.read("dataframe")
        self.logger.info(f"[StemTrueFalseRewriterGenerator] 改写 {len(df)} 条数据")

        prompts = [self._prompt_builder.build_prompt(q) for q in df[input_key]]
        responses = self.llm_serving.generate_from_input(prompts)
        df[output_key] = [self._postprocess(r) for r in responses]

        before = len(df)
        df = df[df[output_key].str.strip() != ""]
        self.logger.info(
            f"[StemTrueFalseRewriterGenerator] 去空后保留 {len(df)}/{before} 条"
        )

        storage.write(df)
        return [output_key]
