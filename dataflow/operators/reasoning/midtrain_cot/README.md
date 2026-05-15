# Midtrain CoT Medium Distillation

This folder contains a midtrain-oriented design for converting overlong reasoning traces into medium-length training traces.

## Why this is separate from `reasoning/refine`

The existing `reasoning/refine` operators are useful offline post-processing tools, but most of them are local text compressors:

- step-level judge: classify each step as necessary/redundant/compressible;
- chunk-level compressor: classify chunks and apply fixed compression ratios;
- pattern refiner: classify thinking patterns and keep/compress/delete fragments.

Those methods can reduce token length, but midtrain data construction has a stronger requirement: the output should preserve reasoning capability signals, not merely shorten text.  A sample that is short but missing a key bridge, formula, case split, or informative failed attempt can make the downstream model produce shorter CoT while also weakening difficult reasoning behavior.

Therefore this folder treats the task as **Long-CoT -> Medium-CoT distillation** instead of generic CoT cleaning.

## Design goal

Given a long CoT, produce a medium CoT that is:

1. shorter and clearer than the original;
2. long enough to teach the solution path;
3. answer-equivalent to the original;
4. faithful to important formulas, variables, case distinctions, and intermediate conclusions;
5. not overly template-like;
6. suitable for mixed midtrain data rather than replacing all long CoT.

## Pipeline

The recommended operator is `CoTMidtrainMediumRefiner`.

It uses three LLM stages:

```text
raw long CoT
  -> reasoning skeleton extraction
  -> medium CoT rewrite with a dynamic length budget
  -> verifier-gated acceptance
  -> accepted medium CoT or fallback to original
```

### 1. Reasoning skeleton extraction

The extractor converts the original trace into structured JSON:

```json
{
  "problem_type": "algebra|geometry|number_theory|proof|calculation|other",
  "final_answer": "...",
  "key_steps": [
    {
      "id": "s1",
      "role": "setup|derivation|case_split|computation|verification|useful_failed_attempt|pure_failed_attempt|conclusion",
      "content": "...",
      "equations": ["..."],
      "depends_on": [],
      "keep_reason": "..."
    }
  ],
  "discardable_noise": ["..."],
  "compression_risks": ["..."]
}
```

This is the main conceptual change.  The operator does not ask the model to delete local steps directly.  It first asks: *what is the reasoning skeleton that should survive?*

### 2. Dynamic medium-length rewrite

The target budget is chosen from the original approximate token length and problem type.

A rough default policy:

- very short CoT: skip or light clean;
- 800-2000 tokens: keep roughly 45%-70%;
- 2000-5000 tokens: keep roughly 28%-48%;
- >5000 tokens: cap the output to a medium range, usually below ~2200 rough tokens.

The lower bound is higher for proof/geometry/olympiad-style tasks, because those tasks often need more explicit bridges.

### 3. Verification

The verifier checks:

- answer equivalence;
- reasoning sufficiency;
- formula/value/case safety;
- style naturalness and non-template behavior.

Only low-risk candidates are accepted by default.  Medium-risk candidates can be accepted by setting `accept_medium_risk=True`.

## Why keep a fallback to the original CoT?

For midtrain, false positives are more harmful than false negatives.  If the rewrite loses a key bridge or corrupts a formula, the operator falls back to the original CoT.  This means the pipeline can be run at scale without forcing every long trace into a compressed form.

## Recommended midtrain mixture

Do not replace all long CoT with medium CoT.  Use a mixture, for example:

```text
20%-30% original long CoT
50%-60% accepted medium CoT
10%-20% concise/short CoT
0%-10% answer-only or direct solution examples
```

The exact ratio should be selected by downstream validation.  The goal is to reduce pathological overthinking while preserving the ability to solve hard problems that genuinely need longer reasoning.

## Suggested evaluation

Track at least these metrics:

1. accepted rate;
2. fallback rate and fallback reasons;
3. rough token compression ratio;
4. answer-equivalence pass rate;
5. reasoning-sufficiency pass rate;
6. formula-safety pass rate;
7. style/template-risk rate;
8. downstream math benchmark performance;
9. downstream non-math benchmark performance;
10. generated CoT length distribution after training.

A minimal ablation:

```text
A. baseline: no CoT cleaning
B. old: CoTPatternRefinerFast balanced
C. new: CoTMidtrainMediumRefiner accepted medium only
D. mixed: original long + accepted medium + short/direct examples
```

The most likely strong setting is D, not C, because the objective is length control without losing long-horizon reasoning capacity.

## Usage sketch

```python
from dataflow.operators.reasoning.midtrain_cot import CoTMidtrainMediumRefiner

op = CoTMidtrainMediumRefiner(
    llm_serving=llm_serving,
    min_chars_to_clean=2000,
    accept_medium_risk=False,
    fallback_mode="original",
    force_verify=True,
)

op.run(
    storage=storage,
    input_key="cot",
    output_key="cot_midtrain_medium",
    output_stats_key="cot_midtrain_medium_stats",
    problem_key="question",
    answer_key="answer",
)
```

## Notes and limitations

- The current implementation uses a cheap rough token estimator rather than a model tokenizer.  This is intentional for portability, but tokenizer-specific accounting can be added later.
- The verifier is model-based.  For high-value math datasets, it is better to add task-specific exact answer checkers, symbolic checkers, or Lean/SymPy-based verification where applicable.
- The implementation prioritizes safety and fallback behavior over maximum compression.
