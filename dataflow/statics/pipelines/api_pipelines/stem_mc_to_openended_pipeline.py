"""
STEM 多选题 → 开放式问答 Pipeline
====================================
将 STEM 数据集中的「多选题 (multiple_choice)」清洗并改写为「开放式问答 (open_ended)」。

Pipeline 流程
-------------
Step 0 — StemMCPreprocessorFilter
    提取核心命题，规范化答案标签 (A,B,C 格式)，修复 M1 格式，提取/结构化选项，
    过滤无效记录。
    输出：question_clean, answer_label, options_text, option_a/b/c/d, num_options

Step 1 — StemMCToOpenEndedRewriterGenerator (LLM)
    调用 LLM 用 Few-Shot + CoT 将题干改写为中立的开放式问答。
    改写时包含所有选项，但不暗示正确答案。
    输出：question_rewritten

Step 2 — StemMCAnswerLeakFilter (LLM Judge)
    检验改写后问题是否泄漏哪些选项是正确的，过滤泄漏样本。

Step 3 — ReasoningAnswerNgramFilter
    N-gram 过滤，去除改写后与原题干高度重复的样本。

Step 4 — StemColumnAlignGenerator
    字段整理，将 question_rewritten 重命名为 question，
    对齐 open_ended 数据集格式。

Step 5 — StemSubjectTaggerSampleEvaluator (LLM)
    对每个问题打学科标签（数学/物理/化学/生物/计算机科学/其他）。
    用于验证数据分布和学科覆盖情况。

用法
----
1. 修改下方 TODO 占位符（INPUT_FILE、API 配置环境变量）
2. 设置环境变量：
       export DF_API_URL="https://your-api-endpoint/v1/chat/completions"
       export DF_API_KEY="your-api-key"
       export DF_MODEL_NAME="your-model-name"
3. 运行：python stem_mc_to_openended_pipeline.py
"""

import os

from dataflow.serving import APILLMServing_request
from dataflow.utils.storage import FileStorage
from dataflow.operators.reasoning import (
    ReasoningAnswerNgramFilter,
    StemMCPreprocessorFilter,
    StemMCToOpenEndedRewriterGenerator,
    StemMCAnswerLeakFilter,
    StemColumnAlignGenerator,
    StemSubjectTaggerSampleEvaluator,
)


class StemMCToOpenEndedPipeline:
    """
    多选题 → 开放式问答 转换 Pipeline。

    数据流：
        question, text (multiple_choice)
        → [Step 0] question_clean, answer_label, options_text, num_options
        → [Step 1] question_rewritten
        → [Step 2] (answer leak filter for MC)
        → [Step 3] (ngram dedup filter)
        → [Step 4] question (aligned to open_ended format)
        → [Step 5] subject_tag, subject_conf
    """

    def __init__(self):
        # ── Storage ───────────────────────────────────────────────────────
        self.storage = FileStorage(
            first_entry_file_name="/data/workspace/stem_mc_sample_20k.jsonl",
            cache_path="./cache_stem_mc_rewrite_v1_2)",
            file_name_prefix="mc_rewrite_step",
            cache_type="jsonl",
        )

        # ── LLM Serving ───────────────────────────────────────────────────
        self.llm_serving = APILLMServing_request(
            api_url=os.environ.get("DF_API_URL", "https://api.openai.com/v1/chat/completions"),
            key_name_of_api_key="DF_API_KEY",
            model_name=os.environ.get("DF_MODEL_NAME", "gpt-4o"),
            max_workers=32,
            read_timeout=1800,
            connect_timeout=600
        )

        # ── 算子（带 _stepN 后缀，N 为执行顺序）────────────────────────
        # Step 0: 预处理（纯规则，无需 LLM）
        # - 提取核心命题（去除 wrapper）
        # - 规范化答案标签为 A,B,C 格式
        # - 修复 M1 格式（选项内嵌无分行）
        # - 提取/结构化选项
        self.preprocessor_step0 = StemMCPreprocessorFilter()

        # Step 1: 改写（LLM）
        self.rewriter_step1 = StemMCToOpenEndedRewriterGenerator(
            llm_serving=self.llm_serving
        )

        # Step 2: 答案泄漏检测（LLM Judge）
        # 检测改写后问题是否暗示哪些选项是正确的
        self.leak_filter_step2 = StemMCAnswerLeakFilter(
            llm_serving=self.llm_serving
        )

        # Step 3: N-gram 过滤
        # 将 question_clean 作为参照，question_rewritten 作为待检文本；
        # 过滤改写后与原题干高度重复（几乎没变）的样本。
        self.ngram_filter_step3 = ReasoningAnswerNgramFilter(
            min_score=0.3,   # 至少 30% 的 n-gram 是新的（不是复读原文）
            max_score=1.0,
            ngrams=5,
        )

        # Step 4: 字段整理
        self.column_aligner_step4 = StemColumnAlignGenerator()

        # Step 5: 学科打标（LLM）
        self.subject_tagger_step5 = StemSubjectTaggerSampleEvaluator(
            llm_serving=self.llm_serving
        )

    def forward(self):
        # Step 0: 预处理
        self.preprocessor_step0.run(
            storage=self.storage.step(),
            input_question_key="question",
            input_answer_key="text",
            output_question_key="question_clean",
            output_answer_key="answer_label",
            output_options_text_key="options_text",
            output_num_options_key="num_options",
            min_required_options=2,
        )

        # Step 1: 改写
        self.rewriter_step1.run(
            storage=self.storage.step(),
            input_question_key="question_clean",
            input_options_key="options_text",
            output_key="question_rewritten",
        )

        # Step 2: 答案泄漏检测（MC 版本）
        # 需要传入原始选项和答案信息以供 LLM Judge 判断泄漏情况
        self.leak_filter_step2.run(
            storage=self.storage.step(),
            input_question_key="question_rewritten",
            input_answer_key="answer_label",
            input_options_key="options_text",
        )

        # Step 3: N-gram 过滤
        self.ngram_filter_step3.run(
            storage=self.storage.step(),
            input_question_key="question_clean",
            input_answer_key="question_rewritten",
        )

        # Step 4: 字段整理，对齐 open_ended 格式
        self.column_aligner_step4.run(
            storage=self.storage.step(),
            input_rewritten_key="question_rewritten",
        )

        # Step 5: 学科打标
        self.subject_tagger_step5.run(
            storage=self.storage.step(),
            input_key="question",
            output_subject_key="subject_tag",
            output_conf_key="subject_conf",
        )


if __name__ == "__main__":
    # 运行前确保以下环境变量已设置：
    #   export DF_API_KEY="your-api-key"
    #   export DF_API_URL="https://your-api-endpoint/v1/chat/completions"
    #   export DF_MODEL_NAME="your-model-name"
    pipeline = StemMCToOpenEndedPipeline()
    pipeline.forward()
