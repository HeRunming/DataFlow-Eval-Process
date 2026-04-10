"""
StemMCToOpenEndedRewriterGenerator
===================================
调用 LLM 将多选题题干改写为开放式问答题。
自动检测中/英文，分别使用对应的 Few-Shot prompt。

特别处理多选题的特点：
- 需要包含所有选项供学生分析
- 改写过程中不能暗示哪些选项是正确的
- 改写后问题应该能够根据题干独立求解
"""

import re

from dataflow.utils.registry import OPERATOR_REGISTRY
from dataflow import get_logger
from dataflow.utils.storage import DataFlowStorage
from dataflow.core import OperatorABC, LLMServingABC
from dataflow.prompts.reasoning.stem import StemMCRewritePrompt


@OPERATOR_REGISTRY.register()
class StemMCToOpenEndedRewriterGenerator(OperatorABC):
    """
    将多选题题干改写为开放式问答题。

    使用中英双语 Few-Shot prompt，自动根据输入文本语言选择模板。
    对 LLM 输出做后处理（去掉前缀、思维链标签等）。
    
    多选题改写特点：
    - 需要保留完整选项列表供学生分析
    - 改写方式必须中立，不能泄漏答案
    - 改写后问题能够根据题干独立求解
    """

    def __init__(self, llm_serving: LLMServingABC = None):
        super().__init__()
        self.logger = get_logger()
        self.llm_serving = llm_serving
        self._prompt_builder = StemMCRewritePrompt()

    @staticmethod
    def get_desc(lang: str = "zh") -> str:
        if lang == "zh":
            return (
                "多选题改写算子（LLM）。\n"
                "将多选题的题干改写为中立的开放式问答题，包含所有选项但不泄漏答案。\n"
                "- 自动检测中英文，使用对应的 Few-Shot prompt\n"
                "- 对 LLM 输出做后处理，去除前缀、思维链标签\n\n"
                "输入字段：input_question_key（题干），input_options_key（选项列表）\n"
                "输出字段：output_key（改写后问题，默认 question_rewritten）"
            )
        return (
            "Multiple-choice to open-ended question rewriter (LLM-based).\n"
            "Rewrites MC question stems into neutral open-ended questions without answer leakage."
        )

    @staticmethod
    def _postprocess(raw: str) -> str:
        """去掉 LLM 可能输出的前缀/后缀噪音。"""
        if not isinstance(raw, str):
            return ""
        prefixes = ["改写后：", "改写后:", "Rewritten:", "Rewritten：", "Question:", "Revised:"]
        for p in prefixes:
            if raw.strip().startswith(p):
                raw = raw.strip()[len(p):].strip()
        raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
        raw = re.sub(r"^#+\s*", "", raw, flags=re.MULTILINE).strip()  # 去掉 markdown 标题
        return raw.strip()

    def run(
        self,
        storage: DataFlowStorage,
        input_question_key: str = "question_clean",
        input_options_key: str = "options_text",
        output_key: str = "question_rewritten",
    ) -> list[str]:
        df = storage.read("dataframe")
        self.logger.info(f"[StemMCToOpenEndedRewriterGenerator] 改写 {len(df)} 条数据")

        # 构建 prompts：将题干与选项合并为单一字符串传入新版 build_prompt
        prompts = [
            self._prompt_builder.build_prompt(
                f"{q}\n{opts}" if opts and str(opts).strip() else str(q)
            )
            for q, opts in zip(df[input_question_key], df[input_options_key])
        ]
        responses = self.llm_serving.generate_from_input(prompts)
        df[output_key] = [self._postprocess(r) for r in responses]

        before = len(df)
        df = df[df[output_key].str.strip() != ""]
        self.logger.info(
            f"[StemMCToOpenEndedRewriterGenerator] 去空后保留 {len(df)}/{before} 条"
        )

        storage.write(df)
        return [output_key]
