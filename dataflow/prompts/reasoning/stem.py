"""
STEM Rewrite Prompts
====================
STEM 题目改写 Pipeline 所需的 Prompt 模板。

包含：
- StemTrueFalseRewritePrompt    : 将判断题命题改写为开放式问答（中英双语 Few-Shot）
- StemAnswerLeakDetectionPrompt : 检验改写后问题是否隐含原始答案
- StemSubjectTaggingPrompt      : 对问题打 STEM 学科标签
- StemMCRewritePrompt           : 将多选题题干改写为开放式问答（中英双语 Few-Shot）
- StemMCAnswerLeakDetectionPrompt: 检验改写后问题是否泄漏多选题答案
"""

from dataflow.utils.registry import PROMPT_REGISTRY
from dataflow.core.prompt import PromptABC

import re


@PROMPT_REGISTRY.register()
class StemTrueFalseRewritePrompt(PromptABC):
    """
    将「判断题」命题改写为「开放式问答」。
    自动检测中英文，使用对应 Few-Shot prompt。
    """

    _ZH_RE = re.compile(r"[\u4e00-\u9fff]")

    _TEMPLATE_ZH = """\
你是一名资深数学/科学教育专家，负责将「判断题」改写为「开放式问答」。

## 改写原则
1. **不泄漏答案**：改写后的问题必须保持中立，不能用语言暗示原命题是正确还是错误。
   - ✗ 错误示范："请解释为什么费马大定理是正确的。"（暗示答案是"正确"）
   - ✗ 错误示范："为什么以下说法不成立：…"（暗示答案是"不正确"）
   - ✓ 正确示范："请分析费马大定理的内容，并判断其真伪，给出理由。"
2. **保持数学/科学核心**：改写后问题仍需考查相同的知识点。
3. **自然流畅**：输出应该是一个独立的问答题，不含"原判断题为…"之类的元描述。
4. **简洁**：改写后问题通常为 1-3 句话，不要过于冗长。

## Few-Shot 示例

### 示例 1
原命题：设 f(x) 在 [a,b] 上连续，则 f(x) 在 [a,b] 上一定可积。
改写后：设 f(x) 是定义在闭区间 [a,b] 上的连续函数，请说明 f(x) 在 [a,b] 上的黎曼可积性，并给出证明或反例。

### 示例 2
原命题：两个向量的叉积满足交换律，即 a × b = b × a。
改写后：请说明三维向量叉积（外积）运算是否满足交换律，并给出详细推导。

### 示例 3
原命题：质量不同的两个物体在真空中自由落体，重的物体先落地。
改写后：在真空环境中，质量不同的两个物体同时从同一高度做自由落体运动，请分析它们的运动规律，哪个先落地？为什么？

### 示例 4
原命题：DNA 复制过程是半保留复制。
改写后：请描述 DNA 的复制机制，并说明"半保留复制"这一概念的含义及其实验依据。

## 你的任务
现在请将以下判断题的核心命题改写为开放式问答题。
**只输出改写后的问题，不要输出任何解释或前缀。**

原命题：{question}
改写后："""

    _TEMPLATE_EN = """\
You are an expert educator in mathematics and science. Your task is to rewrite a True/False statement into an open-ended question.

## Rules
1. **No answer leakage**: The rewritten question must be neutral — it must NOT hint at whether the original statement is true or false.
   - ✗ Bad: "Explain why Fermat's Last Theorem is correct." (leaks: it's true)
   - ✗ Bad: "Why is the following statement incorrect: …" (leaks: it's false)
   - ✓ Good: "Analyze Fermat's Last Theorem and determine whether it is true or false, with justification."
2. **Preserve the core knowledge point**: The rewritten question should test the same concept.
3. **Self-contained**: The output should be a standalone question, without meta-phrases like "The original T/F statement is…"
4. **Concise**: 1-3 sentences is ideal.

## Few-Shot Examples

### Example 1
Original: A continuous function on [a,b] is always Riemann integrable.
Rewritten: Let f(x) be a continuous function on the closed interval [a,b]. Discuss whether f(x) is guaranteed to be Riemann integrable on [a,b], and provide a proof or counterexample.

### Example 2
Original: The cross product of two vectors satisfies the commutative property: a × b = b × a.
Rewritten: Does the cross product of two three-dimensional vectors satisfy the commutative law? Provide a detailed derivation to support your answer.

### Example 3
Original: In a vacuum, a heavier object falls faster than a lighter one.
Rewritten: Two objects with different masses are dropped simultaneously from the same height in a vacuum. Analyze their motion and determine which, if either, reaches the ground first and why.

## Your Task
Rewrite the following True/False statement into an open-ended question.
**Output ONLY the rewritten question. No explanation, no prefix.**

Original statement: {question}
Rewritten:"""

    def __init__(self):
        pass

    def build_prompt(self, question: str) -> str:
        """根据问题语言自动选择中文或英文模板。"""
        if self._ZH_RE.search(question):
            return self._TEMPLATE_ZH.format(question=question)
        return self._TEMPLATE_EN.format(question=question)


@PROMPT_REGISTRY.register()
class StemAnswerLeakDetectionPrompt(PromptABC):
    """
    检验改写后的开放式问题是否隐含（泄漏）了原始判断题的答案。
    """

    _TEMPLATE = """\
You are a rigorous QA evaluator. Your job is to determine whether a rewritten open-ended question implicitly reveals the answer to the original True/False question.

## Definitions
- **Answer Leak (judgement_test = false)**: The rewritten question's phrasing, word choice, or framing makes it obvious whether the original statement was TRUE or FALSE *without needing domain knowledge*.
  - Examples of leaky phrasing: "prove that X holds", "why is X wrong", "explain the mistake in X", "confirm that X is valid"
- **No Leak (judgement_test = true)**: The rewritten question is genuinely neutral. A student reading it cannot tell whether the answer is "True" or "False" without actually knowing the underlying science/math.

## Input
Original answer label: {answer_label}
Rewritten open-ended question: {question}

## Output
Think briefly, then output exactly this JSON:
{{
    "judgement_test": true/false,
    "error_type": "<brief reason if false, else null>"
}}
"""

    def __init__(self):
        pass

    def build_prompt(self, question: str, answer_label: str) -> str:
        return self._TEMPLATE.format(question=question, answer_label=answer_label)


@PROMPT_REGISTRY.register()
class StemSubjectTaggingPrompt(PromptABC):
    """
    对 STEM 问题打学科标签。
    支持：数学、物理、化学、生物、计算机科学、其他
    """

    _TEMPLATE = """\
你是一位 STEM 学科分类专家。请根据以下问题的内容，判断它属于哪个学科领域。

## 学科分类（选择最匹配的一项）
- 数学（Mathematics）：包括代数、几何、微积分、概率统计、离散数学、数论等
- 物理（Physics）：包括经典力学、电磁学、热力学、光学、量子力学、相对论等
- 化学（Chemistry）：包括有机化学、无机化学、物理化学、分析化学、生物化学等
- 生物（Biology）：包括分子生物学、遗传学、生态学、生理学、细胞生物学等
- 计算机科学（Computer Science）：包括算法、数据结构、编程、机器学习、系统、网络等
- 其他（Other）：其他自然科学（地理、天文等）或无法归类的题目

## 要求
- 只输出 JSON，不要输出任何解释
- subject 字段的值必须是以下之一：数学、物理、化学、生物、计算机科学、其他

## 问题
{question}

## 输出格式
{{
    "subject": "<学科名称>",
    "confidence": "<high|medium|low>"
}}
"""

    def __init__(self):
        pass

    def build_prompt(self, question: str) -> str:
        return self._TEMPLATE.format(question=question)


@PROMPT_REGISTRY.register()
class StemMCRewritePrompt(PromptABC):
    """
    将「客观题（单选/多选/判断）」命题改写为「开放式问答」。
    自动检测中英文，使用对应 Zero-Shot 强策略 prompt。
    """

    _ZH_RE = re.compile(r"[\u4e00-\u9fff]")

    _TEMPLATE_ZH = """\
你是一名资深学科教育专家，负责将「带选项的客观题（单选/多选/判断）」改写为「高质量的开放式问答题」。

## 核心改写策略（务必严格遵循）
1. **彻底杜绝罗列选项（最重要）**：坚决避免把原题的选项照搬成"请判断以下四个说法是否正确：(1)...(2)..."这种机械形式。你必须理解题目的核心考点，对选项进行提炼、融合或直接丢弃：
   - **计算/推导/事实记忆类**：直接抛弃所有选项，直接提问。例如原题问"1英里等于多少米？ A.1609 B.1852..."，直接改写为"1英里等于多少米？请给出具体的换算数值。"（绝对不要出现 1609 等干扰项数字）。物理公式推导题同理，直接让学生推导即可。
   - **概念/性质分析类**：提炼选项之间的"核心冲突点"进行提问。例如选项在争论某物质"一定是金属/一定是非金属/都可能"，应提炼改写为："请结合反应原理分析该物质的性质，它是否一定为金属单质？或者存在其他可能性？请说明理由。"
   - **多情景复杂判断类**：如果原题选项是完全独立的四个不同情景，尽量用一句高度概括的设问引导学生全面分析物理/化学过程，而不是逐条验证。
2. **绝不泄漏答案**：提问必须保持绝对中立，不能通过引导性语言暗示某个结论是正确的或错误的。
3. **保持考点完全一致**：改写后的问题必须考查与原题完全相同的知识点和思维深度。
4. **自然流畅且独立**：输出必须是一个直接可以拿给学生做的独立问题。不要包含"原题为"、"请看以下选项"、"四个选项"等元描述。

## 你的任务
深刻理解以下题目的核心考点，运用上述策略，将其改写为一道行云流水的开放式问答题。
**只输出改写后的题目文本，绝不输出任何解释性前缀、你的思考过程或原文的选项列表。**

原题：{question}
改写后："""

    _TEMPLATE_EN = """\
You are an expert educator tasked with rewriting objective questions (Multiple Choice, True/False) into high-quality open-ended questions.

## Core Rewriting Strategies (Strictly Follow)
1. **NEVER Merely List Options (Crucial)**: Absolutely avoid lazy rewrites like "Evaluate whether the following 4 statements are true: (1)... (2)...". You must deeply understand the core concept and synthesize, abstract, or completely discard the original options:
   - **Calculation/Derivation/Factual Recall**: Discard options entirely and ask directly. If the original asks "What is the length of 1 mile in meters? A. 1609, B. 1852...", simply rewrite as "How many meters are in 1 mile? Provide the exact conversion value." (NEVER list the distractor numbers). The same applies to math/physics derivations.
   - **Conceptual/Analytical**: Abstract the core conflict among the options. If options debate whether a substance is "always a metal," "always a non-metal," or "either," rewrite as: "Based on the reaction principles, analyze the nature of this substance. Must it be a metal, or are there other possibilities? Explain your reasoning."
   - **Complex Multi-Scenario**: If the options are entirely disparate scenarios, guide the student to analyze the whole system or process comprehensively with a broad question, rather than asking them to verify bullet points.
2. **Zero Answer Leakage**: The question must remain completely neutral. Never hint at the correct answer or imply a statement is inherently wrong.
3. **Preserve Core Knowledge**: The rewritten question must test the exact same underlying mathematical, scientific, or logical concepts as the original.
4. **Self-Contained & Natural**: The output must be a standalone question ready for a student. Do not include meta-language like "The original question is...", "Based on the options...", or "Evaluate the following...".

## Your Task
Deeply analyze the core concept of the following question, apply the strategies above, and rewrite it into an elegant, open-ended question.
**Output ONLY the rewritten question text. Do NOT output any explanatory prefixes, your thought process, or lists of the original options.**

Original Question: {question}
Rewritten Question:"""

    def __init__(self):
        pass

    def build_prompt(self, question: str) -> str:
        """根据问题语言自动选择中文或英文模板。"""
        if self._ZH_RE.search(question):
            return self._TEMPLATE_ZH.format(question=question)
        return self._TEMPLATE_EN.format(question=question)


@PROMPT_REGISTRY.register()
class StemMCAnswerLeakDetectionPrompt(PromptABC):
    """
    检验改写后的开放式问题是否泄漏了原始客观题的答案，或保留了机械的选项格式。

    采用语义对比（Semantic Comparison）策略，降低对底层 pipeline 抽取的 answer_label 的依赖，
    以应对 label 提取 bug（如误将所有选项提为正确答案）导致判断失效的情况。
    """

    _TEMPLATE = """\
You are a rigorous QA evaluator evaluating the quality of objective questions rewritten into open-ended format.
Your primary task is to detect whether the rewritten question **leaks the answer** or **lazily retains the original multiple-choice structure**.

## Critical Instruction Regarding `answer_label`
The provided `answer_label` is automatically extracted and MAY CONTAIN BUGS (e.g., marking "A,B,C,D" as correct when only one is). **Do NOT blindly trust it.** You must base your evaluation primarily on a semantic comparison between the `Original options` and the `Rewritten question`.

## Evaluation Criteria
1. **Answer Leak (`has_leak`)**:
   - Does the rewritten question treat a specific option's conclusion as a given fact or explicitly ask the student to prove it?
   - *Leak Example*: Original options are [A: velocity is 0, B: velocity is v0]. Rewritten question: "Explain why the final velocity becomes 0." (Leaks Option A).
   - *Neutral Example*: "Derive the expression for the final velocity." (No leak).
   - A question has a leak if it drastically narrows the analytical focus to align with only one specific outcome among the choices, without the student having to calculate it first.

2. **Option Retention (`retains_options`)**:
   - Does the rewritten question simply copy-paste the original options into a list to be verified (e.g., asking "Evaluate if the following statements are true: (1)... (2)...")?
   - A high-quality open-ended question should synthesize the core conflict or discard the options entirely, not present them as a checklist.

## Input
Original options: {options}
Original correct answer label (Reference ONLY, might be noisy): {answer_label}
Rewritten open-ended question: {question}

## Output Requirements
Think step-by-step:
1. Compare the semantic meaning of the rewritten question against the original options. Does it give away the punchline?
2. Check the structural format. Are the options just listed out?
Then, output exactly this JSON schema:
{{
    "analysis": "<Brief, 1-2 sentences explaining your reasoning based on semantic comparison>",
    "has_leak": true/false,
    "leak_type": "<null if false, else 'direct_statement', 'narrowed_focus', 'implied_truth'>",
    "retains_options": true/false,
    "confidence": "<high|medium|low>"
}}"""

    def __init__(self):
        pass

    def build_prompt(self, question: str, answer_label: str, options: str) -> str:
        """
        检查改写后问题是否泄漏答案或机械保留选项。

        Args:
            question: 改写后的开放式问题
            answer_label: 原始客观题的正确答案（如 "A,B,D"）
            options: 原始选项的具体文本内容（必须传入具体内容，而不仅是字母）

        Returns:
            完整的 prompt 字符串
        """
        return self._TEMPLATE.format(
            question=question,
            answer_label=answer_label,
            options=options
        )

