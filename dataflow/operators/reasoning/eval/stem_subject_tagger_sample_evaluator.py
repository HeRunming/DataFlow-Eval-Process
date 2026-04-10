"""
StemSubjectTaggerSampleEvaluator
=================================
调用 LLM 对 STEM 问题打学科标签。
支持：数学、物理、化学、生物、计算机科学、其他

用途：
1. 验证留下来的数据是否属于 STEM / 数学领域
2. 观察各学科类别分布是否均衡，发现数据偏斜问题
"""

import re
import json

from dataflow.utils.registry import OPERATOR_REGISTRY
from dataflow import get_logger
from dataflow.utils.storage import DataFlowStorage
from dataflow.core import OperatorABC, LLMServingABC
from dataflow.prompts.reasoning.stem import StemSubjectTaggingPrompt


@OPERATOR_REGISTRY.register()
class StemSubjectTaggerSampleEvaluator(OperatorABC):
    """
    STEM 学科标签打标算子（LLM）。

    输出字段：
        output_subject_key (默认 subject_tag)  : 学科名称
        output_conf_key    (默认 subject_conf) : 置信度 (high/medium/low)
    """

    _VALID_SUBJECTS = {"数学", "物理", "化学", "生物", "计算机科学", "其他"}

    def __init__(self, llm_serving: LLMServingABC = None):
        super().__init__()
        self.logger = get_logger()
        self.llm_serving = llm_serving
        self._prompt_builder = StemSubjectTaggingPrompt()

    @staticmethod
    def get_desc(lang: str = "zh") -> str:
        if lang == "zh":
            return (
                "STEM 学科标签打标算子（LLM）。\n"
                "对每个问题调用 LLM 判断其所属学科：数学、物理、化学、生物、计算机科学、其他。\n\n"
                "用途：\n"
                "- 验证数据是否属于 STEM/数学领域\n"
                "- 分析学科分布是否均衡\n\n"
                "输入字段：input_key（问题文本，默认 question）\n"
                "输出字段：subject_tag（学科名称），subject_conf（置信度）"
            )
        return (
            "STEM subject tagger (LLM-based).\n"
            "Tags each question with a subject label: 数学/物理/化学/生物/计算机科学/其他.\n"
            "Useful for verifying data distribution and domain coverage."
        )

    @classmethod
    def _parse_subject(cls, response: str) -> tuple[str, str]:
        """
        从 LLM 输出中提取学科标签和置信度。
        返回 (subject_tag, confidence)；解析失败时返回 ("其他", "low")。
        """
        if not response or not response.strip():
            return "其他", "low"

        json_match = re.search(r'\{[^{}]*"subject"[^{}]*\}', response, re.DOTALL)
        if json_match:
            try:
                obj = json.loads(json_match.group(0))
                subject = str(obj.get("subject", "其他")).strip()
                confidence = str(obj.get("confidence", "low")).strip()
                if subject not in cls._VALID_SUBJECTS:
                    subject = cls._fuzzy_map_subject(subject)
                return subject, confidence
            except Exception:
                pass

        subject = cls._fuzzy_map_subject(response)
        return subject, "low"

    @staticmethod
    def _fuzzy_map_subject(text: str) -> str:
        """基于关键词的模糊学科映射，用于 LLM 输出不规范时的兜底。"""
        t = text.lower()
        if any(kw in t for kw in ["数学", "math", "algebra", "calculus", "geometry"]):
            return "数学"
        if any(kw in t for kw in ["物理", "physics", "力学", "电磁", "量子", "热力"]):
            return "物理"
        if any(kw in t for kw in ["化学", "chemistry", "有机", "无机", "分子", "反应"]):
            return "化学"
        if any(kw in t for kw in ["生物", "biology", "细胞", "基因", "dna", "遗传", "生态"]):
            return "生物"
        if any(kw in t for kw in ["计算机", "computer", "algorithm", "编程", "程序", "网络", "机器学习"]):
            return "计算机科学"
        return "其他"

    def run(
        self,
        storage: DataFlowStorage,
        input_key: str = "question",
        output_subject_key: str = "subject_tag",
        output_conf_key: str = "subject_conf",
    ) -> list[str]:
        df = storage.read("dataframe")
        self.logger.info(f"[StemSubjectTaggerSampleEvaluator] 对 {len(df)} 条数据打学科标签")

        prompts = [self._prompt_builder.build_prompt(q) for q in df[input_key]]
        responses = self.llm_serving.generate_from_input(prompts)
        results = [self._parse_subject(r) for r in responses]

        df[output_subject_key] = [r[0] for r in results]
        df[output_conf_key] = [r[1] for r in results]

        dist = df[output_subject_key].value_counts()
        total = len(df)
        self.logger.info("[StemSubjectTaggerSampleEvaluator] 学科分布：")
        for subject, count in dist.items():
            self.logger.info("  %-12s : %5d  (%.1f%%)", subject, count, count / total * 100)

        storage.write(df)
        return [output_subject_key, output_conf_key]
