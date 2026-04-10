"""
STEM 判断题 → 开放式问答 Pipeline
====================================
将 STEM 数据集中的「判断题 (true_false)」清洗并改写为「开放式问答 (open_ended)」。

Pipeline 流程
-------------
Step 0 — StemTrueFalsePreprocessorFilter
    提取核心命题，规范化答案标签 (True/False)，过滤无效记录。
    输出：question_clean, answer_label

Step 1 — StemTrueFalseRewriterGenerator (LLM)
    调用 LLM 用 Few-Shot + CoT 将命题改写为中立的开放式问答。
    输出：question_rewritten

Step 2 — StemAnswerLeakFilter (LLM Judge)
    检验改写后问题是否隐含原始答案，过滤泄漏样本。

Step 3 — ReasoningAnswerNgramFilter
    N-gram 过滤，去除改写后与原命题高度重复的样本。

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
3. 运行：python stem_true_false_to_openended_pipeline.py
"""

import os

from dataflow.serving import APILLMServing_request
from dataflow.utils.storage import FileStorage
from dataflow.operators.reasoning import (
    ReasoningAnswerNgramFilter,
    StemTrueFalsePreprocessorFilter,
    StemTrueFalseRewriterGenerator,
    StemAnswerLeakFilter,
    StemColumnAlignGenerator,
    StemSubjectTaggerSampleEvaluator,
)


class StemTrueFalseToOpenEndedPipeline:
    """
    判断题 → 开放式问答 转换 Pipeline。

    数据流：
        question (true_false)
        → [Step 0] question_clean, answer_label
        → [Step 1] question_rewritten
        → [Step 2] (answer leak filter)
        → [Step 3] (ngram dedup filter)
        → [Step 4] question (aligned to open_ended format)
        → [Step 5] subject_tag, subject_conf
    """

    def __init__(self):
        # ── Storage ───────────────────────────────────────────────────────
        self.storage = FileStorage(
            first_entry_file_name="TODO_YOUR_INPUT_PATH/question_type=true_false/part-*.json",  # TODO
            cache_path="./cache_stem_tf_rewrite",
            file_name_prefix="tf_rewrite_step",
            cache_type="jsonl",
        )

        # ── LLM Serving ───────────────────────────────────────────────────
        self.llm_serving = APILLMServing_request(
            api_url=os.environ.get("DF_API_URL", "https://api.openai.com/v1/chat/completions"),
            key_name_of_api_key="DF_API_KEY",
            model_name=os.environ.get("DF_MODEL_NAME", "gpt-4o"),
            max_workers=200,
        )

        # ── 算子（带 _stepN 后缀，N 为执行顺序）────────────────────────
        # Step 0: 预处理（纯规则，无需 LLM）
        self.preprocessor_step0 = StemTrueFalsePreprocessorFilter()

        # Step 1: 改写（LLM）
        self.rewriter_step1 = StemTrueFalseRewriterGenerator(
            llm_serving=self.llm_serving
        )

        # Step 2: 答案泄漏检测（LLM Judge）
        self.leak_filter_step2 = StemAnswerLeakFilter(
            llm_serving=self.llm_serving
        )

        # Step 3: N-gram 过滤
        # 将 question_clean 作为参照，question_rewritten 作为待检文本；
        # 过滤改写后与原命题高度重复（几乎没变）的样本。
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
        )

        # Step 1: 改写
        self.rewriter_step1.run(
            storage=self.storage.step(),
            input_key="question_clean",
            output_key="question_rewritten",
        )

        # Step 2: 答案泄漏检测
        self.leak_filter_step2.run(
            storage=self.storage.step(),
            input_question_key="question_rewritten",
            input_answer_key="answer_label",
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
    pipeline = StemTrueFalseToOpenEndedPipeline()
    pipeline.forward()
