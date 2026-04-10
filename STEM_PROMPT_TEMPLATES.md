# STEM MC Pipeline - Prompt Templates Quick Reference

## Prompt File Location
**File**: `/data/workspace/DataFlow/dataflow/prompts/reasoning/stem.py`

---

## 1. StemMCRewritePrompt

### Purpose
Rewrite multiple-choice question stems into neutral open-ended questions

### Class Definition
```python
@PROMPT_REGISTRY.register()
class StemMCRewritePrompt(PromptABC):
    def build_prompt(self, question: str, options: str) -> str
```

### Method Signature
```python
build_prompt(question: str, options: str) -> str
```

### Chinese Template (_TEMPLATE_ZH)
```
你是一名资深数学/科学教育专家，负责将「多选题」的题干改写为「开放式问答」。

## 改写原则
1. **不泄漏答案**：改写后必须中立，不能暗示哪些选项是正确的。
   - ✗ 错误："下列哪个选项正确？"（直接保留多选框架）
   - ✗ 错误："A、B、C 都成立。请解释为什么"（暗示答案）
   - ✓ 正确："请分析下列说法，判断每一项的正确性"
2. **保留完整选项信息**：改写时要包含所有选项，供学生逐一分析。
3. **核心知识点保留**：改写后问题考查相同的知识点。
4. **自然独立**：输出应该是一个完整的论述题，不包含"原多选题为…"等元描述。

## Few-Shot 示例

### 示例 1
原题干：关于 DNA 复制，下列说法正确的是
A. DNA 复制是半保留复制
B. 复制过程需要解旋酶催化
C. DNA 聚合酶只能从 5' 端合成
D. 复制需要 ATP 供能
正确答案：ABCD

改写后：请说明 DNA 复制的机制和过程，包括：(1) 复制方式的特点；(2) 所需酶的作用；(3) 聚合酶的合成方向限制；(4) 能量来源。请逐项详细分析。

### 示例 2
原题干：下列关于三角函数的性质，错误的说法是
A. 正弦函数的周期是 2π
B. 余弦函数在 [0, π] 上单调递减
C. 正切函数的最小正周期是 π
D. 所有三角函数都有最大值和最小值
正确答案：D

改写后：请分析三角函数（正弦、余弦、正切）的周期性、单调性和值域，并判断：在这些性质中，哪些是普遍成立的，哪些有特殊限制？

## 你的任务
现在请将以下多选题的题干改写为开放式问答题。
**包含所有选项信息，但改写方式中立，不暗示答案。**
**只输出改写后的问题，不要输出任何解释或前缀。**

原题干：{question}
选项列表：{options}
改写后：
```

### English Template (_TEMPLATE_EN)
```
You are an expert educator in mathematics and science. Your task is to rewrite a multiple-choice problem stem into an open-ended question.

## Rules
1. **No answer leakage**: The rewritten question must be neutral — it must NOT indicate which options are correct.
   - ✗ Bad: "Which of the following statements is correct?" (preserves the multiple-choice framework)
   - ✗ Bad: "Why are A, B, C correct?" (directly leaks the answer)
   - ✓ Good: "Analyze each statement and determine which are correct or incorrect, with justification."
2. **Include all options**: The rewritten question should reference all options so students can analyze each one.
3. **Preserve core knowledge**: The rewritten question should test the same concepts.
4. **Self-contained**: The output should be a standalone question without meta-phrases like "The original multiple-choice question is…"

## Few-Shot Examples

### Example 1
Original stem: Regarding DNA replication, which of the following is/are correct?
A. DNA replication is semi-conservative
B. The process requires helicase catalysis
C. DNA polymerase can only synthesize from the 5' end
D. Replication requires ATP energy
Correct answer: ABCD

Rewritten: Explain the mechanism and process of DNA replication, including: (1) the characteristics of the replication mode; (2) the roles of required enzymes; (3) the directional constraints of polymerase synthesis; (4) the energy sources. Analyze each point in detail.

### Example 2
Original stem: Which of the following statement(s) about trigonometric functions is/are INCORRECT?
A. The period of sine is 2π
B. Cosine is monotonically decreasing on [0, π]
C. The period of tangent is π
D. All trigonometric functions have maximum and minimum values
Correct answer: D

Rewritten: Analyze the periodicity, monotonicity, and range of trigonometric functions (sine, cosine, tangent). Determine which properties are universal and which have special restrictions.

## Your Task
Rewrite the stem of the following multiple-choice question into an open-ended question.
**Include all options, but use neutral phrasing that does not suggest the answer.**
**Output ONLY the rewritten question. No explanation, no prefix.**

Original stem: {question}
Options: {options}
Rewritten:
```

### Auto-Detection Logic
```python
_ZH_RE = re.compile(r"[\u4e00-\u9fff]")  # Chinese character regex

def build_prompt(self, question: str, options: str) -> str:
    if self._ZH_RE.search(question):
        return self._TEMPLATE_ZH.format(question=question, options=options)
    return self._TEMPLATE_EN.format(question=question, options=options)
```

---

## 2. StemMCAnswerLeakDetectionPrompt

### Purpose
Verify if rewritten question leaks the original MC answer

### Class Definition
```python
@PROMPT_REGISTRY.register()
class StemMCAnswerLeakDetectionPrompt(PromptABC):
    def build_prompt(self, question: str, answer_label: str, options: str) -> str
```

### Method Signature
```python
build_prompt(question: str, answer_label: str, options: str) -> str
```

### Template
```
You are a rigorous QA evaluator. Your job is to determine whether a rewritten open-ended question implicitly reveals which options were correct in the original multiple-choice problem.

## Definitions
- **Answer Leak (has_leak = true)**: The rewritten question's phrasing, structure, or emphasis makes it obvious which options were correct WITHOUT needing domain knowledge.
  - Examples of leaky phrasing:
    - "Explain why A and B are true"
    - "Analyze why C is incorrect"
    - "List the correct statements among: A, B, C, D"
    - Question structure that mirrors the correct answer pattern
- **No Leak (has_leak = false)**: The rewritten question genuinely treats all options neutrally. A student reading it cannot determine which options were originally marked as correct without actually solving the problem.

## Input
Original correct answer(s): {answer_label}
Rewritten open-ended question: {question}
Original options: {options}

## Output
Think briefly, then output exactly this JSON:
{
    "has_leak": true/false,
    "leak_type": "<brief reason if true, else null>",
    "confidence": "<high|medium|low>"
}

Possible leak_type values (when has_leak=true):
- "direct_indication": Question directly states which options are correct/incorrect
- "implicit_selection": Question structure implies some options matter more than others
- "answer_mirroring": Rewritten question's logic mirrors the correct answer pattern
- "forced_analysis": Only feasible analysis path leads to correct answers
- "terminology_bias": Word choice or emphasis suggests certain answers
```

### Expected Output Format
```json
{
    "has_leak": false,
    "leak_type": null,
    "confidence": "high"
}
```

---

## 3. StemSubjectTaggingPrompt

### Purpose
Classify questions into STEM subject areas

### Class Definition
```python
@PROMPT_REGISTRY.register()
class StemSubjectTaggingPrompt(PromptABC):
    def build_prompt(self, question: str) -> str
```

### Method Signature
```python
build_prompt(question: str) -> str
```

### Template
```
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
{
    "subject": "<学科名称>",
    "confidence": "<high|medium|low>"
}
```

### Expected Output Format
```json
{
    "subject": "数学",
    "confidence": "high"
}
```

---

## 4. StemTrueFalseRewritePrompt (Reference)

### Purpose
Rewrite True/False statements into neutral open-ended questions

### Class Definition
```python
@PROMPT_REGISTRY.register()
class StemTrueFalseRewritePrompt(PromptABC):
    def build_prompt(self, question: str) -> str
```

### Key Template Rules (Chinese)
```
## 改写原则
1. **不泄漏答案**：改写后的问题必须保持中立，不能用语言暗示原命题是正确还是错误。
   - ✗ 错误示范："请解释为什么费马大定理是正确的。"（暗示答案是"正确"）
   - ✗ 错误示范："为什么以下说法不成立：…"（暗示答案是"不正确"）
   - ✓ 正确示范："请分析费马大定理的内容，并判断其真伪，给出理由。"
2. **保持数学/科学核心**：改写后问题仍需考查相同的知识点。
3. **自然流畅**：输出应该是一个独立的问答题，不含"原判断题为…"之类的元描述。
4. **简洁**：改写后问题通常为 1-3 句话，不要过于冗长。
```

---

## 5. StemAnswerLeakDetectionPrompt (Reference)

### Purpose
Verify if rewritten T/F question leaks the original answer

### Class Definition
```python
@PROMPT_REGISTRY.register()
class StemAnswerLeakDetectionPrompt(PromptABC):
    def build_prompt(self, question: str, answer_label: str) -> str
```

### Output Format (simpler than MC version)
```json
{
    "judgement_test": true/false,
    "error_type": "<brief reason if false, else null>"
}
```

---

## Integration Examples

### Using StemMCRewritePrompt
```python
from dataflow.prompts.reasoning.stem import StemMCRewritePrompt

prompt_builder = StemMCRewritePrompt()

question = "关于DNA复制，下列说法正确的是\nA. DNA复制是半保留复制\nB. 复制过程需要解旋酶催化"
options = "A. DNA复制是半保留复制\nB. 复制过程需要解旋酶催化"

prompt = prompt_builder.build_prompt(question, options)
# prompt is now ready to pass to LLM
```

### Using StemMCAnswerLeakDetectionPrompt
```python
from dataflow.prompts.reasoning.stem import StemMCAnswerLeakDetectionPrompt

prompt_builder = StemMCAnswerLeakDetectionPrompt()

rewritten_question = "请说明DNA复制的机制..."
answer_label = "A,B"
options = "A. DNA复制是半保留复制\nB. 复制过程需要解旋酶催化"

prompt = prompt_builder.build_prompt(rewritten_question, answer_label, options)
# prompt ready for LLM Judge

# LLM response parsing
import json
response_json = json.loads(llm_response)
has_leak = response_json["has_leak"]  # true or false
```

### Using StemSubjectTaggingPrompt
```python
from dataflow.prompts.reasoning.stem import StemSubjectTaggingPrompt

prompt_builder = StemSubjectTaggingPrompt()

question = "求解方程组 ax + by = c, dx + ey = f 的x和y值"

prompt = prompt_builder.build_prompt(question)
# prompt ready for LLM

# Expected response
# {"subject": "数学", "confidence": "high"}
```

---

## Key Characteristics Summary

| Prompt | Input | Output | Language | Registry |
|--------|-------|--------|----------|----------|
| StemMCRewritePrompt | question, options | rewritten question | Auto ZH/EN | ✓ |
| StemMCAnswerLeakDetectionPrompt | question, answer_label, options | JSON: {has_leak, leak_type, confidence} | EN | ✓ |
| StemSubjectTaggingPrompt | question | JSON: {subject, confidence} | ZH | ✓ |
| StemTrueFalseRewritePrompt | question | rewritten question | Auto ZH/EN | ✓ |
| StemAnswerLeakDetectionPrompt | question, answer_label | JSON: {judgement_test, error_type} | EN | ✓ |

---

## File Locations
- **All Prompts**: `/data/workspace/DataFlow/dataflow/prompts/reasoning/stem.py`
- **Line Ranges**:
  - StemTrueFalseRewritePrompt: 20-107
  - StemAnswerLeakDetectionPrompt: 109-139
  - StemSubjectTaggingPrompt: 142-178
  - StemMCRewritePrompt: 181-298
  - StemMCAnswerLeakDetectionPrompt: 300-363

