'''python
import re

# 假设存在 @PROMPT_REGISTRY.register() 和 PromptABC
# @PROMPT_REGISTRY.register()
class StemTrueFalseRewritePrompt:
    """
    将「客观题（单选/多选/判断）」命题改写为「开放式问答」。
    自动检测中英文，使用对应 Zero-Shot 强策略 prompt。
    """

    _ZH_RE = re.compile(r"[\u4e00-\u9fff]")

    _TEMPLATE_ZH = """\
你是一名资深学科教育专家，负责将「带选项的客观题（单选/多选/判断）」改写为「高质量的开放式问答题」。

## 核心改写策略（务必严格遵循）
1. **彻底杜绝罗列选项（最重要）**：坚决避免把原题的选项照搬成“请判断以下四个说法是否正确：(1)...(2)...”这种机械形式。你必须理解题目的核心考点，对选项进行提炼、融合或直接丢弃：
   - **计算/推导/事实记忆类**：直接抛弃所有选项，直接提问。例如原题问“1英里等于多少米？ A.1609 B.1852...”，直接改写为“1英里等于多少米？请给出具体的换算数值。”（绝对不要出现 1609 等干扰项数字）。物理公式推导题同理，直接让学生推导即可。
   - **概念/性质分析类**：提炼选项之间的“核心冲突点”进行提问。例如选项在争论某物质“一定是金属/一定是非金属/都可能”，应提炼改写为：“请结合反应原理分析该物质的性质，它是否一定为金属单质？或者存在其他可能性？请说明理由。”
   - **多情景复杂判断类**：如果原题选项是完全独立的四个不同情景，尽量用一句高度概括的设问引导学生全面分析物理/化学过程，而不是逐条验证。
2. **绝不泄漏答案**：提问必须保持绝对中立，不能通过引导性语言暗示某个结论是正确的或错误的。
3. **保持考点完全一致**：改写后的问题必须考查与原题完全相同的知识点和思维深度。
4. **自然流畅且独立**：输出必须是一个直接可以拿给学生做的独立问题。不要包含“原题为”、“请看以下选项”、“四个选项”等元描述。

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
'''


'''python
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
'''