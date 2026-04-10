# 🚀 MC Pipeline Developer Quick Start Guide

**Last Updated**: 2026-04-08  
**For**: Developers integrating or extending the STEM MC-to-OE pipeline

---

## Table of Contents

1. [Installation & Setup](#installation--setup)
2. [Quick Test](#quick-test)
3. [Common Usage Patterns](#common-usage-patterns)
4. [Extending the Pipeline](#extending-the-pipeline)
5. [Debugging Guide](#debugging-guide)

---

## Installation & Setup

### 1. Prerequisites
```bash
# Python 3.8+
python --version

# Required packages
pip install pandas requests  # plus existing dataflow dependencies
```

### 2. Environment Variables
```bash
export DF_API_URL="https://api.openai.com/v1/chat/completions"  # or your endpoint
export DF_API_KEY="sk-..."  # your API key
export DF_MODEL_NAME="gpt-4o"  # or claude-3-opus, etc.
```

### 3. Data Preparation
```bash
# Input: JSONL file with MC questions
cat > input.jsonl << 'DATA'
{"id": "mc_001", "question": "下列哪个选项是正确的？\nA. 水的沸点为100°C\nB. 冰的融点为0°C\nC. 以上都对\nD. 以上都不对", "text": "AB", "subject": "chemistry"}
DATA
```

---

## Quick Test

### Test 1: Answer Normalization
```python
from dataflow.operators.reasoning.filter.stem_mc_preprocessor_filter import StemMCPreprocessorFilter

preprocessor = StemMCPreprocessorFilter()

# Test cases
test_answers = ["AC", "BCD", "AB,C", "A、B、D"]
for ans in test_answers:
    normalized = preprocessor._normalize_answer(ans)
    print(f"{ans:10s} → {normalized}")
    # AC         → A,C
    # BCD        → B,C,D
    # AB,C       → A,B,C
    # A、B、D    → A,B,D
```

### Test 2: Wrapper Extraction
```python
from dataflow.operators.reasoning.filter.stem_mc_preprocessor_filter import StemMCPreprocessorFilter

preprocessor = StemMCPreprocessorFilter()

# Test different wrapper formats
questions = [
    "题目：{下列正确的是}\nA. ...",
    "题目：「下列正确的是」\nA. ...",
    "题目：【下列正确的是】\nA. ...",
    "题目：《下列正确的是》\nA. ...",
]

for q in questions:
    extracted = preprocessor._extract_core_proposition(q)
    print(f"Extracted: {extracted}")
```

### Test 3: Option Parsing
```python
from dataflow.operators.reasoning.filter.stem_mc_preprocessor_filter import StemMCPreprocessorFilter

preprocessor = StemMCPreprocessorFilter()

question = """下列哪个选项是正确的？
A. 水的沸点为100°C
B. 冰的融点为0°C
C. 以上都对
D. 以上都不对"""

stem, options = preprocessor._extract_options(question)
print(f"Stem: {stem}")
print(f"Options: {options}")
# Stem: 下列哪个选项是正确的？
# Options: [
#   {"choice": "A", "text": "水的沸点为100°C"},
#   {"choice": "B", "text": "冰的融点为0°C"},
#   {"choice": "C", "text": "以上都对"},
#   {"choice": "D", "text": "以上都不对"}
# ]
```

---

## Common Usage Patterns

### Pattern 1: Standalone Preprocessing
```python
from dataflow.operators.reasoning.filter.stem_mc_preprocessor_filter import StemMCPreprocessorFilter
from dataflow.utils.storage import FileStorage
import pandas as pd

# Create sample data
data = {
    "question": ["题干1\nA. ...", "题干2\nA. ..."],
    "text": ["AC", "BD"],
    "subject": ["chemistry", "math"],
}
df = pd.DataFrame(data)

# Create storage
storage = FileStorage(
    first_entry_file_name=None,  # Use dataframe directly
    cache_path="./cache",
    file_name_prefix="mc_test",
    cache_type="jsonl",
)
storage.write(df)

# Run preprocessor
preprocessor = StemMCPreprocessorFilter()
preprocessor.run(
    storage=storage.step(),
    input_question_key="question",
    input_answer_key="text",
    output_question_key="question_clean",
    output_answer_key="answer_label",
)
```

### Pattern 2: Full Pipeline
```python
from dataflow.statics.pipelines.api_pipelines.stem_mc_to_openended_pipeline import StemMCToOpenEndedPipeline

pipeline = StemMCToOpenEndedPipeline()
# Update input path in __init__:
pipeline.storage.first_entry_file_name = "your_input_path/part-*.json"
pipeline.forward()
```

### Pattern 3: Custom Prompt Extension
```python
from dataflow.prompts.reasoning.stem import StemMCRewritePrompt
from dataflow.utils.registry import PROMPT_REGISTRY

# Get existing prompt
base_prompt = PROMPT_REGISTRY.get_module("StemMCRewritePrompt")

# Create custom prompt
prompt_builder = base_prompt()
custom_prompt = prompt_builder.build_prompt(
    question="关于函数 f(x)=x² 的性质",
    options="A. 奇函数\nB. 偶函数\nC. 非奇非偶\nD. 既奇既偶"
)
print(custom_prompt)
```

---

## Extending the Pipeline

### Use Case 1: Add Custom Preprocessing Step

```python
from dataflow.core import OperatorABC
from dataflow.utils.registry import OPERATOR_REGISTRY

@OPERATOR_REGISTRY.register()
class CustomMCValidator(OperatorABC):
    """Custom validation step for MC questions."""
    
    def run(self, storage, **kwargs):
        df = storage.read("dataframe")
        # Your custom logic here
        storage.write(df)
        return ["validation_result"]
```

### Use Case 2: Modify Leak Detection Thresholds

```python
from dataflow.operators.reasoning.filter.stem_mc_answer_leak_filter import StemMCAnswerLeakFilter

# Extend the class
class StrictMCAnswerLeakFilter(StemMCAnswerLeakFilter):
    """Stricter leak detection for high-stakes exams."""
    
    def __init__(self, llm_serving):
        super().__init__(llm_serving)
        # Override sensitivity settings
        self.min_confidence = 0.95  # Higher threshold
        self.leak_penalty_weight = 2.0  # Double penalty for leaks
```

### Use Case 3: Add Language Support

```python
from dataflow.prompts.reasoning.stem import StemMCRewritePrompt

class StemMCRewritePromptES(StemMCRewritePrompt):
    """Spanish support for MC rewriting."""
    
    _TEMPLATE_ES = """
    # Tarea: Reescribe una pregunta de opción múltiple...
    [Your Spanish template here]
    """
    
    def build_prompt(self, question: str, options: str) -> str:
        if self._detect_language(question) == "es":
            return self._TEMPLATE_ES.format(question=question, options=options)
        return super().build_prompt(question, options)
```

---

## Debugging Guide

### Issue 1: Empty Output

**Symptom**: Pipeline runs but produces no output records

**Diagnosis**:
```python
# Check what's being filtered
import logging
logging.basicConfig(level=logging.DEBUG)

# Run with verbose output
preprocessor.run(storage)  # Check logs for filter counts
```

**Solution**:
1. Verify input data format matches JSONL spec
2. Check answer field contains only A-D letters
3. Review logs for specific filter reason

### Issue 2: High Leak Rate

**Symptom**: Many records marked as LEAKED

**Diagnosis**:
```python
# Check specific record
from dataflow.operators.reasoning.filter.stem_mc_answer_leak_filter import StemMCAnswerLeakFilter

leak_filter = StemMCAnswerLeakFilter(llm_serving)
record = {
    "question_rewritten": "...",
    "answer": "A,C",
    "options": "..."
}
# Manually test with LLM
```

**Solutions**:
1. Upgrade to better LLM (GPT-4o vs GPT-3.5)
2. Refine leak detection prompt
3. Increase temperature in rewriter for diversity
4. Review false positives manually

### Issue 3: Option Parsing Failures

**Symptom**: Some questions show empty options

**Diagnosis**:
```python
from dataflow.operators.reasoning.filter.stem_mc_preprocessor_filter import StemMCPreprocessorFilter

preprocessor = StemMCPreprocessorFilter()

# Test specific question
question = "..."  # Your problematic question
stem, options = preprocessor._extract_options(question)
print(f"Options found: {len(options)}")  # Should be 4

# Debug regex
import re
lines = question.split('\n')
for line in lines:
    match = re.match(r'^([A-D])[.、．\s]\s*(.*)', line)
    print(f"Line: {line} → Match: {match}")
```

**Solutions**:
1. Check for non-standard option delimiters (use actual characters)
2. Verify question has exactly 4 options (A-D)
3. Mark as SILVER_M1 if options are inline (not multi-line)

### Issue 4: API Timeouts

**Symptom**: Pipeline hangs during LLM calls

**Diagnosis**:
```bash
# Check API availability
curl -v -H "Authorization: Bearer $DF_API_KEY" \
  $DF_API_URL \
  -d '{"model": "'$DF_MODEL_NAME'", "messages": [{"role": "user", "content": "test"}]}'
```

**Solutions**:
1. Verify API credentials in environment variables
2. Check network connectivity to API endpoint
3. Increase timeout in APILLMServing_request
4. Reduce batch size to prevent overwhelming API

### Issue 5: Memory Issues

**Symptom**: Out of Memory error on large batches

**Diagnosis**:
```bash
# Monitor memory during pipeline run
watch -n 1 'ps aux | grep python'
```

**Solutions**:
1. Reduce batch size (default 100 → 50)
2. Enable disk caching: `cache_path="./tmp_cache"`
3. Process in multiple passes with filtering
4. Increase available system RAM

---

## Performance Tuning

### Optimization 1: Batch Processing
```python
# Process in smaller batches for stability
pipeline.storage.batch_size = 50  # Default is 100

# Trade-off: More batches = slower overall, but more stable
```

### Optimization 2: Parallel LLM Calls
```python
# Increase concurrent API calls
pipeline.llm_serving = APILLMServing_request(
    api_url=os.environ.get("DF_API_URL"),
    key_name_of_api_key="DF_API_KEY",
    model_name=os.environ.get("DF_MODEL_NAME"),
    max_workers=500,  # Increase from default 200
)
```

### Optimization 3: Caching Strategy
```python
# Skip preprocessing if already cached
pipeline.storage.cache_type = "jsonl"
pipeline.storage.cache_path = "/fast_ssd/pipeline_cache"

# Pre-populate cache from previous runs
```

---

## Key Files Reference

| File | Purpose | Key Classes |
|------|---------|------------|
| `stem_mc_preprocessor_filter.py` | Data cleaning | `StemMCPreprocessorFilter` |
| `stem_mc_to_openended_rewriter_generator.py` | LLM rewriting | `StemMCToOpenEndedRewriterGenerator` |
| `stem_mc_answer_leak_filter.py` | Quality check | `StemMCAnswerLeakFilter` |
| `stem_mc_to_openended_pipeline.py` | Orchestration | `StemMCToOpenEndedPipeline` |
| `stem.py` (prompts) | Prompt templates | `StemMCRewritePrompt`, `StemMCAnswerLeakDetectionPrompt` |

---

## Support & Resources

- **Documentation**: See `/data/workspace/DataFlow/MC_PIPELINE_IMPLEMENTATION_SUMMARY.md`
- **Changelog**: See `/data/workspace/DataFlow/DEV_CHANGELOG_ITERATION5.md`
- **Production Report**: See `/data/workspace/DataFlow/PRODUCTION_READY_REPORT.md`
- **Test Data**: Sample questions in `/tmp/multiple_choice_samples.jsonl`

---

**Happy coding!** 🎉
