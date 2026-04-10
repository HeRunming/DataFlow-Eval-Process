# STEM MC Pipeline - Complete Documentation Index

**Last Updated**: 2026-04-09

## 📋 Quick Start

If you're looking for...

### I need to understand the overall pipeline architecture
→ Start with **[STEM_PIPELINE_ARCHITECTURE.txt](STEM_PIPELINE_ARCHITECTURE.txt)**

### I need to see/edit the prompt templates
→ Go to **[STEM_PROMPT_TEMPLATES.md](STEM_PROMPT_TEMPLATES.md)**

### I need comprehensive technical documentation
→ Read **[STEM_MC_PIPELINE_ANALYSIS.md](STEM_MC_PIPELINE_ANALYSIS.md)**

### I need to find source code files
→ See **File Locations** section below

---

## 📂 Documentation Files

### 1. STEM_PIPELINE_ARCHITECTURE.txt
**Purpose**: Visual architecture and data flow diagrams

**Contents**:
- ASCII diagrams of the 6-step pipeline
- Data transformations at each step
- Retention rates and bottlenecks
- Configuration details
- Usage instructions
- Cumulative data flow example

**Best for**: Understanding overall pipeline structure and step interactions

---

### 2. STEM_PROMPT_TEMPLATES.md
**Purpose**: Quick reference for all prompt classes

**Contents**:
- 5 prompt classes defined
- Full template text (Chinese + English where applicable)
- Method signatures and parameters
- Expected output formats (JSON examples)
- Integration code examples
- Language auto-detection logic

**Best for**: Working with prompts, understanding prompt behavior, modifying templates

---

### 3. STEM_MC_PIPELINE_ANALYSIS.md
**Purpose**: Comprehensive technical documentation

**Contents**:
- Complete directory structure
- Detailed prompt class documentation (with line numbers)
- Operator class documentation for all 6 steps
- Pipeline configuration details
- Data flow examples
- Key implementation notes
- Developer notes

**Best for**: Deep technical understanding, debugging, extending the pipeline

---

## 🗂️ Source Code File Locations

### Prompt File
```
/data/workspace/DataFlow/dataflow/prompts/reasoning/stem.py

Contains 5 prompt classes:
  - StemMCRewritePrompt (lines 181-298)
  - StemMCAnswerLeakDetectionPrompt (lines 300-363)
  - StemSubjectTaggingPrompt (lines 142-178)
  - StemTrueFalseRewritePrompt (lines 20-107) [reference]
  - StemAnswerLeakDetectionPrompt (lines 109-139) [reference]
```

### Pipeline File
```
/data/workspace/DataFlow/dataflow/statics/pipelines/api_pipelines/stem_mc_to_openended_pipeline.py

Main pipeline class: StemMCToOpenEndedPipeline
```

### Operator Files (6 Steps)

**Step 0**: Preprocessing
```
/data/workspace/DataFlow/dataflow/operators/reasoning/filter/stem_mc_preprocessor_filter.py
Class: StemMCPreprocessorFilter
```

**Step 1**: LLM Rewriting
```
/data/workspace/DataFlow/dataflow/operators/reasoning/generate/stem_mc_to_openended_rewriter_generator.py
Class: StemMCToOpenEndedRewriterGenerator
```

**Step 2**: Answer Leak Detection
```
/data/workspace/DataFlow/dataflow/operators/reasoning/filter/stem_mc_answer_leak_filter.py
Class: StemMCAnswerLeakFilter
```

**Step 3**: N-gram Filtering
```
/data/workspace/DataFlow/dataflow/operators/reasoning/filter/reasoning_answer_ngram_filter.py
Class: ReasoningAnswerNgramFilter
```

**Step 4**: Column Alignment
```
/data/workspace/DataFlow/dataflow/operators/reasoning/generate/stem_column_align_generator.py
Class: StemColumnAlignGenerator
```

**Step 5**: Subject Tagging
```
/data/workspace/DataFlow/dataflow/operators/reasoning/eval/stem_subject_tagger_sample_evaluator.py
Class: StemSubjectTaggerSampleEvaluator
```

---

## 🎯 Key Prompt Classes Summary

### StemMCRewritePrompt
- **Purpose**: Rewrite MC question stems to neutral open-ended questions
- **Method**: `build_prompt(question: str, options: str) -> str`
- **Languages**: Auto-detects Chinese/English
- **Few-Shot**: 2 Chinese + 2 English examples
- **Output**: Plain text (rewritten question)

### StemMCAnswerLeakDetectionPrompt
- **Purpose**: Verify rewritten questions don't leak the answers
- **Method**: `build_prompt(question: str, answer_label: str, options: str) -> str`
- **Language**: English only
- **Output Format**: JSON with `{has_leak, leak_type, confidence}`
- **Leak Types**: 5 types (direct_indication, implicit_selection, answer_mirroring, forced_analysis, terminology_bias)

### StemSubjectTaggingPrompt
- **Purpose**: Classify questions into STEM subjects
- **Method**: `build_prompt(question: str) -> str`
- **Language**: Chinese only
- **Output Format**: JSON with `{subject, confidence}`
- **Subjects**: Math, Physics, Chemistry, Biology, Computer Science, Other

### Related Prompts (T/F versions)
- **StemTrueFalseRewritePrompt**: For T/F statements (similar to MC but simpler)
- **StemAnswerLeakDetectionPrompt**: For T/F leak detection (simpler than MC version)

---

## 📊 Pipeline Data Flow

```
Input (20k samples)
    ↓
Step 0: Preprocessing (98% kept)
    ↓ 19,600
Step 1: LLM Rewriting (100% kept)
    ↓ 19,600
Step 2: Answer Leak Filter (87% kept) ★ PRIMARY FILTER
    ↓ 17,052
Step 3: N-gram Dedup (95% kept)
    ↓ 16,199
Step 4: Column Alignment (100% kept)
    ↓ 16,199
Step 5: Subject Tagging (100% kept)
    ↓ 16,199
Output (16k samples, 80% retention)
```

**Key Bottleneck**: Step 2 (Answer Leak Detection) filters ~13% of samples

---

## 🔧 Configuration

### Environment Variables
```bash
export DF_API_URL="https://your-api/v1/chat/completions"
export DF_API_KEY="your-api-key"
export DF_MODEL_NAME="gpt-4o"  # default
```

### Storage
- **Input**: `/data/workspace/stem_mc_sample_20k.jsonl`
- **Cache**: `./cache_stem_mc_rewrite/`
- **Output**: Step-wise JSONL files (mc_rewrite_step0.jsonl, etc.)

### Filter Thresholds
- **Step 0**: min_required_options = 2
- **Step 2**: Binary (has_leak = true/false)
- **Step 3**: min_score = 0.3 (30% new n-grams required), ngrams = 5

---

## 🔍 Common Questions

### Where are the prompt templates?
→ `/data/workspace/DataFlow/dataflow/prompts/reasoning/stem.py`

### How does answer leak detection work?
→ See "Step 2: Answer Leak Detection" in STEM_PIPELINE_ARCHITECTURE.txt

### What are the input/output formats?
→ See "Data Flow Example" in STEM_MC_PIPELINE_ANALYSIS.md

### How do I run the pipeline?
→ See "Usage" section in STEM_PIPELINE_ARCHITECTURE.txt

### What's the retention rate at each step?
→ See "Cumulative Retention" in STEM_PIPELINE_ARCHITECTURE.txt

### How do I modify a prompt?
→ Edit the template in `/data/workspace/DataFlow/dataflow/prompts/reasoning/stem.py`

### What are the 5 leak types?
→ See StemMCAnswerLeakDetectionPrompt documentation

### How does language auto-detection work?
→ Regex match Chinese characters: `[\u4e00-\u9fff]`

---

## 📝 Document Conventions

### File Names
- `STEM_*.md` = Markdown documentation
- `STEM_*.txt` = Text files (sometimes ASCII art)
- `STEM_*_INDEX.md` = Navigation/index files

### Locations Referenced
- Paths are absolute (start with `/data/workspace/`)
- Line numbers refer to actual file contents
- Classes use Python naming conventions

### Code Examples
- Python imports shown explicitly
- Method signatures in standard format
- JSON examples show expected output

---

## 🚀 Getting Started

1. **New to the pipeline?**
   - Read STEM_PIPELINE_ARCHITECTURE.txt (5-10 min)
   - Watch data flow through the 6 steps

2. **Need to work with prompts?**
   - Read STEM_PROMPT_TEMPLATES.md (10-15 min)
   - See examples for each prompt class

3. **Need deep technical knowledge?**
   - Read STEM_MC_PIPELINE_ANALYSIS.md (20-30 min)
   - Reference specific sections as needed

4. **Ready to modify/extend?**
   - Locate files from "Source Code File Locations"
   - Use STEM_MC_PIPELINE_ANALYSIS.md for details
   - Test changes in isolation first

---

## 📚 Related Documentation

### Within DataFlow
- `/data/workspace/DataFlow/00_START_HERE.md` - General DataFlow intro
- `/data/workspace/DataFlow/FILE_MANIFEST.md` - Complete file list
- `/data/workspace/DataFlow/DEVELOPER_QUICK_START.md` - Dev setup

### External References
- Prompt Registry: Uses `@PROMPT_REGISTRY.register()` decorator
- Operator Registry: Uses `@OPERATOR_REGISTRY.register()` decorator
- Both in `dataflow.utils.registry` module

---

## 🔗 Documentation Cross-References

| Topic | Location | Document |
|-------|----------|----------|
| Pipeline Overview | Lines 56-174 | STEM_MC_PIPELINE_ANALYSIS.md |
| Prompt Classes | All 5 classes | STEM_PROMPT_TEMPLATES.md |
| Step Details | Each step section | STEM_PIPELINE_ARCHITECTURE.txt |
| Data Examples | "Data Flow Example" | STEM_MC_PIPELINE_ANALYSIS.md |
| Configuration | "Pipeline Configuration" | STEM_MC_PIPELINE_ANALYSIS.md |
| Usage Instructions | "USAGE" section | STEM_PIPELINE_ARCHITECTURE.txt |
| Operators | "Key Operators" section | STEM_MC_PIPELINE_ANALYSIS.md |
| Directory Structure | Top section | All documents |

---

## 📋 File Checklist

Generated documentation files:
- ✅ STEM_PIPELINE_ARCHITECTURE.txt
- ✅ STEM_PROMPT_TEMPLATES.md
- ✅ STEM_MC_PIPELINE_ANALYSIS.md
- ✅ STEM_PIPELINE_INDEX.md (this file)

All files saved in: `/data/workspace/DataFlow/`

---

## 🆘 Troubleshooting

**I can't find a specific prompt class**
→ All are in `/data/workspace/DataFlow/dataflow/prompts/reasoning/stem.py`
→ Look for `@PROMPT_REGISTRY.register()` decorator

**I need to understand how a step works**
→ Check STEM_PIPELINE_ARCHITECTURE.txt for step diagrams
→ Check STEM_MC_PIPELINE_ANALYSIS.md for detailed explanations

**I need to modify a prompt**
→ Edit the template string in stem.py
→ Test the modified prompt in isolation first

**I want to add a new step**
→ Reference existing operators as templates
→ Inherit from `OperatorABC`
→ Use appropriate decorator for registry

---

## 📞 Quick Reference

| Need | File | Section |
|------|------|---------|
| Architecture | STEM_PIPELINE_ARCHITECTURE.txt | "PIPELINE DATA FLOW" |
| Prompts | STEM_PROMPT_TEMPLATES.md | Individual prompt sections |
| Technical Details | STEM_MC_PIPELINE_ANALYSIS.md | "Operator Classes" |
| Configuration | STEM_PIPELINE_ARCHITECTURE.txt | "PIPELINE CONFIGURATION" |
| Examples | STEM_MC_PIPELINE_ANALYSIS.md | "Data Flow Example" |
| File Locations | This file | "File Locations" section |

---

**Generated**: 2026-04-09
**Format**: Markdown
**Location**: `/data/workspace/DataFlow/`

