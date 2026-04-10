# STEM MC Pipeline - Prompt Files & Architecture Summary

**Generated: 2026-04-09**

## Quick Reference

### Pipeline File Location
- **Main Pipeline**: `/data/workspace/DataFlow/dataflow/statics/pipelines/api_pipelines/stem_mc_to_openended_pipeline.py`

### Key Prompt Classes
- **Rewrite Prompt**: `StemMCRewritePrompt` (file: `/data/workspace/DataFlow/dataflow/prompts/reasoning/stem.py`)
- **Answer Leak Detection**: `StemMCAnswerLeakDetectionPrompt` (file: `/data/workspace/DataFlow/dataflow/prompts/reasoning/stem.py`)
- **Subject Tagging**: `StemSubjectTaggingPrompt` (file: `/data/workspace/DataFlow/dataflow/prompts/reasoning/stem.py`)

---

## Directory Structure

### Prompts Directory
```
/data/workspace/DataFlow/dataflow/prompts/
├── agenticrag.py
├── chemistry.py
├── code.py
├── core_text.py
├── func_call.py
├── general_text.py
├── kbcleaning.py
├── pdf2vqa.py
├── text2qa.py
├── text2sql.py
├── model_evaluation/
└── reasoning/
    ├── __init__.py
    ├── diy.py
    ├── general.py
    ├── math.py
    └── stem.py                          ← STEM prompts here
```

### Operators - Reasoning Directory
```
/data/workspace/DataFlow/dataflow/operators/reasoning/
├── eval/
│   ├── reasoning_category_dataset_evaluator.py
│   ├── reasoning_difficulty_dataset_evaluator.py
│   ├── reasoning_question_category_sample_evaluator.py
│   ├── reasoning_question_difficulty_sample_evaluator.py
│   ├── reasoning_question_solvable_sample_evaluator.py
│   ├── reasoning_token_dataset_evaluator.py
│   └── stem_subject_tagger_sample_evaluator.py              ← Step 5
├── filter/
│   ├── reasoning_answer_formatter_filter.py
│   ├── reasoning_answer_groundtruth_filter.py
│   ├── reasoning_answer_model_judge_filter.py
│   ├── reasoning_answer_ngram_filter.py
│   ├── reasoning_answer_pipeline_root_filter.py
│   ├── reasoning_answer_token_length_filter.py
│   ├── reasoning_question_filter.py
│   ├── stem_answer_leak_filter.py
│   ├── stem_mc_answer_leak_filter.py                        ← Step 2
│   ├── stem_mc_preprocessor_filter.py                       ← Step 0
│   ├── stem_question_noise_filter.py
│   └── stem_true_false_preprocessor_filter.py
└── generate/
    ├── reasoning_answer_extraction_qwenmatheval_generator.py
    ├── reasoning_answer_generator.py
    ├── reasoning_pretrain_format_convert_generator.py
    ├── reasoning_pseudo_answer_generator.py
    ├── reasoning_question_fusion_generator.py
    ├── reasoning_question_generator.py
    ├── stem_column_align_generator.py                       ← Step 4
    ├── stem_mc_to_openended_rewriter_generator.py          ← Step 1
    └── stem_true_false_rewriter_generator.py
```

---

## Pipeline Architecture

### Data Flow (5 Steps)

```
INPUT: Multiple-Choice STEM Questions
  ↓
[Step 0] StemMCPreprocessorFilter (rule-based)
  ├─ Extract core proposition (remove wrapper)
  ├─ Normalize answer labels (A,B,C format)
  ├─ Fix M1 format (inline options)
  ├─ Parse/structure options
  └─ Filter invalid records
  OUTPUT: question_clean, answer_label, options_text, num_options
  ↓
[Step 1] StemMCToOpenEndedRewriterGenerator (LLM)
  ├─ Call LLM with Few-Shot + CoT prompt
  ├─ Rewrite to neutral open-ended question
  ├─ Include all options but no answer leakage
  └─ Post-process LLM output
  OUTPUT: question_rewritten
  ↓
[Step 2] StemMCAnswerLeakFilter (LLM Judge)
  ├─ Call LLM Judge to verify no answer leakage
  ├─ Check if rewritten question reveals correct options
  └─ Filter leaked samples
  OUTPUT: (filtered dataframe)
  ↓
[Step 3] ReasoningAnswerNgramFilter (rule-based)
  ├─ N-gram similarity check
  ├─ Filter if rewritten too similar to original
  └─ Min 30% new n-grams required
  OUTPUT: (filtered dataframe)
  ↓
[Step 4] StemColumnAlignGenerator (column alignment)
  ├─ Rename question_rewritten → question
  └─ Keep only open_ended format columns
  OUTPUT: (open_ended format)
  ↓
[Step 5] StemSubjectTaggerSampleEvaluator (LLM)
  ├─ Tag with subject: Math, Physics, Chemistry, Biology, CS, Other
  └─ Assign confidence: high/medium/low
  OUTPUT: question, subject_tag, subject_conf
  ↓
OUTPUT: Open-Ended STEM Questions (open_ended dataset format)
```

---

## Prompt Class Details

### 1. StemMCRewritePrompt

**Location**: `/data/workspace/DataFlow/dataflow/prompts/reasoning/stem.py` (lines 181-298)

**Purpose**: Rewrite multiple-choice question stems to open-ended questions

**Key Features**:
- Auto-detects Chinese vs English
- Two templates: `_TEMPLATE_ZH` and `_TEMPLATE_EN`
- Few-shot examples included
- Emphasizes neutrality to prevent answer leakage

**Method**: `build_prompt(question: str, options: str) -> str`

**Rules**:
1. **No answer leakage**: Must be neutral, never hint at correct options
2. **Include all options**: Reference all options for analysis
3. **Preserve core knowledge**: Test same concepts as original
4. **Self-contained**: Standalone question without meta-phrases

**Template Variables**: `{question}`, `{options}`

### 2. StemMCAnswerLeakDetectionPrompt

**Location**: `/data/workspace/DataFlow/dataflow/prompts/reasoning/stem.py` (lines 300-363)

**Purpose**: Verify if rewritten question leaks the original MC answers

**Key Features**:
- Checks for answer leakage in MC context
- Detects 5 types of leaks:
  - `direct_indication`: Directly states which options are correct/incorrect
  - `implicit_selection`: Structure implies some options matter more
  - `answer_mirroring`: Question logic mirrors correct answer pattern
  - `forced_analysis`: Only feasible analysis path leads to correct answers
  - `terminology_bias`: Word choice suggests certain answers

**Method**: `build_prompt(question: str, answer_label: str, options: str) -> str`

**Output Format**:
```json
{
    "has_leak": true/false,
    "leak_type": "<brief reason if true, else null>",
    "confidence": "<high|medium|low>"
}
```

**Template Variables**: `{question}`, `{answer_label}`, `{options}`

### 3. StemSubjectTaggingPrompt

**Location**: `/data/workspace/DataFlow/dataflow/prompts/reasoning/stem.py` (lines 142-178)

**Purpose**: Classify questions into STEM subjects

**Supported Subjects**:
- 数学 (Mathematics)
- 物理 (Physics)
- 化学 (Chemistry)
- 生物 (Biology)
- 计算机科学 (Computer Science)
- 其他 (Other)

**Method**: `build_prompt(question: str) -> str`

**Output Format**:
```json
{
    "subject": "<subject name>",
    "confidence": "<high|medium|low>"
}
```

### 4. StemTrueFalseRewritePrompt (for reference)

**Location**: `/data/workspace/DataFlow/dataflow/prompts/reasoning/stem.py` (lines 20-107)

**Purpose**: Rewrite True/False statements to open-ended questions
- Similar rules to MC rewrite but for T/F format
- Also bilingual (Chinese/English)

### 5. StemAnswerLeakDetectionPrompt (for reference)

**Location**: `/data/workspace/DataFlow/dataflow/prompts/reasoning/stem.py` (lines 109-139)

**Purpose**: Verify if rewritten T/F question leaks the original answer
- Simpler than MC version (just true/false leak, no leak types)

---

## Key Operators

### Step 0: StemMCPreprocessorFilter

**File**: `/data/workspace/DataFlow/dataflow/operators/reasoning/filter/stem_mc_preprocessor_filter.py`

**Class**: `StemMCPreprocessorFilter` (OperatorABC)

**Input**:
- `question`: Raw MC question with possible wrapper
- `text`: Answer label in various formats

**Output**:
- `question_clean`: Core proposition (wrapper removed)
- `answer_label`: Normalized to "A,B,C" format
- `options_text`: Extracted options
- `option_a`, `option_b`, `option_c`, `option_d`: Individual options
- `num_options`: Count of extracted options

**Key Methods**:
- `_extract_core_proposition()`: Remove wrapper prompts
- `_normalize_answer()`: Convert various answer formats to A,B,C
- `_fix_m1_format()`: Insert line breaks for inline options
- `_extract_options()`: Parse A/B/C/D from question text

**Parameters**:
```python
run(
    storage,
    input_question_key="question",
    input_answer_key="text",
    output_question_key="question_clean",
    output_answer_key="answer_label",
    output_options_text_key="options_text",
    output_num_options_key="num_options",
    min_required_options=2,
)
```

### Step 1: StemMCToOpenEndedRewriterGenerator

**File**: `/data/workspace/DataFlow/dataflow/operators/reasoning/generate/stem_mc_to_openended_rewriter_generator.py`

**Class**: `StemMCToOpenEndedRewriterGenerator` (OperatorABC)

**Input**:
- `input_question_key` (default: "question_clean"): Clean MC stem
- `input_options_key` (default: "options_text"): Options list

**Output**:
- `output_key` (default: "question_rewritten"): Rewritten open-ended question

**Key Method**: `_postprocess()` removes LLM output noise:
- Removes prefixes: "改写后:", "Rewritten:", etc.
- Removes think tags: `<think>...</think>`
- Removes markdown headers

**Parameters**:
```python
run(
    storage,
    input_question_key="question_clean",
    input_options_key="options_text",
    output_key="question_rewritten",
)
```

### Step 2: StemMCAnswerLeakFilter

**File**: `/data/workspace/DataFlow/dataflow/operators/reasoning/filter/stem_mc_answer_leak_filter.py`

**Class**: `StemMCAnswerLeakFilter` (OperatorABC)

**Input**:
- `input_question_key` (default: "question_rewritten"): Rewritten question
- `input_answer_key` (default: "answer_label"): Original answer
- `input_options_key` (default: "options_text"): Original options

**Output**: Filtered dataframe (keeps only non-leaked samples)

**Key Method**: `_parse_judgement(response: str) -> bool`
- Extracts `has_leak` from JSON response
- Returns True if sample should be **kept** (no leak)
- Returns False if sample should be **filtered** (has leak)

**Parameters**:
```python
run(
    storage,
    input_question_key="question_rewritten",
    input_answer_key="answer_label",
    input_options_key="options_text",
)
```

### Step 3: ReasoningAnswerNgramFilter

**Location**: `/data/workspace/DataFlow/dataflow/operators/reasoning/filter/`

**Purpose**: N-gram similarity filter to remove near-duplicates

**Pipeline Configuration**:
```python
ngram_filter_step3 = ReasoningAnswerNgramFilter(
    min_score=0.3,      # At least 30% new n-grams
    max_score=1.0,
    ngrams=5,
)
```

### Step 4: StemColumnAlignGenerator

**File**: `/data/workspace/DataFlow/dataflow/operators/reasoning/generate/stem_column_align_generator.py`

**Class**: `StemColumnAlignGenerator` (OperatorABC)

**Purpose**: Align output to open_ended dataset format

**Kept Columns**:
```
answer_model, dataset_name, id, not_zh, ori, 
std_answer_model, text, title, question
```

**Key Operation**:
- Rename `question_rewritten` → `question`
- Keep only open_ended-standard columns

**Parameters**:
```python
run(
    storage,
    input_rewritten_key="question_rewritten",
)
```

### Step 5: StemSubjectTaggerSampleEvaluator

**File**: `/data/workspace/DataFlow/dataflow/operators/reasoning/eval/stem_subject_tagger_sample_evaluator.py`

**Class**: `StemSubjectTaggerSampleEvaluator` (OperatorABC)

**Purpose**: Tag questions with STEM subject labels

**Output Fields**:
- `subject_tag`: Subject classification
- `subject_conf`: Confidence level (high/medium/low)

---

## Pipeline Configuration

### File: stem_mc_to_openended_pipeline.py

**Class**: `StemMCToOpenEndedPipeline`

**Storage Configuration**:
```python
storage = FileStorage(
    first_entry_file_name="/data/workspace/stem_mc_sample_20k.jsonl",
    cache_path="./cache_stem_mc_rewrite",
    file_name_prefix="mc_rewrite_step",
    cache_type="jsonl",
)
```

**LLM Configuration**:
```python
llm_serving = APILLMServing_request(
    api_url=os.environ.get("DF_API_URL", 
        "https://api.openai.com/v1/chat/completions"),
    key_name_of_api_key="DF_API_KEY",
    model_name=os.environ.get("DF_MODEL_NAME", "gpt-4o"),
    max_workers=400,
    read_timeout=1800,
    connect_timeout=600
)
```

**Environment Variables Required**:
- `DF_API_URL`: API endpoint (default: OpenAI)
- `DF_API_KEY`: API key
- `DF_MODEL_NAME`: Model name (default: gpt-4o)

### Filter Thresholds
- **Answer Leak Filter**: Binary (has_leak = true/false)
- **N-gram Filter**: min_score=0.3 (30% new content required)

---

## Data Flow Example

### Input Sample (MC Question)
```
question: "题目：{关于DNA复制，下列说法正确的是\nA. DNA复制是半保留复制\nB. 复制过程需要解旋酶催化\nC. DNA聚合酶只能从5'端合成\nD. 复制需要ATP供能}"
text: "ABCD"
```

### After Step 0 (Preprocessing)
```
question_clean: "关于DNA复制，下列说法正确的是\nA. DNA复制是半保留复制\nB. 复制过程需要解旋酶催化\nC. DNA聚合酶只能从5'端合成\nD. 复制需要ATP供能"
answer_label: "A,B,C,D"
options_text: "A. DNA复制是半保留复制\nB. 复制过程需要解旋酶催化\nC. DNA聚合酶只能从5'端合成\nD. 复制需要ATP供能"
num_options: 4
```

### After Step 1 (Rewriting)
```
question_rewritten: "请说明DNA复制的机制和过程，包括：(1)复制方式的特点；(2)所需酶的作用；(3)聚合酶的合成方向限制；(4)能量来源。请逐项详细分析。"
```

### After Step 2 (Leak Filter)
```
# Kept if LLM judges: has_leak = false
# Filtered if LLM judges: has_leak = true
```

### After Step 4 (Column Alignment)
```
question: "请说明DNA复制的机制和过程..."
subject_tag: "生物"  # Added in Step 5
subject_conf: "high"  # Added in Step 5
```

---

## Prompt Template Characteristics

### StemMCRewritePrompt Features
- **Language Support**: Automatic Chinese/English detection
- **Few-Shot**: 2 examples in Chinese, 2 in English
- **Output Constraint**: "Only output rewritten question. No explanation, no prefix."
- **Focus**: Neutrality to prevent answer leakage

### StemMCAnswerLeakDetectionPrompt Features
- **Evaluation Criteria**: 5 types of leaks identified
- **Confidence Levels**: high/medium/low
- **JSON Output**: Structured evaluation result
- **Reasoning Required**: "Think briefly, then output JSON"

### StemSubjectTaggingPrompt Features
- **Classification**: 6 subjects + Other
- **Output**: JSON with subject and confidence
- **Constraint**: "Only output JSON, no explanation"
- **Language**: Chinese prompt, supports both CN and EN questions

---

## Related STEM Prompt Classes (in stem.py)

### Registered Prompt Classes
1. `StemTrueFalseRewritePrompt` - For T/F statements
2. `StemAnswerLeakDetectionPrompt` - For T/F leak detection
3. `StemSubjectTaggingPrompt` - Subject classification
4. `StemMCRewritePrompt` - MC rewriting (primary)
5. `StemMCAnswerLeakDetectionPrompt` - MC leak detection (primary)

---

## Key Implementation Notes

### Answer Leak Detection Logic
```python
# In StemMCAnswerLeakFilter._parse_judgement()
pattern = r'"has_leak"\s*:\s*(true|false)'
if "has_leak" found:
    has_leak = (value == "true")
    return not has_leak  # Return "should_keep" boolean
else:
    return True  # Default: keep (assume no leak)
```

### Option Extraction Logic
```python
# Regex: ^([ABCD])[.、．:\s]\s*(.+)$
# Supports separators: . 、 ． : space
# Examples: "A. text", "B、text", "C: text"
```

### M1 Format Detection
```python
# M1 = inline options without line breaks
# Example: "Statement A. xxx B. yyy C. zzz"
# Fix: Insert \n before each option letter
```

### Answer Normalization
Supported formats:
- No separator: "AB", "ABC", "ABCD"
- With separator: "A,B,C" or "A、B、C"
- With prefix: "答案是A", "Answer: B", "答案为ABC"

---

## Files Summary

### Main Pipeline
| File | Purpose |
|------|---------|
| `stem_mc_to_openended_pipeline.py` | Main pipeline orchestration |

### Prompts
| File | Prompt Classes |
|------|---|
| `dataflow/prompts/reasoning/stem.py` | StemMCRewritePrompt, StemMCAnswerLeakDetectionPrompt, StemSubjectTaggingPrompt, + T/F variants |

### Operators - Generate
| File | Class |
|------|-------|
| `stem_mc_to_openended_rewriter_generator.py` | StemMCToOpenEndedRewriterGenerator |
| `stem_column_align_generator.py` | StemColumnAlignGenerator |

### Operators - Filter
| File | Class |
|------|-------|
| `stem_mc_preprocessor_filter.py` | StemMCPreprocessorFilter |
| `stem_mc_answer_leak_filter.py` | StemMCAnswerLeakFilter |

### Operators - Eval
| File | Class |
|------|-------|
| `stem_subject_tagger_sample_evaluator.py` | StemSubjectTaggerSampleEvaluator |

---

## Quick Start

### Environment Setup
```bash
export DF_API_URL="https://your-api-endpoint/v1/chat/completions"
export DF_API_KEY="your-api-key"
export DF_MODEL_NAME="your-model-name"
```

### Run Pipeline
```bash
cd /data/workspace/DataFlow
python -m dataflow.statics.pipelines.api_pipelines.stem_mc_to_openended_pipeline
```

### Input Format
JSONL with fields: `question`, `text` (answer label)

### Output Format
Open-ended dataset format with: `question`, `subject_tag`, `subject_conf`

---

## Notes for Developers

1. **Prompt Registration**: All prompts use `@PROMPT_REGISTRY.register()` decorator
2. **Operator Registration**: All operators use `@OPERATOR_REGISTRY.register()` decorator
3. **LLM Judge**: Step 2 uses LLM as strict evaluator with JSON output requirement
4. **Post-processing**: Step 1 output needs cleanup (remove prefixes, think tags, markdown)
5. **Filtering Ratios**: Typical keep rates:
   - Step 0: ~98% (minor data quality issues)
   - Step 2: ~85-90% (some answer leakage)
   - Step 3: ~95% (minor n-gram dedup)

