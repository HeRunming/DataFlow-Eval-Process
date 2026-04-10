"""
StemMCAnswerLeakFilter
=======================
验证改写后的开放式问题是否泄漏了原始多选题的答案，或机械保留了选项列表格式。
调用 LLM 作为 Judge，解析 {"has_leak": true/false, "retains_options": true/false} 格式输出：
  - has_leak = false 且 retains_options = false → 质量合格，保留
  - has_leak = true                             → 答案泄漏，过滤
  - retains_options = true                      → 机械罗列选项，过滤

多选题的答案泄漏检测特点：
- 需要考虑所有正确答案组合（例如 A,B,D）
- 需要检测隐含的选项优先级（某些选项被重点分析）
- 需要检测是否暗示哪些选项是错的
- 需要检测是否将选项机械转为逐条核查列表
"""

import re

import pandas as pd

from dataflow.utils.registry import OPERATOR_REGISTRY
from dataflow import get_logger
from dataflow.utils.storage import DataFlowStorage
from dataflow.core import OperatorABC, LLMServingABC
from dataflow.prompts.reasoning.stem import StemMCAnswerLeakDetectionPrompt


@OPERATOR_REGISTRY.register()
class StemMCAnswerLeakFilter(OperatorABC):
    """
    验证改写后的开放式问题是否泄漏了原始多选题的答案，或机械保留了选项列表。

    调用 LLM Judge 检验每条改写结果，过滤掉答案泄漏或机械罗列选项的样本。

    多选题泄漏检测特点：
    - 检测是否直接指示正确答案组合
    - 检测是否通过隐含的优先级暗示答案
    - 检测是否通过逻辑结构镜像原始答案
    - 检测是否使用词汇选择来暗示答案
    - 检测是否将选项机械转为逐条核查清单（retains_options）
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
        self._prompt_builder = StemMCAnswerLeakDetectionPrompt()

    @staticmethod
    def get_desc(lang: str = "zh") -> str:
        if lang == "zh":
            return (
                "多选题答案泄漏过滤算子（LLM Judge）。\n"
                "对改写后的开放式问题，调用 LLM 判断是否泄漏了原始多选题的答案，或机械保留了选项列表。\n"
                "- has_leak = false 且 retains_options = false：质量合格，保留该样本\n"
                "- has_leak = true：答案泄漏，过滤该样本\n"
                "- retains_options = true：机械罗列选项，过滤该样本\n\n"
                "输入字段：input_question_key（改写后的问题），\n"
                "         input_answer_key（原始答案标签），\n"
                "         input_options_key（原始选项列表）"
            )
        return (
            "Answer leak filter for multiple-choice questions (LLM Judge).\n"
            "Filters out rewritten questions that implicitly reveal which options were correct."
        )

    @staticmethod
    def _parse_judgement(response: str) -> bool:
        """从 LLM 输出中提取 has_leak 和 retains_options，返回 False 表示应过滤。"""
        if not response or not response.strip():
            return False

        # 检查 has_leak
        leak_pattern = re.compile(r'"has_leak"\s*:\s*(true|false)', re.IGNORECASE)
        m = leak_pattern.search(response)
        if m:
            has_leak = m.group(1).lower() == "true"
            if has_leak:
                return False  # 有泄漏，过滤

        # 检查 retains_options
        retains_pattern = re.compile(r'"retains_options"\s*:\s*(true|false)', re.IGNORECASE)
        m2 = retains_pattern.search(response)
        if m2:
            retains_options = m2.group(1).lower() == "true"
            if retains_options:
                return False  # 机械保留选项，过滤

        # 如果找到了 has_leak=false 且无 retains_options=true，则保留
        if m:
            return True

        # 备用方案：搜索 JSON 中的 has_leak
        lower = response.lower()
        if "has_leak" in lower:
            after_has_leak = lower[lower.index("has_leak"):]
            # 检查紧跟其后的 true/false
            if "true" in after_has_leak[:20]:
                return False  # 有泄漏，应过滤
            elif "false" in after_has_leak[:20]:
                return True   # 无泄漏，应保留

        # 默认保留（无泄漏）
        return True

    def run(
        self,
        storage: DataFlowStorage,
        input_question_key: str = "question_rewritten",
        input_answer_key: str = "answer_label",
        input_options_key: str = "options_text",
    ) -> list[str]:
        df = storage.read("dataframe")
        self.logger.info(f"[StemMCAnswerLeakFilter] 检验 {len(df)} 条数据")

        # 构建 prompts，包含改写后的问题、原始答案和原始选项
        prompts = [
            self._prompt_builder.build_prompt(q, a, opts)
            for q, a, opts in zip(
                df[input_question_key],
                df[input_answer_key],
                df[input_options_key]
            )
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
            f"[StemMCAnswerLeakFilter] 无泄漏保留 {len(df_filtered)}/{before} 条"
        )

        storage.write(df_filtered)
        return [input_question_key]
