"""
StemAnswerLeakFilter
=====================
验证改写后的开放式问题是否泄漏了原始判断题的答案。
调用 LLM 作为 Judge，解析 {"judgement_test": true/false} 格式输出：
  - judgement_test = true  → 未泄漏，保留
  - judgement_test = false → 泄漏，过滤
"""

import re

import pandas as pd

from dataflow.utils.registry import OPERATOR_REGISTRY
from dataflow import get_logger
from dataflow.utils.storage import DataFlowStorage
from dataflow.core import OperatorABC, LLMServingABC
from dataflow.prompts.reasoning.stem import StemAnswerLeakDetectionPrompt


@OPERATOR_REGISTRY.register()
class StemAnswerLeakFilter(OperatorABC):
    """
    验证改写后的开放式问题是否隐含原始判断题答案。

    调用 LLM Judge 检验每条改写结果，过滤掉答案泄漏的样本。
    """

    def __init__(
        self,
        llm_serving: LLMServingABC = None,
        system_prompt: str = "You are a strict QA evaluator.",
    ):
        super().__init__()
        self.logger = get_logger()
        self.llm_serving = llm_serving
        self.system_prompt = system_prompt
        self._prompt_builder = StemAnswerLeakDetectionPrompt()

    @staticmethod
    def get_desc(lang: str = "zh") -> str:
        if lang == "zh":
            return (
                "答案泄漏过滤算子（LLM Judge）。\n"
                "对改写后的开放式问题，调用 LLM 判断是否隐含了原始判断题的答案（True/False）。\n"
                "- judgement_test = true：未泄漏，保留该样本\n"
                "- judgement_test = false：答案泄漏，过滤该样本\n\n"
                "输入字段：input_question_key（改写后的问题），input_answer_key（原始答案标签）"
            )
        return (
            "Answer leak filter using LLM as judge.\n"
            "Filters out rewritten questions that implicitly reveal the original True/False answer."
        )

    @staticmethod
    def _parse_judgement(response: str) -> bool:
        """从 LLM 输出中提取 judgement_test 的布尔值。"""
        if not response or not response.strip():
            return False
        pattern = re.compile(r'"judgement_test"\s*:\s*(true|false)', re.IGNORECASE)
        m = pattern.search(response)
        if m:
            return m.group(1).lower() == "true"
        lower = response.lower()
        if "judgement_test" in lower:
            return "true" in lower[lower.index("judgement_test"):]
        return False

    def run(
        self,
        storage: DataFlowStorage,
        input_question_key: str = "question_rewritten",
        input_answer_key: str = "answer_label",
    ) -> list[str]:
        df = storage.read("dataframe")
        self.logger.info(f"[StemAnswerLeakFilter] 检验 {len(df)} 条数据")

        prompts = [
            self._prompt_builder.build_prompt(q, a)
            for q, a in zip(df[input_question_key], df[input_answer_key])
        ]
        responses = self.llm_serving.generate_from_input(
            user_inputs=prompts,
            system_prompt=self.system_prompt,
        )
        judgements = [self._parse_judgement(r) for r in responses]

        before = len(df)
        df = df.reset_index(drop=True)
        mask = pd.Series(judgements, index=df.index)
        df_filtered = df[mask]
        self.logger.info(
            f"[StemAnswerLeakFilter] 无泄漏保留 {len(df_filtered)}/{before} 条"
        )

        storage.write(df_filtered)
        return [input_question_key]
